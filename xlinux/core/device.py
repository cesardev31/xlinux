"""Everything that talks to the iPhone: detect, install, launch, capture and debug."""

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
import urllib.request
import zipfile
from pathlib import Path

from . import config
from .util import log, output, run


def pmd3(*args, **kw):
    return output(["pymobiledevice3", *args], **kw)


def connected_devices():
    """[(udid, name)] of the iPhones connected over USB."""
    try:
        devices = json.loads(pmd3("usbmux", "list", check=False) or "[]")
    except ValueError:
        return []
    return [(d["Identifier"], d.get("DeviceName", "iPhone")) for d in devices
            if d.get("ConnectionType") == "USB"]


def first_device():
    devices = connected_devices()
    if not devices:
        sys.exit("error: no iPhone connected over USB (is it unlocked, and did you tap 'Trust'?)")
    return devices[0]


def ensure_developer_image():
    """The Developer Disk Image (debugserver, DVT) gets unmounted on every reboot."""
    mounted = pmd3("mounter", "list", check=False)
    if '"DeveloperDiskImage"' not in mounted:
        log("Mounting the Developer Disk Image")
        run(["pymobiledevice3", "mounter", "auto-mount"], cwd=config.data_dir() / "tmp",
            capture_output=True, text=True)


INSTALL_TIMEOUT = 240


def _ipa_bundle_id(path):
    """Bundle ID of an .ipa or a .app directory."""
    path = Path(path)
    if path.is_dir():
        return plistlib.loads((path / "Info.plist").read_bytes())["CFBundleIdentifier"]
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.count("/") == 2 and n.endswith(".app/Info.plist"))
        return plistlib.loads(z.read(name))["CFBundleIdentifier"]


# With a free Apple ID the certificate lasts 7 days: past this margin the app is
# reinstalled (and re-signed by xtool) even if it didn't change.
REINSTALL_AFTER = 5 * 24 * 3600


def _content_hash(path):
    """Content fingerprint of a .app (or .ipa), to avoid reinstalling the same thing."""
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


def _container(path):
    """/var/containers/Bundle/Application/<UUID> of an app or process path."""
    parts = path.removeprefix("/private").split("/")
    return "/".join(parts[:6]) if parts[1:5] == ["var", "containers", "Bundle", "Application"] else None


def running_pids(bundle_id):
    """PIDs of the app on the iPhone (DVT proclist, matched by the .app
    directory), plus orphans: instances of an app with the same .app name
    whose install was replaced since they started. A debug one left behind
    keeps a debugserver attached and makes the next lldb attach fail ("a
    process is already being debugged")."""
    try:
        apps = json.loads(pmd3("apps", "list", "-t", "Any"))
        procs = json.loads(pmd3("developer", "dvt", "proclist", check=False) or "[]")
    except ValueError:
        return []
    mine = [info["Path"] for real_id, info in apps.items()
            if real_id == bundle_id or real_id.endswith("." + bundle_id)]
    if not mine:
        return []
    container, app_dir = _container(mine[0]), mine[0].rstrip("/").rsplit("/", 1)[1]
    installed = {_container(info["Path"]) for info in apps.values() if info.get("Path")}
    pids = []
    for p in procs:
        path = str(p.get("realAppName", ""))
        c = _container(path)
        if not c:
            continue
        orphan = c not in installed and path.removeprefix("/private").startswith(f"{c}/{app_dir}/")
        if c == container or orphan:
            pids.append(p["pid"])
    return pids


def terminate(bundle_id):
    """Kill the app's running instances. A debug app left without its debugger
    (frozen at a JIT stop) hangs the iOS installer."""
    for pid in running_pids(bundle_id):
        log(f"Stopping the previous app instance (pid {pid})")
        run(["pymobiledevice3", "developer", "dvt", "kill", str(pid)], capture_output=True, text=True, check=False)


def install(ipa, udid=None):
    """Install an .ipa or a .app directory (xtool accepts both; a .app avoids
    compressing). If it's identical to the last one installed on this iPhone
    and still there, it isn't reinstalled."""
    ipa = Path(ipa)
    bundle_id = _ipa_bundle_id(ipa)
    # One record per iPhone and app (not per build directory): installing
    # release then debug (or vice versa) must reinstall even if neither changed.
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
            log("The app didn't change since the last install: skipping reinstall")
            return
        except SystemExit:
            pass  # no longer on the iPhone: install it
    ensure_developer_image()
    terminate(bundle_id)
    log("Signing and installing with xtool")
    ok, out = _xtool_install(ipa, udid)
    if ok:
        stamp.write_text(json.dumps({"hash": digest, "udid": udid, "time": time.time()}))
        return
    if out == "timeout":
        # Seen in practice: if an install is interrupted halfway, iOS gets stuck
        # on that bundle ID and xtool waits forever uploading the app.
        # Uninstalling it (only that app; its local data is lost) unsticks it.
        log(f"The install got stuck; uninstalling {bundle_id} from the iPhone and retrying")
        try:
            real_id, _ = installed_app(bundle_id)
            run(["pymobiledevice3", "apps", "uninstall", real_id], capture_output=True, text=True, check=False)
        except SystemExit:
            pass
        ok, out = _xtool_install(ipa, udid)
        if ok:
            stamp.write_text(json.dumps({"hash": digest, "udid": udid, "time": time.time()}))
            return
    sys.exit(f"error: xtool could not install the app:\n{out[-2000:]}")


