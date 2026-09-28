"""Flutter adapter."""

import shutil

from ...core.util import log
from . import custom_device

NAME = "Flutter"


def setup():
    if not shutil.which("flutter"):
        log("Flutter not found: install it (https://docs.flutter.dev/get-started/install/linux) "
            "and run `xlinux setup` again to register the iPhone")
        return
    custom_device.register()


def doctor_checks(which):
    return [
        (bool(which("flutter")), "flutter", "install Flutter and add it to PATH"),
        (custom_device.is_registered(), "custom device for VS Code / flutter run",
         "run `xlinux setup`"),
    ]
