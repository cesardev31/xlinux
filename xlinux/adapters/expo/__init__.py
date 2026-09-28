"""Expo (React Native) adapter."""

import os
import shutil
import socket
import subprocess
import sys

from ...core import device
from ...core.util import log
from .build import is_expo

NAME = "Expo"


def setup():
    pass  # CocoaPods and the rest are installed the first time a project needs them


def doctor_checks(which):
    return [(bool(which("node")) and bool(which("npx")), "Node.js (for Expo projects)",
             "install Node.js (e.g. with nvm)")]


def detects(project_dir):
    return is_expo(project_dir)


def lan_address():
    """This machine's address on the local network (what the iPhone can reach)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def run_debug(project):
    """Launch the installed app and serve its JavaScript with Metro."""
    device.launch(project.bundle_identifier())
    host = lan_address()
    log(f"Metro on http://{host}:8081 — in the app's dev launcher, pick it or enter that URL "
        "(iPhone and computer on the same network)")
    env = {**os.environ, "REACT_NATIVE_PACKAGER_HOSTNAME": host}
    npx = shutil.which("npx") or "npx"
    sys.exit(subprocess.call([npx, "expo", "start", "--dev-client", "--lan", "--port", "8081"],
                             cwd=project.dir, env=env))
