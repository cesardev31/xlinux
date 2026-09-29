"""Local bridge to the iPhone for debugging apps without sudo.

Listens on 127.0.0.1:
  --debugserver-port  -> the iPhone's debugserver (iOS 17+, userspace tunnel, no check-in)
  --forward PORT      -> (repeatable) the same port on the iPhone, over usbmux
                         (e.g. Flutter's Dart VM Service)
  --control-port      -> line-delimited JSON commands. On iOS 17+ debugserver can't
                         launch apps ("Operation not permitted"): they're launched
                         suspended through DVT and lldb attaches to the pid.
                         {"cmd": "launch", "bundle_id": ..., "args": [...]}
                           -> {"pid": N, "bundle_id": installed id, "path": app path}
                         (the installed id may carry xtool's XTL-<team>. prefix;
                         orphaned instances of the app are stopped first)
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
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.tcp_forwarder import TcpForwarderBase, UsbmuxTcpForwarder

DEBUGPROXY = "com.apple.internal.dt.remote.debugproxy"


class RawRsdForwarder(TcpForwarderBase):
    def __init__(self, rsd, src_port, device_port, listening_event):
        super().__init__(src_port, listening_event)
        self.rsd = rsd
        self.device_port = device_port

    async def _establish_remote_connection(self):
        return await self.rsd.create_service_connection(self.device_port)


def _container(path):
    """/var/containers/Bundle/Application/<UUID> of an app or process path."""
    parts = path.removeprefix("/private").split("/")
    return "/".join(parts[:6]) if parts[1:5] == ["var", "containers", "Bundle", "Application"] else None


async def resolve_app(rsd, bundle_id):
    """(installed bundle id, app path, containers of every installed app)."""
    async with InstallationProxyService(lockdown=rsd) as proxy:
        apps = await proxy.browse(options={"ApplicationType": "Any"}, attributes=["CFBundleIdentifier", "Path"])
    installed = {_container(a.get("Path", "")) for a in apps}
    for app in apps:
        real = app.get("CFBundleIdentifier", "")
        if real == bundle_id or real.endswith("." + bundle_id):
            return real, app["Path"], installed
    raise ValueError(f"{bundle_id} is not installed on the iPhone")


async def stop_orphans(device_info, process_control, app_path, installed):
    """Instances of an app with the same .app name whose install was replaced
    since they started: a debug one keeps a debugserver attached and the next
    lldb attach fails ("a process is already being debugged")."""
    app_dir = app_path.rstrip("/").rsplit("/", 1)[1]
    for proc in await device_info.proclist():
        path = str(proc.get("realAppName", ""))
        container = _container(path)
        if container and container not in installed and \
                path.removeprefix("/private").startswith(f"{container}/{app_dir}/"):
            await process_control.kill(proc["pid"])


def control_handler(rsd):
    async def handle(reader, writer):
        async def reply(response):
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()

        try:
            request = json.loads(await reader.readline())
            if request.get("cmd") != "launch":
                raise ValueError(f"unknown command: {request.get('cmd')}")
            bundle_id, path, installed = await resolve_app(rsd, request["bundle_id"])
            async with DvtProvider(rsd) as dvt, ProcessControl(dvt) as process_control, \
                    DeviceInfo(dvt) as device_info:
                await stop_orphans(device_info, process_control, path, installed)
                pid = await process_control.launch(
                    bundle_id=bundle_id,
                    arguments=request.get("args", []),
                    kill_existing=True,  # the current instance, if any
                    start_suspended=True,
                )
                await reply({"pid": pid, "bundle_id": bundle_id, "path": path})
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
