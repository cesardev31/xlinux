"""Run macOS command-line tools on Linux with Darling.

support/core/darling_compat.c is injected (it reports macOS 12 from `uname`
and emulates aligned `vm_map`, which Darling doesn't support). Absolute Linux
paths are visible inside Darling under /Volumes/SystemRoot.
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
        sys.exit("error: the Darling compatibility shim is missing. Run `xlinux setup`.")
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return run(["darling", "shell", "env", f"DYLD_INSERT_LIBRARIES={ROOT}{shim}",
                translate(binary), *[translate(a) for a in args]], **kw)
