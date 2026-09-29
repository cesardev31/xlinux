"""Flutter debug: Dart's JIT on iOS needs lldb attached (the
NOTIFY_DEBUGGER_ABOUT_RX_PAGES helper), and the `flutter` tool needs the Dart
VM Service URL, which is forwarded over USB to the same local port."""

import shlex

from ...core import config, device
from ...core.device import DebugSession

JIT_HELPER = config.SUPPORT / "flutter/flutter_lldb_helper.py"
# Engine options we decide ourselves (the VM Service is forwarded over USB).
_RESERVED = ("--vm-service-port", "--vm-service-host", "--observatory-port")


def run_debug(project, engine_options, udid):
    """Launch in debug mode and print the line `flutter run` / `flutter attach` look for."""
    if isinstance(engine_options, str):
        engine_options = shlex.split(engine_options)
    vm_port = device.free_port()
    args = [o for o in engine_options if not o.startswith(_RESERVED)]
    args += [f"--vm-service-port={vm_port}", "--disable-service-auth-codes"]
    session = DebugSession(project.app, project.bundle_identifier(), udid,
                           lldb_helpers=[JIT_HELPER], forward_ports=[vm_port])
    # `flutter run` and VS Code read the app's output from this command's
    # stdout: forward its print()s ("flutter: …") from the iPhone's log.
    # Started before the launch so main()'s first prints aren't missed.
    logs = device.forward_logs("Runner", "flutter: ", udid)
    try:
        session.run_until_parent_exits(args, on_ready=lambda: print(
            f"The Dart VM service is listening on http://127.0.0.1:{vm_port}/", flush=True))
    finally:
        logs.terminate()


def run_release(project):
    device.launch(project.bundle_identifier())
    device.stream_logs("Runner", match="flutter")
