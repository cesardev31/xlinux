import fcntl
import os
import subprocess
import sys
import threading

from . import config


_terminal = None


def show_progress_in_terminal():
    """When a framework's tool runs us with our output captured (Flutter hides
    a custom device's install output unless it fails), also write progress to
    the controlling terminal, so `flutter run` doesn't look frozen for minutes.
    Only for those commands: never where a terminal UI owns /dev/tty."""
    global _terminal
    if sys.stderr.isatty():
        return
    try:
        _terminal = open("/dev/tty", "w")
    except OSError:
        _terminal = None  # no terminal (e.g. VS Code)


def log(msg):
    line = f"\033[1;36m==>\033[0m {msg}"
    print(line, file=sys.stderr, flush=True)
    if _terminal:
        try:
            print(f"\r\033[K{line}", file=_terminal, flush=True)
        except OSError:
            pass


_progress_shown = False


def progress(msg):
    """One line that keeps updating (build/install percentages), on the
    terminal the user is looking at."""
    global _progress_shown
    target = _terminal or (sys.stderr if sys.stderr.isatty() else None)
    if target:
        try:
            print(f"\r\033[K\033[1;36m==>\033[0m {msg}", end="", file=target, flush=True)
            _progress_shown = True
        except OSError:
            pass


def progress_done():
    global _progress_shown
    if _progress_shown:
        target = _terminal or sys.stderr
        try:
            print("\r\033[K", end="", file=target, flush=True)
        except OSError:
            pass
        _progress_shown = False


def run_progress(cmd, parse, timeout=None, **kw):
    """Run a command whose output reports progress, showing it on one updating
    line. `parse(line)` returns a progress message or None. Returns
    (returncode, output); returncode is None on timeout."""
    cmd = [str(c) for c in cmd]
    if os.environ.get("XLINUX_VERBOSE"):
        print("   $ " + " ".join(cmd), file=sys.stderr, flush=True)
    kw.setdefault("env", config.tool_env())
    # Universal newlines: tools redraw their progress with "\r".
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", **kw)
    fired = []
    timer = threading.Timer(timeout, lambda: (fired.append(True), proc.kill())) if timeout else None
    if timer:
        timer.start()
    lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            message = parse(line)
            if message:
                progress(message)
        proc.wait()
    finally:
        if timer:
            timer.cancel()
        progress_done()
    return (None if fired else proc.returncode), "\n".join(lines)


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
