"""Lanza la app en el iPhone bajo lldb, como hace Xcode para Flutter debug.

En iOS moderno el JIT de Dart solo funciona con un debugger adjunto: el
engine llama a NOTIFY_DEBUGGER_ABOUT_RX_PAGES y el helper de Flutter
(flutter_lldb_helper.py) toca esas páginas desde lldb. Este script corre
dentro del Python de lldb:

  FIL_ARGS='{...}' lldb --batch -o "command script import lldb_driver.py"

FIL_ARGS (JSON) = {"helpers": [...], "local_app": ..., "pid": ..., "debugserver": "host:puerto",
                   "remote_app": ruta del .app en el iPhone}

En iOS 17+ debugserver no puede lanzar apps: device_bridge.py la lanza
suspendida (con los argumentos de Flutter) y aquí lldb se adjunta al pid.
"""

import json
import os
import sys
import threading
import time

import lldb


def _flush(process):
    for read in (process.GetSTDOUT, process.GetSTDERR):
        while True:
            chunk = read(4096)
            if not chunk:
                break
            sys.stdout.write(chunk)
    sys.stdout.flush()


def _report_crash(process):
    for thread in process:
        reason = thread.GetStopReason()
        if reason in (lldb.eStopReasonException, lldb.eStopReasonSignal):
            print(f"\n[lldb] la app se detuvo: {thread.GetStopDescription(200)}")
            for frame in list(thread)[:25]:
                print(f"[lldb]   {frame}")


def run(debugger, helpers, local_app, pid, debugserver, remote_app):
    interp = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()

    def cmd(line):
        print(f"[lldb] {line}", flush=True)
        interp.HandleCommand(line, result)
        if not result.Succeeded():
            raise SystemExit(f"[lldb] falló `{line}`: {result.GetError()}")

    debugger.SetAsync(False)
    for helper in helpers:
        cmd(f'command script import "{helper}"')
    # Sin la caché compartida de iOS en disco (Xcode la extrae en DeviceSupport),
    # lldb leería los símbolos de ~500 librerías del sistema desde la memoria del
    # iPhone y la app se quedaría congelada minutos. Solo necesitamos los de
    # Flutter.framework, que están en local gracias al mapeo de rutas de abajo.
    cmd("settings set target.memory-module-load-level minimal")
    cmd("platform select remote-ios")
    # lldb en Linux no abre bundles .app: se le da el ejecutable de adentro.
    executable = os.path.join(local_app, "Runner")
    cmd(f'target create --arch arm64-apple-ios "{executable}"')
    target = debugger.GetSelectedTarget()
    cmd(f'target modules search-paths add "{remote_app}" "{local_app}"')
    # En modo síncrono `process connect` sin proceso espera un "stop" que nunca
    # llega: la conexión se hace en modo asíncrono.
    debugger.SetAsync(True)
    listener = debugger.GetListener()
    error = lldb.SBError()
    print(f"[lldb] conectando a debugserver ({debugserver})", flush=True)
    target.ConnectRemote(listener, f"connect://{debugserver}", "gdb-remote", error)
    if not error.Success():
        raise SystemExit(f"[lldb] no se pudo conectar a debugserver: {error}")
    process = target.AttachToProcessWithID(lldb.SBListener(), int(pid), error)
    if not error.Success():
        raise SystemExit(f"[lldb] no se pudo adjuntar al pid {pid}: {error}")
    print(f"[lldb] adjuntado a la app (pid {pid})", flush=True)
    for _ in range(100):
        if process.GetState() == lldb.eStateStopped:
            break
        time.sleep(0.1)

    # Modo síncrono: Continue() espera la siguiente parada, y lldb procesa por
    # su cuenta los callbacks + auto-continue de los breakpoints (el helper JIT
    # en NOTIFY_DEBUGGER_ABOUT_RX_PAGES). En asíncrono eso solo pasa si alguien
    # consume el evento, y la app se quedaba congelada (pantalla negra).
    debugger.SetAsync(False)
    stop_file = os.environ.get("FIL_STOP_FILE")

    def watch_stop_file():
        while process.IsValid() and process.GetState() not in (lldb.eStateExited, lldb.eStateDetached):
            if stop_file and os.path.exists(stop_file):
                print("\n[lldb] deteniendo la app", flush=True)
                process.Kill()
                return
            _flush(process)
            time.sleep(0.2)

    threading.Thread(target=watch_stop_file, daemon=True).start()
    print("[lldb] app corriendo", flush=True)

    while True:
        process.Continue()
        _flush(process)
        state = process.GetState()
        if state in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateCrashed):
            print(f"\n[lldb] la app terminó (estado {debugger.StateAsCString(state)}, "
                  f"código {process.GetExitStatus()})", flush=True)
            return
        if state != lldb.eStateStopped:
            continue
        reasons = [(t, t.GetStopReason()) for t in process if t.GetStopReason() != lldb.eStopReasonNone]
        if os.environ.get("FIL_VERBOSE"):
            for t, _ in reasons:
                print(f"[lldb] parada hilo {t.GetIndexID()}: {t.GetStopDescription(200)} "
                      f"en {t.GetFrameAtIndex(0).GetFunctionName()}", flush=True)
        if any(r == lldb.eStopReasonException for _, r in reasons):
            _report_crash(process)
            return


def __lldb_init_module(debugger, _dict):
    args = json.loads(os.environ.get("FIL_ARGS", "{}"))
    if not args:
        print("[lldb] falta FIL_ARGS")
        return
    try:
        run(debugger, args["helpers"], args["local_app"], args["pid"], args["debugserver"], args["remote_app"])
    except BaseException as e:  # SystemExit incluido: lldb lo tragaría en silencio
        import traceback
        traceback.print_exc()
        print(f"[lldb] error: {e}", flush=True)
