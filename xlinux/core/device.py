"""Todo lo que habla con el iPhone: detectar, instalar, lanzar y depurar."""

import base64
import hashlib
import json
import os
import plistlib
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

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


INSTALL_TIMEOUT = 240


def _ipa_bundle_id(path):
    """Bundle ID de un .ipa o de una carpeta .app."""
    path = Path(path)
    if path.is_dir():
        return plistlib.loads((path / "Info.plist").read_bytes())["CFBundleIdentifier"]
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.count("/") == 2 and n.endswith(".app/Info.plist"))
        return plistlib.loads(z.read(name))["CFBundleIdentifier"]


# Con cuenta gratis el certificado dura 7 días: pasado este margen se reinstala
# (y xtool vuelve a firmar) aunque la app no haya cambiado.
REINSTALL_AFTER = 5 * 24 * 3600


def _content_hash(path):
    """Huella del contenido de un .app (o .ipa), para no reinstalar lo mismo."""
    path = Path(path)
    h = hashlib.sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    for f in files:
        h.update(str(f.relative_to(path) if path.is_dir() else f.name).encode())
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _xtool_install(ipa, udid):
    cmd = ["xtool", "install", ipa] + (["--udid", udid] if udid else [])
    try:
        result = run(cmd, capture_output=True, text=True, check=False, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    out = (result.stdout or "") + (result.stderr or "")
    return "Successfully installed" in out, out


def running_pids(bundle_id):
    """PIDs de la app en el iPhone (DVT proclist, emparejando por la carpeta del .app)."""
    try:
        _, remote_app = installed_app(bundle_id)
    except SystemExit:
        return []
    container = remote_app.removeprefix("/private").rsplit("/", 1)[0]
    try:
        procs = json.loads(pmd3("developer", "dvt", "proclist", check=False) or "[]")
    except ValueError:
        return []
    return [p["pid"] for p in procs if str(p.get("realAppName", "")).startswith(container)]


def terminate(bundle_id):
    """Cierra las instancias vivas de la app. Una app de debug que quedó sin
    debugger (congelada en una parada del JIT) traba al instalador de iOS."""
    for pid in running_pids(bundle_id):
        log(f"Cerrando la instancia anterior de la app (pid {pid})")
        run(["pymobiledevice3", "developer", "dvt", "kill", str(pid)], capture_output=True, text=True, check=False)


def install(ipa, udid=None):
    """Instala un .ipa o una carpeta .app (xtool acepta ambos; el .app evita
    comprimir). Si es idéntica a la última instalada en este iPhone y sigue
    ahí, no se reinstala."""
    ipa = Path(ipa)
    bundle_id = _ipa_bundle_id(ipa)
    # Uno por iPhone y app (no por carpeta de build): instalar release y luego
    # debug (o al revés) tiene que reinstalar aunque cada build no haya cambiado.
    stamp = config.data_dir() / "installed" / (udid or "default") / f"{bundle_id}.json"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    digest = _content_hash(ipa)
    try:
        last = json.loads(stamp.read_text())
    except (OSError, ValueError):
        last = {}
    if (last.get("hash") == digest and last.get("udid") == udid
            and time.time() - last.get("time", 0) < REINSTALL_AFTER):
        try:
            installed_app(bundle_id)
            log("La app no cambió desde la última instalación: no se reinstala")
            return
        except SystemExit:
            pass  # ya no está en el iPhone: instalar
    ensure_developer_image()
    terminate(bundle_id)
    log("Firmando e instalando con xtool")
    ok, out = _xtool_install(ipa, udid)
    if ok:
        stamp.write_text(json.dumps({"hash": digest, "udid": udid, "time": time.time()}))
        return
    if out == "timeout":
        # Visto en la práctica: si una instalación se corta a la mitad, iOS queda
        # trabado con ese bundle ID y xtool espera para siempre al subir la app.
        # Desinstalarla (solo esa; se pierden sus datos locales) lo destraba.
        log(f"La instalación se trabó; desinstalando {bundle_id} del iPhone y reintentando")
        try:
            real_id, _ = installed_app(bundle_id)
            run(["pymobiledevice3", "apps", "uninstall", real_id], capture_output=True, text=True, check=False)
        except SystemExit:
            pass
        ok, out = _xtool_install(ipa, udid)
        if ok:
            stamp.write_text(json.dumps({"hash": digest, "udid": udid, "time": time.time()}))
            return
    sys.exit(f"error: xtool no pudo instalar la app:\n{out[-2000:]}")


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


def _visual_command(udid, *args):
    """Comando CoreDevice fijado al iPhone indicado y con túnel userspace."""
    env = config.tool_env()
    env["PYMOBILEDEVICE3_UDID"] = udid
    return [config.pymobiledevice3_python(), "-m", "pymobiledevice3", *args, "--userspace"], env


def screenshot(path, udid):
    """Captura la pantalla completa del iPhone como PNG."""
    ensure_developer_image()
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd, env = _visual_command(
        udid, "developer", "core-device", "screen-capture", "screenshot", path
    )
    run(cmd, env=env, capture_output=True, text=True)
    if not path.exists() or path.stat().st_size == 0:
        sys.exit("error: pymobiledevice3 no produjo la captura de pantalla")
    return path


def screenshot_base64(udid):
    """Captura PNG codificada para el protocolo de custom devices de Flutter."""
    with tempfile.TemporaryDirectory(prefix="xlinux-screenshot-") as directory:
        path = screenshot(Path(directory) / "iphone.png", udid)
        return base64.b64encode(path.read_bytes()).decode("ascii")


def screenshot_info(path, udid):
    """Captura y devuelve metadatos pequeños para consumidores automáticos."""
    path = screenshot(path, udid)
    with path.open("rb") as image:
        header = image.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit("error: la captura no es un PNG válido")
    width, height = struct.unpack(">II", header[16:24])
    return {"ok": True, "path": str(path), "format": "png", "width": width, "height": height}


def _hid_coordinate(value):
    if not 0.0 <= value <= 1.0:
        raise ValueError("las coordenadas deben estar entre 0 y 1")
    return round(value * 65535)


def agent_input(udid, action, values=(), duration=0.3):
    """Ejecuta una acción determinista para agentes usando coordenadas 0..1."""
    ensure_developer_image()
    if action == "tap":
        x, y = (_hid_coordinate(float(v)) for v in values)
        command = ["developer", "core-device", "universal-hid-service", "tap", str(x), str(y)]
    elif action == "swipe":
        x1, y1, x2, y2 = (_hid_coordinate(float(v)) for v in values)
        # drag genera contacto real; el comando swipe de CoreDevice sólo mueve el puntero.
        command = ["developer", "core-device", "universal-hid-service", "drag",
                   str(x1), str(y1), str(x2), str(y2), "--duration", str(duration)]
    elif action == "type":
        command = ["developer", "core-device", "universal-hid-service", "type", str(values[0])]
    elif action == "button":
        command = ["developer", "core-device", "hid", "button", str(values[0])]
    else:
        raise ValueError(f"acción desconocida: {action}")
    cmd, env = _visual_command(udid, *command)
    run(cmd, env=env, capture_output=True, text=True)
    return {"ok": True, "action": action}


def mirror(udid, mode="web", bind="127.0.0.1", port=None, password=None,
           audio=False, share_clipboard=False):
    """Sirve la pantalla del iPhone por navegador o VNC, con control HID."""
    ensure_developer_image()
    if mode not in ("web", "vnc"):
        raise ValueError(f"modo de mirror desconocido: {mode}")
    if bind not in ("127.0.0.1", "localhost", "::1") and not password:
        sys.exit("error: usa --password al publicar el mirror fuera de localhost")
    port = port or (8080 if mode == "web" else 5901)
    port_option = "--http-port" if mode == "web" else "--port"
    command = ["developer", "core-device", "display", f"serve-{mode}",
               "--bind", bind, port_option, str(port)]
    if password:
        command += ["--password", password]
    if mode == "vnc":
        if audio:
            command.append("--audio")
        if share_clipboard:
            command.append("--share-clipboard")
    cmd, env = _visual_command(udid, *command)
    protocol = "http" if mode == "web" else "vnc"
    log(f"Pantalla del iPhone en {protocol}://{bind}:{port} (Ctrl+C para salir)")
    return subprocess.run(cmd, env=env).returncode


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
        bridge_info = self._wait_for(self.bridge, "BRIDGE_READY", 90, "el puente con el iPhone")
        debugserver = bridge_info.get("DEBUGSERVER", f"127.0.0.1:{debug_port}")
        log(f"Túnel con el iPhone: {bridge_info.get('TUNNEL', '?')}")

        control = socket.create_connection(("127.0.0.1", control_port), timeout=90)
        control.sendall((json.dumps({"cmd": "launch", "bundle_id": real_id, "args": list(launch_args)}) + "\n").encode())
        response = json.loads(control.makefile().readline() or "{}")
        if "pid" not in response:
            control.close()
            sys.exit(f"error: iOS no lanzó la app: {response.get('error', 'sin respuesta')}")

        self.stop_file = tempfile.mktemp(prefix="xlinux-stop-")
        env = config.tool_env()
        env["XLINUX_STOP_FILE"] = self.stop_file
        env["XLINUX_ARGS"] = json.dumps({
            "helpers": self.lldb_helpers, "local_app": str(self.local_app), "pid": response["pid"],
            "debugserver": debugserver, "remote_app": remote_app})
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
        """Lee la salida hasta `marker`; devuelve las líneas CLAVE=valor vistas."""
        info = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = process.stdout.readline()
            if not line:
                sys.exit(f"error: {what} terminó antes de tiempo")
            if os.environ.get("XLINUX_VERBOSE"):
                print(line.rstrip(), file=sys.stderr, flush=True)
            key, sep, value = line.strip().partition("=")
            if sep and key.isupper():
                info[key] = value
            if marker in line:
                return info
            if "[lldb] error" in line or "Traceback" in line:
                print(line.rstrip(), file=sys.stderr)
        sys.exit(f"error: {what} no respondió en {timeout}s")

    def wait(self):
        """Muestra la salida de lldb hasta que la app termine."""
        for line in self.lldb.stdout:
            if line.startswith("[lldb]") or os.environ.get("XLINUX_VERBOSE"):
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
            # lldb no siempre logra matarla: sin debugger una app de debug queda
            # congelada en la siguiente parada del JIT y traba la próxima instalación.
            terminate(self.bundle_id)

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
