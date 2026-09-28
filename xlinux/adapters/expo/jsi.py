"""ExpoModulesJSI: the one Expo module built at build time, not prebuilt.

On macOS a CocoaPods script phase runs its build-xcframework.sh (xcodebuild on
a SwiftPM package with Swift <-> C++ interop). Here the package is built with
our SwiftPM for iOS and the framework is assembled the way that script does,
into node_modules/expo-modules-jsi/apple/Products (where the pod expects it).

Swift 6.4 rejects two constructs Xcode 27's Swift (6.3) accepts:
support/patches/expo-modules-jsi-swift-6.4.patch fixes them without changing
the public ABI the prebuilt ExpoModulesCore links against.
"""

import hashlib
import json
import os
import plistlib
import shutil
from pathlib import Path

from ...core import config, toolchain
from ...core.util import log, run

PATCH = config.SUPPORT / "patches/expo-modules-jsi-swift-6.4.patch"
NAME = "ExpoModulesJSI"


def _fingerprint(package, pods_root):
    h = hashlib.sha256(Path(__file__).read_bytes() + PATCH.read_bytes())
    h.update((package / "package.json").read_bytes())
    jsi = pods_root / "Headers/Public/React-jsi/jsi/jsi.h"
    if jsi.exists():
        h.update(jsi.read_bytes())
    return h.hexdigest()


def ensure(project_dir, pods_root):
    package = Path(project_dir) / "node_modules/expo-modules-jsi"
    if not package.is_dir():
        return
    slice_dir = package / f"apple/Products/{NAME}.xcframework/ios-arm64"
    framework = slice_dir / f"{NAME}.framework"
    stamp = slice_dir / ".xlinux-build"
    fingerprint = _fingerprint(package, pods_root)
    if stamp.exists() and stamp.read_text() == fingerprint and (framework / NAME).exists():
        return
    version = json.loads((package / "package.json").read_text())["version"]
    log(f"{NAME} {version} (SwiftPM, Swift <-> C++)")

    # A patched copy of the package: node_modules stays untouched.
    work = config.data_dir() / "expo" / f"expo-modules-jsi-{version}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(package / "apple", work / "apple", ignore=shutil.ignore_patterns(".build", "Products"))
    run(["git", "apply", "-p1", PATCH], cwd=work)
    source = work / "apple"
    env = config.tool_env()
    env["PODS_ROOT"] = str(pods_root)
    run(["bash", source / "scripts/generate-modulemap.sh"], env=env)

    # swift/bridging (C++ interop) lives in the toolchain, not in xtool's SDK.
    include = config.swift_bin().parent / "include"
    scratch_name = f"{NAME}-{version}"
    os.environ["PODS_ROOT"] = str(pods_root)  # read by its Package.swift
    try:
        products = toolchain.swiftpm_build(source, debug=False, scratch_name=scratch_name, extra_flags=[
            "-Xswiftc", "-Xcc", "-Xswiftc", f"-I{include}", "-Xcxx", f"-I{include}", "-Xcc", f"-I{include}"])
    finally:
        os.environ.pop("PODS_ROOT", None)
    intermediates = config.data_dir() / "spm-build" / scratch_name / "out/Intermediates.noindex"

    shutil.rmtree(framework, ignore_errors=True)
    (framework / "Modules").mkdir(parents=True)
    (framework / "Headers").mkdir()
    # SwiftPM's dynamic library comes out empty: its objects are archives the
    # linker only pulls from on demand, so link them in whole.
    run(["swiftc", "-target", "arm64-apple-ios16.4", "-sdk", config.IPHONE_SDK,
         "-resource-dir", config.SWIFT_RESOURCES, *toolchain.SWIFT_LINKER_FLAGS, "-emit-library",
         "-Xlinker", "-force_load", "-Xlinker", products / f"{NAME}.o",
         "-Xlinker", "-force_load", "-Xlinker", products / f"{NAME}-Cxx.o",
         "-lc++", "-Xlinker", "-install_name", "-Xlinker", f"@rpath/{NAME}.framework/{NAME}",
         # React, JSI and Hermes symbols come from the app at load time.
         "-Xlinker", "-undefined", "-Xlinker", "dynamic_lookup", *toolchain.builtins(),
         "-o", framework / NAME], capture_output=True, text=True)
    swiftmodule = framework / f"Modules/{NAME}.swiftmodule"
    shutil.copytree(products / f"{NAME}.swiftmodule", swiftmodule)
    shutil.rmtree(swiftmodule / "Project", ignore_errors=True)
    interface = next(intermediates.glob(f"{NAME}.build/*/{NAME}-t.build/Objects-normal/arm64/{NAME}.swiftinterface"))
    # Like build-xcframework.sh: drop the `extension __ObjC.` blocks Swift emits for C++ types.
    lines, skip = [], False
    for line in interface.read_text().splitlines(keepends=True):
        if line.startswith("extension __ObjC."):
            skip = True
        if not skip and "_ConstraintThatIsNotPartOfTheAPIOfThisLibrary" not in line:
            lines.append(line)
        if skip and line.startswith("}"):
            skip = False
    (swiftmodule / "arm64-apple-ios.swiftinterface").write_text("".join(lines))
    generated = next(intermediates.glob("GeneratedModuleMaps-iphoneos"))
    shutil.copy2(generated / f"{NAME}-Swift.h", framework / "Headers")
    shutil.copy2(generated / f"{NAME}.modulemap", framework / "Headers/module.modulemap")
    with open(framework / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleExecutable": NAME, "CFBundleIdentifier": "expo.modules.jsi",
                       "CFBundlePackageType": "FMWK", "CFBundleName": NAME, "CFBundleVersion": "1",
                       "CFBundleShortVersionString": version, "MinimumOSVersion": "16.4",
                       "CFBundleSupportedPlatforms": ["iPhoneOS"]}, f)
    (slice_dir / ".build-hash").write_text(fingerprint)  # Expo's own "not a stub" marker
    stamp.write_text(fingerprint)
