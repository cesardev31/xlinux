"""Paths and environment.

The code lives in this repo; everything heavy (Swift toolchain, iOS SDK,
Darling, pymobiledevice3, each framework's artifacts) lives in the data
directory, e.g. an external SSD. Commands can be invoked from VS Code or from
a framework's own tool (e.g. Flutter custom devices), which don't source any
`env.sh`, so the environment is assembled here.
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SUPPORT = REPO / "support"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "xlinux/config.json"
DEFAULT_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "xlinux"

MIN_IOS = "15.0"
XTOOL_SDK = Path.home() / ".swiftpm/swift-sdks/darwin.artifactbundle"
IPHONE_SDK = XTOOL_SDK / "Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk"
MACOS_SDK = XTOOL_SDK / "Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
SWIFT_RESOURCES = XTOOL_SDK / "Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift"
TOOLSET_BIN = XTOOL_SDK / "toolset/bin"


# The project's previous name: its config is still read if there is no new one.
LEGACY_CONFIG_FILE = CONFIG_FILE.parent.parent / "flutter-ios-linux/config.json"


def load_config():
    for path in (CONFIG_FILE, LEGACY_CONFIG_FILE):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return {}


def save_config(values):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(values, indent=2) + "\n")


def data_dir():
    return Path(os.environ.get("XLINUX_DATA") or load_config().get("data_dir") or DEFAULT_DATA_DIR)


def require_data_dir():
    data = data_dir()
    if not (data / "swiftly").is_dir():
        sys.exit(f"error: data directory not found at {data} (run `xlinux setup`; if it's on an external drive, is it mounted?)")
    return data


def swift_bin():
    toolchains = sorted((data_dir() / "swiftly/toolchains").glob("*/usr/bin"))
    return toolchains[-1] if toolchains else None


def pymobiledevice3_python():
    return data_dir() / "uv-tools/pymobiledevice3/bin/python"


def llvm_tool(name):
    """The system LLVM's `llvm-<name>` (lipo, otool, install-name-tool...),
    whatever its version: /usr/bin, or the newest /usr/lib/llvm-N (Debian/Ubuntu)."""
    found = shutil.which(f"llvm-{name}")
    if found:
        return found
    versions = sorted(Path("/usr/lib").glob(f"llvm-*/bin/llvm-{name}"),
                      key=lambda p: int(re.sub(r"\D", "", p.parent.parent.name) or 0))
    return str(versions[-1]) if versions else None


def compat_shim():
    return data_dir() / "cache/libdarling_compat.dylib"


def tool_env():
    """Environment with Swift, xtool, Darling and pymobiledevice3 available."""
    data = data_dir()
    env = os.environ.copy()
    # support/core/bin: our xcrun/actool/clang stand-ins (e.g. Flutter native assets need xcrun).
    # <data>/bin: xtool extracted from its AppImage, which works without FUSE
    # (the AppImage fails when launched from the Flutter snap).
    path = [str(SUPPORT / "core/bin"), str(data / "bin"), str(Path.home() / ".local/bin")]
    if swift_bin():
        path.insert(0, str(swift_bin()))
    # pymobiledevice3's venv goes last: its `python3` mustn't shadow the system one.
    env["PATH"] = os.pathsep.join(path + [env.get("PATH", ""), str(pymobiledevice3_python().parent)])
    env["DPREFIX"] = str(data / "darling-prefix")
    env["XTL_TMPDIR"] = str(data / "tmp")
    env["UV_TOOL_DIR"] = str(data / "uv-tools")
    env["UV_TOOL_BIN_DIR"] = str(data / "bin")  # not ~/.local/bin: no clash with other installs
    env["UV_CACHE_DIR"] = str(data / "tmp/uv-cache")
    env.setdefault("NO_COLOR", "1")
    return env
