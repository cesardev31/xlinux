"""Compila una app Flutter para iPhone, lo mismo que `flutter build ios` + Xcode.

release:  Dart -> kernel -> gen_snapshot (binario macOS en Darling) -> App.framework
debug:    App.framework es un stub; el código va como kernel_blob.bin (JIT)
ambos:    flutter_assets, Runner (swiftc/clang + SDK de xtool), Runner.app, .ipa
"""

import json
import plistlib
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from . import config
from .util import log, output, run

ENGINE_BASE_URL = "https://storage.googleapis.com/flutter_infra_release/flutter"
DARLING_ROOT = "/Volumes/SystemRoot"


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
        pbx = (self.ios / "Runner.xcodeproj/project.pbxproj").read_text()
        ids = [i.strip('"') for i in re.findall(r"PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);", pbx)]
        ids = [i for i in ids if "RunnerTests" not in i]
        if not ids:
            sys.exit("error: no encontré PRODUCT_BUNDLE_IDENTIFIER en Runner.xcodeproj")
        return ids[0]


def flutter_info():
    out = output(["flutter", "--version", "--machine"])
    info = json.loads(out[out.index("{"):])
    return Path(info["flutterRoot"]), info["engineRevision"]


def ensure_engine(revision):
    """Artefactos iOS del engine (lo que haría `flutter precache --ios`)."""
    root = config.engine_dir(revision)
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


def gen_snapshot(engine, args):
    """El gen_snapshot de iOS solo existe para macOS: se corre en Darling con
    el shim de compatibilidad (ver support/darling_compat.c)."""
    shim = config.compat_shim()
    if not shim.exists():
        sys.exit("error: falta el shim de Darling. Corre `flutter-ios-linux setup`.")
    translated = []
    for a in args:
        a = str(a)
        if a.startswith("/"):
            a = DARLING_ROOT + a
        elif a.startswith("--") and "=/" in a:
            key, value = a.split("=", 1)
            a = f"{key}={DARLING_ROOT}{value}"
        translated.append(a)
    run(["darling", "shell", "env", f"DYLD_INSERT_LIBRARIES={DARLING_ROOT}{shim}",
         f"{DARLING_ROOT}{engine / 'ios-release/gen_snapshot_arm64'}", *translated],
        capture_output=True, text=True)


