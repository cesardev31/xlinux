# AGENTS.md

Guidance for AI coding agents working **on** xlinux or **with** it.

## What xlinux is

A CLI that builds, installs and debugs iOS apps on a real iPhone from Linux
(no Mac). Supported: Flutter, and Expo / React Native (debug builds with
expo-dev-client + Metro). It glues together xtool,
Darling, pymobiledevice3, Swift/LLVM and Flutter's own tooling; see README.md.

## Using xlinux in an app project

```
curl -fsSL https://github.com/cesardev31/xlinux/releases/latest/download/install.sh | sh
xlinux setup                          # installs everything missing (Xcode .xip: manual download)
xlinux doctor                         # check the environment and the iPhone
flutter run -d iphone-linux           # debug + hot reload (custom device)
xlinux build [--debug] [--install] [--project DIR]
xlinux run                            # release: build, install, launch, stream logs
xlinux device screenshot out.png
xlinux device agent snapshot|tap X Y|swipe X1 Y1 X2 Y2|type TEXT|button NAME
xlinux mcp                            # MCP server (stdio) to see and drive the iPhone
```

- Coordinates for `device agent` and the MCP tools are normalized 0..1.
- With a free Apple ID the installed bundle ID gets an `XTL-<team>.` prefix.
- A debug Flutter app only starts correctly through `flutter run`/VS Code.
- Never make purchases or payments, delete data or send messages on the phone
  without the user's confirmation.

## Working on the xlinux code

- `xlinux/core/` is framework-agnostic; anything Flutter- or Expo-specific goes
  in `xlinux/adapters/<framework>/`. `xlinux/core/xcode/` builds CocoaPods-based
  Xcode projects (settings, targets, link, .app) and is shared. `support/` holds files shipped into builds or run
  on the device side (lldb driver, xcrun/actool stand-ins, Darling shim).
- Pure Python 3 stdlib in the CLI; the MCP server runs on pymobiledevice3's
  interpreter (it needs `mcp` and `pillow`).
- No test suite yet. Verify changes with `xlinux doctor`, a build of a sample
  app (`xlinux build --debug --project <app>`) and, for debugging changes,
  `tools/bench_debug.py`.
- External tools are installed by `xlinux/core/deps.py`: `ensure_*` helpers,
  idempotent; rarely used ones are called lazily where they're needed.
- Heavy data (SDK, toolchains, caches) lives in the data directory
  (`~/.local/share/xlinux` by default, configurable via `xlinux setup --data-dir`).
- Everything user-facing (output, help, docs, comments) is in English.
- License: GPL-3.0-or-later. Credit third-party code in README's credits table.
