"""Core `setup` (prepare whatever is missing) and `doctor` (diagnostics).

Each adapter contributes its own steps through setup() and doctor_checks()."""

import shutil
import sys

from . import config, device
from .util import log, output, run


def extract_xtool():
    """xtool's AppImage needs FUSE, which isn't available when it is launched
    from e.g. the Flutter snap: extract it once and use <data>/bin/xtool."""
    data = config.data_dir()
    wrapper = data / "bin/xtool"
    if wrapper.exists():
        return
    appimage = shutil.which("xtool")
    if not appimage:
        sys.exit("error: xtool not found. Download its AppImage to ~/.local/bin/xtool.")
    log("Extracting the xtool AppImage")
    (data / "xtool").mkdir(parents=True, exist_ok=True)
    run([appimage, "--appimage-extract"], cwd=data / "xtool", capture_output=True, text=True)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text('#!/bin/sh\n'
                       '# xtool extracted from its AppImage: works without FUSE.\n'
                       'exec "$(dirname "$(readlink -f "$0")")/../xtool/squashfs-root/AppRun" "$@"\n')
    wrapper.chmod(0o755)


def build_compat_shim():
    shim = config.compat_shim()
    shim.parent.mkdir(parents=True, exist_ok=True)
    log("Building the Darling compatibility shim")
    run(["clang", "-target", "x86_64-apple-macos11", "-isysroot", config.MACOS_SDK,
         "-B", config.TOOLSET_BIN, "-fuse-ld=lld", "-dynamiclib", "-O2", "-Wall",
         "-o", shim, config.SUPPORT / "core/darling_compat.c"], capture_output=True, text=True)


def install_macro_server():
    """xtool's OpenAppleMacros (the SDK's swift-plugin-server) lacks the UIKit
    #Preview variants (KitViewMacro...), used e.g. by Stripe. If a patched
    build exists in <data>/bin it is installed over xtool's (see
    support/patches/README.md)."""
    patched = config.data_dir() / "bin/OpenAppleMacrosServer"
    target = config.XTOOL_SDK / "OpenAppleMacrosServer"
    if not patched.exists() or not target.exists():
        return
    if target.read_bytes() == patched.read_bytes():
        return
    backup = target.with_name("OpenAppleMacrosServer.orig")
    if not backup.exists():
        shutil.copy2(target, backup)
    log("Installing OpenAppleMacrosServer with UIKit #Preview stubs")
    shutil.copy2(patched, target)


def setup(adapters, data_dir=None):
    values = config.load_config()
    if data_dir:
        values["data_dir"] = str(data_dir)
        config.save_config(values)
    config.require_data_dir()
    if not config.MACOS_SDK.exists():
        sys.exit("error: Apple SDK missing. Run `xtool setup` with your Xcode.xip first.")
    extract_xtool()
    build_compat_shim()
    install_macro_server()
    for adapter in adapters:
        adapter.setup()
    doctor(adapters)


def _check(ok, label, hint=""):
    print(f"  {'✅' if ok else '❌'} {label}" + (f"\n       → {hint}" if not ok and hint else ""))
    return ok


def doctor(adapters):
    data = config.data_dir()
    env = config.tool_env()
    which = lambda name: shutil.which(name, path=env["PATH"])  # noqa: E731
    print("doctor\n")
    print("Core:")
    ok = _check((data / "swiftly").is_dir(), f"data directory ({data})", "is the drive mounted?")
    ok &= _check(config.swift_bin() is not None, "Swift toolchain", "install it with swiftly into the data directory")
    ok &= _check(bool(which("xtool")), "xtool", "download its AppImage to ~/.local/bin/xtool")
    ok &= _check(config.IPHONE_SDK.exists(), "iOS SDK (xtool)", "run `xtool setup` with Xcode.xip")
    ok &= _check(bool(which("darling")), "Darling", "install the darling-core/system/cli .deb packages")
    ok &= _check(config.compat_shim().exists(), "Darling shim", "run `xlinux setup`")
    ok &= _check(config.pymobiledevice3_python().exists(), "pymobiledevice3",
                 "UV_TOOL_DIR=<data>/uv-tools uv tool install pymobiledevice3")

    for adapter in adapters:
        print(f"\n{adapter.NAME}:")
        for passed, label, hint in adapter.doctor_checks(which):
            ok &= _check(passed, label, hint)

    print("\niPhone:")
    devices = device.connected_devices() if config.pymobiledevice3_python().exists() else []
    if _check(bool(devices), "connected over USB" + (f": {devices[0][1]} ({devices[0][0]})" if devices else ""),
              "plug it in, unlock it and tap 'Trust'"):
        dev_mode = output(["pymobiledevice3", "amfi", "developer-mode-status"], check=False).strip()
        _check(dev_mode == "true", "Developer Mode", "Settings → Privacy & Security → Developer Mode")
        mounted = '"DeveloperDiskImage"' in output(["pymobiledevice3", "mounter", "list"], check=False)
        _check(mounted, "Developer Disk Image mounted (debug)", "mounted automatically when debugging")
    print()
    if not ok:
        sys.exit(1)
