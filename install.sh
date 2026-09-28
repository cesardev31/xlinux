#!/bin/sh
# xlinux installer: downloads a release from GitHub and puts `xlinux` in ~/.local/bin.
#
#   curl -fsSL https://github.com/cesardev31/xlinux/releases/latest/download/install.sh | sh
#
# Environment:
#   XLINUX_VERSION      release tag to install (default: the latest release)
#   XLINUX_INSTALL_DIR  where the CLI lives (default: ~/.local/lib/xlinux)
#   XLINUX_BIN_DIR      where the `xlinux` command goes (default: ~/.local/bin)
#   XLINUX_ARCHIVE_URL  download this xlinux.tar.gz instead (mirrors, testing)
#
# Only the CLI (a few hundred KB) is installed here. `xlinux setup` then
# installs the toolchains, the iOS SDK and the rest into the data directory.
set -eu

REPO="cesardev31/xlinux"
VERSION="${XLINUX_VERSION:-latest}"
INSTALL_DIR="${XLINUX_INSTALL_DIR:-$HOME/.local/lib/xlinux}"
BIN_DIR="${XLINUX_BIN_DIR:-$HOME/.local/bin}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*" >&2; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || fail "xlinux runs on Linux (on macOS, use Xcode)."
case "$(uname -m)" in
    x86_64 | amd64) ;;
    aarch64 | arm64) say "note: on arm64, release builds are unavailable (Darling is x86_64-only)" ;;
    *) fail "unsupported architecture: $(uname -m)" ;;
esac
command -v python3 >/dev/null || fail "python3 is required (3.10 or newer)."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    fail "python3 $(python3 -c 'import platform; print(platform.python_version())') is too old; 3.10 or newer is required."
command -v tar >/dev/null || fail "tar is required."

if [ -n "${XLINUX_ARCHIVE_URL:-}" ]; then
    url="$XLINUX_ARCHIVE_URL"
elif [ "$VERSION" = latest ]; then
    url="https://github.com/$REPO/releases/latest/download/xlinux.tar.gz"
else
    url="https://github.com/$REPO/releases/download/$VERSION/xlinux.tar.gz"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM

say "Downloading xlinux ($VERSION)"
if command -v curl >/dev/null; then
    curl -fsSL "$url" -o "$tmp/xlinux.tar.gz" || fail "download failed: $url"
elif command -v wget >/dev/null; then
    wget -qO "$tmp/xlinux.tar.gz" "$url" || fail "download failed: $url"
else
    fail "curl or wget is required."
fi
tar -xzf "$tmp/xlinux.tar.gz" -C "$tmp"
[ -x "$tmp/xlinux/bin/xlinux" ] || fail "unexpected archive layout."

# Replace any previous version in one move (the data directory isn't touched).
mkdir -p "$(dirname "$INSTALL_DIR")" "$BIN_DIR"
rm -rf "$INSTALL_DIR.old"
[ -e "$INSTALL_DIR" ] && mv "$INSTALL_DIR" "$INSTALL_DIR.old"
mv "$tmp/xlinux" "$INSTALL_DIR"
rm -rf "$INSTALL_DIR.old"
ln -sf "$INSTALL_DIR/bin/xlinux" "$BIN_DIR/xlinux"

say "Installed $("$BIN_DIR/xlinux" --version) in $INSTALL_DIR"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "Add $BIN_DIR to your PATH, e.g.:  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.profile" ;;
esac
cat >&2 <<EOF

Next:
  xlinux setup      # installs Swift, xtool, the iOS SDK... (~20-40 GB, asks for sudo once)
                    # use --data-dir DIR to keep all of it on another drive
  xlinux doctor     # checks the environment and the iPhone
EOF
