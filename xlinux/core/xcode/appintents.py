"""App Intents metadata (Metadata.appintents) without Xcode.

iOS discovers an app's App Intents and App Shortcuts (Shortcuts app,
Spotlight, Siri) through <App>.app/Metadata.appintents. Xcode writes it with
appintentsmetadataprocessor, a macOS arm64-only tool, from the constant
values swiftc extracts from the module (`-emit-const-values-path`, which
the Linux swiftc emits identically). This module turns those constant values
into the same JSON (extract.actionsdata, version.json).

The format is private and undocumented: it was reconstructed from a paired
Xcode build (source + output) and ~150 Metadata.appintents published on
GitHub, so only what was observed is generated. Anything else (entities,
queries, phrases with parameters...) stops the build with an explicit
error instead of producing metadata iOS might silently reject.
"""

import json
from pathlib import Path

# Protocols whose conformers swiftc describes (-const-gather-protocols-file).
PROTOCOLS = ["AppIntent", "EntityQuery", "AppEntity", "TransientEntity", "AppEnum", "AppShortcutProviding",
             "AppShortcutsProvider", "AnyResolverProviding", "AppIntentsPackage", "DynamicOptionsProvider"]

# Xcode's generator version this output mirrors (Xcode 26.x, the paired reference).
GENERATOR = {"name": "xcode-tools", "version": "17F113"}
VERSION_JSON = {"version": "3.0", "toolsVersion": GENERATOR["version"]}
ANY_PLATFORM = {"LNPlatformNameWildcard": {"introducedVersion": "*"}}

# LNValueType primitive type identifiers, and the input types each one can be
# resolved from (the same for every parameter of a type in all samples).
PRIMITIVES = {"Swift.String": 0, "Swift.Bool": 1, "Swift.Int": 2, "Swift.Double": 7,
              "Foundation.Date": 8, "Foundation.URL": 11}
RESOLVABLE = {0: [0, 2, (2,)], 1: [1], 2: [0, 2, 7], 7: [0, 2, 7], 8: [8], 11: [0, 11]}
# PerformResult protocols -> outputFlags bits.
OUTPUT_FLAGS = {"AppIntents.OpensIntent": 1, "AppIntents.ShowsSnippetView": 2, "AppIntents.ProvidesDialog": 4}


class Unsupported(Exception):
    pass


def localized(key):
    return {"alternatives": [], "key": key}


def primitive(type_id):
    return {"primitive": {"wrapper": {"typeIdentifier": type_id}}}


def resolvable(type_id):
    out = []
    for r in RESOLVABLE[type_id]:
        value = primitive(r) if isinstance(r, int) else \
            {"array": {"wrapper": {"capabilities": 3, "memberValueType": primitive(r[0])}}}
        out.append({"kindValue": 0, "valueType": value})
    return out


def literal(node, what):
    """The string of a RawLiteral (or an init whose first argument is one,
    e.g. IntentDescription("...") / DisplayRepresentation(title: "..."))."""
    if node is None:
        return None
    kind = node.get("valueKind")
    if kind == "NilLiteral":
        return None
    if kind == "RawLiteral":
        return str(node["value"])
    if kind == "InitCall" and node["value"].get("arguments"):
        return literal(node["value"]["arguments"][0], what)
    raise Unsupported(f"{what}: only literal strings are supported (found {kind})")


def short_name(type_name):
    return type_name.rsplit(".", 1)[-1]


def properties(t):
    return {p["label"]: p for p in t.get("properties", [])}


def unwrap_optional(type_name):
    if type_name.startswith("Swift.Optional<") and type_name.endswith(">"):
        return type_name[len("Swift.Optional<"):-1], True
    return type_name, False


def parameter(prop, enums, where):
    t = prop["value"]["type"]  # AppIntents.IntentParameter<T>
    inner, optional = unwrap_optional(t[t.index("<") + 1:-1])
    args = {a.get("label"): a for a in prop["value"].get("arguments", [])}
    entry = {"capabilities": 0, "dynamicOptionsSupport": 0, "inputConnectionBehavior": 0, "isInput": False,
             "isOptional": optional, "name": prop["label"][1:]}
    description = literal(args.get("description"), f"{where}.{entry['name']} description")
    if description is not None:
        entry["parameterDescription"] = localized(description)
    if inner in PRIMITIVES:
        type_id = PRIMITIVES[inner]
        entry["resolvableInputTypes"] = resolvable(type_id)
        entry["valueType"] = primitive(type_id)
    elif inner in enums:
        entry["resolvableInputTypes"] = []
        entry["valueType"] = {"linkEnumeration": {"wrapper": {"identifier": short_name(inner)}}}
    else:
        raise Unsupported(f"{where}.{entry['name']}: parameters of type {inner} aren't supported yet "
                          "(supported: String, Bool, Int, Double, Date, URL and AppEnum types)")
    title = literal(args.get("title"), f"{where}.{entry['name']} title")
    if title is not None:
        entry["title"] = localized(title)
    entry["typeSpecificMetadata"] = []
    return entry


