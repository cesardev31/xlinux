# Parches a proyectos de terceros

## OpenAppleMacros-preview-uikit.patch

[OpenAppleMacros](https://github.com/xtool-org/OpenAppleMacros) (MIT,
© Kabir Oberai) es la reimplementación libre de las macros de Xcode que usa
[xtool](https://github.com/xtool-org/xtool) como `swift-plugin-server` del SDK.
No incluye las variantes de `#Preview` para UIKit (`KitViewMacro`, `Common`,
`PreviewCommonGroup`), que usa p. ej. Stripe. El parche agrega stubs vacíos
(igual que el `SwiftUIView` existente: las previews solo sirven en Xcode).

Base: commit `e932208f5610a5024d3a043e202f0f67b926e1cf`.

```
git clone https://github.com/xtool-org/OpenAppleMacros <datos>/src/OpenAppleMacros
cd <datos>/src/OpenAppleMacros && git apply <repo>/support/patches/OpenAppleMacros-preview-uikit.patch
swift build -c release --product OpenAppleMacrosServer
cp .build/out/Products/Release-linux-x86_64/OpenAppleMacrosServer <datos>/bin/
xlinux setup   # lo instala sobre el de xtool (guarda el original como .orig)
```
