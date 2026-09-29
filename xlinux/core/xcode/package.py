"""Link an app target and assemble its .app (after build.py built its pods).

Mirrors the phases Xcode and CocoaPods run for an app: link with the Pods
xcconfig's OTHER_LDFLAGS, embed the dynamic frameworks listed in
Pods-<App>-frameworks.sh, copy resources (Pods-<App>-resources.sh and the
app's own), build resource bundles, compile asset catalogs with the actool
stand-in and write Info.plist. Storyboards can't be compiled without Xcode:
the launch storyboard becomes UILaunchScreen (see core/app.py).
"""

import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .. import app as appkit
from .. import config, toolchain
from ..util import log
from .build import BuildError, run

ACTOOL = config.SUPPORT / "core/bin/actool"
NOT_EMBEDDED = {"Headers", "PrivateHeaders", "Modules", "_CodeSignature"}


def link(project, target_name, objects, output):
    s = project.settings(project.targets[target_name])
    args = ["swiftc", "-target", f"arm64-apple-ios{s.get('IPHONEOS_DEPLOYMENT_TARGET')}", "-sdk", config.IPHONE_SDK,
            "-resource-dir", config.SWIFT_RESOURCES, *toolchain.SWIFT_LINKER_FLAGS, "-emit-executable"]
    for path in s.list("LIBRARY_SEARCH_PATHS"):
        args += ["-L", path]
    for path in s.list("FRAMEWORK_SEARCH_PATHS"):
        args += ["-F", path]
    flags = s.list("OTHER_LDFLAGS")
    i = 0
    while i < len(flags):
        f = flags[i]
        if f == "-framework" and i + 1 < len(flags):
            args += [f, flags[i + 1]]
            i += 1
        elif f == "-weak_framework" and i + 1 < len(flags):
            args += ["-Xlinker", f, "-Xlinker", flags[i + 1]]
            i += 1
        elif f.startswith(("-l", "-L", "-F")):
            args.append(f)
        else:
            args += ["-Xlinker", f]
        i += 1
    for rpath in s.list("LD_RUNPATH_SEARCH_PATHS"):
        args += ["-Xlinker", "-rpath", "-Xlinker", rpath]
    output.parent.mkdir(parents=True, exist_ok=True)
    run([*args, *objects, *toolchain.builtins(), "-o", output])


def actool(catalog, dest, app_icon=None, partial=None):
    cmd = [sys.executable, ACTOOL, "--compile", dest]
    if app_icon:
        cmd += ["--app-icon", app_icon, "--output-partial-info-plist", partial]
    result = subprocess.run([str(c) for c in cmd + [catalog]], capture_output=True, text=True,
                            env=config.tool_env())
    if result.returncode:
        raise BuildError(f"actool failed for {catalog}:\n{result.stderr[-2000:]}")


def writable(path):
    """CocoaPods checkouts are read-only; copies must stay replaceable."""
    for p in [Path(path), *Path(path).rglob("*")] if Path(path).is_dir() else [Path(path)]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode | 0o200)


def copy_resource(src, dest):
    src = Path(src)
    if src.suffix == ".xcassets":
        actool(src, dest)
    elif src.suffix in (".storyboard", ".xib"):
        log(f"warning: {src.name} skipped (no ibtool on Linux)")
    elif src.is_dir():
        shutil.copytree(src, dest / src.name, dirs_exist_ok=True)
        writable(dest / src.name)
    elif src.exists():
        shutil.copyfile(src, dest / src.name)


