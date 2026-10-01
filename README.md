# xlinux

Build, install and debug apps on a real iPhone **from Linux, no Mac required**.

It supports **Flutter** (release builds, debug with hot reload, the iPhone as
a device in VS Code / `flutter run`, real native plugins: Firebase, Stripe,
Google Sign-In, WebView…) and **Expo / React Native** (debug builds with
`expo-dev-client` and Metro, and release builds with the JavaScript bundled as
Hermes bytecode; the whole CocoaPods project, React Native, Reanimated, Expo
modules and Google Sign-In compiled on Linux).
The core is framework-agnostic.

It does not reimplement Xcode. It glues together open-source pieces that
already existed (xtool, Darling, pymobiledevice3, LLVM/Swift, OpenAppleMacros…)
and fills the gaps where they didn't fit. Credits are [at the end](#credits-and-licenses).

```
flutter run -d iphone-linux        # debug + hot reload (or pick the iPhone in VS Code)
xlinux run                         # Flutter: release build, install, launch, logs
                                   # Expo: debug build, install, launch, Metro
xlinux run --release               # Expo: release build (JS bundled), install, launch, logs
xlinux build [--debug] [--install] [--project DIR]
xlinux run --flavor qa --dart-define-from-file config/qa.json
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
| The app's `print()` output in `flutter run` and VS Code's debug console (forwarded from the iPhone's log) | ✅ |
| iPhone listed in `flutter devices` / VS Code | ✅ |
| Native assets (Dart build hooks, e.g. `objective_c`) in debug and release | ✅ |
| Native plugins via SwiftPM: firebase_core, google_sign_in (Google login), flutter_stripe, webview, url_launcher, shared_preferences | ✅ |
| CocoaPods-only plugins (podspec → SwiftPM conversion, no Ruby): Swift pods + xcframeworks, e.g. `flutter_validations_sdk` (Truora + TensorFlow Lite) | ✅ |
| App extensions (e.g. WidgetKit widgets) from the Xcode project, Swift sources | ✅ |
| CocoaPods-only plugins with Objective-C/C/C++: real CocoaPods (no Runner.xcodeproj changes) + the same pod builder as Expo, e.g. `device_info` | ✅ |
| Expo / React Native, debug (expo-dev-client + Metro): `pod install`, 49 pods incl. React Native, Reanimated, Screens, Expo modules, Google Sign-In; tested with Expo 56 / RN 0.85 | ✅ |
| Expo / React Native release: JavaScript bundled as Hermes bytecode, prebuilt frameworks switched to their release flavor, runs without Metro | ✅ |

## How it works

| On macOS this is done by… | Here |
|---|---|
| `gen_snapshot` (Dart AOT → ARM64) | Flutter's macOS binary running under **Darling**, with `support/core/darling_compat.c` (reports macOS 12 from `uname` and emulates aligned `vm_map`, which Darling lacks and which crashed Dart's GC) |
| Xcode compiling Runner (Swift/ObjC) | `swiftc`/`clang` + the iOS SDK extracted from `Xcode.xip` by **xtool** |
| CocoaPods (`pod install`) | `xlinux/core/cocoapods.py`: reads podspecs (a minimal Ruby reader for local ones, CDN JSON for third-party ones), resolves versions, fetches sources without history and generates one `Package.swift` per pod; `resource_bundles` are assembled like CocoaPods does (actool included). Pods with Objective-C/C/C++ go through real CocoaPods instead (`xlinux/adapters/flutter/pods.py`: Podfile in the data directory, `integrate_targets => false`, built with `core/xcode`) |
| Xcode + SwiftPM building plugins | **Swift Build** (SwiftPM 6.4) with xtool's toolset; generates the same `FlutterGeneratedPluginSwiftPackage` as macOS, with the Runner as a SwiftPM executable |
| `xcrun`, `clang`, `lipo`, `otool`, `install_name_tool`, `codesign` | `support/core/bin/`: our own xcrun, a clang that infers `-target` like Apple's, LLVM tools, a no-op codesign (xtool signs at the end) |
| `actool` (asset catalogs → `Assets.car`) | `support/core/bin/actool`: imagesets to loose PNGs (`name@2x.png`), rasterized SVG/PDF (cairosvg / pdftocairo), AppIcon → `CFBundleIcons` |
| `ibtool` (`Main.storyboard`) | `support/flutter/FlutterLinuxSceneDelegate.swift` creates the window in code; `UILaunchScreen` in Info.plist |
| Xcode macros (`#Preview`, `@Observable`…) | xtool's **OpenAppleMacros**, with stubs for UIKit `#Preview` (`support/patches/`) |
| `flutter build ios` / `flutter assemble` | `flutter assemble` with the same `-d` defines Xcode passes; in release, `gen_snapshot_arm64` in Flutter's cache is a wrapper that runs it under Darling |
| signing and installing | **xtool** (free or paid Apple ID) |
| debugging (Dart's JIT needs a debugger) | **pymobiledevice3** (kernel `tunneld` tunnel, or userspace without sudo) + `support/core/device_bridge.py` + lldb with `support/core/lldb_driver.py` and Flutter's JIT helper |
| the device in VS Code | a Flutter *custom device* (`xlinux/adapters/flutter/custom_device.py`) |
| `pod install` for React Native / Expo | the real **CocoaPods**, on Homebrew's portable Ruby in the data directory, with stand-ins for the macOS tools pod scripts call (`support/core/cocoapods/`) |
| Xcode building `Pods.xcodeproj` and the app target | `xlinux/core/xcode/`: reads the project with CocoaPods' `xcodeproj` gem, resolves build settings the way Xcode layers them (xcconfigs, `$(inherited)`, conditional keys) and compiles each target with clang/swiftc, cached per target |
| `ExpoModulesJSI`'s xcodebuild script phase | SwiftPM for iOS + a small patch for Swift 6.4 (`support/patches/`), relinked and assembled like its own script does |
| Expo's Swift macro plugin (macOS binary on npm) | built from its sources for Linux, once per version |
| Xcode's script phases (flavor switches, `app.config`, "Bundle React Native code and images") | run as Xcode runs them, with the target's build settings as environment; `hermesc` is hermes-compiler's Linux build (same version React Native depends on) |
| single-size app icons (one 1024 px image) | the actool stand-in generates the iPhone/iPad sizes, like Xcode does |

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
  xcode/                   Xcode projects without Xcode (CocoaPods-based apps):
    settings.py            xcconfig files and layered build settings
    project.py             targets and settings (reads the project with CocoaPods' xcodeproj gem)
    build.py               compiles targets (cached), prebuilt React Native rules
    package.py             links the app and assembles the .app
xlinux/adapters/flutter/   everything Flutter-specific
  build.py                 flutter assemble, Runner, .app
  plugins.py               native plugins via SwiftPM (like `flutter build ios` on macOS)
  debug.py                 JIT helper + VM Service for `flutter run`/`attach`
  custom_device.py         the iPhone in `flutter devices` and VS Code
  pods.py                  plugins with Objective-C/C pods, through real CocoaPods
xlinux/adapters/expo/      Expo / React Native
  build.py                 expo prebuild, pod install, pods + app, .app
  jsi.py                   ExpoModulesJSI (the one Expo module built from source)
  macros.py                Expo's Swift macro plugin for Linux
xlinux/mcp_server.py       MCP server (screenshots, touches, text, buttons, apps, hot reload, logs)
support/core/              device_bridge.py, lldb_driver.py, darling_compat.c, xcodeproj_dump.rb,
                           app_logs.py (the app's log lines for `flutter run` / VS Code)
support/core/cocoapods/    CocoaPods on Linux: plist patch + stand-ins (xcodebuild, clang, ditto…)
support/core/bin/          xcrun, actool, apple-clang, lipo, otool, install_name_tool, codesign
support/flutter/           FlutterLinuxSceneDelegate.swift, flutter_lldb_helper.py
support/patches/           patches to third-party projects (OpenAppleMacros, xtool, expo-modules-jsi)
tools/bench_debug.py       measures startup and hot reload the way VS Code drives them
```

A new adapter provides its build and its debugging story. Signing,
installing, the device, Swift Build, `xcrun`/`actool` and packaging come from
the core.

## Installation

```
curl -fsSL https://github.com/cesardev31/xlinux/releases/latest/download/install.sh | sh
xlinux setup      # add --data-dir /path to keep everything on another drive
xlinux doctor
```

The installer puts the CLI (plain Python 3.10+, no dependencies) in
`~/.local/lib/xlinux` and the `xlinux` command in `~/.local/bin`; running it
again updates it. `xlinux setup` then installs the rest into the **data
directory** (default `~/.local/share/xlinux`; ≈20-40 GB with SwiftPM
dependencies, about what Xcode takes on a Mac; an external SSD works well). It
can be interrupted and run again: it picks up where it left off.

| What | How `setup` gets it |
|---|---|
| uv | its GitHub release, into `<data>/bin` (unless already installed) |
| Swift 6.4 | [swiftly](https://github.com/swiftlang/swiftly), everything inside the data directory |
| xtool | its AppImage from GitHub, extracted (works without FUSE, e.g. from the Flutter snap) |
| pymobiledevice3 | `uv tool install`, into the data directory |
| System packages: LLVM (`lipo`/`otool`/`install_name_tool`), poppler, unzip, git and the ones Swift needs | `apt-get`, with **one** sudo prompt; other distributions get the list to install |
| iOS/macOS SDK | **the one manual step**: Apple doesn't allow redistributing it, so you download `Xcode_27.xip` with your Apple ID ([developer.apple.com/download](https://developer.apple.com/download/all/?q=Xcode)). `setup` finds it in your Downloads folder (or `--xip PATH`), signs you in to Apple and extracts it |
| Flutter's custom device | registered in `~/.config/flutter/custom_devices.json` (Flutter itself is yours to install) |

Installed automatically the first time they're needed (or all at once with
`xlinux setup --all`):

| What | When |
|---|---|
| Darling (`.deb` packages from its GitHub release; x86_64 Debian/Ubuntu) + the compatibility shim | first release build (debug doesn't need it) |
| cairosvg | first SVG in an asset catalog |
| OpenAppleMacros with UIKit `#Preview` (`support/patches/`) | first plugin that uses it (e.g. Stripe) |
| MCP SDK + Pillow | first `xlinux mcp` |
| Flutter's iOS engine | first build |
| Ruby (Homebrew's portable build) + CocoaPods | first Expo / React Native build |
| Expo's Swift macro plugin | first Expo build (per plugin version, a few minutes) |

From a git checkout, `bin/xlinux` works the same way. To publish a release
(no CI needed): bump `__version__` in `xlinux/__init__.py`, commit and run
`tools/release.sh` (it tags, packages `xlinux.tar.gz` and uploads it with
`install.sh` through `gh`).

Recommended for debugging (much faster), in another terminal:

```
xlinux device tunnel
```

The command uses the configured data directory and asks for sudo when needed.
Keep it running in that terminal; Ctrl+C stops the tunnel.

On the iPhone: enable Developer Mode and trust your Apple ID (Settings →
General → VPN & Device Management).

## Expo / React Native

From the project folder (with `node_modules` installed):

```
xlinux run            # debug build, install, launch the app and start Metro
xlinux run --release  # release build: JavaScript bundled, no Metro needed
xlinux build [--debug] [--install]
```

It does what `expo run:ios` does on a Mac: `expo prebuild` generates `ios/`
(again when `app.json` or `package.json` change), `pod install` fills
`ios/Pods`, and every pod plus the app target is compiled; build products go to
the data directory. The first build takes a while (≈20-30 min for a
medium-sized app: it also builds `ExpoModulesJSI` and Expo's macro plugin);
after that only changed targets are rebuilt. The debug app uses
`expo-dev-client`: open it, pick the Metro server `xlinux run` prints (or type
its URL) with the iPhone on the same network as the computer, and JavaScript
changes reload live. A release build runs the pods' script phases that switch
the prebuilt frameworks (Expo modules, React Native, Hermes) to their release
flavor and React Native's bundle phase (`expo export:embed` + `hermesc`); it
works without the computer.

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

**React Native / Expo on Linux**
- React Native ships prebuilt (`React.xcframework`), but its headers are
  reachable twice: as sources under `Pods/Headers` and inside the xcframework
  through a VFS overlay. Only the xcframework copy is kept on the search path.
- Clang assigns a header to a module by the folder it was found in, and the
  overlay's virtual folders don't match React's module map: a header pulled
  into one module hides its declarations and macros from the next. Objective-C++
  and C++ are compiled without Clang modules (C/Objective-C and Swift keep them).
- Build settings as Xcode reads them: `KEY[config=*Debug*] = $(inherited) …`
  builds on the plain `KEY` of the same file; some pod scripts store lists as
  stringified Ruby arrays; per-file `-x objective-c++` on `.m` files; the
  `-Swift.h` header is also searched in the target's DerivedSources.
- Object files must be unique per path (a target can have four `ShadowNodes.cpp`).
- `ExpoModulesJSI`: SwiftPM's dynamic library comes out empty (its objects are
  archives), so it's relinked with `-force_load`; `swift/bridging` comes from
  the Swift toolchain (xtool's SDK doesn't ship `usr/include`).
- CocoaPods on Linux needs `xcodebuild -version`, an executable `command`, a
  `clang` that targets iOS for `prepare_command` stubs, binary plists read
  through Python, and `TAR_OPTIONS=--warning=no-unknown-keyword` (GNU tar's
  warnings about Apple's xattrs otherwise end up inside extracted files).

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
- App Groups: stock xtool drops them on free Apple IDs; with
  `support/patches/xtool-free-app-groups.patch` they work (tested: a widget
  reading the app's `UserDefaults(suiteName:)`). xtool renames groups to
  `group.XTL-<team>.<id>`, so derive the group from the bundle ID instead of
  hardcoding it.
- Extensions: Swift sources only; their `Assets.xcassets` isn't compiled yet.
- `type_text` (MCP) only types ASCII.
- Expo: the launch storyboard becomes an equivalent `UILaunchScreen` (the
  centered logo on the system background); expo-splash-screen's own overlay,
  which keeps the splash until `hideAsync()`, needs the compiled storyboard
  and is skipped;
  debug and release share `ios/Pods`, so switching configuration swaps the
  prebuilt frameworks each time; `pod install` writes `ios/Pods` (~1 GB)
  inside the project, as on a Mac.

## Roadmap

1. Dart changes without reinstalling (push the kernel into the app's container
   and launch with `--flutter-assets-dir`).
2. Expo / React Native: expo-splash-screen's overlay without ibtool (writing
   the compiled storyboard), upstreaming the `expo-modules-jsi` patch.

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
| [CocoaPods](https://github.com/CocoaPods/CocoaPods) (incl. [Xcodeproj](https://github.com/CocoaPods/Xcodeproj)) | The CocoaPods contributors | MIT | `pod install` and reading Xcode projects for React Native / Expo |
| [Portable Ruby](https://github.com/Homebrew/homebrew-portable-ruby) | Homebrew / Ruby core team | BSD-2-Clause (Ruby: Ruby license / BSD-2-Clause) | running CocoaPods without a system Ruby |
| [Expo](https://github.com/expo/expo) | 650 Industries | MIT | Expo modules and tooling; **our patch** to `expo-modules-jsi` in `support/patches/`; its Swift macro plugin built for Linux |
| [React Native](https://github.com/facebook/react-native), [Hermes](https://github.com/facebook/hermes) | Meta Platforms and contributors | MIT | the prebuilt React Native and Hermes frameworks apps link against |

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

### Flutter flavors

`xlinux build --flavor qa` and `xlinux run --flavor qa` select `Release-qa`;
`xlinux build --debug --flavor qa` selects `Debug-qa`. These configurations
must exist on Runner and its extension targets in `ios/Runner.xcodeproj`.
Without `--flavor`, the configuration remains Debug or Release.
The selected configuration supplies bundle IDs, display names, plist variables
and entitlements, including app groups. The flavor is also passed to Flutter
assemble so Dart's `appFlavor` and flavor-specific assets work.

For hot reload, use `flutter run -d iphone-linux --flavor qa`. The custom-device
commands recover the flavor from the running Flutter process on Linux.

Regression tests: `python3 -m unittest discover -s tests -v`. The project fixture
test requires xlinux's installed CocoaPods Ruby and `xcodeproj` gem.
