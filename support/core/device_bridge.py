"""Local bridge to the iPhone for debugging apps without sudo.

Listens on 127.0.0.1:
  --debugserver-port  -> the iPhone's debugserver (iOS 17+, userspace tunnel, no check-in)
  --forward PORT      -> (repeatable) the same port on the iPhone, over usbmux
                         (e.g. Flutter's Dart VM Service)
  --control-port      -> line-delimited JSON commands. On iOS 17+ debugserver can't
                         launch apps ("Operation not permitted"): they're launched
                         suspended through DVT and lldb attaches to the pid.
                         {"cmd": "launch", "bundle_id": ..., "args": [...]} -> {"pid": N}
                         The DVT session stays open (and the app suspended) until
                         the client closes the socket: if it closes earlier, iOS
                         resumes the app and Flutter starts without a debugger.

If pymobiledevice3's kernel tunnel is running (`sudo pymobiledevice3 remote
tunneld`) it is used instead: lldb connects straight to debugserver over the
system network stack, much faster than the userspace tunnel's pure-Python TCP
(Flutter's JIT stops write MBs into the app's memory). Otherwise the userspace
tunnel is used. The bridge announces the address as `DEBUGSERVER=host:port`.

pymobiledevice3 ships a debugserver forwarder, but it performs the lockdown
check-in, while iOS 17+'s debugproxy expects a raw TCP connection (that's how
its own `debugserver lldb` flow uses it), so lldb used to hang.

Runs on the pymobiledevice3 environment's Python.
"""

import argparse
import asyncio
import json
import logging

import contextlib

from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
from pymobiledevice3.tcp_forwarder import TcpForwarderBase, UsbmuxTcpForwarder

DEBUGPROXY = "com.apple.internal.dt.remote.debugproxy"


class RawRsdForwarder(TcpForwarderBase):
    def __init__(self, rsd, src_port, device_port, listening_event):
        super().__init__(src_port, listening_event)
        self.rsd = rsd
        self.device_port = device_port

    async def _establish_remote_connection(self):
        return await self.rsd.create_service_connection(self.device_port)


def control_handler(rsd):
    async def handle(reader, writer):
        async def reply(response):
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()

        try:
            request = json.loads(await reader.readline())
            if request.get("cmd") != "launch":
                raise ValueError(f"unknown command: {request.get('cmd')}")
            async with DvtProvider(rsd) as dvt, ProcessControl(dvt) as process_control:
                pid = await process_control.launch(
                    bundle_id=request["bundle_id"],
                    arguments=request.get("args", []),
                    kill_existing=True,
                    start_suspended=True,
                )
                await reply({"pid": pid})
                await reader.read()  # wait for the client (with lldb attached) to close
        except Exception as e:  # reported to the client instead of killing the bridge
            try:
                await reply({"error": f"{type(e).__name__}: {e}"})
            except ConnectionError:
                pass
        finally:
            writer.close()

    return handle


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid")
    parser.add_argument("--debugserver-port", type=int, required=True)
    parser.add_argument("--forward", type=int, action="append", default=[])
    parser.add_argument("--control-port", type=int, required=True)
    args = parser.parse_args()

    async with open_rsd(args.udid) as (rsd, kernel_tunnel):
        debug_port = rsd.get_service_port(DEBUGPROXY)
        forward_debugserver = not kernel_tunnel
        ready = [asyncio.Event() for _ in range(int(forward_debugserver) + len(args.forward))]
        forwarders = []
        if forward_debugserver:
            forwarders.append(RawRsdForwarder(rsd, args.debugserver_port, debug_port, ready[0]))
            print(f"DEBUGSERVER=127.0.0.1:{args.debugserver_port}", flush=True)
        else:
            print(f"DEBUGSERVER=[{rsd.service.address[0]}]:{debug_port}", flush=True)
        forwarders += [UsbmuxTcpForwarder(args.udid, port, port, listening_event=event)
                       for port, event in zip(args.forward, ready[int(forward_debugserver):])]
        tasks = [asyncio.create_task(f.start()) for f in forwarders]
        control = await asyncio.start_server(control_handler(rsd), "127.0.0.1", args.control_port)
        tasks.append(asyncio.create_task(control.serve_forever()))
        for event in ready:
            await event.wait()
        print(f"TUNNEL={'kernel' if kernel_tunnel else 'userspace'}", flush=True)
        print("BRIDGE_READY", flush=True)
        await asyncio.gather(*tasks)


@contextlib.asynccontextmanager
async def open_rsd(udid):
    """RSD over the kernel tunnel (tunneld) if available; otherwise userspace."""
    try:
        from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid
        rsd = await get_tunneld_device_by_udid(udid)
    except Exception:
        rsd = None
    if rsd is not None:
        try:
            yield rsd, True
        finally:
            await rsd.close()
        return
    async with UserspaceRsdTunnel(serial=udid) as rsd:
        yield rsd, False


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
