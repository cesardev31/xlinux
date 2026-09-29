"""Print an app's log messages from the iPhone's system log, as they arrive.

    app_logs.py <process name> <message prefix> [udid]

Runs on pymobiledevice3's Python. Frameworks' tools read a device's logs from
the tool that launches the app (e.g. a Flutter custom device's runDebug
output): this is what lets `print()` show up in `flutter run` and VS Code.
Stops when its parent process exits.
"""

import asyncio
import ctypes
import signal
import sys
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.os_trace import OsTraceService


def exit_with_parent():
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


async def main(process, prefix, udid):
    lockdown = await create_using_usbmux(serial=udid) if udid else await create_using_usbmux()
    async with OsTraceService(lockdown) as trace:
        async for entry in trace.syslog():
            if Path(getattr(entry, "filename", "") or "").name != process:
                continue
            message = str(getattr(entry, "message", ""))
            if message.startswith(prefix):
                print(message, flush=True)


if __name__ == "__main__":
    exit_with_parent()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    except (KeyboardInterrupt, SystemExit, BrokenPipeError):
        pass
