"""Correr herramientas de línea de comandos de macOS en Linux con Darling.

Se inyecta support/core/darling_compat.c (finge macOS 12 en `uname` y emula
`vm_map` con alineación, que Darling no soporta). Las rutas absolutas de
Linux se ven dentro de Darling bajo /Volumes/SystemRoot.
"""

import sys

from . import config
from .util import run

ROOT = "/Volumes/SystemRoot"


def translate(arg):
    arg = str(arg)
    if arg.startswith("/"):
        return ROOT + arg
    if arg.startswith("--") and "=/" in arg:
        key, value = arg.split("=", 1)
        return f"{key}={ROOT}{value}"
    return arg


def run_macos_tool(binary, args, **kw):
    shim = config.compat_shim()
    if not shim.exists():
        sys.exit("error: falta el shim de Darling. Corre `flutter-ios-linux setup`.")
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return run(["darling", "shell", "env", f"DYLD_INSERT_LIBRARIES={ROOT}{shim}",
                translate(binary), *[translate(a) for a in args]], **kw)
