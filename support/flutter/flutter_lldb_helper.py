#
# Copia del helper que genera Flutter en ios/Flutter/ephemeral/ (el proyecto
# puede no tenerlo si nunca se generó en macOS). No editar.
#
import os
import time

import lldb

# flutter-ios-linux: estadísticas opcionales (FIL_JIT_STATS=1) para medir el
# costo de las paradas del JIT.
_stats = {"hits": 0, "bytes": 0, "start": time.time(), "last": 0.0}

def handle_new_rx_page(frame: lldb.SBFrame, bp_loc, extra_args, intern_dict):
    """Intercept NOTIFY_DEBUGGER_ABOUT_RX_PAGES and touch the pages."""
    base = frame.register["x0"].GetValueAsAddress()
    page_len = frame.register["x1"].GetValueAsUnsigned()

    # Note: NOTIFY_DEBUGGER_ABOUT_RX_PAGES will check contents of the
    # first page to see if handled it correctly. This makes diagnosing
    # misconfiguration (e.g. missing breakpoint) easier.
    if os.environ.get("FIL_JIT_STATS"):
        _stats["hits"] += 1
        _stats["bytes"] += page_len
        now = time.time()
        if now - _stats["last"] > 5:
            _stats["last"] = now
            print(f"[jit] {_stats['hits']} paradas, {_stats['bytes'] // 1024} KiB escritos, "
                  f"{now - _stats['start']:.0f} s", flush=True)
    data = bytearray(page_len)
    data[0:8] = b'IHELPED!'

    error = lldb.SBError()
    frame.GetThread().GetProcess().WriteMemory(base, data, error)
    if not error.Success():
        print(f'Failed to write into {base}[+{page_len}]', error)
        return

def __lldb_init_module(debugger: lldb.SBDebugger, _):
    target = debugger.GetDummyTarget()
    # Caveat: must use BreakpointCreateByRegEx here and not
    # BreakpointCreateByName. For some reasons callback function does not
    # get carried over from dummy target for the later.
    bp = target.BreakpointCreateByRegex("^NOTIFY_DEBUGGER_ABOUT_RX_PAGES$")
    bp.SetScriptCallbackFunction('{}.handle_new_rx_page'.format(__name__))
    bp.SetAutoContinue(True)
    print("-- LLDB integration loaded --")
