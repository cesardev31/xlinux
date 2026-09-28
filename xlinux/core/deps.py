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
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import config
from .util import log, output, run

SWIFT_VERSION = "6.4.0"
SWIFTLY_URL = "https://download.swift.org/swiftly/linux/swiftly-{arch}.tar.gz"
SWIFT_KEYS_URL = "https://www.swift.org/keys/all-keys.asc"
SWIFT_RELEASES_URL = "https://www.swift.org/api/v1/install/releases.json"
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


DOWNLOAD_ATTEMPTS = 8
DOWNLOAD_TIMEOUT = 60  # seconds without data: e.g. after the laptop was suspended


def _download_chunk(url, partial):
    """Append what's missing of `url` to `partial` (HTTP Range). Returns True when complete."""
    have = partial.stat().st_size if partial.exists() else 0
    request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    try:
        response = urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT)
    except urllib.error.HTTPError as error:
        if error.code == 416:  # nothing left to download
            return True
        raise
    with response:
        if have and response.status != 206:  # the server ignored Range: start over
            have = 0
        total = have + int(response.headers.get("Content-Length") or 0)
        with open(partial, "ab" if have else "wb") as out:
            done = have
            while chunk := response.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total and sys.stderr.isatty():
                    print(f"\r    {done >> 20} / {total >> 20} MB", end="", file=sys.stderr, flush=True)
    if sys.stderr.isatty():
        print(file=sys.stderr)
    return not total or done >= total


def download(url, dest):
    """Download `url` to `dest`, resuming after network drops (retried here) or
    an interrupted setup (the `.part` file is kept and continued next time)."""
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    log(f"Downloading {url.rsplit('/', 1)[-1]}" + (" (resuming)" if partial.exists() else ""))
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            if _download_chunk(url, partial):
                break
        except (OSError, urllib.error.URLError) as error:  # timeouts and resets included
            if attempt == DOWNLOAD_ATTEMPTS:
                sys.exit(f"error: downloading {url} failed ({error}).\n"
                         "Check the connection and run the same command again: it resumes where it stopped.")
            wait = min(5 * attempt, 30)
            print(f"\n    connection lost ({error}); retrying in {wait} s", file=sys.stderr, flush=True)
            time.sleep(wait)
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


def _swift_platform():
    """swiftly's name for this distribution, e.g. ("ubuntu2604", "ubuntu26.04")."""
    platform_info = json.loads((config.data_dir() / "swiftly/config.json").read_text())["platform"]
    return platform_info["name"], platform_info["nameFull"]


def _verify_swift(archive, url):
    """Check the toolchain's PGP signature with swift.org's keys (as swiftly does)."""
    if not shutil.which("gpg"):
        log("warning: gpg not found, the Swift toolchain's signature isn't verified")
        return
    signature = download(url + ".sig", archive.with_name(archive.name + ".sig"))
    keys = download(SWIFT_KEYS_URL, archive.with_name("swift-all-keys.asc"))
    with tempfile.TemporaryDirectory(dir=config.data_dir() / "tmp") as home:
        env = dict(os.environ, GNUPGHOME=home)
        run(["gpg", "--batch", "--quiet", "--import", keys], env=env, capture_output=True)
        result = run(["gpg", "--batch", "--verify", signature, archive], env=env, check=False,
                     capture_output=True, text=True)
    if result.returncode != 0:
        archive.unlink()
        signature.unlink()
        sys.exit(f"error: the Swift toolchain's signature doesn't verify; the download was "
                 f"removed, run `xlinux setup` again.\n{result.stderr.strip()}")


def ensure_swift():
    """Swift inside the data directory, laid out and registered like swiftly
    does (swiftly detects the distribution). The toolchain itself (~1 GB) is
    downloaded here rather than by `swiftly install`, which starts over when
    the connection drops (e.g. the laptop is suspended) instead of resuming."""
    if config.swift_bin():
        return
    data = config.data_dir()
    env = _swiftly_env()
    (data / "tmp").mkdir(parents=True, exist_ok=True)
    if not (data / "swiftly/config.json").exists():
        archive = download(SWIFTLY_URL.format(arch=arch()), data / "downloads" / f"swiftly-{arch()}.tar.gz")
        with tempfile.TemporaryDirectory(dir=data / "tmp") as tmp:
            run(["tar", "-xzf", archive, "-C", tmp])
            run([Path(tmp) / "swiftly", "init", "--no-modify-profile", "--skip-install",
                 "--quiet-shell-followup", "--assume-yes"], env=env)

    # The release's tag (e.g. swift-6.4.0-RELEASE), as swiftly looks it up.
    with urllib.request.urlopen(SWIFT_RELEASES_URL, timeout=DOWNLOAD_TIMEOUT) as response:
        tag = next((r["tag"] for r in json.load(response) if r["name"] == SWIFT_VERSION),
                   f"swift-{SWIFT_VERSION}-RELEASE")
    name, name_full = _swift_platform()
    suffix = "-aarch64" if arch() == "aarch64" else ""
    url = (f"https://download.swift.org/{tag.lower()}/{name}{suffix}/{tag}/"
           f"{tag}-{name_full}{suffix}.tar.gz")
    log(f"Installing Swift {SWIFT_VERSION} for {name_full} (about 1 GB)")
    archive = download(url, data / "downloads" / url.rsplit("/", 1)[-1])
    _verify_swift(archive, url)

    # Extracted aside and moved in whole: an interrupted extraction must not
    # look like an installed toolchain.
    toolchains = data / "swiftly/toolchains"
    staging = data / "tmp/swift-toolchain"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    run(["tar", "-xzf", archive, "-C", staging, "--strip-components=1"])
    toolchains.mkdir(parents=True, exist_ok=True)
    staging.rename(toolchains / SWIFT_VERSION)

    swiftly_config = data / "swiftly/config.json"
    values = json.loads(swiftly_config.read_text())
    values["installedToolchains"] = sorted({*values.get("installedToolchains", []), SWIFT_VERSION})
    values["inUse"] = SWIFT_VERSION
    swiftly_config.write_text(json.dumps(values, indent=2) + "\n")
    archive.unlink()  # 1 GB; the toolchain is what's kept


