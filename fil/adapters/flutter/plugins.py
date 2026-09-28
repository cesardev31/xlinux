"""Compila el Runner junto con los plugins nativos usando SwiftPM.

Replica lo que `flutter build ios` genera en macOS cuando SwiftPM está activo
(ver flutter_tools/lib/src/macos/swift_package_manager.dart):

  build/ios-linux-spm-<modo>/Packages/
    .packages/FlutterFramework/           target vacío: Flutter.framework llega por -F
    .packages/<plugin>-<versión>  ->      symlink a <plugin>/ios/<plugin> (su Package.swift)
    FlutterGeneratedPluginSwiftPackage/   depende de todos los plugins
  build/ios-linux-spm-<modo>/Runner/      (nuestro) el Runner como ejecutable SwiftPM

SwiftPM hace de "resolvedor": descarga y compila las dependencias de cada
plugin (Firebase, Stripe, GoogleSignIn...), incluidos xcframeworks binarios.
"""

import json
import os
import shutil
import sys
from pathlib import Path

from ...core import config, toolchain
from ...core.util import log

GENERATED = "FlutterGeneratedPluginSwiftPackage"
FRAMEWORK_PKG = "FlutterFramework"


def swiftpm_plugins(project):
    """[(nombre, ruta al paquete Swift)] de los plugins iOS con Package.swift."""
    deps_file = project.dir / ".flutter-plugins-dependencies"
    if not deps_file.exists():
        return []
    plugins = json.loads(deps_file.read_text()).get("plugins", {}).get("ios", [])
    found = []
    for plugin in plugins:
        for platform_dir in ("ios", "darwin"):
            package = Path(plugin["path"]) / platform_dir / plugin["name"]
            if (package / "Package.swift").exists():
                found.append((plugin["name"], Path(plugin["path"]).name, package))
                break
        else:
            if plugin.get("native_build", True) and _has_native_code(Path(plugin["path"])):
                sys.exit(f"error: el plugin {plugin['name']} solo soporta CocoaPods (sin Package.swift); "
                         "todavía no está soportado.")
    return found


def _has_native_code(plugin_dir):
    for platform_dir in ("ios", "darwin"):
        base = plugin_dir / platform_dir
        if base.is_dir() and any(p.suffix in (".swift", ".m", ".mm") for p in base.rglob("*")):
            return True
    return False


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def _symlink(link, target):
    if link.is_symlink() and os.readlink(link) == str(target):
        return
    if link.is_symlink() or link.exists():
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def generate(project, spm_dir):
    packages = spm_dir / "Packages"
    rel = packages / ".packages"
    plugins = swiftpm_plugins(project)

    # FlutterFramework: en Xcode es un target vacío; los plugins solo lo usan
    # como dependencia y encuentran Flutter.framework por FRAMEWORK_SEARCH_PATHS.
    _write(rel / FRAMEWORK_PKG / "Package.swift", f"""// swift-tools-version: 5.9
// Generado por flutter-ios-linux. No editar.
import PackageDescription

let package = Package(
    name: "{FRAMEWORK_PKG}",
    products: [.library(name: "{FRAMEWORK_PKG}", targets: ["{FRAMEWORK_PKG}"])],
    targets: [.target(name: "{FRAMEWORK_PKG}")]
)
""")
    _write(rel / FRAMEWORK_PKG / f"Sources/{FRAMEWORK_PKG}/{FRAMEWORK_PKG}.swift", "")

    for name, basename, package in plugins:
        _symlink(rel / basename, package)
    package_deps = [f'        .package(name: "{name}", path: "../.packages/{basename}"),'
                    for name, basename, _ in plugins]
    package_deps.append(f'        .package(name: "{FRAMEWORK_PKG}", path: "../.packages/{FRAMEWORK_PKG}"),')
    target_deps = [f'                .product(name: "{name.replace("_", "-")}", package: "{name}"),'
                   for name, _, _ in plugins]
    target_deps.append(f'                .product(name: "{FRAMEWORK_PKG}", package: "{FRAMEWORK_PKG}"),')
    _write(packages / GENERATED / "Package.swift", f"""// swift-tools-version: 5.9
// Generado por flutter-ios-linux. No editar.
import PackageDescription

let package = Package(
    name: "{GENERATED}",
    platforms: [.iOS("{config.MIN_IOS}")],
    products: [.library(name: "{GENERATED}", type: .static, targets: ["{GENERATED}"])],
    dependencies: [
{chr(10).join(package_deps)}
    ],
    targets: [
        .target(
            name: "{GENERATED}",
            dependencies: [
{chr(10).join(target_deps)}
            ]
        )
    ]
)
""")
    _write(packages / GENERATED / f"Sources/{GENERATED}/{GENERATED}.swift", "")
    return plugins


