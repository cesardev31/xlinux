# xlinux

Compila, instala y depura apps en un iPhone real **desde Linux, sin Mac**.

Hoy soporta **Flutter**: release, debug con hot reload, el iPhone como dispositivo
en VS Code / `flutter run`, y plugins nativos reales (Firebase, Stripe, Google
Sign-In, WebView…). El núcleo es agnóstico al framework, así que se pueden
agregar otros adaptadores (el siguiente candidato es Expo / React Native).

No reimplementa Xcode. Junta piezas de código abierto que ya existían (xtool,
Darling, pymobiledevice3, LLVM/Swift, OpenAppleMacros…) y tapa los huecos donde
no encajaban. Los créditos están [al final](#créditos-y-licencias).

```
flutter run -d iphone-linux        # debug + hot reload (o elige el iPhone en VS Code)
xlinux run                         # release: compila, instala, abre y muestra logs
xlinux build [--debug] [--install] [--project DIR]
xlinux doctor                      # diagnóstico
xlinux setup                       # preparar el entorno (una vez)
xlinux device screenshot iphone.png
xlinux device mirror               # visor web en 127.0.0.1:8080
xlinux device mirror --mode vnc    # VNC con control táctil
```

`flutter-ios-linux` (el nombre anterior) sigue funcionando como alias.

## Estado

Probado en un iPhone 11 con iOS 27.2 y una cuenta de Apple **gratuita**, con una
app Flutter real en producción (Volaré).

| | |
|---|---|
| Release (Dart AOT) compila, instala y abre rápido | ✅ |
| Debug con JIT + lldb (sin sudo; más rápido con `tunneld`) | ✅ |
| Hot reload (`flutter run` y protocolo de VS Code), ~0,5 s | ✅ |
| iPhone visible en `flutter devices` / VS Code | ✅ |
| Native assets (hooks de Dart, p. ej. `objective_c`) en debug y release | ✅ |
| Plugins nativos por SwiftPM: firebase_core, google_sign_in (login con Google), flutter_stripe, webview, url_launcher, shared_preferences | ✅ |
| Plugins solo con CocoaPods | ❌ |
| Otros frameworks (Expo / React Native) | 🔜 |

## Cómo funciona

| En macOS lo hace… | Aquí |
|---|---|
| `gen_snapshot` (Dart AOT → ARM64) | el binario de macOS de Flutter corriendo en **Darling**, con `support/core/darling_compat.c` (finge macOS 12 en `uname` y emula `vm_map` alineado, que Darling no soporta y tumbaba el GC de Dart) |
| Xcode compila Runner (Swift/ObjC) | `swiftc`/`clang` + SDK de iOS extraído de `Xcode.xip` por **xtool** |
| Xcode + SwiftPM compilan plugins | **Swift Build** (SwiftPM 6.4) con el toolset de xtool; se genera el mismo `FlutterGeneratedPluginSwiftPackage` que en macOS y el Runner como ejecutable SwiftPM |
| `xcrun`, `clang`, `lipo`, `otool`, `install_name_tool`, `codesign` | `support/core/bin/`: xcrun propio, clang que deduce `-target` como el de Apple, herramientas de LLVM, codesign no-op (firma xtool al final) |
| `actool` (asset catalogs → `Assets.car`) | `support/core/bin/actool`: imagesets a PNG sueltos (`nombre@2x.png`), SVG/PDF rasterizados (cairosvg / pdftocairo), AppIcon → `CFBundleIcons` |
| `ibtool` (`Main.storyboard`) | `support/flutter/FlutterLinuxSceneDelegate.swift` crea la ventana por código; `UILaunchScreen` en Info.plist |
| macros de Xcode (`#Preview`, `@Observable`…) | **OpenAppleMacros** de xtool, con stubs para `#Preview` de UIKit (`support/patches/`) |
| `flutter build ios` / `flutter assemble` | `flutter assemble` con los mismos `-d` que Xcode; en release, `gen_snapshot_arm64` de la caché de Flutter es un envoltorio que lo corre en Darling |
| firmar e instalar | **xtool** (Apple ID gratis o de pago) |
| debug (el JIT de Dart necesita debugger) | **pymobiledevice3** (túnel de kernel `tunneld` o userspace sin sudo) + `support/core/device_bridge.py` + lldb con `support/core/lldb_driver.py` y el helper JIT de Flutter |
| dispositivo en VS Code | *custom device* de Flutter (`xlinux/adapters/flutter/custom_device.py`) |

## Arquitectura

```
xlinux/core/               agnóstico al framework
  toolchain.py             swiftc/clang/Swift Build para iOS, mirrors git sin historial
  app.py                   .app/.ipa sin ibtool/actool (Info.plist, íconos, recursos)
  darling.py               correr herramientas CLI de macOS
  device.py                detectar, instalar, lanzar, capturar; DebugSession (DVT + debugserver + lldb)
  setup.py                 setup/doctor comunes
  config.py                rutas y entorno (funciona sin cargar ningún env.sh)
xlinux/adapters/flutter/   todo lo específico de Flutter
  build.py                 flutter assemble, Runner, .app
  plugins.py               plugins nativos vía SwiftPM (como `flutter build ios` en macOS)
  debug.py                 helper JIT + VM Service para `flutter run`/`attach`
  custom_device.py         el iPhone en `flutter devices` y VS Code
support/core/              device_bridge.py, lldb_driver.py, darling_compat.c
support/core/bin/          xcrun, actool, apple-clang, lipo, otool, install_name_tool, codesign
support/flutter/           FlutterLinuxSceneDelegate.swift, flutter_lldb_helper.py
support/patches/           parches a proyectos de terceros (OpenAppleMacros)
tools/bench_debug.py       mide arranque y hot reload como lo usa VS Code
```

Un adaptador nuevo aporta su build y su forma de depurar. Firma, instalación,
dispositivo, Swift Build, `xcrun`/`actool` y empaquetado vienen del core.

## Instalación

Lo pesado (≈40 GB con dependencias de SwiftPM) va en un **directorio de datos**,
por ejemplo un SSD externo:

| Qué | Cómo |
|---|---|
| Swift 6.4 | [swiftly](https://github.com/swiftlang/swiftly) con `SWIFTLY_HOME_DIR` en el directorio de datos |
| xtool | AppImage en `~/.local/bin/xtool`; `xtool setup` con `Xcode_27.xip` (lo descargas tú con tu Apple ID) |
| Darling | `.deb` de `darling-core`, `darling-system`, `darling-cli` |
| pymobiledevice3 | `UV_TOOL_DIR=<datos>/uv-tools uv tool install pymobiledevice3` |
| cairosvg (para `actool`) | `uv venv <datos>/py-tools && uv pip install --python <datos>/py-tools/bin/python cairosvg` |
| LLVM del sistema | `llvm-lipo`, `llvm-otool`, `llvm-install-name-tool` (paquete `llvm-21`) y `pdftocairo` (poppler) |
| OpenAppleMacros con `#Preview` de UIKit | ver `support/patches/README.md` (solo si algún plugin usa `#Preview` de UIKit, p. ej. Stripe) |
| engine de Flutter para iOS | se descarga solo en la primera compilación |

```
bin/xlinux setup --data-dir /ruta/al/directorio-de-datos
bin/xlinux doctor
```

`setup` extrae el AppImage de xtool (así funciona sin FUSE desde el snap de
Flutter), compila el shim de Darling, instala el servidor de macros parchado si
existe y registra el iPhone en `~/.config/flutter/custom_devices.json`.

Recomendado para depurar (debug mucho más rápido), en otra terminal:

```
sudo <datos>/uv-tools/pymobiledevice3/bin/pymobiledevice3 remote tunneld
```

En el iPhone: modo desarrollador activado y confiar en tu Apple ID (Ajustes →
General → VPN y gestión de dispositivos).

## Ver y controlar el iPhone

`device screenshot` captura la pantalla completa mediante CoreDevice. También
queda registrado en el custom device, así que `flutter screenshot -d
iphone-linux` puede guardar una captura directamente. `device mirror` transmite
la pantalla mediante el túnel userspace de pymobiledevice3, sin sudo. El modo
web ofrece un visor en el navegador; el modo VNC acepta clics y los convierte en
eventos táctiles HID.

Para agentes hay una interfaz sin UI que escribe una respuesta JSON por acción.
Las coordenadas están normalizadas entre `0` y `1`, independientemente del modelo
de iPhone:

```bash
xlinux device agent snapshot
# {"ok": true, "path": "/tmp/xlinux/screen.png", "width": ..., "height": ...}
xlinux device agent tap 0.5 0.8
xlinux device agent swipe 0.5 0.8 0.5 0.2 --duration 0.4
xlinux device agent type "texto"
xlinux device agent button home
```

Esto permite automatizar el ciclo `editar → hot reload → snapshot → inspección
visual → interacción → snapshot` desde cualquier agente con acceso al shell.

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
- El helper JIT de Flutter escribe la región completa (hasta 512 KiB por parada) por el
  protocolo del debugger; basta con tocar 8 bytes por página de 16 KiB (las páginas son
  nuevas y valen cero): el primer hot reload bajó de 6,8 s a ~0,5 s.
- Túnel de kernel (`tunneld`): si está corriendo, lldb habla directo con debugserver; el
  túnel userspace (TCP en Python) funciona sin sudo pero a veces se atasca minutos.
- Una app de debug que queda sin debugger se congela en la siguiente parada del JIT y
  traba al instalador de iOS: antes de instalar se cierran sus instancias (DVT kill).
- Registro de plugins Dart: aunque Flutter crea que el iPhone es "linux", el
  `dart_plugin_registrant.dart` incluye todas las plataformas y elige con
  `Platform.isIOS` en tiempo de ejecución: hot restart funciona con plugins.
- Caché: si el código nativo generado, los plugins, el engine y las herramientas no
  cambian, se salta `swift build` (~30 s en Volaré aun sin cambios); si el `.app` es
  idéntico al instalado en ese iPhone (registro por iPhone y app), no se reinstala.
  El `.app` se instala sin comprimir.
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
  `SdkRoot`): se usa `flutter assemble`, que busca los artefactos iOS en la caché de
  Flutter (se enlazan al directorio de datos).
- Macros: el `#Preview` de UIKit (`KitViewMacro`) no está en OpenAppleMacros; se compila
  una versión con stubs (`support/patches/`) y `setup` la instala.
- Swift Build no activa los cross-import overlays (`_PassKit_SwiftUI` →
  `PayWithApplePayButton`): se pasa `-enable-cross-import-overlays`.
- `-ObjC` al enlazar el Runner: sin él el linker descarta las categorías de Objective-C
  de las librerías estáticas (AppAuth → "unrecognized selector ... presentAuthorizationRequest"
  y Google Sign-In falla en ejecución). CocoaPods/Xcode lo agregan siempre.
- Las copias de SwiftPM son de solo lectura: `actool` no debe copiar permisos.
- `flutter assemble` release deja App.framework universal: se adelgaza con `lipo -thin`
  como hace Xcode ("embed and thin").
- El AppImage de xtool no monta FUSE cuando lo lanza el snap de Flutter → se usa extraído.

## Limitaciones conocidas

- `actool` sin `Assets.car`: no hay colores ni datos del catálogo (`UIColor(named:)`,
  `NSDataAsset`), y se pierde el "template rendering" de los íconos.
- Debug: cuando cambia el Dart hay que reinstalar la app (~40 s en una app grande); sin
  cambios nativos ni de Dart, `flutter run` salta compilación nativa e instalación (~11 s).
- Cuenta gratis: el certificado dura 7 días, máximo 3 apps, sin push, Apple Pay ni
  Sign in with Apple; xtool antepone `XTL-<team>.` al bundle ID. No borres la última app
  firmada o iOS pide volver a confiar en el desarrollador.
- Rutas por defecto pensadas para la máquina donde nació (`config.DEFAULT_DATA_DIR`);
  usa `setup --data-dir`.

## Próximos pasos

1. Cambios de Dart sin reinstalar (subir el kernel al contenedor de la app y lanzar con
   `--flutter-assets-dir`).
2. Plugins solo-CocoaPods (podspec → Package.swift).
3. Adaptador Expo / React Native: `expo prebuild` + CocoaPods; el debug es más simple
   (Hermes no usa JIT, Metro por la red), el build es más difícil (Pods.xcodeproj).

## Créditos y licencias

xlinux es sobre todo **pegamento**. El trabajo pesado lo hacen estos proyectos,
que se **usan** (se instalan aparte; no se redistribuyen aquí salvo donde se indica):

| Proyecto | Autor(es) | Licencia | Para qué se usa |
|---|---|---|---|
| [xtool](https://github.com/xtool-org/xtool) | Kabir Oberai | MIT | SDK de iOS desde `Xcode.xip`, toolset/linker, Swift Build para iOS, firma e instalación; su `PackLib` fue la referencia para empaquetar SwiftPM en `.app` |
| [OpenAppleMacros](https://github.com/xtool-org/OpenAppleMacros) | Kabir Oberai | MIT | macros de Xcode en Linux; **parche propio** en `support/patches/` |
| [Darling](https://github.com/darlinghq/darling) | Darling Team | GPL-3.0 | correr el `gen_snapshot` de macOS en Linux |
| [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) | doronz88 y colaboradores | GPL-3.0-or-later | túneles RSD (userspace / `tunneld`), DVT, Developer Disk Image, debugserver, instalación, syslog; `device_bridge.py` usa su librería |
| [Flutter](https://github.com/flutter/flutter) | The Flutter Authors | BSD-3-Clause | engine iOS, `flutter assemble`, custom devices; **`support/flutter/flutter_lldb_helper.py` es una copia modificada** de su helper JIT (mantiene el aviso de copyright) |
| [Dart SDK](https://github.com/dart-lang/sdk) | The Dart project authors | BSD-3-Clause | compilador (`gen_snapshot`, frontend_server) |
| [Swift](https://github.com/swiftlang/swift), [SwiftPM](https://github.com/swiftlang/swift-package-manager), [Swift Build](https://github.com/swiftlang/swift-build), [swiftly](https://github.com/swiftlang/swiftly) | Apple y la comunidad de Swift | Apache-2.0 (con excepción de runtime) | compilar Swift/ObjC para iOS, resolver plugins |
| [LLVM](https://github.com/llvm/llvm-project) (clang, lld, lldb, llvm-lipo/otool/…) | LLVM Developer Group | Apache-2.0 con LLVM Exception | compilar, enlazar, depurar, manipular Mach-O |
| [CairoSVG](https://github.com/Kozea/CairoSVG) | Kozea | LGPL-3.0-or-later | rasterizar SVG en `actool` |
| [Poppler](https://poppler.freedesktop.org/) (`pdftocairo`) | Poppler developers | GPL-2.0 / GPL-3.0 | rasterizar PDF en `actool` |
| [libimobiledevice / usbmuxd](https://libimobiledevice.org/) | libimobiledevice project | LGPL-2.1 / GPL | conexión USB con el iPhone |
| [uv](https://github.com/astral-sh/uv) | Astral | MIT / Apache-2.0 | instalar las herramientas de Python |

**SDK de Apple:** el SDK de iOS sale de `Xcode.xip`, que descargas tú desde
developer.apple.com con tu Apple ID y aceptando su licencia; xlinux no lo
redistribuye. Revisa los términos de Apple antes de usar esto en un contexto comercial.

xlinux no está afiliado a Apple, Google, Flutter ni a ninguno de los proyectos
anteriores. "iPhone", "iOS" y "Xcode" son marcas de Apple Inc.

### Licencia de xlinux

Copyright (C) 2026 Cesar Andres Pereira.

xlinux es software libre: puedes redistribuirlo y/o modificarlo bajo los términos
de la **GNU General Public License versión 3** (o, a tu elección, cualquier
versión posterior) publicada por la Free Software Foundation. Se distribuye con
la esperanza de que sea útil, pero **sin ninguna garantía**. Ver [`LICENSE`](LICENSE).

Los componentes de terceros listados arriba conservan sus propias licencias.
`support/flutter/flutter_lldb_helper.py` deriva de Flutter (BSD-3-Clause) y
mantiene su aviso; el parche de `support/patches/` aplica sobre OpenAppleMacros (MIT).
