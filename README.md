# flutter-ios-linux

Compila, instala y depura apps en un iPhone real **desde Linux, sin Mac**.
Hoy soporta Flutter, con hot reload desde VS Code o `flutter run`. El núcleo es
agnóstico al framework, así que otros adaptadores (p. ej. Expo) son posibles.

```
flutter run -d iphone-linux        # debug + hot reload (o elige el iPhone en VS Code)
flutter-ios-linux run              # release: compila, instala, abre y muestra logs
flutter-ios-linux build [--debug] [--install] [--project DIR]
flutter-ios-linux doctor           # diagnóstico
flutter-ios-linux setup            # preparar el entorno (una vez)
```

## Estado

Probado en un iPhone 11 con iOS 27.2 y una cuenta de Apple gratuita.

| | |
|---|---|
| Release (Dart AOT) compila, instala y abre | ✅ |
| Debug con JIT + lldb, sin sudo | ✅ |
| Hot reload (`flutter run` y protocolo de VS Code) | ✅ |
| iPhone visible en `flutter devices` / VS Code | ✅ |
| Native assets (hooks de Dart, p. ej. `objective_c`) en debug | ✅ |
| Plugins nativos por SwiftPM | 🚧 Volaré (Firebase, Stripe, GoogleSignIn, WebView…): 1710/1770 pasos compilan |
| Plugins solo con CocoaPods | ❌ |
| Native assets en release | ❌ (`flutter assemble` release necesita el `gen_snapshot` de macOS) |

## Cómo funciona

| En macOS lo hace… | Aquí |
|---|---|
| `gen_snapshot` (Dart AOT → ARM64) | el binario de macOS de Flutter corriendo en **Darling**, con `support/core/darling_compat.c` (finge macOS 12 en `uname` y emula `vm_map` alineado, que Darling no soporta y tumbaba el GC de Dart) |
| Xcode compila Runner (Swift/ObjC) | `swiftc`/`clang` + SDK de iOS extraído de `Xcode.xip` por **xtool** |
| Xcode + SwiftPM compilan plugins | **Swift Build** (el sistema de build de SwiftPM 6.4) con el toolset de xtool; se genera el mismo `FlutterGeneratedPluginSwiftPackage` que en macOS y el Runner como ejecutable SwiftPM |
| `xcrun`, `clang`, `lipo`, `otool`, `install_name_tool`, `codesign` | `support/core/bin/`: xcrun propio, clang que deduce `-target` como el de Apple, herramientas de LLVM, codesign no-op (firma xtool al final) |
| `actool` (asset catalogs → `Assets.car`) | `support/core/bin/actool`: imagesets a PNG sueltos (`nombre@2x.png`), SVG/PDF rasterizados (cairosvg / pdftocairo), AppIcon → `CFBundleIcons` |
| `ibtool` (`Main.storyboard`) | `support/flutter/FlutterLinuxSceneDelegate.swift` crea la ventana por código; `UILaunchScreen` en Info.plist |
| macros de Xcode (`#Preview`, `@Observable`…) | OpenAppleMacros de xtool (con stubs para `#Preview` de UIKit) |
| `flutter build ios` / `flutter assemble` | debug: `flutter assemble` con los mismos `-d` que Xcode; release: kernel + `gen_snapshot` a mano |
| firmar e instalar | **xtool** (Apple ID gratis o de pago) |
| debug (el JIT de Dart necesita debugger) | **pymobiledevice3** (túnel userspace, sin sudo) + `support/core/device_bridge.py` + lldb con `support/core/lldb_driver.py` y el helper JIT de Flutter |
| dispositivo en VS Code | *custom device* de Flutter (`fil/adapters/flutter/custom_device.py`) |

## Arquitectura

```
fil/core/                  agnóstico al framework
  toolchain.py             swiftc/clang/Swift Build para iOS, mirrors git sin historial
  app.py                   .app/.ipa sin ibtool/actool (Info.plist, íconos, recursos)
  darling.py               correr herramientas CLI de macOS
  device.py                detectar, instalar, lanzar; DebugSession (DVT + debugserver + lldb)
  setup.py                 setup/doctor comunes
  config.py                rutas y entorno (funciona sin cargar ningún env.sh)
fil/adapters/flutter/      todo lo específico de Flutter
  build.py                 kernel, gen_snapshot, flutter assemble, Runner, .app
  plugins.py               plugins nativos vía SwiftPM (como `flutter build ios` en macOS)
  debug.py                 helper JIT + VM Service para `flutter run`/`attach`
  custom_device.py         el iPhone en `flutter devices` y VS Code
support/core/              device_bridge.py, lldb_driver.py, darling_compat.c
support/core/bin/          xcrun, actool, apple-clang, lipo, otool, install_name_tool, codesign
support/flutter/           FlutterLinuxSceneDelegate.swift, flutter_lldb_helper.py
```

