"""Install the external tools xlinux needs, into the data directory.

Every `ensure_*` is idempotent: it returns at once if the tool is already
there. `xlinux setup` runs the ones every build needs; the rest run the first
time something needs them (Darling for release builds, cairosvg for vector
assets, the patched macro server for UIKit `#Preview`, the MCP SDK for
`xlinux mcp`). Only system packages need sudo, asked for once per batch.
"""

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import config
from .util import log, output, run

SWIFT_VERSION = "6.4"
SWIFTLY_URL = "https://download.swift.org/swiftly/linux/swiftly-{arch}.tar.gz"
XCODE_DOWNLOAD_URL = "https://developer.apple.com/download/all/?q=Xcode"
# Base commit of support/patches/OpenAppleMacros-preview-uikit.patch.
OPEN_APPLE_MACROS = ("https://github.com/xtool-org/OpenAppleMacros", "e932208f5610a5024d3a043e202f0f67b926e1cf")
# The .deb packages `darling shell` needs (the rest of the release is GUI and scripting runtimes).
DARLING_DEBS = ("darling-core", "darling-system", "darling-cli", "darling-cli-gui-common",
                "darling-cli-python2-common")


def arch():
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine())


def interactive():
    return sys.stdin.isatty() and sys.stderr.isatty()


def _which(name):
    return shutil.which(name, path=config.tool_env()["PATH"])


def download(url, dest):
    """Download `url` to `dest` (kept: an interrupted setup doesn't download it again)."""
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    log(f"Downloading {url.rsplit('/', 1)[-1]}")
    with urllib.request.urlopen(url) as response, open(partial, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total and sys.stderr.isatty():
                print(f"\r    {done >> 20} / {total >> 20} MB", end="", file=sys.stderr, flush=True)
    if total and sys.stderr.isatty():
        print(file=sys.stderr)
    partial.rename(dest)
    return dest


def github_asset(repo, match):
    """(name, url) of the first asset of `repo`'s latest release for which match(name) is true."""
    request = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request) as response:
        release = json.load(response)
    for asset in release.get("assets", []):
        if match(asset["name"]):
            return asset["name"], asset["browser_download_url"]
    sys.exit(f"error: no matching download in the latest {repo} release ({release.get('tag_name')})")


def has_apt():
    return bool(shutil.which("apt-get")) and Path("/etc/debian_version").exists()


def sudo(script, why):
    """Run a shell script as root (the one step that needs it), explaining why first."""
    log(f"{why} (needs sudo):")
    print("\n".join(f"    {line}" for line in script.strip().splitlines()), file=sys.stderr, flush=True)
    run(["sudo", "sh", "-ec", script], env=os.environ.copy())


# --- Always needed (`xlinux setup`) ---

def ensure_uv():
    if _which("uv"):
        return _which("uv")
    triple = f"{arch()}-unknown-linux-gnu"
    name, url = github_asset("astral-sh/uv", lambda n: n == f"uv-{triple}.tar.gz")
    archive = download(url, config.data_dir() / "downloads" / name)
    target = config.data_dir() / "bin"
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if Path(member.name).name in ("uv", "uvx") and member.isfile():
                (target / Path(member.name).name).write_bytes(tar.extractfile(member).read())
                (target / Path(member.name).name).chmod(0o755)
    return str(target / "uv")


def _swiftly_env():
    home = config.data_dir() / "swiftly"
    env = config.tool_env()
    env.update(SWIFTLY_HOME_DIR=str(home), SWIFTLY_BIN_DIR=str(home / "bin"),
               SWIFTLY_TOOLCHAINS_DIR=str(home / "toolchains"), TMPDIR=str(config.data_dir() / "tmp"))
    return env


