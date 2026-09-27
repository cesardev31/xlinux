"""Todo lo que habla con el iPhone: detectar, instalar, lanzar y depurar."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from . import config
from .util import log, output, run


def pmd3(*args, **kw):
    return output(["pymobiledevice3", *args], **kw)


def connected_devices():
    """[(udid, nombre)] de los iPhone conectados por USB."""
    try:
        devices = json.loads(pmd3("usbmux", "list", check=False) or "[]")
    except ValueError:
        return []
    return [(d["Identifier"], d.get("DeviceName", "iPhone")) for d in devices
            if d.get("ConnectionType") == "USB"]


def first_device():
    devices = connected_devices()
    if not devices:
        sys.exit("error: no hay ningún iPhone conectado por USB (¿desbloqueado y en 'Confiar'?)")
    return devices[0]


def ensure_developer_image():
    """La Developer Disk Image (debugserver, DVT) se desmonta en cada reinicio."""
    mounted = pmd3("mounter", "list", check=False)
    if '"DeveloperDiskImage"' not in mounted:
        log("Montando la Developer Disk Image")
        run(["pymobiledevice3", "mounter", "auto-mount"], cwd=config.data_dir() / "tmp",
            capture_output=True, text=True)


def install(ipa, udid=None):
    log("Firmando e instalando con xtool")
    cmd = ["xtool", "install", ipa] + (["--udid", udid] if udid else [])
    result = run(cmd, capture_output=True, text=True, check=False)
    if "Successfully installed" not in (result.stdout or ""):
        sys.exit(f"error: xtool no pudo instalar la app:\n{(result.stdout or '')[-2000:]}{result.stderr or ''}")


def installed_app(bundle_id):
    """(bundle id real, ruta en el iPhone). Con cuenta gratis xtool antepone XTL-<team>."""
    apps = json.loads(pmd3("apps", "list", "-t", "User"))
    for real_id, info in apps.items():
        if real_id == bundle_id or real_id.endswith("." + bundle_id):
            return real_id, info["Path"]
    sys.exit(f"error: la app {bundle_id} no está instalada en el iPhone")


def launch(bundle_id):
    """Abre la app sin debugger (release/profile, o frameworks sin JIT)."""
    ensure_developer_image()
    real_id, _ = installed_app(bundle_id)
    log(f"Abriendo {real_id}")
    run(["pymobiledevice3", "developer", "dvt", "launch", real_id], capture_output=True, text=True)


def stream_logs(process_name="Runner", match=None):
    cmd = ["pymobiledevice3", "syslog", "live", "-pn", process_name] + (["-m", match] if match else [])
    subprocess.run(cmd, env=config.tool_env())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DebugSession:
    """Lanza la app bajo lldb y reenvía puertos del iPhone a 127.0.0.1.

    iOS 17+: debugserver no puede lanzar apps, así que support/core/device_bridge.py
    la lanza suspendida por DVT y support/core/lldb_driver.py se adjunta al pid.

    local_app:    el .app compilado en Linux (para los símbolos)
    bundle_id:    el del proyecto (el instalado puede tener prefijo XTL-<team>)
    lldb_helpers: scripts de lldb a importar (p. ej. el helper JIT de Flutter)
    forward_ports: puertos del iPhone a exponer en el mismo puerto local
    """

    def __init__(self, local_app, bundle_id, udid, lldb_helpers=(), forward_ports=()):
        self.local_app = local_app
        self.bundle_id = bundle_id
        self.udid = udid
        self.lldb_helpers = [str(h) for h in lldb_helpers]
        self.forward_ports = list(forward_ports)
        self.bridge = None
        self.lldb = None
        self.stop_file = None

    def start(self, launch_args):
        ensure_developer_image()
        real_id, remote_app = installed_app(self.bundle_id)
        debug_port, control_port = free_port(), free_port()

        cmd = [config.pymobiledevice3_python(), config.SUPPORT / "core/device_bridge.py", "--udid", self.udid,
               "--debugserver-port", str(debug_port), "--control-port", str(control_port)]
        for port in self.forward_ports:
            cmd += ["--forward", str(port)]
        self.bridge = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, env=config.tool_env())
        self._wait_for(self.bridge, "BRIDGE_READY", 90, "el puente con el iPhone")

        control = socket.create_connection(("127.0.0.1", control_port), timeout=90)
        control.sendall((json.dumps({"cmd": "launch", "bundle_id": real_id, "args": list(launch_args)}) + "\n").encode())
        response = json.loads(control.makefile().readline() or "{}")
        if "pid" not in response:
            control.close()
            sys.exit(f"error: iOS no lanzó la app: {response.get('error', 'sin respuesta')}")

        self.stop_file = tempfile.mktemp(prefix="fil-stop-")
        env = config.tool_env()
        env["FIL_STOP_FILE"] = self.stop_file
        env["FIL_ARGS"] = json.dumps({
            "helpers": self.lldb_helpers, "local_app": str(self.local_app), "pid": response["pid"],
            "debugserver": f"127.0.0.1:{debug_port}", "remote_app": remote_app})
        self.lldb = subprocess.Popen(
            ["lldb", "--batch", "-o", f"command script import {config.SUPPORT / 'core/lldb_driver.py'}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        try:
            self._wait_for(self.lldb, "[lldb] app corriendo", 120, "lldb")
        finally:
            # La sesión DVT mantiene la app suspendida: se libera cuando lldb ya
            # está adjunto (si no, la app arranca sin debugger).
            control.close()

    @staticmethod
    def _wait_for(process, marker, timeout, what):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = process.stdout.readline()
            if not line:
                sys.exit(f"error: {what} terminó antes de tiempo")
            if os.environ.get("FIL_VERBOSE"):
                print(line.rstrip(), file=sys.stderr, flush=True)
            if marker in line:
                return
            if "[lldb] error" in line or "Traceback" in line:
                print(line.rstrip(), file=sys.stderr)
        sys.exit(f"error: {what} no respondió en {timeout}s")

    def wait(self):
        """Muestra la salida de lldb hasta que la app termine."""
        for line in self.lldb.stdout:
            if line.startswith("[lldb]") or os.environ.get("FIL_VERBOSE"):
                print(line.rstrip(), file=sys.stderr, flush=True)
        self.lldb.wait()

    def stop(self):
        if self.lldb and self.lldb.poll() is None:
            open(self.stop_file, "w").close()
            try:
                self.lldb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.lldb.kill()
        if self.bridge and self.bridge.poll() is None:
            self.bridge.terminate()
        if self.stop_file and os.path.exists(self.stop_file):
            os.unlink(self.stop_file)

    def run_until_parent_exits(self, launch_args, on_ready=None):
        """start() + wait(), limpiando si nos matan o si el proceso padre (la
        herramienta del framework, VS Code...) desaparece sin avisar."""

        def terminate(*_):
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        parent = os.getppid()

        def watch_parent():
            while os.getppid() == parent:
                time.sleep(1)
            self.stop()
            os._exit(0)

        threading.Thread(target=watch_parent, daemon=True).start()
        try:
            self.start(launch_args)
            if on_ready:
                on_ready()
            self.wait()
        finally:
            self.stop()