Un adaptador nuevo aporta su build y su forma de depurar. Firma, instalación,
dispositivo, Swift Build, `xcrun`/`actool` y empaquetado vienen del core.

## Instalación

Lo pesado (≈40 GB con dependencias de SwiftPM) va en un **directorio de datos**,
aquí un SSD externo montado en `/run/media/cesar/games/ios-dev`:

| Qué | Cómo |
|---|---|
| Swift 6.4 | `swiftly` con `SWIFTLY_HOME_DIR` en el directorio de datos |
| xtool | AppImage en `~/.local/bin/xtool`; `xtool setup` con `Xcode_27.xip` (SDK de iOS) |
| Darling | `.deb` de `darling-core`, `darling-system`, `darling-cli` |
| pymobiledevice3 | `UV_TOOL_DIR=<datos>/uv-tools uv tool install pymobiledevice3` |
| cairosvg (para `actool`) | `uv venv <datos>/py-tools && uv pip install --python <datos>/py-tools/bin/python cairosvg` |
| LLVM del sistema | `llvm-lipo`, `llvm-otool`, `llvm-install-name-tool` (paquete `llvm-21`) y `pdftocairo` (poppler) |
| engine de Flutter para iOS | se descarga solo en la primera compilación |

```
flutter-ios-linux setup --data-dir /run/media/cesar/games/ios-dev
flutter-ios-linux doctor
```

`setup` extrae el AppImage de xtool (sin FUSE funciona desde el snap de Flutter),
compila el shim de Darling y registra el iPhone en `~/.config/flutter/custom_devices.json`.

En el iPhone: modo desarrollador activado y confiar en tu Apple ID (Ajustes →
General → VPN y gestión de dispositivos).

## Detalles que costaron (para no redescubrirlos)

**Debug**
- iOS 17+: `debugserver` no puede lanzar apps ("Operation not permitted"). Se lanzan
  **suspendidas por DVT** y lldb se adjunta al pid. La sesión DVT tiene que seguir
  abierta hasta que lldb se adjunte; si no, Flutter arranca sin debugger ("debug mode
  Flutter apps can only be launched from Flutter tooling").
- El `debugproxy` de iOS 17+ espera TCP crudo; el reenviador de pymobiledevice3 hace
  check-in de lockdown y lldb se cuelga.
- lldb en modo **síncrono** tras adjuntarse: en asíncrono los callbacks del breakpoint
  `NOTIFY_DEBUGGER_ABOUT_RX_PAGES` no corren y la app queda en negro.
- `target.memory-module-load-level minimal`: sin la caché compartida extraída
  (DeviceSupport de Xcode) lldb leería ~500 librerías desde la memoria del iPhone.

**Build**
- El lld del toolchain de Swift es más viejo que el SDK de iOS 27: se usa el de xtool
  (`-B toolset/bin`).
- SwiftPM siempre hace `git clone --mirror` completo; para dependencias con `exact:`
  (firebase-ios-sdk) se baja solo el tag con `--depth 1` y se configura como mirror.
- Swift Build verifica `actool` relativo al paquete (y no acepta symlinks), pero lo
  ejecuta desde el `PATH`; además cachea la descripción del build (`XCBuildData`).
- `flutter build bundle --target-platform ios` no sirve con native assets (le falta
  `SdkRoot`): en debug se usa `flutter assemble`, que busca los artefactos iOS en la
  caché de Flutter (se enlazan al directorio de datos).
- El AppImage de xtool no monta FUSE cuando lo lanza el snap de Flutter → se usa extraído.

## Limitaciones conocidas

- `actool` sin `Assets.car`: no hay colores ni datos del catálogo (`UIColor(named:)`,
  `NSDataAsset`), y se pierde el "template rendering" de los íconos.
- Hot reload tarda más que en Mac (cada página JIT nueva hace una parada en lldb).
- Flutter solo acepta custom devices "linux": un hot restart envía el registrante de
  plugins Dart de Linux.
- Cuenta gratis: el certificado dura 7 días, máximo 3 apps, sin push, Apple Pay ni
  Sign in with Apple. No borres la última app firmada o iOS pide volver a confiar en el
  desarrollador.
- Tras reiniciar el iPhone la Developer Disk Image se vuelve a montar sola al depurar.

## Próximos pasos

1. Terminar Volaré (plugins de Firebase, Stripe, GoogleSignIn, WebView).
2. Native assets en release y cachear el build nativo entre corridas.
3. Plugins solo-CocoaPods (podspec → Package.swift).
4. Adaptador Expo / React Native: `expo prebuild` + CocoaPods; el debug es más simple
   (Hermes no usa JIT, Metro por la red), el build es más difícil (Pods.xcodeproj).
