"""App extensions (widgets, share extensions…) declared in the Xcode project.

Xcode builds each extension target into Runner.app/PlugIns/<Name>.appex;
xtool then registers, signs and installs every .appex it finds there. Here
the targets are discovered from project.pbxproj (their Info.plist declares
NSExtension) and compiled with swiftc. Compiled bundles are cached by
content, so unchanged extensions don't slow down the edit/run loop.
"""

import hashlib
import plistlib
import re
import shutil
from pathlib import Path

from . import app as appkit
from . import config, deps, toolchain
from .util import log


class Extension:
    def __init__(self, name, directory, info_plist, bundle_id, entitlements=None, settings=None, target=None):
        self.settings = settings
        self.target = target  # the Xcode target, when the project was read with xcode
        self.name = name
        self.entitlements = entitlements
        self.dir = directory
        self.info_plist = info_plist
        self.bundle_id = bundle_id

    def sources(self):
        """The target's Compile Sources (Xcode projects), else every Swift
        file next to its Info.plist (Flutter's convention)."""
        if self.target:
            return [Path(src["path"]) for src in self.target.get("sources", [])]
        return sorted(p for p in self.dir.rglob("*.swift"))

    def inputs(self):
        """Every file the build depends on (for the fingerprint)."""
        if self.target:
            return sorted({*self.sources(), self.info_plist})
        return sorted(p for p in self.dir.rglob("*") if p.is_file())

    def deployment_target(self):
        return (self.settings.get("IPHONEOS_DEPLOYMENT_TARGET") if self.settings else "") or config.MIN_IOS


def discover(ios_dir, app_bundle_id, xcode=None):
    """Extension targets of the Xcode project in `ios_dir`."""
    found = {}
    if xcode:
        targets = [t for t in xcode.targets.values()
                   if t.get("product_type") == "com.apple.product-type.app-extension"]
        for target in targets:
            if not target.get("configuration_available", True):
                raise SystemExit(f"error: {target['name']} has no {xcode.configuration} configuration")
        settings_list = [xcode.settings(t) for t in targets]
    else:
        targets = []
        settings_list = list(appkit.build_settings(ios_dir))
    for settings, target in zip(settings_list, targets or [None] * len(settings_list)):
        plist_path = settings.get("INFOPLIST_FILE", "")
        if not plist_path or plist_path in found:
            continue
        plist = ios_dir / plist_path
        if not plist.exists():
            continue
        with open(plist, "rb") as f:
            if "NSExtension" not in plistlib.load(f):
                continue
        name = settings.get("PRODUCT_NAME", "")
        name = (settings.expand(name) if target else name.replace("$(TARGET_NAME)", "")) or \
            (target["name"] if target else plist.parent.name)
        bundle_id = settings.get("PRODUCT_BUNDLE_IDENTIFIER") or f"{app_bundle_id}.{name}"
        entitlements = settings.get("CODE_SIGN_ENTITLEMENTS")
        entitlements = ios_dir / entitlements if entitlements else None
        found[plist_path] = Extension(name, plist.parent, plist, bundle_id,
                                      entitlements if entitlements and entitlements.exists() else None, settings,
                                      target)
    return list(found.values())


def _info_plist(ext, app_info):
    variables = {
        "EXECUTABLE_NAME": ext.name, "PRODUCT_NAME": ext.name, "PRODUCT_MODULE_NAME": ext.name,
        "PRODUCT_BUNDLE_IDENTIFIER": ext.bundle_id, "DEVELOPMENT_LANGUAGE": "en",
        # iOS expects extensions to carry the containing app's version.
        "MARKETING_VERSION": app_info.get("CFBundleShortVersionString", "1.0"),
        "CURRENT_PROJECT_VERSION": app_info.get("CFBundleVersion", "1"),
    }
    with open(ext.info_plist, "rb") as f:
        info = appkit.expand(plistlib.load(f), ext.settings or variables)
    info.update({
        "CFBundleExecutable": ext.name,
        "CFBundleIdentifier": ext.bundle_id,
        "CFBundlePackageType": "XPC!",
        "CFBundleShortVersionString": variables["MARKETING_VERSION"],
        "CFBundleVersion": variables["CURRENT_PROJECT_VERSION"],
        "MinimumOSVersion": ext.deployment_target(),
        "CFBundleSupportedPlatforms": ["iPhoneOS"],
        "UIDeviceFamily": [1, 2],
        "DTPlatformName": "iphoneos",
        "DTSDKName": "iphoneos",
    })
    info.setdefault("CFBundleInfoDictionaryVersion", "6.0")
    return info


