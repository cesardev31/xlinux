"""Native Xcode projects (SwiftUI / UIKit apps), the equivalent of
`xcodebuild` without a Mac.

  pod install        -> Pods/, only if the project has a Podfile
  pod targets        -> core/xcode/build.py, each cached by its inputs
  asset symbols      -> Color.brand / Image(.logo) (core/xcode/assets.py)
  app target         -> compiled, linked and assembled (core/xcode/package.py)
  app extensions     -> PlugIns/*.appex (core/extensions.py)

Xcode 16+ projects (synchronized folders, generated Info.plist, MainActor
default isolation) are read as they are. Build products live in the data
directory.
"""

import hashlib
import shutil
import sys
from pathlib import Path

from ...core import config, deps, extensions, toolchain
from ...core.util import DirLock, log
from ...core.xcode import assets
from ...core.xcode import build as xbuild
from ...core.xcode import package, project as xproject

APPLICATION = "com.apple.product-type.application"


def xcodeprojs(directory):
    return sorted(p for p in Path(directory).glob("*.xcodeproj") if p.name != "Pods.xcodeproj")


def is_xcode_project(project_dir):
    return bool(xcodeprojs(project_dir))


class Project:
    def __init__(self, path, debug, target=None, configuration=None):
        self.dir = Path(path).resolve()
        found = xcodeprojs(self.dir)
        if not found:
            sys.exit(f"error: no Xcode project (.xcodeproj) in {self.dir}")
        if len(found) > 1:
            log(f"Several Xcode projects; using {found[0].name}")
        self.xcodeproj = found[0]
        self.debug = debug
        self.configuration = configuration or ("Debug" if debug else "Release")
        self.target_name = target
        key = hashlib.sha1(str(self.dir).encode()).hexdigest()[:8]
        self.build_dir = config.data_dir() / "xcode-build" / f"{self.dir.name}-{key}"
        self.app = None  # set by build()
        self.bundle_id = None
        self.executable = None
        self.lock = None

    def bundle_identifier(self):
        return self.bundle_id


def app_target(xcode, wanted):
    apps = [t for t in xcode.targets.values() if t.get("product_type") == APPLICATION]
    if wanted:
        if wanted not in xcode.targets:
            sys.exit(f"error: no target {wanted} in the project (targets: {', '.join(xcode.targets)})")
        return xcode.targets[wanted]
    if not apps:
        sys.exit("error: the Xcode project has no iOS app target")
    if len(apps) > 1:
        log(f"Several app targets ({', '.join(t['name'] for t in apps)}); building {apps[0]['name']} "
            "(choose with --target)")
    return apps[0]


def check_supported(target, settings):
    sdk = settings.get("SDKROOT")
    platforms = settings.get("SUPPORTED_PLATFORMS")
    if sdk and sdk not in ("iphoneos", "auto") and "iphoneos" not in platforms:
        sys.exit(f"error: {target['name']} builds for {sdk}, not iOS")
    if target.get("packages"):
        sys.exit(f"error: {target['name']} uses Swift packages ({', '.join(target['packages'])}), "
                 "which xlinux doesn't build for native Xcode projects yet")
    if not target.get("configuration_available", True):
        sys.exit(f"error: {target['name']} has no {settings.get('CONFIGURATION')} configuration")


def asset_symbols(target, settings):
    """Generated Swift symbols for the target's asset catalogs, or None."""
    if settings.get("ASSETCATALOG_COMPILER_GENERATE_ASSET_SYMBOLS", "YES") == "NO":
        return None
    catalogs = [r for r in target.get("resources", []) if r.endswith(".xcassets")]
    extensions_too = settings.get("ASSETCATALOG_COMPILER_GENERATE_SWIFT_ASSET_SYMBOL_EXTENSIONS", "YES") != "NO"
    output = Path(settings.builtins["DERIVED_FILE_DIR"]) / "GeneratedAssetSymbols.swift"
    return assets.generate(catalogs, output, extensions=extensions_too)


def build(project_dir, debug=True, target=None, configuration=None):
    config.require_data_dir()
    toolchain.require_sdk()
    project = Project(project_dir, debug, target, configuration)
    project.lock = DirLock(project.build_dir, f"{project.dir.name} ({project.configuration.lower()})")

    ruby = deps.ensure_cocoapods()  # its xcodeproj gem reads the project
    pods = None
    if (project.dir / "Podfile").exists():
        xproject.pod_install(project.dir)
        pods = xproject.XcodeProject(xproject.dump(project.dir / "Pods/Pods.xcodeproj", project.configuration, ruby),
                                     project.build_dir, project.configuration)
    xcode = xproject.XcodeProject(xproject.dump(project.xcodeproj, project.configuration, ruby),
                                  project.build_dir, project.configuration)
    target = app_target(xcode, project.target_name)
    name = target["name"]
    settings = xcode.settings(target)
    check_supported(target, settings)
    if deps.needs_patched_macros([project.dir]):
        deps.ensure_macro_server()  # e.g. a UIKit #Preview
    context = xbuild.Context(project.dir / "Pods")

    try:
        if pods:
            built = xbuild.build_targets(pods, [f"Pods-{name}"], context)
            pods.prepare_all_xcframeworks()
            package.build_resource_bundles(pods)
            log(f"Pods: {built} rebuilt" if built else "Pods: up to date")
        symbols = asset_symbols(target, settings)
        if symbols:
            target = {**target, "sources": target["sources"] + [{"path": str(symbols), "flags": None}]}
        log(f"{name} ({len(target['sources'])} files)")
        objects = xbuild.TargetBuild(xcode, target, settings, context).compile()
        product = settings.get("PRODUCT_NAME") or name
        app_dir = Path(settings.get("CONFIGURATION_BUILD_DIR")) / f"{product}.app"
        if app_dir.exists():
            shutil.rmtree(app_dir)
        package.link(xcode, name, objects, app_dir / product)
        project.bundle_id = package.assemble(xcode, pods, name, app_dir)
        extensions.build_all(project.dir, app_dir, project.build_dir, debug, xcode=xcode)
        toolchain.thin_frameworks(app_dir)
    except (xbuild.BuildError, RuntimeError) as e:
        sys.exit(f"error: {e}")
    project.app = app_dir
    project.executable = product
    log(f"Done: {app_dir}")
    return project