def build_resource_bundles(pods):
    """CocoaPods resource bundle targets (<Pod>-<Bundle>): only resources, no
    code. Returns the bundles."""
    bundles = []
    for target in pods.targets.values():
        if target.get("product_type") != "com.apple.product-type.bundle":
            continue
        s = pods.settings(target)
        bundle = Path(s.get("CONFIGURATION_BUILD_DIR")) / f"{s.get('PRODUCT_NAME') or target['name']}.bundle"
        shutil.rmtree(bundle, ignore_errors=True)
        bundle.mkdir(parents=True)
        for resource in target.get("resources", []):
            copy_resource(resource, bundle)
        with open(bundle / "Info.plist", "wb") as f:
            plistlib.dump({"CFBundleIdentifier": s.get("PRODUCT_BUNDLE_IDENTIFIER") or f"org.cocoapods.{target['name']}",
                           "CFBundleName": bundle.stem, "CFBundlePackageType": "BNDL",
                           "CFBundleInfoDictionaryVersion": "6.0", "CFBundleVersion": "1",
                           "CFBundleShortVersionString": "1.0"}, f)
        bundles.append(bundle)
    return bundles


def script_entries(script, command, configuration):
    """Arguments of `command` lines in the configuration's section of a
    CocoaPods script (Pods-<App>-frameworks.sh / -resources.sh)."""
    entries, inside = [], False
    for line in Path(script).read_text().splitlines():
        line = line.strip()
        if line.startswith(f'if [[ "$CONFIGURATION" == "{configuration}" ]]'):
            inside = True
        elif inside and line == "fi":
            break
        elif inside and line.startswith(command + " "):
            entries.append(shlex.split(line)[1])
    return entries


def assemble(app_project, pods, target_name, app_dir):
    """Frameworks, resources, icons, Info.plist and entitlements into app_dir
    (which already holds the linked executable)."""
    t = app_project.targets[target_name]
    s = app_project.settings(t)
    support = pods.root / "Target Support Files" / f"Pods-{target_name}"
    pods_settings = pods.settings(pods.targets[f"Pods-{target_name}"])
    configuration = app_project.configuration

    frameworks = app_dir / "Frameworks"
    shutil.rmtree(frameworks, ignore_errors=True)
    frameworks.mkdir()
    for fw in script_entries(support / f"Pods-{target_name}-frameworks.sh", "install_framework", configuration):
        src = Path(pods_settings.expand(fw)).resolve()
        shutil.copytree(src, frameworks / src.name, symlinks=False,
                        ignore=lambda _, names: [n for n in names if n in NOT_EMBEDDED or n.endswith(".dSYM")])
        writable(frameworks / src.name)
    for resource in script_entries(support / f"Pods-{target_name}-resources.sh", "install_resource", configuration):
        copy_resource(pods_settings.expand(resource), app_dir)

    partial = app_dir.parent / "assetcatalog_generated_info.plist"
    partial.unlink(missing_ok=True)
    for resource in t.get("resources", []):
        if resource.endswith(".xcassets"):
            actool(resource, app_dir, app_icon=s.get("ASSETCATALOG_COMPILER_APPICON_NAME") or "AppIcon",
                   partial=partial)
        elif not resource.endswith((".storyboard", ".xib")):  # launch storyboard -> UILaunchScreen
            copy_resource(resource, app_dir)

    variables = {k: s.get(k) for k in ("PRODUCT_BUNDLE_IDENTIFIER", "PRODUCT_NAME", "MARKETING_VERSION",
                                       "CURRENT_PROJECT_VERSION", "DEVELOPMENT_LANGUAGE")}
    variables.update(EXECUTABLE_NAME=target_name, PRODUCT_MODULE_NAME=target_name)
    info = appkit.info_plist(app_project.root / s.get("INFOPLIST_FILE"), variables)
    info["MinimumOSVersion"] = s.get("IPHONEOS_DEPLOYMENT_TARGET") or info["MinimumOSVersion"]
    if partial.exists():
        with open(partial, "rb") as f:
            info.update(plistlib.load(f))
    appkit.write_info_plist(app_dir, info)

    entitlements = s.get("CODE_SIGN_ENTITLEMENTS")
    if entitlements:
        appkit.embed_entitlements(app_dir / target_name, app_project.root / entitlements,
                                  variables["PRODUCT_BUNDLE_IDENTIFIER"], variables)
    return variables["PRODUCT_BUNDLE_IDENTIFIER"]
