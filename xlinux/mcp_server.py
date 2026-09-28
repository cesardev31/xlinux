"""MCP server: lets an agent see and drive the iPhone while developing.

    xlinux mcp          (register it in Claude Code: claude mcp add xlinux -- xlinux mcp)

Runs on pymobiledevice3's Python (launched by `xlinux mcp`) and keeps the
tunnel to the iPhone and the CoreDevice touch session open, so actions don't
reopen connections (through the CLI each screenshot took ~3 s).

Coordinates are always normalized 0..1 (x left→right, y top→bottom),
independent of the iPhone model and the screenshot size. Meant for apps under
development: the agent can touch anything on the phone.
"""

import asyncio
import contextlib
import io
import os
import signal
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage
from pymobiledevice3.cli.developer.core_device import (
    _NAMED_BUTTONS,
    ButtonName,
    _do_drag,
    _do_tap,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.core_device.app_service import AppServiceService
from pymobiledevice3.remote.core_device.hid_service import (
    ASCII_TO_HID,
    DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
    HID_BUTTON_STATE_DOWN,
    HID_BUTTON_STATE_UP,
    KEY_LEFT_SHIFT,
    IndigoHIDService,
    touch_session,
)
from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.os_trace import OsTraceService
from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid
from pymobiledevice3.usbmux import list_devices

HID_MAX = 65535

server = MCPServer(
    name="xlinux",
    instructions=(
        "Controls a real iPhone connected over USB to Linux (xlinux). Coordinates "
        "are normalized 0..1 (x left→right, y top→bottom). Typical loop: "
        "screenshot → tap/swipe/type_text (they return a screenshot after the "
        "action) → flutter_hot_reload after editing Dart code → screenshot to "
        "verify. Use it only with apps under development: never make purchases, "
        "payments, deletions or send messages without the user's confirmation."
    ),
)


class Phone:
    """Persistent connection: RSD tunnel (tunneld if available, otherwise
    userspace) and an open touch session. Reopens itself if the iPhone
    disconnects."""

    def __init__(self):
        self.stack = None
        self.rsd = None
        self.udid = None
        self.touch = None
        self.keyboard = None
        self.lock = asyncio.Lock()

    async def _open(self):
        devices = [d for d in await list_devices() if d.connection_type == "USB"]
        if not devices:
            raise RuntimeError("No iPhone connected over USB (is it unlocked, and did you tap 'Trust'?)")
        self.udid = devices[0].serial
        self.stack = contextlib.AsyncExitStack()
        rsd = None
        with contextlib.suppress(Exception):
            rsd = await get_tunneld_device_by_udid(self.udid)
        if rsd is not None:
            self.stack.push_async_callback(rsd.close)
        else:
            rsd = await self.stack.enter_async_context(UserspaceRsdTunnel(serial=self.udid))
        self.rsd = rsd

    async def close(self):
        if self.stack:
            with contextlib.suppress(Exception):
                await self.stack.aclose()
        self.stack = self.rsd = self.touch = self.keyboard = None

    async def run(self, action):
        """Run `action(self)`; if the connection dropped, reopen it and retry once."""
        async with self.lock:
            for attempt in (1, 2):
                try:
                    if self.rsd is None:
                        await self._open()
                    return await action(self)
                except Exception:
                    await self.close()
                    if attempt == 2:
                        raise

    async def touchscreen(self):
        if self.touch is None:
            self.touch = await self.stack.enter_async_context(touch_session(self.rsd))
        return self.touch


phone = Phone()


def _hid(value):
    if not 0.0 <= value <= 1.0:
        raise ValueError("coordinates must be between 0 and 1")
    return round(value * HID_MAX)


async def _capture(scale):
    async def action(p):
        async with ScreenCaptureService(p.rsd) as service:
            return (await service.capture_screenshot())["image"]

    png = await phone.run(action)
    image = PILImage.open(io.BytesIO(png)).convert("RGB")
    if scale and scale < 1:
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=80)
    return Image(data=out.getvalue(), format="jpeg")


@server.tool()
async def screenshot(scale: float = 0.5) -> Image:
    """Capture the iPhone screen. `scale` shrinks the image (0.5 = half) to save
    tokens; action coordinates are 0..1, not pixels."""
    return await _capture(scale)


async def _after(screenshot_after, wait, scale):
    if not screenshot_after:
        return "ok"
    await asyncio.sleep(wait)  # let the animation finish
    return await _capture(scale)


@server.tool()
async def tap(x: float, y: float, screenshot_after: bool = True, wait: float = 0.8,
              scale: float = 0.5) -> Image | str:
    """Tap at (x, y), normalized 0..1. By default returns a screenshot taken
    `wait` seconds later."""
    hx, hy = _hid(x), _hid(y)

    async def action(p):
        await _do_tap(await p.touchscreen(), hx, hy, tsid=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN)

    await phone.run(action)
    return await _after(screenshot_after, wait, scale)


@server.tool()
async def swipe(x1: float, y1: float, x2: float, y2: float, duration: float = 0.4,
                screenshot_after: bool = True, wait: float = 0.8, scale: float = 0.5) -> Image | str:
    """Drag a finger from (x1, y1) to (x2, y2) (0..1). To scroll a list down:
    from y=0.7 to y=0.3."""
    points = [_hid(v) for v in (x1, y1, x2, y2)]

    async def action(p):
        await _do_drag(await p.touchscreen(), *points, steps=30, duration=duration,
                       tsid=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN)

    await phone.run(action)
    return await _after(screenshot_after, wait, scale)


