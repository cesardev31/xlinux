import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .adapters import expo, flutter
from .adapters.expo import build as expo_build
from .adapters.flutter import build as flutter_build
from .adapters.flutter import debug as flutter_debug
from .core import device, setup, util

ADAPTERS = [flutter, expo]


def detect_adapter(project_dir):
    if (Path(project_dir) / "pubspec.yaml").exists():
        return flutter
    if expo.detects(project_dir):
        return expo
    sys.exit(f"error: unrecognized project type in {Path(project_dir).resolve()} "
             "(supported: Flutter, Expo)")


def cmd_setup(args):
    setup.setup(ADAPTERS, args.data_dir, xip=args.xip, everything=args.all)


def cmd_doctor(_args):
    setup.doctor(ADAPTERS)


def _dart_defines(args):
    return flutter_build.read_dart_defines(args.dart_define, args.dart_define_from_file)


def _add_dart_define_options(p):
    p.add_argument("--dart-define", action="append", default=[], metavar="KEY=VALUE",
                   help="like flutter's --dart-define (repeatable)")
    p.add_argument("--dart-define-from-file", action="append", default=[], metavar="FILE",
                   help="like flutter's --dart-define-from-file (.json or .env, repeatable)")


def cmd_build(args):
    if detect_adapter(args.project) is expo:
        project = expo_build.build(args.project, debug=args.debug)
        if args.install:
            device.install(project.app, device.first_device()[0])
        return
    project = flutter_build.build(args.project, debug=args.debug, dart_defines=_dart_defines(args))
    if args.install:
        device.install(project.ipa, device.first_device()[0])


def cmd_run(args):
    if detect_adapter(args.project) is expo:
        # Expo: debug with expo-dev-client and Metro, or a release with the
        # JavaScript bundled in the app.
        project = expo_build.build(args.project, debug=not args.release)
        device.install(project.app, device.first_device()[0])
        project.lock.release()
        if args.release:
            expo.run_release(project)
        else:
            expo.run_debug(project)
        return
    project = flutter_build.build(args.project, debug=False, dart_defines=_dart_defines(args))
    device.install(project.ipa, device.first_device()[0])
    project.lock.release()  # the logs below can run for hours
    flutter_debug.run_release(project)


# --- Commands called by `flutter` (custom device), see adapters/flutter/custom_device.py ---

def cmd_device_ping(_args):
    sys.exit(0 if device.connected_devices() else 1)


def cmd_device_install(_args):
    util.show_progress_in_terminal()
    # `flutter run` already built its bundle for "linux"; we install our own iOS
    # build of the same project (Flutter runs these commands from its root).
    project = flutter_build.build(os.getcwd(), debug=True, package=False,
                                  kernel=flutter_build.flutter_run_kernel(os.getcwd()))
    device.install(project.app, device.first_device()[0])


def cmd_device_uninstall(_args):
    pass  # on purpose: see custom_device.device_config()


def cmd_device_run_debug(args):
    project = flutter_build.Project(os.getcwd(), debug=True)
    flutter_debug.run_debug(project, args.engine_options, device.first_device()[0])


def cmd_device_screenshot(args):
    udid = device.first_device()[0]
    if args.base64:
        print(device.screenshot_base64(udid))
        return
    print(device.screenshot(args.output, udid))


def cmd_device_mirror(args):
    device.mirror(device.first_device()[0], mode=args.mode, bind=args.bind, port=args.port,
                  password=args.password, audio=args.audio,
                  share_clipboard=args.share_clipboard)


def cmd_agent_snapshot(args):
    print(json.dumps(device.screenshot_info(args.output, device.first_device()[0])))


def cmd_agent_input(args):
    values = ([args.x, args.y] if args.agent_command == "tap" else
              [args.x1, args.y1, args.x2, args.y2] if args.agent_command == "swipe" else
              [args.text] if args.agent_command == "type" else [args.name])
    print(json.dumps(device.agent_input(device.first_device()[0], args.agent_command, values,
                                       duration=getattr(args, "duration", 0.3))))


def cmd_mcp(_args):
    """MCP server (stdio) for agents; runs on pymobiledevice3's Python, which
    has the CoreDevice library and the MCP SDK."""
    from .core import config, deps
    deps.ensure_mcp_sdk()
    env = config.tool_env()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(config.REPO), env.get("PYTHONPATH", "")]))
    python = str(config.pymobiledevice3_python())
    os.execve(python, [python, "-m", "xlinux.mcp_server"], env)