def generate_runner(project, spm_dir):
    """El Runner como paquete: el registrante ObjC en su propio target y el
    código Swift del proyecto (más FlutterLinuxSceneDelegate) como ejecutable."""
    runner_src = project.ios / "Runner"
    pkg = spm_dir / "Runner"
    registrant = pkg / "Sources/RunnerRegistrant"
    swift_dir = pkg / "Sources/Runner"
    for d in (registrant, swift_dir):
        if d.exists():
            shutil.rmtree(d)
    (registrant / "include").mkdir(parents=True)
    swift_dir.mkdir(parents=True)

    for f in runner_src.glob("*.h"):
        if f.name != "Runner-Bridging-Header.h":
            shutil.copy(f, registrant / "include" / f.name)
    for f in runner_src.glob("*.m"):
        shutil.copy(f, registrant / f.name)
    if not any(registrant.glob("*.m")):
        (registrant / "Empty.m").write_text("")

    # En Xcode el registrante llega a Swift por el bridging header; aquí es un
    # módulo aparte, así que se importa explícitamente.
    for f in runner_src.glob("*.swift"):
        text = f.read_text()
        (swift_dir / f.name).write_text("import RunnerRegistrant\n" + text)
    shutil.copy(config.SUPPORT / "flutter/FlutterLinuxSceneDelegate.swift", swift_dir)

    _write(pkg / "Package.swift", f"""// swift-tools-version: 5.9
// Generado por flutter-ios-linux. No editar.
import PackageDescription

let package = Package(
    name: "Runner",
    platforms: [.iOS("{config.MIN_IOS}")],
    dependencies: [
        .package(name: "{GENERATED}", path: "../Packages/{GENERATED}"),
    ],
    targets: [
        .target(
            name: "RunnerRegistrant",
            dependencies: [.product(name: "{GENERATED}", package: "{GENERATED}")]
        ),
        .executableTarget(
            name: "Runner",
            dependencies: ["RunnerRegistrant"],
            linkerSettings: [
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/Frameworks"]),
            ]
        ),
    ]
)
""")
    return pkg


def build(project, flutter_fw_parent):
    """Compila el Runner con todos los plugins. Devuelve el directorio de salida."""
    # Carpetas separadas por modo: debug y release se pueden compilar a la vez
    # (SwiftPM bloquea su carpeta de trabajo mientras compila).
    mode = "debug" if project.debug else "release"
    spm_dir = project.dir / f"build/ios-linux-spm-{mode}"
    plugins = generate(project, spm_dir)
    pkg = generate_runner(project, spm_dir)
    log(f"Runner + plugins con SwiftPM ({', '.join(n for n, _, _ in plugins)})")
    # Los plugins hacen `import Flutter`: en Xcode Flutter.framework llega por
    # FRAMEWORK_SEARCH_PATHS, aquí por -F en todos los targets.
    fw = str(flutter_fw_parent)
    shallow = [dep for _, _, package in plugins for dep in toolchain.exact_dependencies(package)]
    out = toolchain.swiftpm_build(pkg, project.debug, scratch_name=f"{project.package}-{mode}", shallow=shallow, extra_flags=[
        "-Xswiftc", "-F", "-Xswiftc", fw, "-Xcc", f"-F{fw}",
        "-Xlinker", "-F", "-Xlinker", fw, "-Xlinker", "-framework", "-Xlinker", "Flutter"])
    if not (out / "Runner").exists():
        sys.exit(f"error: SwiftPM no produjo {out / 'Runner'}")
    return out
