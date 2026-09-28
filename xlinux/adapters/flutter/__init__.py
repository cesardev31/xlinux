"""Flutter adapter."""

from . import custom_device

NAME = "Flutter"


def setup():
    custom_device.register()


def doctor_checks(which):
    return [
        (bool(which("flutter")), "flutter", "install Flutter and add it to PATH"),
        (custom_device.is_registered(), "custom device for VS Code / flutter run",
         "run `xlinux setup`"),
    ]
