# xlinux

Build, install and debug apps on a real iPhone **from Linux, no Mac required**.

Today it supports **Flutter**: release builds, debug with hot reload, the
iPhone as a device in VS Code / `flutter run`, and real native plugins
(Firebase, Stripe, Google Sign-In, WebView…). The core is framework-agnostic,
so more adapters can be added (Expo / React Native is the next candidate).

It does not reimplement Xcode. It glues together open-source pieces that
already existed (xtool, Darling, pymobiledevice3, LLVM/Swift, OpenAppleMacros…)
and fills the gaps where they didn't fit. Credits are [at the end](#credits-and-licenses).

```
flutter run -d iphone-linux        # debug + hot reload (or pick the iPhone in VS Code)
xlinux run                         # release: build, install, launch and stream logs
xlinux build [--debug] [--install] [--project DIR]
xlinux doctor                      # diagnostics
xlinux setup                       # prepare the environment (once)
xlinux mcp                         # MCP server for AI agents (see below)
xlinux device screenshot iphone.png
xlinux device mirror               # web viewer at 127.0.0.1:8080
xlinux device mirror --mode vnc    # VNC with touch control
```

`flutter-ios-linux` (the previous name) still works as an alias.

## Status

Tested on an iPhone 11 running iOS 27.2 with a **free** Apple ID, against real
production Flutter apps.

| | |
|---|---|
| Release (Dart AOT) builds, installs and launches fast | ✅ |
| Debug with JIT + lldb (no sudo; faster with `tunneld`) | ✅ |
| Hot reload (`flutter run` and the VS Code protocol), ~0.5 s | ✅ |
| iPhone listed in `flutter devices` / VS Code | ✅ |
| Native assets (Dart build hooks, e.g. `objective_c`) in debug and release | ✅ |
| Native plugins via SwiftPM: firebase_core, google_sign_in (Google login), flutter_stripe, webview, url_launcher, shared_preferences | ✅ |
| CocoaPods-only plugins (podspec → SwiftPM conversion, no Ruby): Swift pods + xcframeworks, e.g. `flutter_validations_sdk` (Truora + TensorFlow Lite) | ✅ |
| App extensions (e.g. WidgetKit widgets) from the Xcode project, Swift sources | ✅ |
| Pods with Objective-C/C | ❌ |
| Other frameworks (Expo / React Native) | 🔜 |

## How it works