def _fingerprint(ext, info, debug):
    h = hashlib.sha256(Path(__file__).read_bytes())  # compile flags live in this file
    if ext.settings:
        keys = set(ext.settings.builtins).union(*(set(layer) for layer in ext.settings.layers))
        h.update(repr(sorted((k, ext.settings.get(k)) for k in keys)).encode())
    h.update(repr((sorted(info.items(), key=str), debug, config.MIN_IOS, str(config.IPHONE_SDK))).encode())
    if ext.entitlements:
        h.update(ext.entitlements.read_bytes())
    for path in ext.inputs():
        h.update(str(path).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _compile(ext, appex, debug):
    appex.mkdir(parents=True)
    sources = ext.sources()
    if not sources:
        raise SystemExit(f"error: extension {ext.name} has no Swift sources")
    if not deps.macro_server_current() and deps.needs_patched_macros({p.parent for p in sources}):
        deps.ensure_macro_server()  # e.g. a widget's #Preview(as: .systemSmall)
    if any(p.suffix in (".m", ".mm", ".c") for p in (sources if ext.target else ext.dir.rglob("*"))):
        log(f"warning: {ext.name}: only Swift sources are compiled for extensions")
    module = re.sub(r"\W", "_", ext.name)
    # With an @main type (WidgetBundle, Widget…) and no main.swift, swiftc
    # needs -parse-as-library, as Xcode passes.
    library = [] if any(p.name == "main.swift" for p in sources) else ["-parse-as-library"]
    sources = [p for p in sources if p.suffix == ".swift"]
    toolchain.swiftc(["-Onone" if debug else "-O", "-module-name", module, *library,
                      "-application-extension", "-Xlinker", "-application_extension",
                      # Like Xcode (and xtool): Foundation's NSExtensionMain sets up the
                      # extension environment before the @main type runs; entering
                      # through Swift's main traps in ExtensionFoundation.
                      "-framework", "Foundation", "-Xlinker", "-e", "-Xlinker", "_NSExtensionMain",
                      "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../../Frameworks",
                      *sources, *toolchain.builtins(), "-o", appex / ext.name],
                     deployment_target=ext.deployment_target())
    appkit.copy_loose_resources(ext.dir, appex)
    if (ext.dir / "Assets.xcassets").exists():
        log(f"warning: {ext.name}: Assets.xcassets isn't compiled for extensions yet")


def build_all(ios_dir, app, build_dir, debug, xcode=None):
    """Compile every extension into `app`/PlugIns. Returns the extension names."""
    with open(app / "Info.plist", "rb") as f:
        app_info = plistlib.load(f)
    extensions = discover(ios_dir, app_info["CFBundleIdentifier"], xcode)
    for ext in extensions:
        info = _info_plist(ext, app_info)
        cached = build_dir / "extensions" / f"{ext.name}.appex"
        stamp = cached.with_suffix(".fingerprint")
        fingerprint = _fingerprint(ext, info, debug)
        if not (cached.exists() and stamp.exists() and stamp.read_text() == fingerprint):
            log(f"App extension {ext.name} ({info['NSExtension'].get('NSExtensionPointIdentifier', '?')})")
            if cached.exists():
                shutil.rmtree(cached)
            _compile(ext, cached, debug)
            with open(cached / "Info.plist", "wb") as f:
                plistlib.dump(info, f)
            appkit.embed_entitlements(cached / ext.name, ext.entitlements, ext.bundle_id,
                                      ext.settings or {"PRODUCT_BUNDLE_IDENTIFIER": ext.bundle_id})
            stamp.write_text(fingerprint)
        target = app / "PlugIns" / cached.name
        target.parent.mkdir(exist_ok=True)
        shutil.copytree(cached, target, symlinks=True)
    return [ext.name for ext in extensions]