def ensure_swift():
    """Swift through swiftly, entirely inside the data directory. Returns the
    commands swiftly asks to run as root afterwards (its system packages)."""
    if config.swift_bin():
        return ""
    data = config.data_dir()
    env = _swiftly_env()
    (data / "tmp").mkdir(parents=True, exist_ok=True)
    swiftly = data / "swiftly/bin/swiftly"
    if not swiftly.exists():
        archive = download(SWIFTLY_URL.format(arch=arch()), data / "downloads" / f"swiftly-{arch()}.tar.gz")
        with tempfile.TemporaryDirectory(dir=data / "tmp") as tmp:
            run(["tar", "-xzf", archive, "-C", tmp])
            run([Path(tmp) / "swiftly", "init", "--no-modify-profile", "--skip-install",
                 "--quiet-shell-followup", "--assume-yes"], env=env)
    log(f"Installing Swift {SWIFT_VERSION} (about 1 GB)")
    post_install = data / "tmp/swiftly-post-install.sh"
    post_install.unlink(missing_ok=True)
    run([swiftly, "install", SWIFT_VERSION, "--use", "--assume-yes",
         "--post-install-file", post_install], env=env)
    return post_install.read_text() if post_install.exists() else ""


def llvm_tools_found():
    return bool(config.llvm_tool("lipo") and config.llvm_tool("otool") and config.llvm_tool("install-name-tool"))


def system_packages():
    """Debian/Ubuntu packages still missing: system LLVM (lipo, otool,
    install_name_tool), pdftocairo (PDF assets), unzip and git."""
    missing = []
    if not llvm_tools_found():
        missing.append("llvm")
    for command, package in (("pdftocairo", "poppler-utils"), ("unzip", "unzip"), ("git", "git")):
        if not shutil.which(command):
            missing.append(package)
    return missing


def install_system_packages(packages, swift_post_install=""):
    if not packages and not swift_post_install.strip():
        return
    if not has_apt():
        sys.exit("error: install these with your package manager and run `xlinux setup` again:\n"
                 f"  {' '.join(packages) or '(none)'}\n" + swift_post_install)
    script = f"apt-get install -y {' '.join(packages)}\n" if packages else ""
    sudo(script + swift_post_install, "Installing system packages")


def ensure_xtool():
    """xtool, extracted from its AppImage into <data>/bin/xtool: the AppImage
    needs FUSE, which isn't available when launched from e.g. the Flutter snap."""
    data = config.data_dir()
    wrapper = data / "bin/xtool"
    if wrapper.exists():
        write_xtool_wrapper(wrapper)
        return
    appimage = shutil.which("xtool")
    if not appimage:
        name, url = github_asset("xtool-org/xtool", lambda n: n == f"xtool-{arch()}.AppImage")
        appimage = download(url, data / "downloads" / name)
        appimage.chmod(0o755)
    log("Extracting the xtool AppImage")
    shutil.rmtree(data / "xtool/squashfs-root", ignore_errors=True)
    (data / "xtool").mkdir(parents=True, exist_ok=True)
    run([appimage, "--appimage-extract"], cwd=data / "xtool", capture_output=True, text=True)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    write_xtool_wrapper(wrapper)


XTOOL_WRAPPER = """#!/bin/sh
# xtool extracted from its AppImage: works without FUSE. If a build with
# support/patches/xtool-free-app-groups.patch exists, it is used instead.
dir="$(dirname "$(readlink -f "$0")")/../xtool"
[ -x "$dir/xtool-patched" ] && exec "$dir/xtool-patched" "$@"
exec "$dir/squashfs-root/AppRun" "$@"
"""


def write_xtool_wrapper(wrapper):
    if not wrapper.exists() or wrapper.read_text() != XTOOL_WRAPPER:
        wrapper.write_text(XTOOL_WRAPPER)
        wrapper.chmod(0o755)


