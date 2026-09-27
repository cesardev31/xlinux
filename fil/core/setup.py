"""`setup` (preparar lo que falte) y `doctor` (diagnóstico) del core.

Cada adaptador aporta sus propios pasos con setup() y doctor_checks()."""

import shutil
import sys

from . import config, device
from .util import log, output, run


def extract_xtool():
    """El AppImage de xtool necesita FUSE, que no está disponible cuando lo
    lanza p. ej. el snap de Flutter: se extrae una vez y se usa <datos>/bin/xtool."""
    data = config.data_dir()
    wrapper = data / "bin/xtool"
    if wrapper.exists():
        return
    appimage = shutil.which("xtool")
    if not appimage:
        sys.exit("error: no encuentro xtool. Descarga el AppImage a ~/.local/bin/xtool.")
    log("Extrayendo el AppImage de xtool")
    (data / "xtool").mkdir(parents=True, exist_ok=True)
    run([appimage, "--appimage-extract"], cwd=data / "xtool", capture_output=True, text=True)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text('#!/bin/sh\n'
                       '# xtool extraído del AppImage: funciona sin FUSE.\n'
                       'exec "$(dirname "$(readlink -f "$0")")/../xtool/squashfs-root/AppRun" "$@"\n')
    wrapper.chmod(0o755)


def build_compat_shim():
    shim = config.compat_shim()
    shim.parent.mkdir(parents=True, exist_ok=True)
    log("Compilando el shim de compatibilidad para Darling")
    run(["clang", "-target", "x86_64-apple-macos11", "-isysroot", config.MACOS_SDK,
         "-B", config.TOOLSET_BIN, "-fuse-ld=lld", "-dynamiclib", "-O2", "-Wall",
         "-o", shim, config.SUPPORT / "core/darling_compat.c"], capture_output=True, text=True)


def setup(adapters, data_dir=None):
    values = config.load_config()
    if data_dir:
        values["data_dir"] = str(data_dir)
        config.save_config(values)
    config.require_data_dir()
    if not config.MACOS_SDK.exists():
        sys.exit("error: falta el SDK de Apple. Corre `xtool setup` con tu Xcode.xip primero.")
    extract_xtool()
    build_compat_shim()
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
    ok = _check((data / "swiftly").is_dir(), f"directorio de datos ({data})", "¿está montado el SSD?")
    ok &= _check(config.swift_bin() is not None, "toolchain de Swift", "instálalo con swiftly en el directorio de datos")
    ok &= _check(bool(which("xtool")), "xtool", "descarga el AppImage a ~/.local/bin/xtool")
    ok &= _check(config.IPHONE_SDK.exists(), "SDK de iOS (xtool)", "corre `xtool setup` con Xcode.xip")
    ok &= _check(bool(which("darling")), "Darling", "instala los .deb de darling-core/system/cli")
    ok &= _check(config.compat_shim().exists(), "shim de Darling", "corre `flutter-ios-linux setup`")
    ok &= _check(config.pymobiledevice3_python().exists(), "pymobiledevice3",
                 "UV_TOOL_DIR=<datos>/uv-tools uv tool install pymobiledevice3")

    for adapter in adapters:
        print(f"\n{adapter.NAME}:")
        for passed, label, hint in adapter.doctor_checks(which):
            ok &= _check(passed, label, hint)

    print("\niPhone:")
    devices = device.connected_devices() if config.pymobiledevice3_python().exists() else []
    if _check(bool(devices), "conectado por USB" + (f": {devices[0][1]} ({devices[0][0]})" if devices else ""),
              "conéctalo, desbloquéalo y toca 'Confiar'"):
        dev_mode = output(["pymobiledevice3", "amfi", "developer-mode-status"], check=False).strip()
        _check(dev_mode == "true", "modo desarrollador", "Ajustes → Privacidad y seguridad → Modo de desarrollador")
        mounted = '"DeveloperDiskImage"' in output(["pymobiledevice3", "mounter", "list"], check=False)
        _check(mounted, "Developer Disk Image montada (debug)", "se monta sola al depurar")
    print()
    if not ok:
        sys.exit(1)