def installed_app(bundle_id):
    """(actual bundle id, path on the iPhone). With a free Apple ID xtool prefixes XTL-<team>."""
    apps = json.loads(pmd3("apps", "list", "-t", "User"))
    for real_id, info in apps.items():
        if real_id == bundle_id or real_id.endswith("." + bundle_id):
            return real_id, info["Path"]
    sys.exit(f"error: {bundle_id} is not installed on the iPhone")


def launch(bundle_id):
    """Launch the app without a debugger (release/profile, or frameworks without a JIT)."""
    ensure_developer_image()
    real_id, _ = installed_app(bundle_id)
    log(f"Launching {real_id}")
    run(["pymobiledevice3", "developer", "dvt", "launch", real_id], capture_output=True, text=True)


def stream_logs(process_name="Runner", match=None):
    cmd = ["pymobiledevice3", "syslog", "live", "-pn", process_name] + (["-m", match] if match else [])
    subprocess.run(cmd, env=config.tool_env())


def _tunneld_has(udid):
    """Is `tunneld` (the kernel tunnel) running with this iPhone?"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:49151/", timeout=2) as r:
            return udid in json.loads(r.read())
    except (OSError, ValueError):
        return False


def _visual_command(udid, *args):
    """CoreDevice command pinned to the given iPhone: over the kernel tunnel if
    `tunneld` is running (much smoother video), otherwise the userspace one."""
    env = config.tool_env()
    env["PYMOBILEDEVICE3_UDID"] = udid
    tunnel = ["--tunnel", udid] if _tunneld_has(udid) else ["--userspace"]
    return [config.pymobiledevice3_python(), "-m", "pymobiledevice3", *args, *tunnel], env


def screenshot(path, udid):
    """Capture the full iPhone screen as a PNG."""
    ensure_developer_image()
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd, env = _visual_command(
        udid, "developer", "core-device", "screen-capture", "screenshot", path
    )
    run(cmd, env=env, capture_output=True, text=True)
    if not path.exists() or path.stat().st_size == 0:
        sys.exit("error: pymobiledevice3 did not produce the screenshot")
    return path


def screenshot_base64(udid):
    """PNG screenshot encoded for Flutter's custom device protocol."""
    with tempfile.TemporaryDirectory(prefix="xlinux-screenshot-") as directory:
        path = screenshot(Path(directory) / "iphone.png", udid)
        return base64.b64encode(path.read_bytes()).decode("ascii")


def screenshot_info(path, udid):
    """Capture and return small metadata for automated consumers."""
    path = screenshot(path, udid)
    with path.open("rb") as image:
        header = image.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit("error: the screenshot is not a valid PNG")
    width, height = struct.unpack(">II", header[16:24])
    return {"ok": True, "path": str(path), "format": "png", "width": width, "height": height}


def _hid_coordinate(value):
    if not 0.0 <= value <= 1.0:
        raise ValueError("coordinates must be between 0 and 1")
    return round(value * 65535)


def agent_input(udid, action, values=(), duration=0.3):
    """Run a deterministic agent action using 0..1 coordinates."""
    ensure_developer_image()
    if action == "tap":
        x, y = (_hid_coordinate(float(v)) for v in values)
        command = ["developer", "core-device", "universal-hid-service", "tap", str(x), str(y)]
    elif action == "swipe":
        x1, y1, x2, y2 = (_hid_coordinate(float(v)) for v in values)
        # drag makes real touch contact; CoreDevice's swipe only moves the pointer.
        command = ["developer", "core-device", "universal-hid-service", "drag",
                   str(x1), str(y1), str(x2), str(y2), "--duration", str(duration)]
    elif action == "type":
        command = ["developer", "core-device", "universal-hid-service", "type", str(values[0])]
    elif action == "button":
        command = ["developer", "core-device", "hid", "button", str(values[0])]
    else:
        raise ValueError(f"unknown action: {action}")
    cmd, env = _visual_command(udid, *command)
    run(cmd, env=env, capture_output=True, text=True)
    return {"ok": True, "action": action}


