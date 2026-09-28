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
from . import config, toolchain
from .util import log


class Extension:
    def __init__(self, name, directory, info_plist, bundle_id):
        self.name = name
        self.dir = directory
        self.info_plist = info_plist
        self.bundle_id = bundle_id

    def sources(self):
        return sorted(p for p in self.dir.rglob("*.swift"))


def _build_settings(pbxproj_text):
    """buildSettings of every XCBuildConfiguration, as dicts of raw strings."""
    for block in pbxproj_text.split("isa = XCBuildConfiguration;")[1:]:
        settings = block.split("name = ", 1)[0]
        yield {k: v.strip().strip('"') for k, v in re.findall(r"\b([A-Z_]+) = ([^;]+);", settings)}


def discover(ios_dir, app_bundle_id):
    """Extension targets of the Xcode project in `ios_dir`."""
    pbxproj = ios_dir / "Runner.xcodeproj/project.pbxproj"
    if not pbxproj.exists():
        return []
    found = {}
    for settings in _build_settings(pbxproj.read_text()):
        plist_path = settings.get("INFOPLIST_FILE", "")
        if not plist_path or plist_path in found:
            continue
        plist = ios_dir / plist_path
        if not plist.exists():
            continue
        with open(plist, "rb") as f:
            if "NSExtension" not in plistlib.load(f):
                continue
        name = settings.get("PRODUCT_NAME", "").replace("$(TARGET_NAME)", "") or plist.parent.name
        bundle_id = settings.get("PRODUCT_BUNDLE_IDENTIFIER") or f"{app_bundle_id}.{name}"
        found[plist_path] = Extension(name, plist.parent, plist, bundle_id)
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
        info = appkit.expand(plistlib.load(f), variables)
    info.update({
        "CFBundleExecutable": ext.name,
        "CFBundleIdentifier": ext.bundle_id,
        "CFBundlePackageType": "XPC!",
        "CFBundleShortVersionString": variables["MARKETING_VERSION"],
        "CFBundleVersion": variables["CURRENT_PROJECT_VERSION"],
        "MinimumOSVersion": config.MIN_IOS,
        "CFBundleSupportedPlatforms": ["iPhoneOS"],
        "UIDeviceFamily": [1, 2],
        "DTPlatformName": "iphoneos",
        "DTSDKName": "iphoneos",
    })
    info.setdefault("CFBundleInfoDictionaryVersion", "6.0")
    return info


def _fingerprint(ext, info, debug):
    h = hashlib.sha256()
    h.update(repr((sorted(info.items(), key=str), debug, config.MIN_IOS, str(config.IPHONE_SDK))).encode())
    for path in sorted(p for p in ext.dir.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(ext.dir)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _compile(ext, appex, debug):
    appex.mkdir(parents=True)
    sources = ext.sources()
    if not sources:
        raise SystemExit(f"error: extension {ext.name} has no Swift sources in {ext.dir}")
    if any(p.suffix in (".m", ".mm", ".c") for p in ext.dir.rglob("*")):
        log(f"warning: {ext.name}: only Swift sources are compiled for extensions")
    module = re.sub(r"\W", "_", ext.name)
    # With an @main type (WidgetBundle, Widget…) and no main.swift, swiftc
    # needs -parse-as-library, as Xcode passes.
    library = [] if any(p.name == "main.swift" for p in sources) else ["-parse-as-library"]
    toolchain.swiftc(["-Onone" if debug else "-O", "-module-name", module, *library,
                      "-application-extension", "-Xlinker", "-application_extension",
                      "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../../Frameworks",
                      *sources, *toolchain.builtins(), "-o", appex / ext.name])
    appkit.copy_loose_resources(ext.dir, appex)
    if (ext.dir / "Assets.xcassets").exists():
        log(f"warning: {ext.name}: Assets.xcassets isn't compiled for extensions yet")


def build_all(ios_dir, app, build_dir, debug):
    """Compile every extension into `app`/PlugIns. Returns the extension names."""
    with open(app / "Info.plist", "rb") as f:
        app_info = plistlib.load(f)
    extensions = discover(ios_dir, app_info["CFBundleIdentifier"])
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
            stamp.write_text(fingerprint)
        target = app / "PlugIns" / cached.name
        target.parent.mkdir(exist_ok=True)
        shutil.copytree(cached, target, symlinks=True)
    return [ext.name for ext in extensions]
