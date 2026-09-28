"""Adaptador Flutter: lo mismo que `flutter build ios` + Xcode.

`flutter assemble` (como Xcode): App.framework + flutter_assets + native assets
          (release: Dart AOT con el gen_snapshot de macOS en Darling;
           debug: stub + kernel_blob.bin para JIT)
luego:    Runner (+ plugins por SwiftPM), Runner.app, .ipa
"""

import json
import plistlib
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from ...core import app as appkit
from ...core import config, toolchain
from ...core.util import log, output, run
from . import plugins

ENGINE_BASE_URL = "https://storage.googleapis.com/flutter_infra_release/flutter"
SUPPORT = config.SUPPORT / "flutter"
# Reemplaza a Main.storyboard (no hay ibtool en Linux).
SCENE_DELEGATE = "Runner.FlutterLinuxSceneDelegate"


class Project:
    def __init__(self, path, debug):
        self.dir = Path(path).resolve()
        if not (self.dir / "pubspec.yaml").exists():
            sys.exit(f"error: {self.dir} no es un proyecto Flutter (no hay pubspec.yaml)")
        self.ios = self.dir / "ios"
        self.package = re.search(r"^name:\s*(\S+)", (self.dir / "pubspec.yaml").read_text(), re.M).group(1)
        self.debug = debug
        self.build_dir = self.dir / ("build/ios-linux-debug" if debug else "build/ios-linux")
        self.app = self.build_dir / "Runner.app"
        self.ipa = self.build_dir / f"{self.package}.ipa"

    def bundle_identifier(self):
        return appkit.bundle_identifier(self.ios / "Runner.xcodeproj", exclude=("RunnerTests",))


def flutter_info():
    out = output(["flutter", "--version", "--machine"])
    info = json.loads(out[out.index("{"):])
    return Path(info["flutterRoot"]), info["engineRevision"]


def engine_dir(revision):
    return config.data_dir() / "flutter-engine" / revision


def ensure_engine(revision):
    """Artefactos iOS del engine (lo que haría `flutter precache --ios`)."""
    root = engine_dir(revision)
    for mode in ("ios", "ios-release"):
        target = root / mode
        if (target / "Flutter.xcframework").exists():
            continue
        log(f"Descargando artefactos {mode} del engine {revision[:10]}")
        target.mkdir(parents=True, exist_ok=True)
        archive = target / "artifacts.zip"
        urllib.request.urlretrieve(f"{ENGINE_BASE_URL}/{revision}/{mode}/artifacts.zip", archive)
        run(["unzip", "-qo", archive, "-d", target])
        archive.unlink()
    return root


GEN_SNAPSHOT_WRAPPER = """#!/usr/bin/env python3
# Generado por flutter-ios-linux. El gen_snapshot de iOS solo existe para
# macOS: `flutter assemble` (release/profile) lo llama y aquí corre en Darling.
import sys
sys.path.insert(0, {repo!r})
from fil.core import darling
result = darling.run_macos_tool({real!r}, sys.argv[1:], capture_output=False, check=False)
sys.exit(result.returncode)
"""
MARKER = ".flutter-ios-linux"


def link_engine_into_flutter_cache(flutter_root, engine):
    """`flutter assemble` busca los artefactos iOS en su propia caché (lo que
    haría `flutter precache --ios`): se enlazan a los del directorio de datos
    en vez de descargarlos otra vez al disco interno. En ios-release,
    gen_snapshot_arm64 se reemplaza por un envoltorio que lo corre en Darling."""
    cache = flutter_root / "bin/cache/artifacts/engine"
    debug = cache / "ios"
    if not (debug.exists() and not debug.is_symlink()):  # un precache real se respeta
        if debug.is_symlink() and debug.resolve() != (engine / "ios").resolve():
            debug.unlink()
        if not debug.exists():
            debug.symlink_to(engine / "ios")

    release = cache / "ios-release"
    if release.exists() and not release.is_symlink() and not (release / MARKER).exists():
        return  # precache real
    if release.is_symlink():
        release.unlink()
    release.mkdir(exist_ok=True)
    (release / MARKER).write_text(str(engine / "ios-release") + "\n")
    for item in (engine / "ios-release").iterdir():
        link = release / item.name
        if item.name == "gen_snapshot_arm64":
            continue
        if not link.is_symlink():
            link.symlink_to(item)
    wrapper = release / "gen_snapshot_arm64"
    if wrapper.is_symlink():
        wrapper.unlink()
    wrapper.write_text(GEN_SNAPSHOT_WRAPPER.format(
        repo=str(config.REPO), real=str(engine / "ios-release/gen_snapshot_arm64")))
    wrapper.chmod(0o755)


