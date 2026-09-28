# Patches to third-party projects

## OpenAppleMacros-preview-uikit.patch

[OpenAppleMacros](https://github.com/xtool-org/OpenAppleMacros) (MIT,
© Kabir Oberai) is the open reimplementation of Xcode's macros that
[xtool](https://github.com/xtool-org/xtool) uses as the SDK's
`swift-plugin-server`. It lacks the UIKit variants of `#Preview`
(`KitViewMacro`, `Common`, `PreviewCommonGroup`), used e.g. by Stripe. The
patch adds empty stubs for them and for `SwiftUIViewGroup_1` (SwiftUI
`#Preview(arguments:)`), like the existing `SwiftUIView` one: previews are only
meaningful inside Xcode. Submitted upstream as
[xtool-org/OpenAppleMacros#5](https://github.com/xtool-org/OpenAppleMacros/pull/5);
once xtool ships it, this patch and `deps.ensure_macro_server()` can go.

`xlinux` builds and installs it by itself the first time a plugin needs it
(`xlinux/core/deps.py`); by hand:

Base: commit `e932208f5610a5024d3a043e202f0f67b926e1cf`.

```
git clone https://github.com/xtool-org/OpenAppleMacros <data>/src/OpenAppleMacros
cd <data>/src/OpenAppleMacros && git apply <repo>/support/patches/OpenAppleMacros-preview-uikit.patch
swift build -c release --product OpenAppleMacrosServer
cp .build/out/Products/Release-linux-x86_64/OpenAppleMacrosServer <data>/bin/
xlinux setup   # installs it over xtool's (keeping the original as .orig)
```

## xtool-free-app-groups.patch

[xtool](https://github.com/xtool-org/xtool) (MIT, © Kabir Oberai) drops App
Groups when signing with a free Apple ID (a `FIXME` in
`DeveloperServicesCapability.swift`). The Xcode DeveloperServices API does let
free teams register and assign them, as AltStore/SideStore do: with this
one-line patch a widget and its app share `UserDefaults(suiteName:)` on a free
account. xtool renames groups to `group.XTL-<team>.<id>`, so apps should derive
the group from their bundle ID (`"group." + Bundle.main.bundleIdentifier`).

Base: tag `1.20.1`. Needs `libssl-dev` and `libimobiledevice-dev`.

```
git clone --branch 1.20.1 https://github.com/xtool-org/xtool <data>/src/xtool
cd <data>/src/xtool && git apply <repo>/support/patches/xtool-free-app-groups.patch
swift build -c release --product xtool --scratch-path <data>/src/xtool-build
mkdir -p <data>/xtool/patched
cp <data>/src/xtool-build/out/Products/Release-linux-x86_64/{xtool,libXADI.so} <data>/xtool/patched/
ln -sfn patched/xtool <data>/xtool/xtool-patched   # <data>/bin/xtool prefers it
```