@server.tool()
async def type_text(text: str, screenshot_after: bool = False, scale: float = 0.5) -> Image | str:
    """Type ASCII text into the focused field (tap it first). No accents or non-ASCII characters."""
    unsupported = sorted({ch for ch in text if ch not in ASCII_TO_HID})
    if unsupported:
        raise ValueError(f"unsupported characters: {''.join(unsupported)!r} (ASCII only)")

    async def action(p):
        svc = await p.touchscreen()
        if p.keyboard is None:
            p.keyboard = await svc.create_keyboard_service()
        for ch in text:
            usage, shift = ASCII_TO_HID[ch]
            await svc.send_keyboard(p.keyboard, (KEY_LEFT_SHIFT, usage) if shift else (usage,))
            await asyncio.sleep(0.04)
            await svc.send_keyboard(p.keyboard, ())
            await asyncio.sleep(0.02)

    await phone.run(action)
    return await _after(screenshot_after, 0.5, scale)


@server.tool()
async def press_button(name: str) -> str:
    """Press a hardware button: home, lock, volume-up, volume-down, mute."""
    button = ButtonName(name)
    page, code, hold = _NAMED_BUTTONS[button]

    async def action(p):
        async with IndigoHIDService(p.rsd) as hid:
            await hid.send_button(page, code, HID_BUTTON_STATE_DOWN)
            await asyncio.sleep(hold)
            await hid.send_button(page, code, HID_BUTTON_STATE_UP)
            await asyncio.sleep(0.1)

    await phone.run(action)
    return f"pressed {name}"


def _app_summary(app):
    return {"bundle_id": app.get("bundleIdentifier"), "name": app.get("name"),
            "version": app.get("version")}


@server.tool()
async def list_apps() -> list[dict]:
    """User-installed apps (development apps carry an XTL-<team>. prefix with a free Apple ID)."""
    async def action(p):
        async with AppServiceService(p.rsd) as apps:
            return await apps.list_apps(include_hidden_apps=False, include_internal_apps=False,
                                        include_default_apps=False, include_app_clips=False)

    return [_app_summary(a) for a in await phone.run(action)]


@server.tool()
async def launch_app(bundle_id: str, screenshot_after: bool = True, wait: float = 2.0,
                     scale: float = 0.5) -> Image | str:
    """Launch (or restart) an installed app. Accepts the project's bundle ID even
    when it's installed with an XTL-<team>. prefix (free Apple ID). A Flutter app
    in debug mode only starts correctly from `flutter run`/VS Code; use this for
    release builds."""
    async def action(p):
        async with AppServiceService(p.rsd) as apps:
            installed = await apps.list_apps(include_hidden_apps=False, include_internal_apps=False,
                                             include_default_apps=False, include_app_clips=False)
            ids = [a["bundleIdentifier"] for a in installed]
            real = next((i for i in ids if i == bundle_id or i.endswith("." + bundle_id)), None)
            if real is None:
                raise ValueError(f"{bundle_id} is not installed")
            await apps.launch_application(real, kill_existing=True)

    await phone.run(action)
    return await _after(screenshot_after, wait, scale)


def _flutter_runs():
    """Live `flutter run` processes: [(pid, cwd, start time)]."""
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            if not any(a.endswith(b"flutter_tools.snapshot") for a in argv) or b"run" not in argv:
                continue
            if b"--machine" in argv:  # VS Code: reloads by itself on save
                continue
            found.append((int(proc.name), os.readlink(proc / "cwd"), (proc / "stat").stat().st_mtime))
        except OSError:
            continue
    return found


@server.tool()
async def flutter_hot_reload(restart: bool = False, project_dir: str = "",
                             screenshot_after: bool = True, wait: float = 1.5,
                             scale: float = 0.5) -> Image | str:
    """Hot reload (or hot restart with restart=True) the `flutter run` running in
    a terminal (sends it SIGUSR1/SIGUSR2, like the r/R keys). If there are several,
    use `project_dir` to pick one. Not needed in VS Code: it reloads on save."""
    runs = _flutter_runs()
    if project_dir:
        target = str(Path(project_dir).expanduser().resolve())
        runs = [r for r in runs if r[1] == target or r[1].startswith(target + "/")]
    if not runs:
        raise RuntimeError("No `flutter run` found running in a terminal (VS Code reloads on save).")
    pid, cwd, _ = max(runs, key=lambda r: r[2])
    os.kill(pid, signal.SIGUSR2 if restart else signal.SIGUSR1)
    kind = "hot restart" if restart else "hot reload"
    result = await _after(screenshot_after, wait, scale)
    return result if screenshot_after else f"{kind} sent to flutter run (pid {pid}, {cwd})"


@server.tool()
async def device_logs(seconds: float = 3.0, contains: str = "flutter", process: str = "Runner",
                      max_lines: int = 80) -> str:
    """Read the iPhone log for `seconds` seconds, filtered by process and text
    (Flutter's print() shows up as 'flutter: …'). Useful after reproducing a bug."""
    lines = []

    async def collect():
        lockdown = await create_using_usbmux()
        async with OsTraceService(lockdown) as trace:
            async for entry in trace.syslog():
                name = Path(getattr(entry, "filename", "") or "").name
                message = str(getattr(entry, "message", ""))
                if process and process not in name:
                    continue
                if contains and contains.lower() not in message.lower():
                    continue
                lines.append(message)

    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
        await asyncio.wait_for(collect(), timeout=seconds)
    lines = lines[-max_lines:]
    return "\n".join(lines) if lines else f"(no {process!r} lines containing {contains!r} in {seconds} s)"


def main():
    server.run("stdio")


if __name__ == "__main__":
    main()
