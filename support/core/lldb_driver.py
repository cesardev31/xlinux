"""Attach lldb to the app on the iPhone, like Xcode does for Flutter debug.

On modern iOS Dart's JIT only works with a debugger attached: the engine calls
NOTIFY_DEBUGGER_ABOUT_RX_PAGES and Flutter's helper (flutter_lldb_helper.py)
touches those pages from lldb. This script runs inside lldb's Python:

  XLINUX_ARGS='{...}' lldb --batch -o "command script import lldb_driver.py"

XLINUX_ARGS (JSON) = {"helpers": [...], "local_app": ..., "pid": ..., "debugserver": "host:port",
                   "remote_app": path of the .app on the iPhone}

On iOS 17+ debugserver can't launch apps: device_bridge.py launches it
suspended (with Flutter's arguments) and here lldb attaches to the pid.
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
            print(f"\n[lldb] the app stopped: {thread.GetStopDescription(200)}")
            for frame in list(thread)[:25]:
                print(f"[lldb]   {frame}")


def run(debugger, helpers, local_app, pid, debugserver, remote_app):
    interp = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()

    def cmd(line):
        print(f"[lldb] {line}", flush=True)
        interp.HandleCommand(line, result)
        if not result.Succeeded():
            raise SystemExit(f"[lldb] `{line}` failed: {result.GetError()}")

    debugger.SetAsync(False)
    for helper in helpers:
        cmd(f'command script import "{helper}"')
    # Without the iOS shared cache on disk (Xcode extracts it into DeviceSupport),
    # lldb would read the symbols of ~500 system libraries from the iPhone's
    # memory and the app would stay frozen for minutes. We only need
    # Flutter.framework's, available locally thanks to the path mapping below.
    cmd("settings set target.memory-module-load-level minimal")
    cmd("platform select remote-ios")
    # lldb on Linux doesn't open .app bundles: give it the executable inside.
    executable = os.path.join(local_app, "Runner")
    cmd(f'target create --arch arm64-apple-ios "{executable}"')
    target = debugger.GetSelectedTarget()
    cmd(f'target modules search-paths add "{remote_app}" "{local_app}"')
    # In synchronous mode `process connect` with no process waits for a "stop"
    # that never comes: connect in asynchronous mode.
    debugger.SetAsync(True)
    listener = debugger.GetListener()
    error = lldb.SBError()
    print(f"[lldb] connecting to debugserver ({debugserver})", flush=True)
    target.ConnectRemote(listener, f"connect://{debugserver}", "gdb-remote", error)
    if not error.Success():
        raise SystemExit(f"[lldb] could not connect to debugserver: {error}")
    process = target.AttachToProcessWithID(lldb.SBListener(), int(pid), error)
    if not error.Success():
        raise SystemExit(f"[lldb] could not attach to pid {pid}: {error}")
    print(f"[lldb] attached to the app (pid {pid})", flush=True)
    for _ in range(100):
        if process.GetState() == lldb.eStateStopped:
            break
        time.sleep(0.1)

    # Synchronous mode: Continue() waits for the next stop, and lldb runs the
    # breakpoint callbacks + auto-continue on its own (the JIT helper on
    # NOTIFY_DEBUGGER_ABOUT_RX_PAGES). In async mode that only happens if
    # someone consumes the event, and the app stayed frozen (black screen).
    debugger.SetAsync(False)
    stop_file = os.environ.get("XLINUX_STOP_FILE")

    def watch_stop_file():
        while process.IsValid() and process.GetState() not in (lldb.eStateExited, lldb.eStateDetached):
            if stop_file and os.path.exists(stop_file):
                print("\n[lldb] stopping the app", flush=True)
                process.Kill()
                return
            _flush(process)
            time.sleep(0.2)

    threading.Thread(target=watch_stop_file, daemon=True).start()
    print("[lldb] app running", flush=True)

    while True:
        process.Continue()
        _flush(process)
        state = process.GetState()
        if state in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateCrashed):
            print(f"\n[lldb] the app exited (state {debugger.StateAsCString(state)}, "
                  f"code {process.GetExitStatus()})", flush=True)
            return
        if state != lldb.eStateStopped:
            continue
        reasons = [(t, t.GetStopReason()) for t in process if t.GetStopReason() != lldb.eStopReasonNone]
        if os.environ.get("XLINUX_VERBOSE"):
            for t, _ in reasons:
                print(f"[lldb] thread {t.GetIndexID()} stopped: {t.GetStopDescription(200)} "
                      f"in {t.GetFrameAtIndex(0).GetFunctionName()}", flush=True)
        if any(r == lldb.eStopReasonException for _, r in reasons):
            _report_crash(process)
            return


def __lldb_init_module(debugger, _dict):
    args = json.loads(os.environ.get("XLINUX_ARGS", "{}"))
    if not args:
        print("[lldb] XLINUX_ARGS is missing")
        return
    try:
        run(debugger, args["helpers"], args["local_app"], args["pid"], args["debugserver"], args["remote_app"])
    except BaseException as e:  # including SystemExit: lldb would swallow it silently
        import traceback
        traceback.print_exc()
        print(f"[lldb] error: {e}", flush=True)
