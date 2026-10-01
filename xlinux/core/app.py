"""Assemble a .app / .ipa from an Xcode project, without Xcode.

Linux has no ibtool (storyboards): the Info.plist is adjusted so it doesn't
depend on storyboards, and app icons are copied as loose PNGs.
"""

import json
import plistlib
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from . import config
from .util import log, run


def bundle_identifier(xcodeproj, exclude=("Tests",)):
    pbx = (xcodeproj / "project.pbxproj").read_text()
    ids = [i.strip('"') for i in re.findall(r"PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);", pbx)]
    ids = [i for i in ids if not any(e in i for e in exclude)]
    if not ids:
        sys.exit(f"error: PRODUCT_BUNDLE_IDENTIFIER not found in {xcodeproj.name}")
    return ids[0]


def build_settings(ios_dir):
    """buildSettings of every XCBuildConfiguration in Runner.xcodeproj, as
    dicts of raw strings (enough to find each target's Info.plist, bundle ID
    and entitlements without a full pbxproj parser)."""
    pbxproj = ios_dir / "Runner.xcodeproj/project.pbxproj"
    if not pbxproj.exists():
        return
    for block in pbxproj.read_text().split("isa = XCBuildConfiguration;")[1:]:
        settings = block.split("name = ", 1)[0]
        yield {k: v.strip().strip('"') for k, v in re.findall(r"\b([A-Z_]+) = ([^;]+);", settings)}


def entitlements_file(ios_dir, info_plist):
    """CODE_SIGN_ENTITLEMENTS of the target whose Info.plist is `info_plist`
    (relative to ios_dir, e.g. "Runner/Info.plist")."""
    for settings in build_settings(ios_dir):
        if settings.get("INFOPLIST_FILE") == info_plist and settings.get("CODE_SIGN_ENTITLEMENTS"):
            path = ios_dir / settings["CODE_SIGN_ENTITLEMENTS"]
            return path if path.exists() else None
    return None


def embed_entitlements(executable, entitlements, identifier, variables):
    """Ad-hoc sign `executable` with the target's entitlements.

    xtool takes the entitlements to register (App Groups…) and re-sign with
    from the executable's existing signature, like Xcode's codesign step
    leaves them. rcodesign (apple-codesign) writes that signature on Linux."""
    if entitlements is None:
        return
    from . import deps
    rcodesign = deps.ensure_rcodesign()
    with open(entitlements, "rb") as f:
        values = expand(plistlib.load(f), variables)
    resolved = executable.with_name(f".{executable.name}.entitlements")
    with open(resolved, "wb") as f:
        plistlib.dump(values, f)
    try:
        run([rcodesign, "sign", "--entitlements-xml-file", resolved, "--binary-identifier", identifier,
             executable], capture_output=True, text=True)
    finally:
        resolved.unlink()


def read_xcconfig(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"\s*([A-Z_]+)\s*=\s*(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2).strip()
    return values


def expand(value, variables):
    """Replace $(VARIABLE) the way Xcode does when processing the Info.plist."""
    if isinstance(value, str):
        if hasattr(variables, "expand"):
            return variables.expand(value)
        return re.sub(r"\$\(([A-Za-z0-9_]+)\)|\$\{([A-Za-z0-9_]+)\}",
                      lambda m: variables.get(m.group(1) or m.group(2), ""), value)
    if isinstance(value, list):
        return [expand(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, variables) for k, v in value.items()}
    return value


def launch_screen(storyboard):
    """UILaunchScreen equivalent of a simple launch storyboard (a centered
    image on the system background, e.g. Expo's and Flutter's), since
    storyboards can't be compiled without ibtool."""
    try:
        root = ET.parse(storyboard).getroot()
    except (OSError, ET.ParseError):
        return {}
    image = next((v.get("image") for v in root.iter("imageView") if v.get("image")), None)
    return {"UIImageName": image} if image else {}


def info_plist(source, variables, scene_delegate=None):
    """Info.plist ready for a .app built without ibtool.

    scene_delegate: class (Module.Class) that creates the window in code,
    replacing the main storyboard."""
    with open(source, "rb") as f:
        info = expand(plistlib.load(f), variables)
    info.pop("UIMainStoryboardFile", None)
    storyboard = info.pop("UILaunchStoryboardName", None)
    if storyboard and "UILaunchScreen" not in info:
        folder = Path(source).parent
        candidates = [folder / f"{storyboard}.storyboard", folder / f"Base.lproj/{storyboard}.storyboard"]
        found = next((c for c in candidates if c.exists()), None)
        info["UILaunchScreen"] = launch_screen(found) if found else {}
    info.setdefault("UILaunchScreen", {})
    for configs in info.get("UIApplicationSceneManifest", {}).get("UISceneConfigurations", {}).values():
        for scene in configs:
            scene.pop("UISceneStoryboardFile", None)
            if scene_delegate:
                scene["UISceneDelegateClassName"] = scene_delegate
    info.update({
        "MinimumOSVersion": config.MIN_IOS,
        "CFBundleSupportedPlatforms": ["iPhoneOS"],
        "UIDeviceFamily": [1, 2],
        "DTPlatformName": "iphoneos",
        "DTSDKName": "iphoneos",
        "UIRequiredDeviceCapabilities": ["arm64"],
        "LSRequiresIPhoneOS": True,
    })
    return info


def add_icons(iconset, app, info):
    """App icons without actool: loose PNGs + CFBundleIcons (the pre-Assets.car format)."""
    contents = iconset / "Contents.json"
    if not contents.exists():
        return
    names = set()
    for image in json.loads(contents.read_text()).get("images", []):
        filename, size = image.get("filename"), image.get("size", "")
        if not filename or image.get("idiom") not in ("iphone", "ipad", "universal") or size == "1024x1024":
            continue
        base = f"AppIcon{size}"
        suffix = "" if image.get("scale", "1x") == "1x" else "@" + image["scale"]
        idiom = "~ipad" if image["idiom"] == "ipad" else ""
        shutil.copy(iconset / filename, app / f"{base}{suffix}{idiom}.png")
        names.add(base)
    if names:
        info["CFBundleIcons"] = {"CFBundlePrimaryIcon": {"CFBundleIconFiles": sorted(names)}}
        info["CFBundleIcons~ipad"] = info["CFBundleIcons"]


# Files Xcode copies as-is into the .app ("Copy Bundle Resources" phase):
# Firebase's GoogleService-Info.plist, privacy manifests, fonts...
RESOURCE_SUFFIXES = {".plist", ".json", ".xcprivacy", ".png", ".jpg", ".ttf", ".otf",
                     ".strings", ".mp3", ".wav", ".caf"}


def copy_loose_resources(source_dir, app):
    for f in source_dir.iterdir():
        if f.is_file() and f.suffix in RESOURCE_SUFFIXES and f.name != "Info.plist":
            shutil.copy(f, app / f.name)


def write_info_plist(app, info):
    with open(app / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    (app / "PkgInfo").write_text("APPL????")


def package_ipa(app, ipa):
    log(f"Packaging {ipa.name}")
    with zipfile.ZipFile(ipa, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(app.rglob("*")):
            if path.is_dir():
                continue
            entry = zipfile.ZipInfo(str(Path("Payload") / app.name / path.relative_to(app)))
            entry.external_attr = (path.stat().st_mode & 0xFFFF) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(entry, path.read_bytes())
