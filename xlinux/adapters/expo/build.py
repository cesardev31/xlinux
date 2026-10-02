"""Expo adapter: the equivalent of `expo run:ios` without a Mac.

  expo prebuild      -> ios/ (Xcode project + Podfile), when missing or when
                        app.json / package.json changed
  pod install        -> ios/Pods (CocoaPods on a portable Ruby, see core/deps)
  ExpoModulesJSI     -> built with our SwiftPM for iOS (jsi.py)
  49-ish pod targets -> core/xcode/build.py, each cached by its inputs
  app target         -> compiled, linked and assembled (core/xcode/package.py)

Debug builds use expo-dev-client: the app loads its JavaScript from Metro
(`xlinux run` starts it). Release builds embed the bundle, compiled to Hermes
bytecode by React Native's own bundle phase with hermes-compiler's Linux
hermesc. Build products live in the data directory.
"""

import hashlib
import json
import shutil
import sys
from pathlib import Path

from ...core import config, deps
from ...core.util import DirLock, log, run
from ...core.xcode import build as xbuild
from ...core.xcode import package, project as xproject
from . import jsi, macros

CONFIG_FILES = ("app.json", "app.config.js", "app.config.ts", "package.json")
# Pod script phases that must run on every build: they swap prebuilt
# XCFrameworks (Expo modules, React Native, Hermes) between their debug and
# release flavors.
FLAVOR_SCRIPTS = ("XCFramework for build configuration", "for the right configuration")


class Project:
    def __init__(self, path, debug):
        self.dir = Path(path).resolve()
        package_json = self.dir / "package.json"
        if not package_json.exists():
            sys.exit(f"error: {self.dir} is not a JavaScript project (no package.json)")
        self.package = json.loads(package_json.read_text())
        self.ios = self.dir / "ios"
        self.debug = debug
        self.configuration = "Debug" if debug else "Release"
        key = hashlib.sha1(str(self.dir).encode()).hexdigest()[:8]
        self.build_dir = config.data_dir() / "expo-build" / f"{self.dir.name}-{key}"
        self.app = None  # set by build()
        self.bundle_id = None
        self.lock = None

    def xcodeproj(self):
        projects = [p for p in self.ios.glob("*.xcodeproj") if p.name != "Pods.xcodeproj"]
        if not projects:
            sys.exit(f"error: no Xcode project in {self.ios}")
        return projects[0]

    def bundle_identifier(self):
        return self.bundle_id


def is_expo(project_dir):
    try:
        package = json.loads((Path(project_dir) / "package.json").read_text())
    except (OSError, ValueError):
        return False
    return "expo" in {**package.get("dependencies", {}), **package.get("devDependencies", {})}


def _newer(paths, than):
    return not than.exists() or any(p.exists() and p.stat().st_mtime > than.stat().st_mtime for p in paths)


def prebuild(project):
    """ios/ from app.json (Continuous Native Generation), like `expo run:ios`."""
    stamp = project.ios / ".xlinux-prebuild"
    if project.ios.is_dir() and not _newer([project.dir / f for f in CONFIG_FILES], stamp):
        return
    log("expo prebuild (ios)")
    run(["npx", "expo", "prebuild", "--platform", "ios", "--no-install"], cwd=project.dir,
        env={**config.tool_env(), "CI": "1"}, stdout=sys.stderr)
    stamp.touch()


def configure_expo(project, app_target):
    """The app's "[Expo] Configure project" phase: regenerates ExpoModulesProvider.swift."""
    script = project.ios / f"Pods/Target Support Files/Pods-{app_target}/expo-configure-project.sh"
    if script.exists():
        env = {**config.tool_env(), "PODS_ROOT": str(project.ios / "Pods"), "CONFIGURATION": project.configuration,
               "SRCROOT": str(project.ios)}
        run(["bash", script], cwd=project.ios, env=env, capture_output=True, text=True)


def run_pod_scripts(pods, names):
    """Run the pod script phases whose name contains one of `names`."""
    for target in pods.targets.values():
        for script in target.get("scripts", []):
            if any(n in (script.get("name") or "") for n in names):
                pods.run_script(target, pods.settings(target), script)


def hermesc(project):
    """Linux hermesc from hermes-compiler (the version React Native depends on;
    the one in Pods/hermes-engine is a macOS binary)."""
    found = list((project.dir / "node_modules/hermes-compiler/hermesc").glob("linux64-bin/hermesc"))
    return str(found[0]) if found else None


def bundle_javascript(app, target, settings, app_dir, compiler):
    """The app's "Bundle React Native code and images" phase (skipped in Debug)."""
    for script in target.get("scripts", []):
        if "Bundle React Native" in (script.get("name") or ""):
            extra = {"UNLOCALIZED_RESOURCES_FOLDER_PATH": app_dir.name,
                     "CONFIGURATION_BUILD_DIR": str(app_dir.parent)}
            if compiler:
                extra["HERMES_CLI_PATH"] = compiler
            if app.configuration != "Debug":
                log("JavaScript bundle (expo export:embed + hermesc)")
            app.run_script(target, settings, script, extra)



def build(project_dir, debug=True):
    config.require_data_dir()
    project = Project(project_dir, debug)
    project.lock = DirLock(project.build_dir, f"{project.dir.name} ({project.configuration.lower()})")

    prebuild(project)
    xproject.pod_install(project.ios, [project.dir / "package.json"])
    pods_root = project.ios / "Pods"
    jsi.ensure(project.dir, pods_root)
    plugins = macros.ensure(project.dir)

    ruby = deps.ensure_cocoapods()
    xcodeproj = project.xcodeproj()
    app_target = xcodeproj.stem
    pods = xproject.XcodeProject(xproject.dump(pods_root / "Pods.xcodeproj", project.configuration, ruby),
                                 project.build_dir, project.configuration)
    app = xproject.XcodeProject(xproject.dump(xcodeproj, project.configuration, ruby),
                                project.build_dir, project.configuration)
    context = xbuild.Context(pods_root, plugins)

    try:
        run_pod_scripts(pods, FLAVOR_SCRIPTS)
        built = xbuild.build_targets(pods, [f"Pods-{app_target}"], context)
        pods.prepare_all_xcframeworks()
        package.build_resource_bundles(pods)
        run_pod_scripts(pods, ("Generate app.config",))  # EXConstants.bundle/app.config
        log(f"Pods: {built} rebuilt" if built else "Pods: up to date")
        configure_expo(project, app_target)
        target = app.targets[app_target]
        settings = app.settings(target)
        log(f"{app_target} (app)")
        objects = xbuild.TargetBuild(app, target, settings, context).compile()
        app_dir = Path(settings.get("CONFIGURATION_BUILD_DIR")) / f"{app_target}.app"
        if app_dir.exists():
            shutil.rmtree(app_dir)
        package.link(app, app_target, objects, app_dir / app_target)
        project.bundle_id = package.assemble(app, pods, app_target, app_dir)
        bundle_javascript(app, target, settings, app_dir, hermesc(project))
    except (xbuild.BuildError, RuntimeError) as e:
        sys.exit(f"error: {e}")
    project.app = app_dir
    log(f"Done: {app_dir}")
    return project
