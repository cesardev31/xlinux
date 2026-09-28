import argparse
import json
import os
import sys
from pathlib import Path

from .adapters import flutter
from .adapters.flutter import build as flutter_build
from .adapters.flutter import debug as flutter_debug
from .core import device, setup

ADAPTERS = [flutter]


def detect_adapter(project_dir):
    if (Path(project_dir) / "pubspec.yaml").exists():
        return flutter
    sys.exit(f"error: no reconozco el tipo de proyecto en {Path(project_dir).resolve()} "
             "(soportado: Flutter)")


def cmd_setup(args):
    setup.setup(ADAPTERS, args.data_dir)


def cmd_doctor(_args):
    setup.doctor(ADAPTERS)


def cmd_build(args):
    detect_adapter(args.project)
    project = flutter_build.build(args.project, debug=args.debug)
    if args.install:
        device.install(project.ipa, device.first_device()[0])


def cmd_run(args):
    detect_adapter(args.project)
    project = flutter_build.build(args.project, debug=False)
    device.install(project.ipa, device.first_device()[0])
    flutter_debug.run_release(project)


# --- Comandos que llama `flutter` (custom device), ver adapters/flutter/custom_device.py ---

def cmd_device_ping(_args):
    sys.exit(0 if device.connected_devices() else 1)


def cmd_device_install(_args):
    # `flutter run` ya compiló su bundle para "linux"; instalamos nuestro build
    # de iOS del mismo proyecto (Flutter corre estos comandos desde su raíz).
    project = flutter_build.build(os.getcwd(), debug=True, package=False)
    device.install(project.app, device.first_device()[0])


def cmd_device_uninstall(_args):
    pass  # a propósito: ver custom_device.device_config()


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


def main():
    parser = argparse.ArgumentParser(
        prog="xlinux",
        description="Compila, instala y depura apps en iPhone desde Linux.",
        epilog="Flutter en debug con hot reload: `flutter run -d iphone-linux` o elige el iPhone en VS Code.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="preparar el entorno y registrar el iPhone en Flutter/VS Code")
    p.add_argument("--data-dir", help="directorio con las herramientas pesadas (SSD)")
    p.set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="revisar que todo esté listo").set_defaults(func=cmd_doctor)

    p = sub.add_parser("build", help="compilar la app para iPhone")
    p.add_argument("--project", default=".", help="proyecto (por defecto: directorio actual)")
    p.add_argument("--debug", action="store_true", help="modo debug (JIT, para hot reload)")
    p.add_argument("--install", action="store_true", help="firmar e instalar en el iPhone")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("run", help="compilar en release, instalar, abrir y ver logs")
    p.add_argument("--project", default=".")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("device", help="(interno) comandos que usa `flutter` para el custom device")
    dsub = p.add_subparsers(dest="device_command", required=True)
    dsub.add_parser("ping").set_defaults(func=cmd_device_ping)
    dsub.add_parser("install").set_defaults(func=cmd_device_install)
    dsub.add_parser("uninstall").set_defaults(func=cmd_device_uninstall)
    p = dsub.add_parser("run-debug")
    p.add_argument("--engine-options", default="")
    p.set_defaults(func=cmd_device_run_debug)
    p = dsub.add_parser("screenshot", help="capturar la pantalla del iPhone como PNG")
    p.add_argument("output", nargs="?", default="iphone.png")
    p.add_argument("--base64", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_device_screenshot)
    p = dsub.add_parser("mirror", help="ver y controlar el iPhone por web o VNC")
    p.add_argument("--mode", choices=("web", "vnc"), default="web")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--password")
    p.add_argument("--audio", action="store_true", help="reproducir audio del iPhone (VNC)")
    p.add_argument("--share-clipboard", action="store_true", help="compartir portapapeles (VNC)")
    p.set_defaults(func=cmd_device_mirror)
    p = dsub.add_parser("agent", help="acciones JSON para agentes de desarrollo")
    agent = p.add_subparsers(dest="agent_command", required=True)
    p = agent.add_parser("snapshot", help="capturar PNG y devolver ruta/dimensiones como JSON")
    p.add_argument("output", nargs="?", default="/tmp/xlinux/screen.png")
    p.set_defaults(func=cmd_agent_snapshot)
    p = agent.add_parser("tap", help="tocar coordenadas normalizadas 0..1")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.set_defaults(func=cmd_agent_input)
    p = agent.add_parser("swipe", help="arrastrar entre coordenadas normalizadas 0..1")
    p.add_argument("x1", type=float)
    p.add_argument("y1", type=float)
    p.add_argument("x2", type=float)
    p.add_argument("y2", type=float)
    p.add_argument("--duration", type=float, default=0.3)
    p.set_defaults(func=cmd_agent_input)
    p = agent.add_parser("type", help="escribir texto ASCII")
    p.add_argument("text")
    p.set_defaults(func=cmd_agent_input)
    p = agent.add_parser("button", help="pulsar un botón físico")
    p.add_argument("name", choices=("home", "lock", "volume-up", "volume-down", "mute", "siri"))
    p.set_defaults(func=cmd_agent_input)

    args = parser.parse_args()
    try:
        result = args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    if isinstance(result, int) and result not in (0, 130):
        sys.exit(result)
