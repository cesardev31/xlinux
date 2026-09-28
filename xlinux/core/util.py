import os
import subprocess
import sys

from . import config


def log(msg):
    print(f"\033[1;36m==>\033[0m {msg}", file=sys.stderr, flush=True)


def run(cmd, check=True, **kw):
    """Corre un comando con el entorno de la herramienta; aborta si falla."""
    cmd = [str(c) for c in cmd]
    if os.environ.get("XLINUX_VERBOSE"):
        print("   $ " + " ".join(cmd), file=sys.stderr, flush=True)
    kw.setdefault("env", config.tool_env())
    result = subprocess.run(cmd, **kw)
    if check and result.returncode != 0:
        detail = ""
        if isinstance(result.stderr, (str, bytes)) and result.stderr:
            detail = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
            detail = "\n" + detail.strip()[-3000:]
        sys.exit(f"error: falló (código {result.returncode}): {' '.join(cmd[:3])}{detail}")
    return result


def output(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return run(cmd, **kw).stdout