# What Swift needs from the system on Debian/Ubuntu (swift.org's list; g++
# brings the libstdc++/libgcc headers of the distribution's default GCC).
SWIFT_APT_PACKAGES = ("binutils", "git", "gnupg2", "libc6-dev", "libcurl4-openssl-dev", "libedit2",
                      "libncurses-dev", "libpython3-dev", "libsqlite3-0", "libxml2-dev", "libz3-dev",
                      "pkg-config", "tzdata", "unzip", "zlib1g-dev", "g++")


def _apt_missing(packages):
    """The packages that aren't installed (and exist in the configured repositories)."""
    missing = []
    for package in packages:
        status = subprocess.run(["dpkg-query", "-W", "-f=${Status}", package],
                                capture_output=True, text=True).stdout
        if "install ok installed" in status:
            continue
        if subprocess.run(["apt-cache", "show", package], capture_output=True).returncode == 0:
            missing.append(package)
    return missing


def llvm_tools_found():
    return bool(config.llvm_tool("lipo") and config.llvm_tool("otool") and config.llvm_tool("install-name-tool"))


def system_packages():
    """Debian/Ubuntu packages still missing: what Swift needs, system LLVM
    (lipo, otool, install_name_tool) and pdftocairo (PDF assets)."""
    wanted = list(SWIFT_APT_PACKAGES) + ["poppler-utils"]
    if not llvm_tools_found():
        wanted.append("llvm")
    if has_apt():
        return _apt_missing(wanted)
    return [p for p in ("llvm", "poppler-utils", "git", "unzip") if p in wanted and
            not shutil.which({"llvm": "llvm-lipo", "poppler-utils": "pdftocairo"}.get(p, p))]


def install_system_packages(packages):
    if not packages:
        return
    if not has_apt():
        sys.exit("error: install these with your package manager, plus Swift's dependencies\n"
                 "(https://www.swift.org/install/linux/), and run `xlinux setup` again:\n"
                 f"  {' '.join(packages)}")
    sudo(f"apt-get install -y {' '.join(packages)}", "Installing system packages")


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


def cocoapods_env():
    """Environment to run CocoaPods (and its xcodeproj gem) on Linux: the
    portable Ruby and gems installed by ensure_cocoapods(), plus stand-ins for
    the macOS tools pod scripts call (support/core/cocoapods)."""
    data = config.data_dir()
    ruby = next(iter(sorted((data / "ruby").glob("portable-ruby/*/bin/ruby"))), None)
    env = config.tool_env()
    support = config.SUPPORT / "core/cocoapods"
    env.update(
        GEM_HOME=str(data / "ruby/gems"), GEM_PATH=str(data / "ruby/gems"),
        CP_HOME_DIR=str(data / "ruby/cocoapods-home"),
        LANG="C.UTF-8", LC_ALL="C.UTF-8",
        # Ruby's default encoding and binary plists (xcodeproj only reads XML ones on Linux).
        RUBYOPT=f"-EUTF-8 -r{support / 'linux.rb'}",
        # GNU tar warns about Apple's xattr headers on stderr, which Expo's
        # scripts read together with the extracted file.
        TAR_OPTIONS="--warning=no-unknown-keyword",
        PATH=f"{support / 'bin'}:{data / 'ruby/gems/bin'}:{ruby.parent if ruby else ''}:{env['PATH']}",
        XLINUX_RUBY=str(ruby) if ruby else "ruby",
    )
    return env


def ensure_cocoapods():
    """Ruby (Homebrew's portable build, no system install) and CocoaPods in
    the data directory, for React Native / Expo projects."""
    env = cocoapods_env()
    if (config.data_dir() / "ruby/gems/bin/pod").exists() and Path(env["XLINUX_RUBY"]).exists():
        return env
    data = config.data_dir()
    if not Path(env["XLINUX_RUBY"]).exists():
        name, url = github_asset("Homebrew/homebrew-portable-ruby",
                                 lambda n: n.endswith(f".{arch()}_linux.bottle.tar.gz"))
        archive = download(url, data / "downloads" / name)
        (data / "ruby").mkdir(parents=True, exist_ok=True)
        log("Extracting portable Ruby")
        with tarfile.open(archive) as tar:
            tar.extractall(data / "ruby", filter="tar")
        env = cocoapods_env()
    log("Installing CocoaPods (Ruby gem, into the data directory)")
    run([Path(env["XLINUX_RUBY"]).parent / "gem", "install", "cocoapods", "--no-document"], env=env,
        stdout=sys.stderr)
    return cocoapods_env()


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
