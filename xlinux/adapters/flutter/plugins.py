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

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from ...core import cocoapods, config, toolchain
from ...core.util import log

GENERATED = "FlutterGeneratedPluginSwiftPackage"
FRAMEWORK_PKG = "FlutterFramework"


def swiftpm_plugins(project):
    """[(nombre, basename, paquete)] de los plugins iOS nativos. `paquete` es la
    carpeta con Package.swift o, si el plugin solo trae podspec, el .podspec
    (se convierte con core/cocoapods)."""
    deps_file = project.dir / ".flutter-plugins-dependencies"
    if not deps_file.exists():
        return []
    plugins = json.loads(deps_file.read_text()).get("plugins", {}).get("ios", [])
    found = []
    for plugin in plugins:
        root = Path(plugin["path"])
        for platform_dir in ("ios", "darwin"):
            package = root / platform_dir / plugin["name"]
            if (package / "Package.swift").exists():
                found.append((plugin["name"], root.name, package))
                break
        else:
            podspecs = [p for d in ("ios", "darwin") for p in (root / d).glob("*.podspec")]
            if podspecs and _has_native_code(root):
                found.append((plugin["name"], root.name, podspecs[0]))
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
// Generado por xlinux. No editar.
import PackageDescription

let package = Package(
    name: "{FRAMEWORK_PKG}",
    products: [.library(name: "{FRAMEWORK_PKG}", targets: ["{FRAMEWORK_PKG}"])],
    targets: [.target(name: "{FRAMEWORK_PKG}")]
)
""")
    _write(rel / FRAMEWORK_PKG / f"Sources/{FRAMEWORK_PKG}/{FRAMEWORK_PKG}.swift", "")

    pod_resources = []
    for name, basename, package in plugins:
        if package.suffix == ".podspec":
            pod_resources += _generate_from_podspec(name, package, rel / basename, packages / ".pods")
        else:
            _symlink(rel / basename, package)
    (spm_dir / "pod_resources.json").write_text(json.dumps(pod_resources))
    package_deps = [f'        .package(name: "{name}", path: "../.packages/{basename}"),'
                    for name, basename, _ in plugins]
    package_deps.append(f'        .package(name: "{FRAMEWORK_PKG}", path: "../.packages/{FRAMEWORK_PKG}"),')
    target_deps = [f'                .product(name: "{name.replace("_", "-")}", package: "{name}"),'
                   for name, _, _ in plugins]
    target_deps.append(f'                .product(name: "{FRAMEWORK_PKG}", package: "{FRAMEWORK_PKG}"),')
    _write(packages / GENERATED / "Package.swift", f"""// swift-tools-version: 5.9
// Generado por xlinux. No editar.
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


def _generate_from_podspec(name, podspec, dest, pods_dir):
    """Plugin solo con CocoaPods: su podspec y el de sus dependencias pasan a
    paquetes SwiftPM (core/cocoapods). Devuelve los recursos a empaquetar."""
    log(f"Convirtiendo {name} (CocoaPods) a SwiftPM")
    spec = cocoapods.parse_ruby_podspec(podspec)
    root_pod, pods = cocoapods.load_graph(spec, podspec.parent)
    for pod in pods:
        if pod is not root_pod:
            cocoapods.write_package(pod, pods_dir / pod.package_dir_name)
    if dest.is_symlink():
        dest.unlink()
    # El plugin, como los de SwiftPM: producto con guiones, dependiente de
    # FlutterFramework (Flutter.framework llega por -F).
    cocoapods.write_package(root_pod, dest, deps_base="../../.pods/",
                            extra_deps=[(FRAMEWORK_PKG, f"../{FRAMEWORK_PKG}")],
                            product=name.replace("_", "-"))
    return cocoapods.resource_manifest(pods)


