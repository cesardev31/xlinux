# Patches to third-party projects

## OpenAppleMacros-preview-uikit.patch

[OpenAppleMacros](https://github.com/xtool-org/OpenAppleMacros) (MIT,
© Kabir Oberai) is the open reimplementation of Xcode's macros that
[xtool](https://github.com/xtool-org/xtool) uses as the SDK's
`swift-plugin-server`. It lacks the UIKit variants of `#Preview`
(`KitViewMacro`, `Common`, `PreviewCommonGroup`), used e.g. by Stripe. The
patch adds empty stubs (like the existing `SwiftUIView` one: previews are only
meaningful inside Xcode).

Base: commit `e932208f5610a5024d3a043e202f0f67b926e1cf`.

```
git clone https://github.com/xtool-org/OpenAppleMacros <data>/src/OpenAppleMacros
cd <data>/src/OpenAppleMacros && git apply <repo>/support/patches/OpenAppleMacros-preview-uikit.patch
swift build -c release --product OpenAppleMacrosServer
cp .build/out/Products/Release-linux-x86_64/OpenAppleMacrosServer <data>/bin/
xlinux setup   # installs it over xtool's (keeping the original as .orig)
```