def main():
    parser = argparse.ArgumentParser(
        prog="xlinux",
        description="Build, install and debug apps on a real iPhone from Linux.",
        epilog="Flutter debug with hot reload: `flutter run -d iphone-linux`, or pick the iPhone in VS Code.",
    )
    parser.add_argument("--version", action="version", version=f"xlinux {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="install everything that's missing and register the iPhone with Flutter/VS Code")
    p.add_argument("--data-dir", help="directory holding the heavy toolchains (e.g. an external SSD)")
    p.add_argument("--xip", help="path to the Xcode .xip downloaded from Apple (searched for if omitted)")
    p.add_argument("--all", action="store_true",
                   help="also install what's otherwise installed on first use "
                        "(Darling for release builds, cairosvg, MCP SDK, patched macro server)")
    p.set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="check that everything is ready").set_defaults(func=cmd_doctor)

    p = sub.add_parser("build", help="build the app for iPhone")
    p.add_argument("--project", default=".", help="project directory (default: current directory)")
    p.add_argument("--debug", action="store_true", help="debug mode (JIT, for hot reload)")
    p.add_argument("--install", action="store_true", help="sign and install on the iPhone")
    _add_dart_define_options(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("run", help="build, install and launch: Flutter in release mode (streams logs), "
                                     "Expo in debug mode (starts Metro)")
    p.add_argument("--project", default=".")
    p.add_argument("--release", action="store_true",
                   help="Expo: release build (JavaScript bundled) instead of debug + Metro")
    _add_dart_define_options(p)
    p.set_defaults(func=cmd_run)

    sub.add_parser("mcp", help="MCP server so an AI agent can see and drive the iPhone").set_defaults(func=cmd_mcp)

    p = sub.add_parser("device", help="device commands (also used internally by the Flutter custom device)")
    dsub = p.add_subparsers(dest="device_command", required=True)
    dsub.add_parser("ping").set_defaults(func=cmd_device_ping)
    dsub.add_parser("install").set_defaults(func=cmd_device_install)
    dsub.add_parser("uninstall").set_defaults(func=cmd_device_uninstall)
    p = dsub.add_parser("run-debug")
    p.add_argument("--engine-options", default="")
    p.set_defaults(func=cmd_device_run_debug)
    p = dsub.add_parser("screenshot", help="save a PNG screenshot of the iPhone")
    p.add_argument("output", nargs="?", default="iphone.png")
    p.add_argument("--base64", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_device_screenshot)
    p = dsub.add_parser("mirror", help="view and control the iPhone over web or VNC")
    p.add_argument("--mode", choices=("web", "vnc"), default="web")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--password")
    p.add_argument("--audio", action="store_true", help="play the iPhone audio (VNC)")
    p.add_argument("--share-clipboard", action="store_true", help="share the clipboard (VNC)")
    p.set_defaults(func=cmd_device_mirror)
    p = dsub.add_parser("agent", help="JSON actions for development agents")
    agent = p.add_subparsers(dest="agent_command", required=True)
    p = agent.add_parser("snapshot", help="capture a PNG and return its path/size as JSON")
    p.add_argument("output", nargs="?", default="/tmp/xlinux/screen.png")
    p.set_defaults(func=cmd_agent_snapshot)
    p = agent.add_parser("tap", help="tap at normalized 0..1 coordinates")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.set_defaults(func=cmd_agent_input)
    p = agent.add_parser("swipe", help="drag between normalized 0..1 coordinates")
    p.add_argument("x1", type=float)
    p.add_argument("y1", type=float)
    p.add_argument("x2", type=float)
    p.add_argument("y2", type=float)
    p.add_argument("--duration", type=float, default=0.3)
    p.set_defaults(func=cmd_agent_input)
    p = agent.add_parser("type", help="type ASCII text")
    p.add_argument("text")
    p.set_defaults(func=cmd_agent_input)
    p = agent.add_parser("button", help="press a hardware button")
    p.add_argument("name", choices=("home", "lock", "volume-up", "volume-down", "mute", "siri"))
    p.set_defaults(func=cmd_agent_input)

    args = parser.parse_args()
    try:
        result = args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    if isinstance(result, int) and result not in (0, 130):
        sys.exit(result)