def pack_pod_resources(project, app):
    """Bundles de recursos de los pods convertidos, como los deja CocoaPods."""
    mode = "debug" if project.debug else "release"
    manifest = project.dir / f"build/ios-linux-spm-{mode}/pod_resources.json"
    if manifest.exists():
        cocoapods.pack_resources(json.loads(manifest.read_text()), app)


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
// Generado por xlinux. No editar.
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
                // Fuerza la carga de objetos Objective-C que solo contienen
                // categorías. Dependencias como AppAuth implementan APIs de
                // iOS mediante categorías y el linker las descartaría si no
                // hay un símbolo tradicional que las referencie.
                .unsafeFlags(["-Xlinker", "-ObjC"]),
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
    names = ", ".join(n for n, _, _ in plugins)
    fingerprint = native_fingerprint(project, spm_dir, plugins, flutter_fw_parent)
    stamp = spm_dir / "native.json"
    try:
        previous = json.loads(stamp.read_text())
    except (OSError, ValueError):
        previous = {}
    if previous.get("fingerprint") == fingerprint and (Path(previous.get("out", "")) / "Runner").exists():
        log(f"Runner + plugins sin cambios nativos: se reutiliza la compilación ({names})")
        return Path(previous["out"])

    log(f"Runner + plugins con SwiftPM ({names})")
    # Los plugins hacen `import Flutter`: en Xcode Flutter.framework llega por
    # FRAMEWORK_SEARCH_PATHS, aquí por -F en todos los targets.
    fw = str(flutter_fw_parent)
    # Los plugins convertidos desde CocoaPods ya tienen sus pods bajados por nosotros.
    shallow = [dep for _, _, package in plugins if package.suffix != ".podspec"
               for dep in toolchain.exact_dependencies(package)]
    out = toolchain.swiftpm_build(pkg, project.debug, scratch_name=f"{project.package}-{mode}", shallow=shallow, extra_flags=[
        "-Xswiftc", "-F", "-Xswiftc", fw, "-Xcc", f"-F{fw}",
        "-Xlinker", "-F", "-Xlinker", fw, "-Xlinker", "-framework", "-Xlinker", "Flutter"])
    if not (out / "Runner").exists():
        sys.exit(f"error: SwiftPM no produjo {out / 'Runner'}")
    stamp.write_text(json.dumps({"fingerprint": fingerprint, "out": str(out)}))
    return out


def native_fingerprint(project, spm_dir, plugins, flutter_fw_parent):
    """Huella de todo lo que afecta la compilación nativa: el código generado
    (Runner, registrante, Package.swift), los plugins y sus versiones, el
    engine, el modo y las propias herramientas de xlinux. Si no cambia, el
    `swift build` (que en una app con Firebase/Stripe tarda ~30 s aun sin
    cambios) se puede saltar."""
    h = hashlib.sha256()
    h.update(f"{project.debug}|{flutter_fw_parent}".encode())
    for name, basename, package in plugins:
        h.update(f"{name}|{basename}|{package}".encode())
        if package.suffix == ".podspec":
            package = package.parent
        # Un plugin en desarrollo (path:) puede cambiar sin cambiar de versión.
        for f in sorted(package.rglob("*")):
            if f.is_file() and f.suffix in (".swift", ".m", ".mm", ".h", ".c", ".cpp") or f.name == "Package.swift":
                h.update(str(f).encode())
                h.update(str(f.stat().st_mtime_ns).encode())
    generated = [spm_dir / "Runner/Package.swift", *sorted((spm_dir / "Runner/Sources").rglob("*")),
                 *sorted((spm_dir / "Packages").glob("*/Package.swift")),
                 *sorted((spm_dir / "Packages").glob(".*/*/Package.swift"))]
    tools = [Path(__file__), Path(toolchain.__file__), Path(cocoapods.__file__), config.SUPPORT / "core/bin/actool"]
    for f in generated + tools:
        if f.is_file():
            h.update(str(f.relative_to(spm_dir) if f.is_relative_to(spm_dir) else f).encode())
            h.update(f.read_bytes())
    return h.hexdigest()
