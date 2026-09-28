import fcntl
import os
import subprocess
import sys

from . import config


def log(msg):
    print(f"\033[1;36m==>\033[0m {msg}", file=sys.stderr, flush=True)


def run(cmd, check=True, **kw):
    """Run a command with the tool environment; abort if it fails."""
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
        sys.exit(f"error: command failed (exit {result.returncode}): {' '.join(cmd[:3])}{detail}")
    return result


def output(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return run(cmd, **kw).stdout


class DirLock:
    """Exclusive lock on a build directory, held until release() or process
    exit. Two builds of the same app at once (e.g. `flutter run` and `xlinux
    build`) would delete and fill the same .app under each other: the second
    one waits instead."""

    def __init__(self, directory, what):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = open(directory / ".xlinux.lock", "a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.seek(0)
            holder = self.file.read().strip() or "?"
            log(f"Waiting for another build of {what} to finish (pid {holder})")
            fcntl.flock(self.file, fcntl.LOCK_EX)
        self.file.seek(0)
        self.file.truncate()
        self.file.write(f"{os.getpid()}\n")
        self.file.flush()

    def release(self):
        if not self.file.closed:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
