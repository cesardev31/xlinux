"""Adaptador Flutter: lo mismo que `flutter build ios` + Xcode.

release:  Dart -> kernel -> gen_snapshot (binario macOS en Darling) -> App.framework
debug:    App.framework es un stub; el código va como kernel_blob.bin (JIT)
ambos:    flutter_assets, Runner (+ plugins por SwiftPM), Runner.app, .ipa
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
from ...core import config, darling, toolchain
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


def link_engine_into_flutter_cache(flutter_root, engine):
    """`flutter assemble` busca los artefactos iOS en su propia caché (lo que
    haría `flutter precache --ios`): se enlazan a los del directorio de datos
    en vez de descargarlos otra vez al disco interno."""
    cache = flutter_root / "bin/cache/artifacts/engine"
    for mode in ("ios", "ios-release"):
        link = cache / mode
        if link.is_symlink() and link.resolve() == (engine / mode).resolve():
            continue
        if link.exists() and not link.is_symlink():
            continue  # precache real: se respeta
        if link.is_symlink():
            link.unlink()
        link.symlink_to(engine / mode)


def compile_kernel(flutter_root, project, out):
    log("Dart -> kernel (frontend_server)")
    cache = flutter_root / "bin/cache"
    run([
        cache / "dart-sdk/bin/dartaotruntime",
        cache / "dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot",
        "--sdk-root", cache / "artifacts/engine/common/flutter_patched_sdk_product/",
        "--target=flutter", "--no-print-incremental-dependencies",
        "-Ddart.vm.profile=false", "-Ddart.vm.product=true",
        "--delete-tostring-package-uri=dart:ui", "--delete-tostring-package-uri=package:flutter",
        "--aot", "--tfa", "--target-os", "ios",
        "--packages", project.dir / ".dart_tool/package_config.json",
        "--output-dill", out, "--verbosity=error",
        f"package:{project.package}/main.dart",
    ], cwd=project.dir, stdout=subprocess.DEVNULL)


def compile_aot(engine, dill, framework_dir, build_dir):
    log("kernel -> App.framework arm64 (gen_snapshot en Darling)")
    framework_dir.mkdir(parents=True, exist_ok=True)
    darling.run_macos_tool(engine / "ios-release/gen_snapshot_arm64", [
        "--deterministic", "--snapshot_kind=app-aot-macho-dylib",
        f"--macho={framework_dir / 'App'}", f"--macho-object={build_dir / 'app.o'}",
        f"--macho-min-os-version={config.MIN_IOS}",
        "--macho-rpath=@executable_path/Frameworks,@loader_path/Frameworks",
        "--macho-install-name=@rpath/App.framework/App", dill,
    ])
    if not (framework_dir / "App").exists():
        sys.exit("error: gen_snapshot no generó App.framework/App")


def stub_app_framework(framework_dir, build_dir):
    """En debug el código Dart va en kernel_blob.bin (JIT): App es un dylib vacío."""
    log("App.framework stub (debug)")
    framework_dir.mkdir(parents=True, exist_ok=True)
    stub = build_dir / "debug_app.c"
    stub.write_text("static const int Moo = 88;\n")
    toolchain.dylib(stub, framework_dir / "App", "@rpath/App.framework/App", extra=["-fapplication-extension"])


def assemble_debug(project, frameworks):
    """Debug: `flutter assemble` como lo invoca Xcode (xcode_backend.dart).

    Genera App.framework (stub + flutter_assets + kernel) y corre los hooks de
    native assets, que necesitan SdkRoot y `xcrun` (support/core/bin/xcrun)."""
    log("App.framework + flutter_assets + native assets (flutter assemble)")
    out = project.build_dir / "assemble"
    run(["flutter", "--no-version-check", "assemble", f"--output={out}/",
         "-dTargetPlatform=ios", "-dIosArchs=arm64", "-dTargetFile=lib/main.dart",
         "-dBuildMode=debug", "-dConfiguration=Debug", f"-dSdkRoot={config.IPHONE_SDK}",
         "-dTrackWidgetCreation=true", "-dTreeShakeIcons=false", "-dDartObfuscation=false",
         "-dSplitDebugInfo=", "-dAction=build", f"-dSrcRoot={project.ios}",
         "debug_ios_bundle_flutter_assets"],
        cwd=project.dir, capture_output=True, text=True)
    shutil.copytree(out / "App.framework", frameworks / "App.framework", symlinks=True, dirs_exist_ok=True)


def build_assets(project, out):
    log("flutter_assets (flutter build bundle)")
    run(["flutter", "build", "bundle", "--debug" if project.debug else "--release",
         "--target-platform", "ios", "--asset-dir", out],
        cwd=project.dir, stdout=subprocess.DEVNULL)


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
    if debug:
        assemble_debug(project, frameworks)
    else:
        dill = project.build_dir / "app.dill"
        compile_kernel(flutter_root, project, dill)
        compile_aot(engine, dill, app_framework, project.build_dir)
        build_assets(project, app_framework / "flutter_assets")
    # Native assets (hooks de Dart, p. ej. objective_c/path_provider por FFI):
    # `flutter build bundle` los deja como frameworks; Xcode los embebe en el .app.
    for fw in (project.dir / "build/native_assets/ios").glob("*.framework"):
        shutil.copytree(fw, frameworks / fw.name, symlinks=True, dirs_exist_ok=True)
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
