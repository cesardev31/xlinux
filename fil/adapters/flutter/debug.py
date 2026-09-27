"""Debug de Flutter: el JIT de Dart en iOS necesita lldb adjunto (helper
NOTIFY_DEBUGGER_ABOUT_RX_PAGES) y la herramienta `flutter` necesita la URL del
Dart VM Service, que se reenvía por USB al mismo puerto local."""

import shlex

from ...core import config, device
from ...core.device import DebugSession

JIT_HELPER = config.SUPPORT / "flutter/flutter_lldb_helper.py"
# Opciones del engine que decidimos nosotros (el VM Service se reenvía por USB).
_RESERVED = ("--vm-service-port", "--vm-service-host", "--observatory-port")


def run_debug(project, engine_options, udid):
    """Lanza en debug e imprime la línea que buscan `flutter run` / `flutter attach`."""
    if isinstance(engine_options, str):
        engine_options = shlex.split(engine_options)
    vm_port = device.free_port()
    args = [o for o in engine_options if not o.startswith(_RESERVED)]
    args += [f"--vm-service-port={vm_port}", "--disable-service-auth-codes"]
    session = DebugSession(project.app, project.bundle_identifier(), udid,
                           lldb_helpers=[JIT_HELPER], forward_ports=[vm_port])
    session.run_until_parent_exits(args, on_ready=lambda: print(
        f"The Dart VM service is listening on http://127.0.0.1:{vm_port}/", flush=True))


def run_release(project):
    device.launch(project.bundle_identifier())
    device.stream_logs("Runner", match="flutter")
