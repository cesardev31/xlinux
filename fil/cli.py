import argparse
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
    project = flutter_build.build(os.getcwd(), debug=True)
    device.install(project.ipa, device.first_device()[0])


def cmd_device_uninstall(_args):
    pass  # a propósito: ver custom_device.device_config()


def cmd_device_run_debug(args):
    project = flutter_build.Project(os.getcwd(), debug=True)
    flutter_debug.run_debug(project, args.engine_options, device.first_device()[0])


def main():
    parser = argparse.ArgumentParser(
        prog="flutter-ios-linux",
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

    args = parser.parse_args()
    args.func(args)