def mirror(udid, mode="web", bind="127.0.0.1", port=None, password=None,
           audio=False, share_clipboard=False):
    """Serve the iPhone screen over a browser or VNC, with HID control."""
    ensure_developer_image()
    if mode not in ("web", "vnc"):
        raise ValueError(f"unknown mirror mode: {mode}")
    if bind not in ("127.0.0.1", "localhost", "::1") and not password:
        sys.exit("error: use --password when exposing the mirror beyond localhost")
    port = port or (8080 if mode == "web" else 5901)
    port_option = "--http-port" if mode == "web" else "--port"
    command = ["developer", "core-device", "display", f"serve-{mode}",
               "--bind", bind, port_option, str(port)]
    if password:
        command += ["--password", password]
    if mode == "web":
        # CoreDevice audio is AAC-ELD, whose decoder only exists on macOS.
        command.append("--no-audio")
    if mode == "vnc":
        if audio:
            command.append("--audio")
        if share_clipboard:
            command.append("--share-clipboard")
    cmd, env = _visual_command(udid, *command)
    protocol = "http" if mode == "web" else "vnc"
    log(f"iPhone screen at {protocol}://{bind}:{port} (Ctrl+C to quit)")
    process = subprocess.Popen(cmd, env=env)
    try:
        return process.wait()
    except KeyboardInterrupt:
        # Ctrl+C also reaches the server, which shuts down on its own: wait for it.
        try:
            return process.wait(timeout=15)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            process.kill()
            return 130


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DebugSession:
    """Launch the app under lldb and forward iPhone ports to 127.0.0.1.

    iOS 17+: debugserver can't launch apps, so support/core/device_bridge.py
    launches it suspended through DVT and support/core/lldb_driver.py attaches
    to the pid.

    local_app:     the .app built on Linux (for symbols)
    bundle_id:     the project's (the installed one may have an XTL-<team> prefix)
    lldb_helpers:  lldb scripts to import (e.g. Flutter's JIT helper)
    forward_ports: iPhone ports exposed on the same local port
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
        # Instances left from a previous session (install() skips this when
        # the app didn't change) would still have a debugserver attached.
        terminate(self.bundle_id)
        real_id, remote_app = installed_app(self.bundle_id)
        debug_port, control_port = free_port(), free_port()

        cmd = [config.pymobiledevice3_python(), config.SUPPORT / "core/device_bridge.py", "--udid", self.udid,
               "--debugserver-port", str(debug_port), "--control-port", str(control_port)]
        for port in self.forward_ports:
            cmd += ["--forward", str(port)]
        self.bridge = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, env=config.tool_env())
        bridge_info = self._wait_for(self.bridge, "BRIDGE_READY", 90, "the iPhone bridge")
        debugserver = bridge_info.get("DEBUGSERVER", f"127.0.0.1:{debug_port}")
        log(f"iPhone tunnel: {bridge_info.get('TUNNEL', '?')}")

        control = socket.create_connection(("127.0.0.1", control_port), timeout=90)
        control.sendall((json.dumps({"cmd": "launch", "bundle_id": real_id, "args": list(launch_args)}) + "\n").encode())
        response = json.loads(control.makefile().readline() or "{}")
        if "pid" not in response:
            control.close()
            sys.exit(f"error: iOS did not launch the app: {response.get('error', 'no response')}")

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
            self._wait_for(self.lldb, "[lldb] app running", 120, "lldb")
        finally:
            # The DVT session keeps the app suspended: release it only once lldb
            # is attached (otherwise the app starts without a debugger).
            control.close()

    @staticmethod
    def _wait_for(process, marker, timeout, what):
        """Read output until `marker`; return the KEY=value lines seen."""
        info = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = process.stdout.readline()
            if not line:
                sys.exit(f"error: {what} exited early")
            if os.environ.get("XLINUX_VERBOSE"):
                print(line.rstrip(), file=sys.stderr, flush=True)
            key, sep, value = line.strip().partition("=")
            if sep and key.isupper():
                info[key] = value
            if marker in line:
                return info
            if "[lldb] error" in line or "Traceback" in line:
                print(line.rstrip(), file=sys.stderr)
        sys.exit(f"error: {what} did not respond within {timeout}s")

    def wait(self):
        """Show lldb's output until the app exits."""
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
            # lldb doesn't always manage to kill it: without a debugger a debug app
            # freezes at the next JIT stop and hangs the next install.
            terminate(self.bundle_id)

    def run_until_parent_exits(self, launch_args, on_ready=None):
        """start() + wait(), cleaning up if we're killed or if the parent process
        (the framework's tool, VS Code...) disappears without notice."""

        def on_signal(*_):
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
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