def output(t, enums, where):
    flags, output_type = 0, None
    for alias in t.get("associatedTypeAliases", []):
        if alias["typeAliasName"] != "PerformResult":
            continue
        for protocol in alias.get("opaqueTypeProtocolRequirements") or []:
            flags |= OUTPUT_FLAGS.get(protocol, 0)
        name = alias["substitutedTypeName"]
        if "AppIntents.ReturnsValue<" in name:
            value = name.split("AppIntents.ReturnsValue<", 1)[1]
            depth, end = 1, 0
            for end, ch in enumerate(value):
                depth += {"<": 1, ">": -1}.get(ch, 0)
                if depth == 0:
                    break
            value, _ = unwrap_optional(value[:end])
            if value in PRIMITIVES:
                output_type = primitive(PRIMITIVES[value])
            elif value in enums:
                output_type = {"linkEnumeration": {"wrapper": {"identifier": short_name(value)}}}
            else:
                raise Unsupported(f"{where}: ReturnsValue<{value}> isn't supported yet")
    if "AppIntents.WidgetConfigurationIntent" in t.get("conformances", []):
        flags |= 8
    return flags, output_type


def action(t, enums):
    where = t["typeName"]
    props = properties(t)
    title = literal(props.get("title"), f"{where}.title")
    if title is None:
        raise Unsupported(f"{where}: no static title")
    open_app = props.get("openAppWhenRun")
    flags, output_type = output(t, enums, where)
    entry = {
        "assistantDefinedSchemaTraits": [], "assistantDefinedSchemas": [], "authenticationPolicy": 0,
        "availabilityAnnotations": ANY_PLATFORM,
    }
    description = literal(props.get("description"), f"{where}.description") if "description" in props else None
    if description is not None:
        entry["descriptionMetadata"] = {"descriptionText": localized(description), "searchKeywords": []}
    entry.update({
        "effectiveBundleIdentifiers": [], "fullyQualifiedTypeName": t["typeName"],
        "identifier": short_name(t["typeName"]), "isAuthPolExplicit": False, "isDiscoverable": True,
        "mangledTypeName": t["mangledTypeName"], "mangledTypeNameByBundleIdentifier": {},
        "mangledTypeNameByBundleIdentifierV2": {}, "mangledTypeNameV2": t["mangledTypeName"],
        "openAppWhenRun": bool(open_app) and str(open_app.get("value")).lower() == "true",
        "outputFlags": flags,
    })
    if output_type:
        entry["outputType"] = output_type
    entry.update({
        "parameters": [parameter(p, enums, where) for label, p in props.items()
                       if label.startswith("_") and p["type"].startswith("AppIntents.IntentParameter<")],
        "presentationStyle": 0, "requiredCapabilities": [], "supportedModes": 1,
        "systemProtocolMetadata": [], "systemProtocolMetadataV2": [], "systemProtocols": [],
        "title": localized(title), "typeSpecificMetadata": [],
        "visibilityMetadata": {"assistantOnly": False, "isDiscoverable": True},
    })
    return entry


def enumeration(t):
    where = t["typeName"]
    props = properties(t)
    titles = {}
    for item in (props.get("caseDisplayRepresentations") or {}).get("value") or []:
        titles[item["key"]["value"]["name"]] = literal(item["value"], f"{where} case title")
    cases = []
    for case in t.get("cases", []):
        if case["name"] not in titles:
            raise Unsupported(f"{where}.{case['name']}: no literal caseDisplayRepresentations entry")
        cases.append({"displayRepresentation": {"title": localized(titles[case["name"]])},
                      "identifier": case.get("rawValue") or case["name"]})
    type_title = literal(props.get("typeDisplayRepresentation"), f"{where}.typeDisplayRepresentation")
    return {
        "assistantDefinedSchemas": [], "availabilityAnnotations": ANY_PLATFORM, "cases": cases,
        "displayTypeName": localized(type_title or short_name(where)), "effectiveBundleIdentifiers": [],
        "fullyQualifiedTypeName": t["typeName"], "identifier": short_name(t["typeName"]), "isSystem": False,
        "mangledTypeName": t["mangledTypeName"], "mangledTypeNameByBundleIdentifier": {},
        "systemProtocolMetadata": [], "visibilityMetadata": {"assistantOnly": False, "isDiscoverable": True},
    }


