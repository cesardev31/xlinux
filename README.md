# flutter-ios-linux

Compila, instala y depura apps Flutter en un iPhone real **desde Linux, sin Mac**,
con hot reload desde VS Code o `flutter run`.

```
flutter run -d iphone-linux        # debug + hot reload (o elige el iPhone en VS Code)
flutter-ios-linux run              # release: compila, instala, abre y muestra logs
flutter-ios-linux build [--debug] [--install]
flutter-ios-linux doctor
```

## Cómo funciona

| Paso en macOS (Xcode) | Aquí |
|---|---|
| `gen_snapshot` (Dart AOT → ARM64) | el binario macOS de Flutter corriendo en **Darling**, con `support/darling_compat.c` (finge macOS 12 en `uname` y emula `vm_map` alineado, que Darling no soporta y tumbaba el GC de Dart) |
| compilar Runner (Swift/ObjC) | `swiftc`/`clang` + SDK de iOS extraído de `Xcode.xip` por **xtool** |
| `Main.storyboard` / `LaunchScreen` (ibtool) | `support/FlutterLinuxSceneDelegate.swift` crea la ventana por código; `UILaunchScreen` en Info.plist |
| íconos (actool) | PNG sueltos + `CFBundleIcons` |
| firmar e instalar | **xtool** (Apple ID gratis o de pago) |
| debug (JIT necesita debugger) | **pymobiledevice3** (túnel userspace, sin sudo) + `support/device_bridge.py` + lldb con `support/lldb_driver.py` y el helper JIT de Flutter |
| dispositivo en VS Code | *custom device* de Flutter (`fil/custom_device.py`) |

Detalles que costaron (para no redescubrirlos):
- iOS 17+: `debugserver` no puede lanzar apps. Se lanzan **suspendidas por DVT** y lldb se
  adjunta al pid; la sesión DVT debe seguir abierta hasta que lldb se adjunte o Flutter
  arranca sin debugger ("debug mode Flutter apps can only be launched from Flutter tooling").
- El `debugproxy` de iOS 17+ espera TCP crudo; el reenviador de pymobiledevice3 hace
  check-in de lockdown y lldb se cuelga.
- lldb en modo **síncrono** tras adjuntarse: en asíncrono los callbacks del breakpoint
  `NOTIFY_DEBUGGER_ABOUT_RX_PAGES` no corren y la app queda en negro.
- `target.memory-module-load-level minimal`: sin la caché compartida extraída (DeviceSupport
  de Xcode) lldb leería ~500 librerías desde la memoria del iPhone.
- El AppImage de xtool no monta FUSE cuando lo lanza el snap de Flutter → se usa extraído.

## Instalación

Lo pesado (≈25 GB) va en un directorio de datos (aquí un SSD externo):
Swift (swiftly), SDK de iOS (`xtool setup` con `Xcode.xip`), Darling (.deb),
pymobiledevice3 (`uv tool install`), engine de Flutter (se descarga solo).

```
flutter-ios-linux setup --data-dir /run/media/cesar/games/ios-dev
flutter-ios-linux doctor
```

`setup` extrae xtool, compila el shim de Darling y registra el iPhone en
`~/.config/flutter/custom_devices.json`.

## Limitaciones conocidas

- Plugins nativos (CocoaPods / SwiftPM): todavía no se compilan.
- Hot reload tarda más que en Mac (cada página JIT nueva hace una parada en lldb).
- Flutter solo acepta custom devices "linux": un hot restart envía el registrante de
  plugins Dart de Linux.
- Cuenta gratis: el certificado dura 7 días, máximo 3 apps; no borres la última app
  firmada o iOS pide volver a confiar en el desarrollador.
- Tras reiniciar el iPhone la Developer Disk Image se vuelve a montar sola al depurar.
