#!/usr/bin/env python3
"""Measure the debug loop the way VS Code drives it (`flutter run --machine`).

  tools/bench_debug.py <project> <file.dart> <text to replace>

Reports startup (until app.started) and two hot reloads, changing the given
text in the file (restored at the end). Use it with a throwaway project.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

project, dart_file, needle = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
original = (project / dart_file).read_text()
assert needle in original, f"{needle!r} not found in {dart_file}"

proc = subprocess.Popen(["flutter", "run", "--machine", "-d", "iphone-linux"], cwd=project,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
app_id = None
t0 = time.time()


def wait_for(pred, timeout=900):
    global app_id
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise SystemExit("flutter run exited")
        if not line.startswith("[{"):
            continue
        try:
            messages = json.loads(line)
        except ValueError:
            continue
        for msg in messages:
            if msg.get("event") == "app.start":
                app_id = msg["params"]["appId"]
            if msg.get("event") == "app.progress" and msg["params"].get("message"):
                print(f"  {time.time() - t0:6.1f} s  {msg['params']['message']}", flush=True)
            if msg.get("event") == "app.progress" and msg["params"].get("finished"):
                print(f"  {time.time() - t0:6.1f} s  (fin de {msg['params'].get('progressId')})", flush=True)
            if pred(msg):
                return msg
    raise SystemExit("timeout")


def reload(n, text):
    (project / dart_file).write_text(original.replace(needle, text))
    start = time.time()
    proc.stdin.write(json.dumps([{"method": "app.restart", "id": 100 + n,
                                  "params": {"appId": app_id, "fullRestart": False, "pause": False}}]) + "\n")
    proc.stdin.flush()
    msg = wait_for(lambda m: m.get("id") == 100 + n)
    return time.time() - start, msg.get("result", {}).get("message")


try:
    wait_for(lambda m: m.get("event") == "app.start")
    t_start = time.time()
    wait_for(lambda m: m.get("event") == "app.started")
    t_started = time.time()
    print(f"build + install + launch:         {t_start - t0:6.1f} s")
    print(f"launch → app.started:             {t_started - t_start:6.1f} s")
    for n in (1, 2):
        dt, message = reload(n, f"{needle} ({n})")
        print(f"hot reload {n}:                     {dt:6.1f} s  ({message})")
finally:
    (project / dart_file).write_text(original)
    if app_id:
        proc.stdin.write(json.dumps([{"method": "app.stop", "id": 999, "params": {"appId": app_id}}]) + "\n")
        proc.stdin.flush()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    else:
        proc.kill()
