"""Compilar para iOS arm64 con el SDK que xtool extrae de Xcode.xip."""

import shutil
import sys

from . import config
from .util import run


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


def swiftpm_build(package, debug, extra_flags=(), scratch_name=None):
    """`swift build` para iOS. Devuelve el directorio con los productos.

    La caché de repos y la compilación van al directorio de datos (SSD):
    dependencias como Firebase pesan varios GB."""
    configuration = "debug" if debug else "release"
    scratch = config.data_dir() / "spm-build" / (scratch_name or package.name)
    run(["swift", "build", "--swift-sdk", "arm64-apple-ios", "-c", configuration,
         "--package-path", package, "--scratch-path", scratch,
         "--cache-path", config.data_dir() / "swiftpm-cache", *extra_flags],
        cwd=package)
    return scratch / "arm64-apple-ios" / configuration


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
