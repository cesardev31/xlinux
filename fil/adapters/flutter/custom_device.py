"""Registra el iPhone como "custom device" de Flutter.

Así aparece en `flutter devices`, en el selector de dispositivos de VS Code y
funciona con `flutter run -d iphone-linux` (hot reload incluido). Flutter
llama a los comandos `device ...` de esta herramienta.

Limitación de Flutter: un custom device solo puede declararse linux-x64 o
linux-arm64. El bundle que compila `flutter` se ignora (instalamos nuestro
propio build de iOS), pero el registrante de plugins Dart que envía un hot
restart es el de Linux.
"""

import json
import os
from pathlib import Path

from ...core import config
from ...core.util import log, run

DEVICE_ID = "iphone-linux"
FLUTTER_CUSTOM_DEVICES = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "flutter/custom_devices.json"


def device_config():
    cli = str(config.REPO / "bin/flutter-ios-linux")
    return {
        "id": DEVICE_ID,
        "label": "iPhone (flutter-ios-linux)",
        "sdkNameAndVersion": "iOS vía flutter-ios-linux",
        "platform": "linux-x64",
        "enabled": True,
        "ping": [cli, "device", "ping"],
        "pingSuccessRegex": None,
        "postBuild": None,
        "install": [cli, "device", "install"],
        # No desinstalar: al borrar la última app firmada, iOS olvida la
        # confianza en el desarrollador y hay que volver a aprobarla a mano.
        "uninstall": [cli, "device", "uninstall"],
        "runDebug": [cli, "device", "run-debug", "--engine-options=${engineOptions}"],
        "forwardPort": None,
        "forwardPortSuccessRegex": None,
        "screenshot": None,
    }


def is_registered():
    try:
        devices = json.loads(FLUTTER_CUSTOM_DEVICES.read_text()).get("custom-devices", [])
    except (OSError, ValueError):
        return False
    return any(d.get("id") == DEVICE_ID for d in devices)


def register():
    run(["flutter", "config", "--enable-custom-devices"], capture_output=True, text=True)
    try:
        data = json.loads(FLUTTER_CUSTOM_DEVICES.read_text())
    except (OSError, ValueError):
        data = {}
    devices = [d for d in data.get("custom-devices", []) if d.get("id") != DEVICE_ID]
    devices.append(device_config())
    data["custom-devices"] = devices
    FLUTTER_CUSTOM_DEVICES.parent.mkdir(parents=True, exist_ok=True)
    FLUTTER_CUSTOM_DEVICES.write_text(json.dumps(data, indent=2) + "\n")
    log(f"iPhone registrado como custom device '{DEVICE_ID}' en {FLUTTER_CUSTOM_DEVICES}")
