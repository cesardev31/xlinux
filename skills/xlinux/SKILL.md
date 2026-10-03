---
name: xlinux
description: Build, install, debug and drive iOS apps on a real iPhone from Linux with xlinux (Flutter, Expo / React Native, native SwiftUI / UIKit Xcode projects). Use when the user wants to run a Flutter app on an iPhone from Linux, take screenshots of the iPhone, tap/type on it, hot reload, or read device logs.
---

# xlinux

xlinux builds and debugs iOS apps on a USB-connected iPhone from Linux, no Mac.

## First check

Run `xlinux doctor`. If something fails, follow its hint (usually `xlinux setup`,
plugging in/unlocking the iPhone, or enabling Developer Mode).

## Running the app

- Debug with hot reload: `flutter run -d iphone-linux` from the Flutter project
  (or pick "iPhone (xlinux)" in VS Code). Run it in the background or ask the
  user to run it in a terminal; it stays attached.
- Release: `xlinux run --project <dir>` (build, install, launch, logs).
- Build only: `xlinux build [--debug] [--install] --project <dir>`.
- Expo / React Native: `xlinux run` from the project (debug build, installs,
  launches and starts Metro; the dev launcher connects to the printed URL).
  The first build takes ~20-30 min; later builds only rebuild what changed.
- Native Xcode project (SwiftUI / UIKit, a folder with an `.xcodeproj`):
  `xlinux run` (Debug build, installs, launches, prints the app's own logs;
  `--release`, `--target`, `--configuration`). No hot reload: every change is
  a rebuild + reinstall. App Intents / App Shortcuts work (Siri, Shortcuts);
  phrases in other languages go in `AppShortcuts.xcstrings`.

The first build of an app with native plugins can take several minutes
(SwiftPM fetches dependencies); later builds are cached.

## Seeing and driving the iPhone

Prefer the MCP server if it is registered (`claude mcp add xlinux -- xlinux mcp`):
tools `screenshot`, `tap`, `swipe`, `type_text`, `press_button`, `list_apps`,
`launch_app`, `flutter_hot_reload`, `device_logs`.

Without MCP, use the CLI (one JSON line per action):

```
xlinux device agent snapshot            # → {"path": ".../screen.png", ...}; then read the image
xlinux device agent tap 0.5 0.8         # normalized 0..1 coordinates
xlinux device agent swipe 0.5 0.7 0.5 0.3
xlinux device agent type "hello"        # ASCII only, into the focused field
xlinux device agent button home
```

Loop: edit code → hot reload → screenshot → interact → verify.

## Rules

- Never make purchases or payments, delete data or send messages without the
  user's confirmation.
- A debug Flutter build only works when launched by `flutter run`/VS Code;
  `launch_app` is for release builds.
- With a free Apple ID the bundle ID is prefixed with `XTL-<team>.`; tools
  accept the unprefixed ID.
