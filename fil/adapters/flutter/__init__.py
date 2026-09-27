"""Adaptador Flutter."""

from . import custom_device

NAME = "Flutter"


def setup():
    custom_device.register()


def doctor_checks(which):
    return [
        (bool(which("flutter")), "flutter", "instala Flutter y agrégalo al PATH"),
        (custom_device.is_registered(), "custom device para VS Code / flutter run",
         "corre `flutter-ios-linux setup`"),
    ]