def compile_aot(engine, dill, framework_dir, build_dir):
    log("kernel -> App.framework arm64 (gen_snapshot en Darling)")
    framework_dir.mkdir(parents=True, exist_ok=True)
    gen_snapshot(engine, [
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
    run(["clang", "-target", f"arm64-apple-ios{config.MIN_IOS}", "-isysroot", config.IPHONE_SDK,
         "-fuse-ld=lld", "-B", config.TOOLSET_BIN, "-dynamiclib", "-fapplication-extension",
         "-Xlinker", "-rpath", "-Xlinker", "@executable_path/Frameworks",
         "-Xlinker", "-rpath", "-Xlinker", "@loader_path/Frameworks",
         "-install_name", "@rpath/App.framework/App", stub, "-o", framework_dir / "App"],
        capture_output=True, text=True)


def build_assets(project, out):
    log("flutter_assets (flutter build bundle)")
    run(["flutter", "build", "bundle", "--debug" if project.debug else "--release",
         "--target-platform", "ios", "--asset-dir", out],
        cwd=project.dir, stdout=subprocess.DEVNULL)


def compile_runner(project, flutter_fw_parent, obj, executable):
    log("Runner nativo (swiftc + SDK de iOS)")
    obj.mkdir(parents=True, exist_ok=True)
    runner = project.ios / "Runner"
    target = f"arm64-apple-ios{config.MIN_IOS}"
    objects = []
    for m in sorted(runner.glob("*.m")):
        o = obj / (m.stem + ".o")
        run(["clang", "-target", target, "-isysroot", config.IPHONE_SDK, "-fobjc-arc", "-fmodules",
             "-O2", "-F", flutter_fw_parent, "-I", runner, "-c", m, "-o", o],
            capture_output=True, text=True)
        objects.append(o)
    swift_sources = sorted(runner.glob("*.swift")) + [config.SUPPORT / "FlutterLinuxSceneDelegate.swift"]
    cmd = [
        "swiftc", "-target", target, "-sdk", config.IPHONE_SDK, "-resource-dir", config.SWIFT_RESOURCES,
        "-use-ld=lld", "-Xclang-linker", "-B", "-Xclang-linker", config.TOOLSET_BIN,
        "-Xfrontend", "-enable-cross-import-overlays",
        "-Onone" if project.debug else "-O", "-module-name", "Runner",
        "-F", flutter_fw_parent, "-I", runner,
    ]
    bridging = runner / "Runner-Bridging-Header.h"
    if bridging.exists():
        cmd += ["-import-objc-header", bridging]
    cmd += swift_sources + objects
    cmd += ["-framework", "Flutter", "-Xlinker", "-rpath", "-Xlinker", "@executable_path/Frameworks",
            "-o", executable]
    run(cmd, capture_output=True, text=True)


def _expand(value, variables):
    if isinstance(value, str):
        return re.sub(r"\$\(([A-Z_]+)\)", lambda m: variables.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, variables) for k, v in value.items()}
    return value


def app_info_plist(project, variables):
    with open(project.ios / "Runner/Info.plist", "rb") as f:
        info = _expand(plistlib.load(f), variables)
    # Sin ibtool no hay storyboards compilados: la ventana la crea
    # FlutterLinuxSceneDelegate y la pantalla de inicio usa UILaunchScreen.
    info.pop("UIMainStoryboardFile", None)
    info.pop("UILaunchStoryboardName", None)
    info["UILaunchScreen"] = {}
    for configs in info.get("UIApplicationSceneManifest", {}).get("UISceneConfigurations", {}).values():
        for scene in configs:
            scene.pop("UISceneStoryboardFile", None)
            if scene.get("UISceneDelegateClassName", "").endswith(".SceneDelegate"):
                scene["UISceneDelegateClassName"] = "Runner.FlutterLinuxSceneDelegate"
    info.update({
        "MinimumOSVersion": config.MIN_IOS,
        "CFBundleSupportedPlatforms": ["iPhoneOS"],
        "UIDeviceFamily": [1, 2],
        "DTPlatformName": "iphoneos",
        "DTSDKName": "iphoneos",
        "UIRequiredDeviceCapabilities": ["arm64"],
    })
    return info


def add_icons(project, info):
    """Íconos sin actool: PNG sueltos + CFBundleIcons (formato previo a Assets.car)."""
    iconset = project.ios / "Runner/Assets.xcassets/AppIcon.appiconset"
    contents = iconset / "Contents.json"
    if not contents.exists():
        return
    names = set()
    for image in json.loads(contents.read_text()).get("images", []):
        filename, size = image.get("filename"), image.get("size", "")
        if not filename or image.get("idiom") not in ("iphone", "ipad") or size == "1024x1024":
            continue
        base = f"AppIcon{size}"
        suffix = "" if image.get("scale", "1x") == "1x" else "@" + image["scale"]
        idiom = "~ipad" if image["idiom"] == "ipad" else ""
        shutil.copy(iconset / filename, project.app / f"{base}{suffix}{idiom}.png")
        names.add(base)
    if names:
        info["CFBundleIcons"] = {"CFBundlePrimaryIcon": {"CFBundleIconFiles": sorted(names)}}
        info["CFBundleIcons~ipad"] = info["CFBundleIcons"]


def _read_xcconfig(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"\s*([A-Z_]+)\s*=\s*(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2).strip()
    return values


def package_ipa(project):
    log(f"Empaquetando {project.ipa.name}")
    with zipfile.ZipFile(project.ipa, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(project.app.rglob("*")):
            if path.is_dir():
                continue
            entry = zipfile.ZipInfo(str(Path("Payload") / path.relative_to(project.build_dir)))
            entry.external_attr = (path.stat().st_mode & 0xFFFF) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(entry, path.read_bytes())


def build(project_dir, debug=False):
    """Compila la app y devuelve el Project con Runner.app y el .ipa listos."""
    config.require_data_dir()
    for path in (config.IPHONE_SDK, config.TOOLSET_BIN):
        if not path.exists():
            sys.exit(f"error: falta {path}. Corre `xtool setup` (ver `flutter-ios-linux doctor`).")
    project = Project(project_dir, debug)
    flutter_root, revision = flutter_info()
    engine = ensure_engine(revision)
    flutter_fw_parent = engine / ("ios" if debug else "ios-release") / "Flutter.xcframework/ios-arm64"

    if project.app.exists():
        shutil.rmtree(project.app)
    frameworks = project.app / "Frameworks"
    app_framework = frameworks / "App.framework"
    frameworks.mkdir(parents=True)

    run(["flutter", "pub", "get"], cwd=project.dir, stdout=subprocess.DEVNULL)
    if debug:
        stub_app_framework(app_framework, project.build_dir)
    else:
        dill = project.build_dir / "app.dill"
        compile_kernel(flutter_root, project, dill)
        compile_aot(engine, dill, app_framework, project.build_dir)
    build_assets(project, app_framework / "flutter_assets")
    compile_runner(project, flutter_fw_parent, project.build_dir / "obj", project.app / "Runner")

    log("Armando Runner.app")
    with open(project.ios / "Flutter/AppFrameworkInfo.plist", "rb") as f:
        fw_info = plistlib.load(f)
    fw_info["MinimumOSVersion"] = config.MIN_IOS
    with open(app_framework / "Info.plist", "wb") as f:
        plistlib.dump(fw_info, f)
    shutil.copytree(flutter_fw_parent / "Flutter.framework", frameworks / "Flutter.framework",
                    ignore=shutil.ignore_patterns("_CodeSignature", "Headers", "Modules", "module.modulemap"))

    xc = _read_xcconfig(project.ios / "Flutter/Generated.xcconfig")
    variables = {
        "EXECUTABLE_NAME": "Runner", "PRODUCT_NAME": "Runner", "PRODUCT_MODULE_NAME": "Runner",
        "DEVELOPMENT_LANGUAGE": "en",
        "PRODUCT_BUNDLE_IDENTIFIER": project.bundle_identifier(),
        "FLUTTER_BUILD_NAME": xc.get("FLUTTER_BUILD_NAME", "1.0.0"),
        "FLUTTER_BUILD_NUMBER": xc.get("FLUTTER_BUILD_NUMBER", "1"),
    }
    info = app_info_plist(project, variables)
    add_icons(project, info)
    with open(project.app / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    (project.app / "PkgInfo").write_text("APPL????")
    package_ipa(project)
    log(f"Listo: {project.ipa}")
    return project
