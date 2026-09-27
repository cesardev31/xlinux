"""Rutas y entorno de flutter-ios-linux.

El código vive en este repo; todo lo pesado (toolchain Swift, SDK de iOS,
engine de Flutter, Darling, pymobiledevice3) vive en el directorio de datos,
normalmente un SSD externo. Los comandos se pueden invocar desde VS Code o
desde la herramienta `flutter` (custom devices), que no cargan ningún
`env.sh`, así que el entorno se arma aquí.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPPORT = REPO / "support"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "flutter-ios-linux/config.json"
DEFAULT_DATA_DIR = "/run/media/cesar/games/ios-dev"

MIN_IOS = "15.0"
XTOOL_SDK = Path.home() / ".swiftpm/swift-sdks/darwin.artifactbundle"
IPHONE_SDK = XTOOL_SDK / "Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk"
MACOS_SDK = XTOOL_SDK / "Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
SWIFT_RESOURCES = XTOOL_SDK / "Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift"
TOOLSET_BIN = XTOOL_SDK / "toolset/bin"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(values):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(values, indent=2) + "\n")


def data_dir():
    return Path(os.environ.get("FIL_DATA") or load_config().get("data_dir") or DEFAULT_DATA_DIR)


def require_data_dir():
    data = data_dir()
    if not (data / "swiftly").is_dir():
        sys.exit(f"error: no encuentro el directorio de datos en {data}. ¿Está montado el SSD?")
    return data


def swift_bin():
    toolchains = sorted((data_dir() / "swiftly/toolchains").glob("*/usr/bin"))
    return toolchains[-1] if toolchains else None


def pymobiledevice3_python():
    return data_dir() / "uv-tools/pymobiledevice3/bin/python"


def engine_dir(revision):
    return data_dir() / "flutter-engine" / revision


def compat_shim():
    return data_dir() / "cache/libdarling_compat.dylib"


def tool_env():
    """Entorno con Swift, xtool, Darling y pymobiledevice3 disponibles."""
    data = data_dir()
    env = os.environ.copy()
    # <datos>/bin primero: trae xtool extraído del AppImage, que sí funciona
    # sin FUSE (el AppImage falla cuando lo lanza el snap de Flutter).
    path = [str(data / "bin"), str(Path.home() / ".local/bin")]
    if swift_bin():
        path.insert(0, str(swift_bin()))
    env["PATH"] = os.pathsep.join(path + [env.get("PATH", "")])
    env["DPREFIX"] = str(data / "darling-prefix")
    env["XTL_TMPDIR"] = str(data / "tmp")
    env["UV_TOOL_DIR"] = str(data / "uv-tools")
    env["UV_CACHE_DIR"] = str(data / "tmp/uv-cache")
    env.setdefault("NO_COLOR", "1")
    return env
