# Copyright 2014 The Flutter Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found at https://github.com/flutter/flutter/blob/master/LICENSE
#
# Based on the flutter_lldb_helper.py Flutter generates in ios/Flutter/ephemeral/
# (a project may not have it if it was never built on macOS).
# Modified by xlinux: touches 8 bytes per page instead of writing the whole
# region (much faster over the debugger protocol), plus optional statistics
# (XLINUX_JIT_STATS).
#
import os
import time

import lldb

# xlinux: optional statistics (XLINUX_JIT_STATS=1) to measure the cost of
# the JIT stops.
_stats = {"hits": 0, "bytes": 0, "start": time.time(), "last": 0.0}
# iOS arm64 page size (debugserver reports vm-page-size:16384).
PAGE_SIZE = int(os.environ.get("XLINUX_JIT_PAGE_SIZE", "16384"))

def handle_new_rx_page(frame: lldb.SBFrame, bp_loc, extra_args, intern_dict):
    """Intercept NOTIFY_DEBUGGER_ABOUT_RX_PAGES and touch the pages."""
    base = frame.register["x0"].GetValueAsAddress()
    page_len = frame.register["x1"].GetValueAsUnsigned()

    # Note: NOTIFY_DEBUGGER_ABOUT_RX_PAGES will check contents of the
    # first page to see if handled it correctly. This makes diagnosing
    # misconfiguration (e.g. missing breakpoint) easier.
    if os.environ.get("XLINUX_JIT_STATS"):
        _stats["hits"] += 1
        _stats["bytes"] += page_len
        now = time.time()
        if now - _stats["last"] > 5:
            _stats["last"] = now
            print(f"[jit] {_stats['hits']} stops, {_stats['bytes'] // 1024} KiB written, "
                  f"{now - _stats['start']:.0f} s", flush=True)
    process = frame.GetThread().GetProcess()
    error = lldb.SBError()
    if os.environ.get("XLINUX_JIT_FULL_WRITE"):
        # Flutter's original behavior: write the whole region.
        data = bytearray(page_len)
        data[0:8] = b'IHELPED!'
        process.WriteMemory(base, data, error)
        if not error.Success():
            print(f'Failed to write into {base}[+{page_len}]', error)
        return

    # xlinux: what matters is that the debugger "touches" every page (the
    # kernel marks it as written by the debugger). The pages are fresh and
    # zeroed, so writing 8 bytes at the start of each one leaves the same memory
    # as writing the whole region, with ~2000x less data over the debugger
    # protocol (each stop used to write up to 512 KiB).
    page = PAGE_SIZE
    for offset in range(0, page_len, page):
        chunk = b'IHELPED!' if offset == 0 else bytes(8)
        process.WriteMemory(base + offset, chunk, error)
        if not error.Success():
            print(f'Failed to write into {base + offset}', error)
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
