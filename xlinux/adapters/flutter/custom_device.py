"""Register the iPhone as a Flutter "custom device".

It then shows up in `flutter devices` and in VS Code's device picker, and
`flutter run -d iphone-linux` works (hot reload included). Flutter calls this
tool's `device ...` commands.

Flutter only allows custom devices to be declared as linux-x64 or
linux-arm64. The bundle `flutter` builds for that platform is ignored (we
install our own iOS build). The Dart plugin registrant is not affected: it
covers every platform and picks one at runtime with `Platform.isIOS`.
"""

import json
import os
from pathlib import Path

from ...core import config
from ...core.util import log, run

DEVICE_ID = "iphone-linux"
FLUTTER_CUSTOM_DEVICES = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "flutter/custom_devices.json"


def device_config():
    cli = str(config.REPO / "bin/xlinux")
    return {
        "id": DEVICE_ID,
        "label": "iPhone (xlinux)",
        "sdkNameAndVersion": "iOS via xlinux",
        "platform": "linux-x64",
        "enabled": True,
        "ping": [cli, "device", "ping"],
        "pingSuccessRegex": None,
        "postBuild": None,
        "install": [cli, "device", "install"],
        # Never uninstall: once the last app signed by an Apple ID is removed,
        # iOS forgets the developer trust and it has to be re-approved by hand.
        "uninstall": [cli, "device", "uninstall"],
        "runDebug": [cli, "device", "run-debug", "--engine-options=${engineOptions}"],
        "forwardPort": None,
        "forwardPortSuccessRegex": None,
        # Flutter expects the command to print the PNG encoded as base64.
        "screenshot": [cli, "device", "screenshot", "--base64"],
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
    log(f"iPhone registered as custom device '{DEVICE_ID}' in {FLUTTER_CUSTOM_DEVICES}")