def assemble(project, frameworks):
    """`flutter assemble` como lo invoca Xcode (xcode_backend.dart).

    Genera App.framework (debug: stub + kernel para JIT; release: Dart AOT con
    gen_snapshot en Darling) + flutter_assets, y corre los hooks de native
    assets, que necesitan SdkRoot y `xcrun` (support/core/bin/xcrun)."""
    mode = "debug" if project.debug else "release"
    log(f"App.framework + flutter_assets + native assets (flutter assemble, {mode})")
    out = project.build_dir / "assemble"
    run(["flutter", "--no-version-check", "assemble", f"--output={out}/",
         "-dTargetPlatform=ios", "-dIosArchs=arm64", "-dTargetFile=lib/main.dart",
         f"-dBuildMode={mode}", f"-dConfiguration={mode.capitalize()}", f"-dSdkRoot={config.IPHONE_SDK}",
         f"-dTrackWidgetCreation={'true' if project.debug else 'false'}",
         f"-dTreeShakeIcons={'false' if project.debug else 'true'}", "-dDartObfuscation=false",
         "-dSplitDebugInfo=", "-dAction=build", f"-dSrcRoot={project.ios}",
         f"{mode}_ios_bundle_flutter_assets"],
        cwd=project.dir, capture_output=True, text=True)
    shutil.copytree(out / "App.framework", frameworks / "App.framework", symlinks=True, dirs_exist_ok=True)
    # Native assets (hooks de Dart, p. ej. objective_c por FFI): Xcode los embebe.
    for fw in (out / "native_assets").glob("*.framework"):
        shutil.copytree(fw, frameworks / fw.name, symlinks=True, dirs_exist_ok=True)


def compile_runner(project, flutter_fw_parent, obj, executable):
    """Runner sin plugins: swiftc/clang directo (más rápido que SwiftPM)."""
    log("Runner nativo (swiftc + SDK de iOS)")
    obj.mkdir(parents=True, exist_ok=True)
    runner = project.ios / "Runner"
    objects = []
    for m in sorted(runner.glob("*.m")):
        o = obj / (m.stem + ".o")
        toolchain.clang(["-fobjc-arc", "-fmodules", "-O2", "-F", flutter_fw_parent, "-I", runner,
                         "-c", m, "-o", o])
        objects.append(o)
    args = ["-Onone" if project.debug else "-O", "-module-name", "Runner", "-F", flutter_fw_parent, "-I", runner]
    bridging = runner / "Runner-Bridging-Header.h"
    if bridging.exists():
        args += ["-import-objc-header", bridging]
    args += sorted(runner.glob("*.swift")) + [SUPPORT / "FlutterLinuxSceneDelegate.swift"] + objects
    args += ["-framework", "Flutter", "-Xlinker", "-rpath", "-Xlinker", "@executable_path/Frameworks",
             "-o", executable]
    toolchain.swiftc(args)


def build(project_dir, debug=False):
    """Compila la app y devuelve el Project con Runner.app y el .ipa listos."""
    config.require_data_dir()
    toolchain.require_sdk()
    project = Project(project_dir, debug)
    flutter_root, revision = flutter_info()
    engine = ensure_engine(revision)
    link_engine_into_flutter_cache(flutter_root, engine)
    flutter_fw_parent = engine / ("ios" if debug else "ios-release") / "Flutter.xcframework/ios-arm64"

    if project.app.exists():
        shutil.rmtree(project.app)
    frameworks = project.app / "Frameworks"
    app_framework = frameworks / "App.framework"
    frameworks.mkdir(parents=True)

    run(["flutter", "pub", "get"], cwd=project.dir, stdout=subprocess.DEVNULL)
    assemble(project, frameworks)
    if plugins.swiftpm_plugins(project):
        out = plugins.build(project, flutter_fw_parent)
        toolchain.pack_swiftpm_outputs(out, project.app, "Runner", skip_frameworks=("Flutter",))
    else:
        compile_runner(project, flutter_fw_parent, project.build_dir / "obj", project.app / "Runner")

    log("Armando Runner.app")
    with open(project.ios / "Flutter/AppFrameworkInfo.plist", "rb") as f:
        fw_info = plistlib.load(f)
    fw_info["MinimumOSVersion"] = config.MIN_IOS
    with open(app_framework / "Info.plist", "wb") as f:
        plistlib.dump(fw_info, f)
    shutil.copytree(flutter_fw_parent / "Flutter.framework", frameworks / "Flutter.framework",
                    ignore=shutil.ignore_patterns("_CodeSignature", "Headers", "Modules", "module.modulemap"))

    xc = appkit.read_xcconfig(project.ios / "Flutter/Generated.xcconfig")
    variables = {
        "EXECUTABLE_NAME": "Runner", "PRODUCT_NAME": "Runner", "PRODUCT_MODULE_NAME": "Runner",
        "DEVELOPMENT_LANGUAGE": "en",
        "PRODUCT_BUNDLE_IDENTIFIER": project.bundle_identifier(),
        "FLUTTER_BUILD_NAME": xc.get("FLUTTER_BUILD_NAME", "1.0.0"),
        "FLUTTER_BUILD_NUMBER": xc.get("FLUTTER_BUILD_NUMBER", "1"),
    }
    info = appkit.info_plist(project.ios / "Runner/Info.plist", variables, scene_delegate=SCENE_DELEGATE)
    appkit.add_icons(project.ios / "Runner/Assets.xcassets/AppIcon.appiconset", project.app, info)
    appkit.copy_loose_resources(project.ios / "Runner", project.app)
    appkit.write_info_plist(project.app, info)
    appkit.package_ipa(project.app, project.ipa)
    log(f"Listo: {project.ipa}")
    return project