def phrase(node, where):
    if node.get("valueKind") == "RawLiteral":
        return str(node["value"])
    if node.get("valueKind") != "InterpolatedStringLiteral":
        raise Unsupported(f"{where}: phrases must be string literals")
    text = ""
    for segment in node["value"]["segments"]:
        kind = segment["valueKind"]
        if kind == "RawLiteral":
            text += str(segment["value"])
        elif kind == "Enum" and segment["value"]["name"] == "applicationName":
            text += "${applicationName}"
        else:
            raise Unsupported(f"{where}: phrases with parameters aren't supported yet "
                              "(only \\(.applicationName))")
    if "${applicationName}" not in text:
        raise Unsupported(f"{where}: every phrase must contain \\(.applicationName)")
    return text


def shortcuts(provider):
    where = provider["typeName"]
    prop = properties(provider).get("appShortcuts")
    if not prop or prop.get("valueKind") not in ("Builder", "Array"):
        raise Unsupported(f"{where}.appShortcuts: only a literal list of AppShortcut(...) is supported")
    members = prop["value"]["members"] if prop["valueKind"] == "Builder" else \
        [{"kind": "buildExpression", "element": e} for e in prop["value"]]
    out = []
    for member in members:
        element = member.get("element") or {}
        if member.get("kind") != "buildExpression" or element.get("valueKind") != "InitCall":
            raise Unsupported(f"{where}.appShortcuts: conditionals and loops aren't supported")
        args = {a.get("label"): a for a in element["value"]["arguments"]}
        intent = args["intent"]["type"]
        entry = {"actionIdentifier": short_name(intent), "availabilityAnnotations": ANY_PLATFORM,
                 "phraseTemplates": [localized(phrase(p, where)) for p in args["phrases"]["value"]]}
        short_title = literal(args.get("shortTitle"), f"{where} shortTitle")
        if short_title is not None:
            entry["shortTitle"] = localized(short_title)
        image = literal(args.get("systemImageName"), f"{where} systemImageName")
        if image is not None:
            entry["systemImageName"] = image
        out.append(entry)
    return out


def metadata(types):
    """extract.actionsdata for the constant values of one module, or None if
    it declares no App Intents."""
    def conforms(t, protocol):
        return f"AppIntents.{protocol}" in t.get("conformances", [])

    for t in types:
        for protocol in ("AppEntity", "TransientEntity", "EntityQuery", "DynamicOptionsProvider"):
            if conforms(t, protocol):
                raise Unsupported(f"{t['typeName']}: {protocol} isn't supported yet")
    enum_types = {t["typeName"]: t for t in types if conforms(t, "AppEnum")}
    intents = [t for t in types if conforms(t, "AppIntent")]
    providers = [t for t in types if conforms(t, "AppShortcutsProvider")]
    if not intents and not enum_types:
        return None
    if len(providers) > 1:
        raise Unsupported("more than one AppShortcutsProvider")
    data = {
        "actions": {short_name(t["typeName"]): action(t, enum_types) for t in intents},
        "assistantEntities": [], "assistantIntentNegativePhrases": [], "assistantIntents": [],
    }
    if providers:
        data["autoShortcutProviderMangledName"] = providers[0]["mangledTypeName"]
    data.update({
        "autoShortcuts": shortcuts(providers[0]) if providers else [],
        "entities": {}, "enums": [enumeration(t) for t in enum_types.values()],
        "generator": GENERATOR, "negativePhrases": [], "queries": {}, "shortcutTileColor": 14, "version": 1,
    })
    return data


def write_protocols(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(PROTOCOLS))


def generate(const_values, app_dir):
    """Write <app_dir>/Metadata.appintents from swiftc's constant values.
    Returns whether the module has App Intents. Raises Unsupported."""
    types = json.loads(Path(const_values).read_text()) if Path(const_values).exists() else []
    data = metadata(types)
    out = Path(app_dir) / "Metadata.appintents"
    if data is None:
        return False
    out.mkdir(parents=True, exist_ok=True)
    (out / "extract.actionsdata").write_text(json.dumps(data, indent=2, sort_keys=True))
    (out / "version.json").write_text(json.dumps(VERSION_JSON, indent=2))
    return True
