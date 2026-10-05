"""Apple ID quota: a free account can create at most 10 App IDs in a rolling
7-day window (every app and every app extension needs one). Apple doesn't
report what's left and its App IDs carry no creation date, so xlinux keeps its
own history of when it first saw each one."""

import datetime
import json
import re
import time

from . import config
from .util import output

WINDOW = 7 * 24 * 3600
FREE_LIMIT = 10


def _history_file():
    return config.data_dir() / "app-ids.json"


def _load():
    try:
        return json.loads(_history_file().read_text())
    except (OSError, ValueError):
        return {}


def team():
    """(team id, free account?) of the active team, or (None, None)."""
    out = output(["xtool", "ds", "teams"], check=False) or ""
    # "<name> [active]: <team id>" followed by "- <program>" lines
    m = re.search(r"\[active\]:\s*(\S+)\n((?:- .*\n?)*)", out)
    if not m:
        return None, None
    return m.group(1), "Free Provisioning" in m.group(2)


def identifiers():
    """Bundle identifiers registered on the active team, or None on error."""
    result = output(["xtool", "ds", "identifiers", "list"], check=False)
    if not result:
        return None
    return re.findall(r"^\s*identifier:\s*(\S+)", result, re.M)


def sync():
    """Record App IDs not seen before. Returns (team, free, entries) where
    entries maps identifier -> {"first_seen", "estimated"}."""
    team_id, free = team()
    ids = identifiers()
    if not team_id or ids is None:
        return None, None, {}
    history = _load()
    known = history.setdefault(team_id, {})
    # The first sync can't know when existing App IDs were created: count them
    # as created now (worst case) and mark them as estimated.
    first_sync = not known
    now = time.time()
    for identifier in ids:
        known.setdefault(identifier, {"first_seen": now, "estimated": first_sync})
    for identifier in list(known):
        if identifier not in ids:
            known[identifier]["deleted"] = True
    path = _history_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=1))
    return team_id, free, known


def recent(entries, now=None):
    """Entries created inside the 7-day window, oldest first."""
    now = now or time.time()
    items = [(e["first_seen"], i) for i, e in entries.items() if now - e["first_seen"] < WINDOW]
    return sorted(items)


def summary_line():
    """One line for the install output, or None if it can't be computed."""
    try:
        _, free, entries = sync()
    except Exception:  # never break an install over this
        return None
    if not free or not entries:
        return None
    used = len(recent(entries))
    return f"App IDs created in the last 7 days: {used}/{FREE_LIMIT} (free Apple ID; `xlinux account` for details)"


def _date(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def show():
    team_id, free, entries = sync()
    if not team_id:
        raise SystemExit("error: couldn't read the Apple account; log in with `xtool auth login`")
    kind = "free (10 App IDs per 7 days, 7-day profiles)" if free else "paid Apple Developer Program"
    print(f"Team {team_id}: {kind}\n")
    print("App IDs (first seen by xlinux):")
    for identifier, e in sorted(entries.items(), key=lambda kv: kv[1]["first_seen"]):
        notes = []
        if e.get("estimated"):
            notes.append("existed before tracking, may be older")
        if e.get("deleted"):
            notes.append("deleted, still counts for 7 days")
        print(f"  {_date(e['first_seen'])}  {identifier}" + (f"  ({'; '.join(notes)})" if notes else ""))
    if not free:
        return
    window = recent(entries)
    print(f"\nCreated in the last 7 days: {len(window)}/{FREE_LIMIT}, "
          f"{max(FREE_LIMIT - len(window), 0)} left")
    if window:
        print(f"Next slot frees up: {_date(window[0][0] + WINDOW)}")
    print("Reinstalling an app that already has an App ID doesn't use a slot; "
          "a new bundle ID or app extension does.")
