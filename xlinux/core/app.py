"""Assemble a .app / .ipa from an Xcode project, without Xcode.

Linux has no ibtool (storyboards): the Info.plist is adjusted so it doesn't
depend on storyboards, and app icons are copied as loose PNGs.
"""

import json
import plistlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

from . import config
from .util import log


def bundle_identifier(xcodeproj, exclude=("Tests",)):
    pbx = (xcodeproj / "project.pbxproj").read_text()
    ids = [i.strip('"') for i in re.findall(r"PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);", pbx)]
    ids = [i for i in ids if not any(e in i for e in exclude)]
    if not ids:
        sys.exit(f"error: PRODUCT_BUNDLE_IDENTIFIER not found in {xcodeproj.name}")
    return ids[0]


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
        return re.sub(r"\$\(([A-Z_]+)\)", lambda m: variables.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, variables) for k, v in value.items()}
    return value


def info_plist(source, variables, scene_delegate=None):
    """Info.plist ready for a .app built without ibtool.

    scene_delegate: class (Module.Class) that creates the window in code,
    replacing the main storyboard."""
    with open(source, "rb") as f:
        info = expand(plistlib.load(f), variables)
    info.pop("UIMainStoryboardFile", None)
    info.pop("UILaunchStoryboardName", None)
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
