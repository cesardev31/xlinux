"""Compilar para iOS arm64 con el SDK que xtool extrae de Xcode.xip."""

import json
import re
import shutil
import sys

from . import config
from .util import log, output, run


def target():
    return f"arm64-apple-ios{config.MIN_IOS}"


def require_sdk():
    for path in (config.IPHONE_SDK, config.TOOLSET_BIN):
        if not path.exists():
            sys.exit(f"error: falta {path}. Corre `xtool setup` (ver `flutter-ios-linux doctor`).")


# El lld del toolchain de Swift es más viejo que el SDK de iOS 27: se usa el de
# xtool (toolset/bin) vía -B.
LINKER_FLAGS = ["-fuse-ld=lld", "-B", str(config.TOOLSET_BIN)]
SWIFT_LINKER_FLAGS = ["-use-ld=lld", "-Xclang-linker", "-B", "-Xclang-linker", str(config.TOOLSET_BIN)]


def clang(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return run(["clang", "-target", target(), "-isysroot", config.IPHONE_SDK, *args], **kw)


def dylib(source, output, install_name, extra=()):
    """Compila un dylib con los rpath habituales de una app iOS."""
    return clang([*LINKER_FLAGS, "-dynamiclib",
                  "-Xlinker", "-rpath", "-Xlinker", "@executable_path/Frameworks",
                  "-Xlinker", "-rpath", "-Xlinker", "@loader_path/Frameworks",
                  "-install_name", install_name, *extra, source, "-o", output])


def swiftc(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return run(["swiftc", "-target", target(), "-sdk", config.IPHONE_SDK,
                "-resource-dir", config.SWIFT_RESOURCES, *SWIFT_LINKER_FLAGS,
                "-Xfrontend", "-enable-cross-import-overlays", *args], **kw)


def exact_dependencies(package):
    """[(url, versión)] de las dependencias remotas con `exact:` de un paquete."""
    dump = json.loads(output(["swift", "package", "dump-package", "--package-path", package]))
    found = []
    for dep in dump.get("dependencies", []):
        for sc in dep.get("sourceControl", []):
            exact = sc.get("requirement", {}).get("exact")
            remote = sc.get("location", {}).get("remote")
            if exact and remote:
                found.append((remote[0]["urlString"], exact[0]))
    return found


def shallow_mirror(url, version):
    """Copia local con solo el tag pedido (sin historial).

    SwiftPM siempre hace `git clone --mirror` completo; para firebase-ios-sdk
    eso son varios GB. Con esta copia configurada como mirror baja ~100x menos."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[-1].removesuffix(".git"))
    repo = config.data_dir() / "git-mirrors" / f"{name}.git"
    tags = output(["git", "ls-remote", "--tags", url, version, f"v{version}"]).split()
    tag = next((t.split("refs/tags/", 1)[1] for t in tags if t.startswith("refs/tags/") and not t.endswith("^{}")), None)
    if not tag:
        return None
    if not repo.exists():
        run(["git", "init", "-q", "--bare", repo])
    have = output(["git", "-C", repo, "tag", "--list", tag]).strip()
    if not have:
        log(f"Descargando {url.rsplit('/', 1)[-1]} {tag} sin historial")
        run(["git", "-C", repo, "fetch", "-q", "--depth", "1", url, f"refs/tags/{tag}:refs/tags/{tag}"])
    return repo


def swiftpm_build(package, debug, extra_flags=(), scratch_name=None, shallow=()):
    """`swift build` para iOS. Devuelve el directorio con los productos.

    La caché de repos y la compilación van al directorio de datos (SSD):
    dependencias como Firebase pesan varios GB. `shallow`: [(url, versión)]
    que se bajan sin historial y se usan como mirror (ver shallow_mirror)."""
    configuration = "debug" if debug else "release"
    name = scratch_name or package.name
    scratch = config.data_dir() / "spm-build" / name
    config_path = config.data_dir() / "spm-config" / name
    config_path.mkdir(parents=True, exist_ok=True)
    for url, version in shallow:
        mirror = shallow_mirror(url, version)
        if mirror:
            run(["swift", "package", "--package-path", package, "--config-path", config_path,
                 "config", "set-mirror", "--original", url, "--mirror", str(mirror)],
                capture_output=True, text=True)
    # Swift 6.4 usa Swift Build por defecto; se configura como lo hace xtool
    # (PackLib/BuildSettings.swift): triple + toolset-swb.json + plataformas del SDK.
    # actool (support/core/bin/actool): Swift Build lo busca en el PATH para
    # compilar catálogos, pero verifica su versión relativo al paquete (y no
    # acepta symlinks), así que se deja ahí un lanzador.
    launcher = package / "actool"
    launcher.write_text(f'#!/bin/sh\nexec "{config.SUPPORT / "core/bin/actool"}" "$@"\n')
    launcher.chmod(0o755)
    env = config.tool_env()
    env["XCODE_EXTRA_PLATFORM_FOLDERS"] = str(config.XTOOL_SDK / "Developer/Platforms")
    env["PATH"] = f"{config.TOOLSET_BIN}:{env['PATH']}"
    env.pop("SDKROOT", None)
    run(["swift", "build", "--build-system", "swiftbuild", "--triple", "arm64-apple-ios",
         "--toolset", config.XTOOL_SDK / "toolset-swb.json", "-c", configuration,
         "--package-path", package, "--scratch-path", scratch, "--config-path", config_path,
         "--cache-path", config.data_dir() / "swiftpm-cache",
         # Módulos como _PassKit_SwiftUI (PayWithApplePayButton) solo existen
         # como cross-import overlays; toolset.json de xtool lo activa, el de
         # Swift Build no.
         "-Xswiftc", "-Xfrontend", "-Xswiftc", "-enable-cross-import-overlays",
         *extra_flags],
        cwd=package, env=env)
    products = scratch / "out/Products" / f"{configuration.capitalize()}-iphoneos"
    return products if products.is_dir() else scratch / "arm64-apple-ios" / configuration


FAT_MAGICS = (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")


def thin_frameworks(app, arch="arm64"):
    """Deja solo `arch` en los binarios universales de Frameworks/ (lo que
    hace Xcode con "embed and thin"). `flutter assemble` en release genera
    App.framework universal, y firmar binarios "gordos" traba a xtool."""
    for binary in (app / "Frameworks").glob("*.framework/*"):
        if binary.is_file() and binary.name == binary.parent.stem:
            with open(binary, "rb") as f:
                if f.read(4) not in FAT_MAGICS:
                    continue
            run(["lipo", "-thin", arch, binary, "-output", binary], capture_output=True, text=True)


def _is_static_archive(binary):
    try:
        with open(binary, "rb") as f:
            return f.read(8) in (b"!<arch>\n", b"!<thin>\n")
    except OSError:
        return True


def pack_swiftpm_outputs(out, app, executable, skip_frameworks=()):
    """Copia al .app el ejecutable, los bundles de recursos y los frameworks
    dinámicos que dejó SwiftPM (igual que el Packer de xtool)."""
    shutil.copy(out / executable, app / executable)
    for bundle in out.glob("*.bundle"):
        shutil.copytree(bundle, app / bundle.name, symlinks=True, dirs_exist_ok=True)
    frameworks = app / "Frameworks"
    frameworks.mkdir(exist_ok=True)
    for fw in out.glob("*.framework"):
        if fw.stem not in skip_frameworks and not _is_static_archive(fw / fw.stem):
            shutil.copytree(fw, frameworks / fw.name, symlinks=True, dirs_exist_ok=True)
    for lib in out.glob("lib*.dylib"):
        shutil.copy(lib, frameworks / lib.name)
