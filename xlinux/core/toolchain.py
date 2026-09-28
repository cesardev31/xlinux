"""Compile for iOS arm64 with the SDK xtool extracts from Xcode.xip."""

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
            sys.exit(f"error: {path} is missing. Run `xtool setup` (see `xlinux doctor`).")


# The Swift toolchain's lld is older than the iOS 27 SDK: use xtool's
# (toolset/bin) through -B.
LINKER_FLAGS = ["-fuse-ld=lld", "-B", str(config.TOOLSET_BIN)]
SWIFT_LINKER_FLAGS = ["-use-ld=lld", "-Xclang-linker", "-B", "-Xclang-linker", str(config.TOOLSET_BIN)]


def builtins():
    """Xcode's compiler-rt for iOS (`__isPlatformVersionAtLeast`, used by
    Swift's `#available`). Apple's clang links it implicitly; ours doesn't
    know where it is, so it is passed explicitly when linking."""
    found = sorted(config.SWIFT_RESOURCES.parent.glob("clang/*/lib/darwin/libclang_rt.ios.a"))
    return [str(found[-1])] if found else []


def clang(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return run(["clang", "-target", target(), "-isysroot", config.IPHONE_SDK, *args], **kw)


def dylib(source, output, install_name, extra=()):
    """Build a dylib with the usual iOS app rpaths."""
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
    """[(url, version)] of a package's remote dependencies pinned with `exact:`."""
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
    """Local copy holding only the requested tag (no history).

    SwiftPM always does a full `git clone --mirror`; for firebase-ios-sdk
    that's several GB. Configured as a mirror, this copy downloads ~100x less."""
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
        log(f"Downloading {url.rsplit('/', 1)[-1]} {tag} without history")
        run(["git", "-C", repo, "fetch", "-q", "--depth", "1", url, f"refs/tags/{tag}:refs/tags/{tag}"])
    return repo


def swiftpm_build(package, debug, extra_flags=(), scratch_name=None, shallow=()):
    """`swift build` for iOS. Returns the products directory.

    The repository cache and build directory live in the data directory:
    dependencies such as Firebase weigh several GB. `shallow`: [(url, version)]
    downloaded without history and used as mirrors (see shallow_mirror)."""
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
    # Swift 6.4 uses Swift Build by default; configure it the way xtool does
    # (PackLib/BuildSettings.swift): triple + toolset-swb.json + SDK platforms.
    # actool (support/core/bin/actool): Swift Build looks it up in PATH to
    # compile asset catalogs, but checks its version relative to the package
    # (and rejects symlinks), so a launcher script is placed there.
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
         # Modules like _PassKit_SwiftUI (PayWithApplePayButton) only exist as
         # cross-import overlays; xtool's toolset.json enables them, Swift
         # Build's doesn't.
         "-Xswiftc", "-Xfrontend", "-Xswiftc", "-enable-cross-import-overlays",
         *extra_flags],
        cwd=package, env=env)
    products = scratch / "out/Products" / f"{configuration.capitalize()}-iphoneos"
    return products if products.is_dir() else scratch / "arm64-apple-ios" / configuration


FAT_MAGICS = (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")


def thin_frameworks(app, arch="arm64"):
    """Keep only `arch` in universal binaries under Frameworks/ (what Xcode's
    "embed and thin" does). A release `flutter assemble` produces a universal
    App.framework, and signing fat binaries hangs xtool."""
    for binary in (app / "Frameworks").glob("*.framework/*"):
        if binary.is_file() and binary.name == binary.parent.stem:
            with open(binary, "rb") as f:
                if f.read(4) not in FAT_MAGICS:
                    continue
            run(["lipo", "-thin", arch, binary, "-output", binary], capture_output=True, text=True)


MH_DYLIB = 6


def _is_dynamic_library(binary):
    """True only for Mach-O MH_DYLIB (or universal binaries whose first slice
    is one). Static frameworks (.a archives or MH_OBJECT) are already linked
    into the executable and must not be embedded in Frameworks/."""
    try:
        with open(binary, "rb") as f:
            head = f.read(8)
            if head[:4] in FAT_MAGICS:  # universal (big-endian): first slice
                f.seek(8 + 8)
                offset = int.from_bytes(f.read(4), "big")
                f.seek(offset)
                head = f.read(8)
            elif head[:8] in (b"!<arch>\n", b"!<thin>\n"):
                return False
            if head[:4] not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
                return False
            f.seek(f.tell() - 8 + 12)
            return int.from_bytes(f.read(4), "little") == MH_DYLIB
    except OSError:
        return False


def pack_swiftpm_outputs(out, app, executable, skip_frameworks=()):
    """Copy the executable, resource bundles and dynamic frameworks SwiftPM
    produced into the .app (like xtool's Packer)."""
    shutil.copy(out / executable, app / executable)
    for bundle in out.glob("*.bundle"):
        shutil.copytree(bundle, app / bundle.name, symlinks=True, dirs_exist_ok=True)
    frameworks = app / "Frameworks"
    frameworks.mkdir(exist_ok=True)
    for fw in out.glob("*.framework"):
        if fw.stem not in skip_frameworks and _is_dynamic_library(fw / fw.stem):
            shutil.copytree(fw, frameworks / fw.name, symlinks=True, dirs_exist_ok=True)
    for lib in out.glob("lib*.dylib"):
        shutil.copy(lib, frameworks / lib.name)