def _find_xip():
    places = [config.data_dir() / "downloads", Path.cwd(), Path.home() / "Downloads"]
    xdg = shutil.which("xdg-user-dir") and output(["xdg-user-dir", "DOWNLOAD"], check=False).strip()
    if xdg:
        places.append(Path(xdg))
    found = [xip for place in places if place.is_dir() for xip in place.glob("Xcode*.xip")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def ensure_apple_sdk(xip=None):
    """Sign in to Apple (xtool auth) and extract the iOS/macOS SDKs from Xcode.xip.

    Apple doesn't allow redistributing the SDK: each user downloads Xcode.xip
    with their own Apple ID. The one manual step."""
    if "Logged in" not in output(["xtool", "auth", "status"], check=False):
        log("Sign in with your Apple ID (a free one works); xtool uses it to sign apps")
        run(["xtool", "auth", "login"])
    if config.IPHONE_SDK.exists():
        return
    xip = Path(xip) if xip else _find_xip()
    while not xip:
        print(f"""
  The iOS SDK comes from Xcode, which only Apple can distribute:
    1. Open {XCODE_DOWNLOAD_URL} and sign in with your Apple ID.
    2. Download "Xcode 27" (Xcode_27.xip, ~3 GB). Any folder works:
       {config.data_dir() / 'downloads'} or your Downloads folder are searched.
""", file=sys.stderr)
        if not interactive():
            sys.exit("error: Xcode .xip not found. Download it and run `xlinux setup` again "
                     "(or `xlinux setup --xip PATH`).")
        answer = input("  Path to the .xip (Enter to search again): ").strip()
        xip = Path(answer).expanduser() if answer else _find_xip()
        if xip and not xip.is_file():
            print(f"  {xip} doesn't exist.", file=sys.stderr)
            xip = None
    log(f"Extracting the SDK from {xip.name} (takes a few minutes)")
    run(["xtool", "sdk", "install", xip])


def ensure_pymobiledevice3():
    if config.pymobiledevice3_python().exists():
        return
    log("Installing pymobiledevice3")
    run([ensure_uv(), "tool", "install", "pymobiledevice3"])


def build_compat_shim():
    """Darling compatibility shim (see support/core/darling_compat.c)."""
    shim = config.compat_shim()
    source = config.SUPPORT / "core/darling_compat.c"
    if shim.exists() and shim.stat().st_mtime >= source.stat().st_mtime:
        return
    shim.parent.mkdir(parents=True, exist_ok=True)
    log("Building the Darling compatibility shim")
    run(["clang", "-target", "x86_64-apple-macos11", "-isysroot", config.MACOS_SDK,
         "-B", config.TOOLSET_BIN, "-fuse-ld=lld", "-dynamiclib", "-O2", "-Wall",
         "-o", shim, source], capture_output=True, text=True)


def install_macro_server():
    """Put the patched OpenAppleMacrosServer (see ensure_macro_server) over
    xtool's, keeping the original as .orig. Re-done after an SDK update."""
    patched = config.data_dir() / "bin/OpenAppleMacrosServer"
    target = config.XTOOL_SDK / "OpenAppleMacrosServer"
    if not patched.exists() or not target.exists():
        return
    if target.read_bytes() == patched.read_bytes():
        return
    backup = target.with_name("OpenAppleMacrosServer.orig")
    if not backup.exists():
        shutil.copy2(target, backup)
    log("Installing OpenAppleMacrosServer with UIKit #Preview stubs")
    shutil.copy2(patched, target)


# --- Installed the first time something needs them ---

def ensure_darling():
    """Darling runs macOS command-line tools; only release builds need it
    (Flutter's gen_snapshot for iOS only exists for macOS)."""
    if not _which("darling"):
        if arch() != "x86_64" or not has_apt():
            sys.exit("error: release builds need Darling (https://www.darlinghq.org), which is only "
                     "packaged for x86_64 Debian/Ubuntu. Install it for your distribution, then retry.")
        name, url = github_asset("darlinghq/darling", lambda n: n.startswith("debs") and n.endswith(".zip"))
        archive = download(url, config.data_dir() / "downloads/darling" / name)
        debs_dir = archive.with_suffix("")
        if not debs_dir.exists():
            with zipfile.ZipFile(archive) as z:
                z.extractall(debs_dir)
        debs = []
        for package in DARLING_DEBS:
            found = sorted(debs_dir.rglob(f"{package}_*.deb"))
            if not found:
                sys.exit(f"error: {package} is missing from {name}")
            debs.append(f"'{found[-1]}'")
        sudo(f"apt-get install -y {' '.join(debs)}", "Installing Darling, needed for release builds")
    build_compat_shim()


def ensure_cairosvg():
    """Python with cairosvg, to rasterize SVG assets (support/core/bin/actool)."""
    python = config.data_dir() / "py-tools/bin/python"
    if python.exists() and run([python, "-c", "import cairosvg"], check=False, capture_output=True).returncode == 0:
        return python
    log("Installing cairosvg (SVG assets)")
    uv = ensure_uv()
    # actool's stdout goes to Swift Build: keep uv's output on stderr.
    if not python.exists():
        run([uv, "venv", "-q", python.parent.parent], stdout=sys.stderr)
    run([uv, "pip", "install", "-q", "--python", python, "cairosvg"], stdout=sys.stderr)
    return python


def ensure_mcp_sdk():
    """MCP SDK + Pillow in pymobiledevice3's Python (for `xlinux mcp`).
    stdout belongs to the MCP protocol: everything here goes to stderr."""
    ensure_pymobiledevice3()
    python = config.pymobiledevice3_python()
    if run([python, "-c", "import mcp, PIL"], check=False, capture_output=True).returncode == 0:
        return
    log("Installing the MCP SDK and Pillow")
    run([ensure_uv(), "pip", "install", "-q", "--python", python, "mcp", "pillow"], stdout=sys.stderr)


def ensure_rcodesign():
    """rcodesign (apple-codesign) in <data>/bin: writes the ad-hoc signature
    carrying a target's entitlements, which xtool reads back when signing."""
    found = _which("rcodesign")
    if found:
        return found
    triple = f"{arch()}-unknown-linux-musl"
    name, url = github_asset("indygreg/apple-platform-rs",
                             lambda n: n.startswith("apple-codesign-") and n.endswith(f"{triple}.tar.gz"))
    archive = download(url, config.data_dir() / "downloads" / name)
    target = config.data_dir() / "bin/rcodesign"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        member = next(m for m in tar.getmembers() if Path(m.name).name == "rcodesign" and m.isfile())
        target.write_bytes(tar.extractfile(member).read())
    target.chmod(0o755)
    return str(target)


MACROS_PATCH = config.SUPPORT / "patches/OpenAppleMacros-preview-uikit.patch"
# xtool's OpenAppleMacros only implements SwiftUI's plain `#Preview { }`: the
# UIKit/AppKit, WidgetKit and `arguments:` variants need the patched server.
PREVIEW_IMPORTS = ("import UIKit", "import AppKit", "import WidgetKit")


def needs_patched_macros(dirs):
    """Whether any Swift source under `dirs` uses a `#Preview` variant that
    xtool's OpenAppleMacros doesn't implement (e.g. Stripe, widgets)."""
    for directory in dirs:
        for root, _, files in os.walk(directory, followlinks=True):
            for name in files:
                if not name.endswith(".swift"):
                    continue
                try:
                    text = (Path(root) / name).read_text(errors="ignore")
                except OSError:
                    continue
                if "#Preview" in text and ("arguments:" in text or any(i in text for i in PREVIEW_IMPORTS)):
                    return True
    return False


def macro_server_current():
    """Whether <data>/bin/OpenAppleMacrosServer was built from the current patch."""
    stamp = config.data_dir() / "bin/OpenAppleMacrosServer.patch-sha256"
    try:
        return stamp.read_text().strip() == hashlib.sha256(MACROS_PATCH.read_bytes()).hexdigest()
    except OSError:
        return False


def ensure_macro_server():
    """Build OpenAppleMacros with support/patches/OpenAppleMacros-preview-uikit.patch
    into <data>/bin/OpenAppleMacrosServer and install it into the SDK. Rebuilt
    when the patch changes."""
    data = config.data_dir()
    if not macro_server_current():
        url, commit = OPEN_APPLE_MACROS
        src = data / "src/OpenAppleMacros"
        log("Building OpenAppleMacros with UIKit #Preview support (once, a few minutes)")
        if not src.exists():
            run(["git", "clone", "-q", url, src])
        run(["git", "-C", src, "checkout", "-q", "-f", commit])
        run(["git", "-C", src, "clean", "-q", "-fd"])
        run(["git", "-C", src, "apply", MACROS_PATCH])
        # A plain Linux build: without our iOS xcrun/clang stand-ins in PATH.
        env = os.environ.copy()
        env["PATH"] = f"{config.swift_bin()}:{env.get('PATH', '')}"
        build = ["swift", "build", "-c", "release", "--product", "OpenAppleMacrosServer", "--package-path", src]
        run(build, env=env)
        bin_path = Path(output([*build, "--show-bin-path"], env=env).strip())
        (data / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(bin_path / "OpenAppleMacrosServer", data / "bin/OpenAppleMacrosServer")
        (data / "bin/OpenAppleMacrosServer.patch-sha256").write_text(
            hashlib.sha256(MACROS_PATCH.read_bytes()).hexdigest() + "\n")
        run(["git", "-C", src, "checkout", "-q", "-f", commit])
    install_macro_server()
