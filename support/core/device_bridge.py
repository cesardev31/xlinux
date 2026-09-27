"""Puente local hacia el iPhone para depurar apps sin sudo.

Abre en 127.0.0.1:
  --debugserver-port  -> debugserver del iPhone (iOS 17+, túnel userspace, sin check-in)
  --forward PUERTO    -> (repetible) el mismo puerto dentro del iPhone, por usbmux
                         (p. ej. el Dart VM Service de Flutter)
  --control-port      -> comandos JSON por línea. En iOS 17+ debugserver no puede
                         lanzar apps ("Operation not permitted"): se lanzan
                         suspendidas por DVT y lldb se adjunta al pid.
                         {"cmd": "launch", "bundle_id": ..., "args": [...]} -> {"pid": N}
                         La sesión DVT sigue abierta (y la app suspendida) hasta
                         que el cliente cierra el socket: si se cierra antes,
                         iOS reanuda la app y Flutter arranca sin debugger.

pymobiledevice3 trae un reenviador para debugserver, pero hace el check-in de
lockdown, y el debugproxy de iOS 17+ espera una conexión TCP cruda (así lo usa
su propio flujo `debugserver lldb`), por lo que lldb se quedaba colgado.

Se corre con el Python del entorno de pymobiledevice3.
"""

import argparse
import asyncio
import json
import logging

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
                raise ValueError(f"comando desconocido: {request.get('cmd')}")
            async with DvtProvider(rsd) as dvt, ProcessControl(dvt) as process_control:
                pid = await process_control.launch(
                    bundle_id=request["bundle_id"],
                    arguments=request.get("args", []),
                    kill_existing=True,
                    start_suspended=True,
                )
                await reply({"pid": pid})
                await reader.read()  # esperar a que el cliente (ya con lldb adjunto) cierre
        except Exception as e:  # se reporta al cliente en vez de tumbar el puente
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

    async with UserspaceRsdTunnel(serial=args.udid) as rsd:
        ready = [asyncio.Event() for _ in range(1 + len(args.forward))]
        forwarders = [RawRsdForwarder(rsd, args.debugserver_port, rsd.get_service_port(DEBUGPROXY), ready[0])]
        forwarders += [UsbmuxTcpForwarder(args.udid, port, port, listening_event=event)
                       for port, event in zip(args.forward, ready[1:])]
        tasks = [asyncio.create_task(f.start()) for f in forwarders]
        control = await asyncio.start_server(control_handler(rsd), "127.0.0.1", args.control_port)
        tasks.append(asyncio.create_task(control.serve_forever()))
        for event in ready:
            await event.wait()
        print("BRIDGE_READY", flush=True)
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
