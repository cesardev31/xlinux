"""Core `setup` (install whatever is missing) and `doctor` (diagnostics).

Each adapter contributes its own steps through setup() and doctor_checks()."""

import shutil
import sys

from . import config, deps, device
from .util import log, output


def _prepare_data_dir(data_dir):
    values = config.load_config()
    if data_dir:
        values["data_dir"] = str(data_dir)
        config.save_config(values)
    data = config.data_dir()
    # A configured directory whose parent is gone is most likely an unmounted
    # drive: don't create it on the system disk by accident.
    if not data.exists() and not data_dir and data != config.DEFAULT_DATA_DIR and not data.parent.exists():
        sys.exit(f"error: data directory {data} not found (is the drive mounted? "
                 "or choose another one with `xlinux setup --data-dir`)")
    for sub in ("bin", "downloads", "tmp"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(data).free >> 30
    log(f"Data directory: {data} ({free} GB free)")
    if free < 30:
        print("    warning: SDK, toolchains and dependencies take ~20-40 GB; "
              "consider `--data-dir` on a bigger drive", file=sys.stderr)


def setup(adapters, data_dir=None, xip=None, everything=False):
    _prepare_data_dir(data_dir)
    # Downloads first, as the user; then everything that needs sudo at once.
    deps.ensure_uv()
    swift_post_install = deps.ensure_swift()
    deps.ensure_xtool()
    deps.ensure_pymobiledevice3()
    deps.install_system_packages(deps.system_packages(), swift_post_install)
    deps.ensure_apple_sdk(xip)
    deps.install_macro_server()
    if everything:
        deps.ensure_darling()
        deps.ensure_cairosvg()
        deps.ensure_mcp_sdk()
        deps.ensure_macro_server()
    for adapter in adapters:
        adapter.setup()
    doctor(adapters)


def _check(ok, label, hint="", optional=False):
    mark = "✅" if ok else "⚪" if optional else "❌"
    print(f"  {mark} {label}" + (f"\n       → {hint}" if not ok and hint else ""))
    return ok or optional


def doctor(adapters):
    data = config.data_dir()
    env = config.tool_env()
    which = lambda name: shutil.which(name, path=env["PATH"])  # noqa: E731
    later = "installed automatically the first time it's needed"
    print("doctor\n")
    print("Core:")
    ok = _check((data / "swiftly").is_dir(), f"data directory ({data})",
                "run `xlinux setup` (is the drive mounted?)")
    ok &= _check(config.swift_bin() is not None, "Swift toolchain", "run `xlinux setup`")
    ok &= _check(bool(which("xtool")), "xtool", "run `xlinux setup`")
    ok &= _check(config.IPHONE_SDK.exists(), "iOS SDK (from Xcode.xip)", "run `xlinux setup`")
    ok &= _check(config.pymobiledevice3_python().exists(), "pymobiledevice3", "run `xlinux setup`")
    ok &= _check(deps.llvm_tools_found(), "LLVM (lipo, otool, install_name_tool)",
                 "install your distribution's `llvm` package")
    _check(bool(which("darling")) and config.compat_shim().exists(), "Darling (release builds)",
           later, optional=True)
    _check((data / "py-tools/bin/python").exists(), "cairosvg (SVG assets)", later, optional=True)
    _check((data / "bin/OpenAppleMacrosServer").exists(), "OpenAppleMacros with UIKit #Preview",
           later + " (plugins such as Stripe)", optional=True)

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
