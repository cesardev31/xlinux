"""Todo lo que habla con el iPhone: detectar, instalar, lanzar y depurar."""

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from . import config
from .util import log, output, run

# Opciones del engine que decidimos nosotros (el VM Service se reenvía por USB).
_RESERVED_ENGINE_OPTIONS = ("--vm-service-port", "--vm-service-host", "--observatory-port")


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


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DebugSession:
    """Lanza la app bajo lldb (necesario para el JIT de Dart en iOS) y deja el
    Dart VM Service accesible en 127.0.0.1.

    iOS 17+: debugserver no puede lanzar apps, así que support/device_bridge.py
    la lanza suspendida por DVT y support/lldb_driver.py se adjunta al pid.
    """

    def __init__(self, project, udid):
        self.project = project
        self.udid = udid
        self.vm_port = _free_port()
        self.bridge = None
        self.lldb = None
        self.stop_file = None

    def start(self, engine_options):
        ensure_developer_image()
        bundle_id, remote_app = installed_app(self.project.bundle_identifier())
        debug_port, control_port = _free_port(), _free_port()

        self.bridge = subprocess.Popen(
            [config.pymobiledevice3_python(), config.SUPPORT / "device_bridge.py", "--udid", self.udid,
             "--debugserver-port", str(debug_port), "--vm-service-port", str(self.vm_port),
             "--control-port", str(control_port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=config.tool_env())
        self._wait_for(self.bridge, "BRIDGE_READY", 90, "el puente con el iPhone")

        args = [o for o in engine_options if not o.startswith(_RESERVED_ENGINE_OPTIONS)]
        args += [f"--vm-service-port={self.vm_port}", "--disable-service-auth-codes"]
        control = socket.create_connection(("127.0.0.1", control_port), timeout=90)
        control.sendall((json.dumps({"cmd": "launch", "bundle_id": bundle_id, "args": args}) + "\n").encode())
        response = json.loads(control.makefile().readline() or "{}")
        if "pid" not in response:
            control.close()
            sys.exit(f"error: iOS no lanzó la app: {response.get('error', 'sin respuesta')}")

        self.stop_file = tempfile.mktemp(prefix="fil-stop-")
        env = config.tool_env()
        env["FIL_STOP_FILE"] = self.stop_file
        env["FIL_ARGS"] = json.dumps([
            str(config.SUPPORT / "flutter_lldb_helper.py"), str(self.project.app),
            str(response["pid"]), f"127.0.0.1:{debug_port}", remote_app])
        self.lldb = subprocess.Popen(
            ["lldb", "--batch", "-o", f"command script import {config.SUPPORT / 'lldb_driver.py'}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        try:
            self._wait_for(self.lldb, "[lldb] app corriendo", 120, "lldb")
        finally:
            # La sesión DVT mantiene la app suspendida: se libera cuando lldb ya
            # está adjunto, si no Flutter arranca sin debugger.
            control.close()
        return f"http://127.0.0.1:{self.vm_port}/"

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


def run_debug(project, engine_options, udid):
    """Lanza en debug e imprime la línea que buscan `flutter run` / `flutter attach`."""
    session = DebugSession(project, udid)

    def terminate(*_):
        session.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)

    # Si `flutter` (o VS Code) muere sin avisarnos, no dejar lldb ni el puente
    # huérfanos: al quedar huérfanos nos adopta otro proceso y cambia el ppid.
    parent = os.getppid()

    def watch_parent():
        while os.getppid() == parent:
            time.sleep(1)
        session.stop()
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        url = session.start(shlex.split(engine_options) if isinstance(engine_options, str) else engine_options)
        print(f"The Dart VM service is listening on {url}", flush=True)
        session.wait()
    finally:
        session.stop()


def launch_release(project):
    """Abre la app (release/profile no necesitan debugger) y muestra sus logs."""
    ensure_developer_image()
    bundle_id, _ = installed_app(project.bundle_identifier())
    log(f"Abriendo {bundle_id}")
    run(["pymobiledevice3", "developer", "dvt", "launch", bundle_id], capture_output=True, text=True)
    log("Logs de la app (Ctrl+C para salir)")
    subprocess.run(["pymobiledevice3", "syslog", "live", "-pn", "Runner", "-m", "flutter"],
                   env=config.tool_env())