| On macOS this is done by… | Here |
|---|---|
| `gen_snapshot` (Dart AOT → ARM64) | Flutter's macOS binary running under **Darling**, with `support/core/darling_compat.c` (reports macOS 12 from `uname` and emulates aligned `vm_map`, which Darling lacks and which crashed Dart's GC) |
| Xcode compiling Runner (Swift/ObjC) | `swiftc`/`clang` + the iOS SDK extracted from `Xcode.xip` by **xtool** |
| CocoaPods (`pod install`) | `xlinux/core/cocoapods.py`: reads podspecs (a minimal Ruby reader for local ones, CDN JSON for third-party ones), resolves versions, fetches sources without history and generates one `Package.swift` per pod; `resource_bundles` are assembled like CocoaPods does (actool included) |
| Xcode + SwiftPM building plugins | **Swift Build** (SwiftPM 6.4) with xtool's toolset; generates the same `FlutterGeneratedPluginSwiftPackage` as macOS, with the Runner as a SwiftPM executable |
| `xcrun`, `clang`, `lipo`, `otool`, `install_name_tool`, `codesign` | `support/core/bin/`: our own xcrun, a clang that infers `-target` like Apple's, LLVM tools, a no-op codesign (xtool signs at the end) |
| `actool` (asset catalogs → `Assets.car`) | `support/core/bin/actool`: imagesets to loose PNGs (`name@2x.png`), rasterized SVG/PDF (cairosvg / pdftocairo), AppIcon → `CFBundleIcons` |
| `ibtool` (`Main.storyboard`) | `support/flutter/FlutterLinuxSceneDelegate.swift` creates the window in code; `UILaunchScreen` in Info.plist |
| Xcode macros (`#Preview`, `@Observable`…) | xtool's **OpenAppleMacros**, with stubs for UIKit `#Preview` (`support/patches/`) |
| `flutter build ios` / `flutter assemble` | `flutter assemble` with the same `-d` defines Xcode passes; in release, `gen_snapshot_arm64` in Flutter's cache is a wrapper that runs it under Darling |
| signing and installing | **xtool** (free or paid Apple ID) |
| debugging (Dart's JIT needs a debugger) | **pymobiledevice3** (kernel `tunneld` tunnel, or userspace without sudo) + `support/core/device_bridge.py` + lldb with `support/core/lldb_driver.py` and Flutter's JIT helper |
| the device in VS Code | a Flutter *custom device* (`xlinux/adapters/flutter/custom_device.py`) |

## Architecture

```
xlinux/core/               framework-agnostic
  toolchain.py             swiftc/clang/Swift Build for iOS, history-less git mirrors
  cocoapods.py             podspec → SwiftPM (no Ruby, no `pod install`)
  extensions.py            app extensions (.appex) found in project.pbxproj
  app.py                   .app/.ipa without ibtool (Info.plist, icons, resources)
  darling.py               run macOS command-line tools
  device.py                detect, install, launch, capture; DebugSession (DVT + debugserver + lldb)
  setup.py                 shared setup/doctor
  config.py                paths and environment (works without sourcing any env.sh)
xlinux/adapters/flutter/   everything Flutter-specific
  build.py                 flutter assemble, Runner, .app
  plugins.py               native plugins via SwiftPM (like `flutter build ios` on macOS)
  debug.py                 JIT helper + VM Service for `flutter run`/`attach`
  custom_device.py         the iPhone in `flutter devices` and VS Code
xlinux/mcp_server.py       MCP server (screenshots, touches, text, buttons, apps, hot reload, logs)
support/core/              device_bridge.py, lldb_driver.py, darling_compat.c
support/core/bin/          xcrun, actool, apple-clang, lipo, otool, install_name_tool, codesign
support/flutter/           FlutterLinuxSceneDelegate.swift, flutter_lldb_helper.py
support/patches/           patches to third-party projects (OpenAppleMacros)
tools/bench_debug.py       measures startup and hot reload the way VS Code drives them
```

A new adapter provides its build and its debugging story. Signing,
installing, the device, Swift Build, `xcrun`/`actool` and packaging come from
the core.

## Installation

The heavy parts (≈40 GB including SwiftPM dependencies) live in a **data
directory** (default `~/.local/share/xlinux`; an external SSD works well):

| What | How |
|---|---|
| Swift 6.4 | [swiftly](https://github.com/swiftlang/swiftly) with `SWIFTLY_HOME_DIR` inside the data directory |
| xtool | AppImage at `~/.local/bin/xtool`; `xtool setup` with `Xcode_27.xip` (downloaded by you with your Apple ID) |
| Darling | the `darling-core`, `darling-system`, `darling-cli` `.deb` packages |
| pymobiledevice3 | `UV_TOOL_DIR=<data>/uv-tools uv tool install pymobiledevice3` |
| MCP SDK + Pillow (for `xlinux mcp`) | `uv pip install --python <data>/uv-tools/pymobiledevice3/bin/python mcp pillow` |
| cairosvg (for `actool`) | `uv venv <data>/py-tools && uv pip install --python <data>/py-tools/bin/python cairosvg` |
| System LLVM | `llvm-lipo`, `llvm-otool`, `llvm-install-name-tool` (the `llvm-21` package) and `pdftocairo` (poppler) |
| OpenAppleMacros with UIKit `#Preview` | see `support/patches/README.md` (only needed if a plugin uses UIKit `#Preview`, e.g. Stripe) |
| Flutter's iOS engine | downloaded automatically on the first build |

```
bin/xlinux setup --data-dir /path/to/data-directory
bin/xlinux doctor
```

`setup` extracts xtool's AppImage (so it works without FUSE from the Flutter
snap), builds the Darling shim, installs the patched macro server if present
and registers the iPhone in `~/.config/flutter/custom_devices.json`.

Recommended for debugging (much faster), in another terminal:

```
sudo <data>/uv-tools/pymobiledevice3/bin/pymobiledevice3 remote tunneld
```

On the iPhone: enable Developer Mode and trust your Apple ID (Settings →
General → VPN & Device Management).

## Seeing and controlling the iPhone

`device screenshot` captures the full screen through CoreDevice. It's also
registered with the custom device, so `flutter screenshot -d iphone-linux`
works too. `device mirror` streams the screen through pymobiledevice3 (over
`tunneld` if running, otherwise the userspace tunnel, no sudo). Web mode offers
a browser viewer; VNC mode turns clicks into HID touch events and decodes the
video on your machine, so it works with any VNC viewer (e.g. Remmina).

For scripts there is a headless interface that prints one JSON response per
action. Coordinates are normalized between `0` and `1`, regardless of the
iPhone model:

```bash
xlinux device agent snapshot
# {"ok": true, "path": "/tmp/xlinux/screen.png", "width": ..., "height": ...}
xlinux device agent tap 0.5 0.8
xlinux device agent swipe 0.5 0.8 0.5 0.2 --duration 0.4
xlinux device agent type "text"
xlinux device agent button home
```

## MCP: let an AI agent see and drive the iPhone

```
claude mcp add xlinux -- xlinux mcp      # Claude Code (or your agent's equivalent)
```

`xlinux mcp` is an MCP server (stdio) that keeps the tunnel and the CoreDevice
touch session open: a screenshot takes ~0.3 s (vs ~3 s through the CLI) and
weighs ~25 KB.

| Tool | What it does |
|---|---|
| `screenshot` | capture the screen (downscaled JPEG) |
| `tap`, `swipe` | touch / drag with 0..1 coordinates; return the screenshot afterwards |
| `type_text` | type ASCII into the focused field |
| `press_button` | home, lock, volume-up, volume-down, mute |
| `list_apps`, `launch_app` | installed apps (accepts the bundle ID without the `XTL-` prefix) |
| `flutter_hot_reload` | r/R of the `flutter run` running in a terminal (SIGUSR1/SIGUSR2) |
| `device_logs` | filtered iPhone log (Flutter's `print()` output) |

An agent can close the loop by itself: edit code → hot reload → screenshot →
tap/navigate → verify. Use it with apps under development: the agent can touch
anything on the phone.

For agents: [`AGENTS.md`](AGENTS.md) and a Claude Code skill in
[`skills/xlinux/`](skills/xlinux/SKILL.md) (copy it to `~/.claude/skills/xlinux/`).

## Hard-won details (so nobody has to rediscover them)

**Debugging**
- iOS 17+: `debugserver` can't launch apps ("Operation not permitted"). Apps are
  **launched suspended through DVT** and lldb attaches to the pid. The DVT
  session has to stay open until lldb attaches; otherwise Flutter starts
  without a debugger ("debug mode Flutter apps can only be launched from
  Flutter tooling").
- iOS 17+'s `debugproxy` expects raw TCP; pymobiledevice3's forwarder performs
  the lockdown check-in and lldb hangs.
- lldb must run in **synchronous** mode after attaching: in async mode the
  `NOTIFY_DEBUGGER_ABOUT_RX_PAGES` breakpoint callbacks never run and the app
  shows a black screen.
- Flutter's JIT helper writes the whole region (up to 512 KiB per stop) over the
  debugger protocol; touching 8 bytes per 16 KiB page is enough (the pages are
  fresh and zeroed): the first hot reload went from 6.8 s to ~0.5 s.
- Kernel tunnel (`tunneld`): when running, lldb talks straight to debugserver;
  the userspace tunnel (TCP in Python) works without sudo but sometimes stalls
  for minutes.
- A debug app left without its debugger freezes at the next JIT stop and hangs
  the iOS installer: running instances are killed before installing (DVT kill).
- Dart plugin registrant: although Flutter thinks the iPhone is "linux",
  `dart_plugin_registrant.dart` covers every platform and picks one with
  `Platform.isIOS` at runtime, so hot restart works with plugins.
- Caching: if the generated native code, plugins, engine and tools don't
  change, `swift build` is skipped (~30 s saved in a Firebase/Stripe app even
  with nothing to do); if the `.app` is identical to the one installed on that
  iPhone (tracked per device and app), it isn't reinstalled. The `.app` is
  installed without compressing it.
- `target.memory-module-load-level minimal`: without the extracted shared cache
  (Xcode's DeviceSupport) lldb would read ~500 libraries from the iPhone's memory.

**Building**
- The Swift toolchain's lld is older than the iOS 27 SDK: xtool's is used
  instead (`-B toolset/bin`).
- SwiftPM always does a full `git clone --mirror`; for `exact:` dependencies
  (firebase-ios-sdk) only the tag is fetched with `--depth 1` and configured as
  a mirror.
- Swift Build checks `actool` relative to the package (and rejects symlinks) but
  runs it from `PATH`; it also caches the build description (`XCBuildData`).
- `flutter build bundle --target-platform ios` doesn't work with native assets
  (no `SdkRoot`): `flutter assemble` is used instead, which looks for the iOS
  artifacts in Flutter's cache (linked to the data directory).
- Macros: UIKit `#Preview` (`KitViewMacro`) is missing from OpenAppleMacros; a
  build with stubs is compiled (`support/patches/`) and `setup` installs it.
- Swift Build doesn't enable cross-import overlays (`_PassKit_SwiftUI` →
  `PayWithApplePayButton`): `-enable-cross-import-overlays` is passed.
- `-ObjC` when linking the Runner: without it the linker drops Objective-C
  categories from static libraries (AppAuth → "unrecognized selector ...
  presentAuthorizationRequest" and Google Sign-In fails at runtime).
  CocoaPods/Xcode always add it.
- SwiftPM checkouts are read-only: `actool` must not copy their permissions.
- A release `flutter assemble` produces a universal App.framework: it's thinned
  with `lipo -thin`, like Xcode's "embed and thin".
- CocoaPods without Ruby: the CDN (`cdn.cocoapods.org`) rejects Python's default
  User-Agent (403). Each pod's target is a directory holding links to its
  sources only: pointing it at the whole repo makes SwiftPM pick up the sample
  apps' resources. Pods look for their bundles at the root of the .app
  (`Bundle.main`), not in SwiftPM bundles.
- Swift's `#available` needs `__isPlatformVersionAtLeast` from compiler-rt;
  Apple's clang links `libclang_rt.ios.a` implicitly, here it's passed explicitly
  (it ships in the Xcode toolchain inside the SDK).
- App extensions: each target whose Info.plist declares `NSExtension` is
  compiled into `PlugIns/<Name>.appex`; xtool registers and signs every
  `.appex` it finds there.
- Only Mach-O MH_DYLIB binaries are embedded in `Frameworks/`: static
  xcframeworks (e.g. TensorFlowLiteC, MH_OBJECT) already live in the executable.
- xtool's AppImage can't mount FUSE when launched from the Flutter snap → the
  extracted copy is used.

## Known limitations

- `actool` without `Assets.car`: no catalog colors or data (`UIColor(named:)`,
  `NSDataAsset`), and icons lose "template rendering".
- Debug: changing Dart code between launches requires reinstalling the app
  (~40 s in a large app); with no native or Dart changes, `flutter run` skips the
  native build and the install (~11 s). While running, hot reload takes ~0.5 s.
- Free Apple ID: certificates last 7 days, 3 apps at most, no push, Apple Pay or
  Sign in with Apple; xtool prefixes the bundle ID with `XTL-<team>.`. Don't
  delete the last app signed with your Apple ID, or iOS asks you to trust the
  developer again.
- App Groups: entitlements files aren't applied yet, and xtool drops App Groups
  on free Apple IDs anyway (on paid ones it renames them to
  `group.XTL-<team>.<id>`). A widget that reads the app's data through
  `UserDefaults(suiteName:)` installs and runs, but only sees its empty state.
- Extensions: Swift sources only; their `Assets.xcassets` isn't compiled yet.
- `type_text` (MCP) only types ASCII.

## Roadmap

1. Dart changes without reinstalling (push the kernel into the app's container
   and launch with `--flutter-assets-dir`).
2. Objective-C/C pods in the CocoaPods conversion.
3. Expo / React Native adapter: `expo prebuild` + CocoaPods; debugging is simpler
   (Hermes has no JIT, Metro over the network), building is harder
   (Pods.xcodeproj).

## Credits and licenses

xlinux is mostly **glue**. The heavy lifting is done by these projects, which
are **used** (installed separately; not redistributed here except where noted):

| Project | Author(s) | License | Used for |
|---|---|---|---|
| [xtool](https://github.com/xtool-org/xtool) | Kabir Oberai | MIT | iOS SDK from `Xcode.xip`, toolset/linker, Swift Build for iOS, signing and installing; its `PackLib` was the reference for packaging SwiftPM into a `.app` |
| [OpenAppleMacros](https://github.com/xtool-org/OpenAppleMacros) | Kabir Oberai | MIT | Xcode macros on Linux; **our patch** in `support/patches/` |
| [Darling](https://github.com/darlinghq/darling) | Darling Team | GPL-3.0 | running macOS's `gen_snapshot` on Linux |
| [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) | doronz88 and contributors | GPL-3.0-or-later | RSD tunnels (userspace / `tunneld`), DVT, Developer Disk Image, debugserver, installing, syslog, CoreDevice screen/HID; `device_bridge.py` and the MCP server use its library |
| [Flutter](https://github.com/flutter/flutter) | The Flutter Authors | BSD-3-Clause | iOS engine, `flutter assemble`, custom devices; **`support/flutter/flutter_lldb_helper.py` is a modified copy** of its JIT helper (keeps its copyright notice) |
| [Dart SDK](https://github.com/dart-lang/sdk) | The Dart project authors | BSD-3-Clause | compiler (`gen_snapshot`, frontend_server) |
| [Swift](https://github.com/swiftlang/swift), [SwiftPM](https://github.com/swiftlang/swift-package-manager), [Swift Build](https://github.com/swiftlang/swift-build), [swiftly](https://github.com/swiftlang/swiftly) | Apple and the Swift community | Apache-2.0 (with runtime exception) | compiling Swift/ObjC for iOS, resolving plugins |
| [LLVM](https://github.com/llvm/llvm-project) (clang, lld, lldb, llvm-lipo/otool/…) | LLVM Developer Group | Apache-2.0 with LLVM Exception | compiling, linking, debugging, Mach-O tooling |
| [CairoSVG](https://github.com/Kozea/CairoSVG) | Kozea | LGPL-3.0-or-later | rasterizing SVG in `actool` |
| [Poppler](https://poppler.freedesktop.org/) (`pdftocairo`) | Poppler developers | GPL-2.0 / GPL-3.0 | rasterizing PDF in `actool` |
| [libimobiledevice / usbmuxd](https://libimobiledevice.org/) | libimobiledevice project | LGPL-2.1 / GPL | USB connection to the iPhone |
| [uv](https://github.com/astral-sh/uv) | Astral | MIT / Apache-2.0 | installing the Python tools |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | Anthropic and contributors | MIT | MCP server |
| [Pillow](https://github.com/python-pillow/Pillow) | Jeffrey A. Clark and contributors | MIT-CMU | downscaling MCP screenshots |

**Apple SDK:** the iOS SDK comes from `Xcode.xip`, which you download yourself
from developer.apple.com with your Apple ID after accepting its license; xlinux
does not redistribute it. Review Apple's terms before using this commercially.

xlinux is not affiliated with Apple, Google, Flutter or any of the projects
above. "iPhone", "iOS" and "Xcode" are trademarks of Apple Inc.

### xlinux license

Copyright (C) 2026 Cesar Andres Pereira.

xlinux is free software: you can redistribute it and/or modify it under the
terms of the **GNU General Public License version 3** (or, at your option, any
later version) as published by the Free Software Foundation. It is distributed
in the hope that it will be useful, but **without any warranty**. See
[`LICENSE`](LICENSE).

The third-party components listed above keep their own licenses.
`support/flutter/flutter_lldb_helper.py` derives from Flutter (BSD-3-Clause) and
keeps its notice; the patch in `support/patches/` applies to OpenAppleMacros (MIT).
