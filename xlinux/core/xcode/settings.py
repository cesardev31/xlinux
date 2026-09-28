"""Xcode build settings: .xcconfig files and the layered resolution Xcode does.

A target's settings come from four layers, lowest first: the project's
xcconfig, the project's settings, the target's xcconfig and the target's
settings. `$(inherited)` refers to the layer below; `KEY[cond=...]` variants
apply only for matching SDK / architecture / configuration and build on the
plain `KEY` of the same file.
"""

import fnmatch
import json
import re
import shlex
from pathlib import Path

VARIABLE = re.compile(r"\$\(([A-Za-z0-9_:]+)\)|\$\{([A-Za-z0-9_:]+)\}")
INHERITED = re.compile(r"\$[({]inherited[)}]")


def condition_matches(cond, configuration):
    """[sdk=iphoneos*], [arch=arm64], [config=*Debug*]: we build iphoneos/arm64."""
    for part in cond.strip("[]").split(","):
        key, _, pattern = part.partition("=")
        if key == "sdk" and not "iphoneos".startswith(pattern.rstrip("*")):
            return False
        if key == "arch" and pattern.rstrip("*") not in ("arm64", ""):
            return False
        if key == "config" and not fnmatch.fnmatch(configuration, pattern):
            return False
    return True


def read_xcconfig(path, configuration):
    values = {}
    if not path or not Path(path).exists():
        return values
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line.startswith('"'):
            line = line.split("//", 1)[0].strip()
        include = re.match(r'#include\??\s+"([^"]+)"', line)
        if include:
            values.update(read_xcconfig(Path(path).parent / include.group(1), configuration))
            continue
        m = re.match(r"([A-Za-z0-9_]+)(\[[^\]]*\])?\s*=\s*(.*)$", line)
        if not m:
            continue
        key, cond, value = m.groups()
        if cond:
            if not condition_matches(cond, configuration):
                continue
            if key in values:
                value = INHERITED.sub(lambda _: values[key], value, count=1)
        values[key] = value
    return values


def project_layer(raw, configuration):
    """Settings stored in the project file, with conditional keys resolved."""
    out = {}
    for key, value in raw.items():
        m = re.match(r"([A-Za-z0-9_]+)(\[[^\]]*\])?$", key)
        if m and (not m.group(2) or condition_matches(m.group(2), configuration)):
            out[m.group(1)] = value
    return out


def normalize_value(value):
    """Some pod scripts store list settings as a stringified Ruby array
    (`["$(inherited)", "\\"${PODS_ROOT}/x\\""] "extra"`): turn it back into a
    plain quoted list so it splits the way Xcode splits it."""
    stripped = value.lstrip()
    if not stripped.startswith("["):
        return value
    try:
        items, end = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return value
    if not isinstance(items, list):
        return value
    parts = []
    for item in items:
        item = str(item).strip()
        if len(item) >= 2 and item[0] == item[-1] == '"':
            item = item[1:-1]
        parts.append(item if item == "$(inherited)" else shlex.quote(item).replace("'", '"'))
    return " ".join(parts) + " " + stripped[end:]


class Settings:
    """Layered build settings with $(inherited) and variable expansion."""

    def __init__(self, layers, builtins):
        self.layers = layers  # lowest first
        self.builtins = builtins

    def raw(self, key, below=None):
        top = len(self.layers) if below is None else below
        for i in range(top - 1, -1, -1):
            if key in self.layers[i]:
                return self.layers[i][key], i
        return None, -1

    def get(self, key, default=""):
        return self._resolve(key, None, 0) or default

    def _resolve(self, key, below, depth):
        if depth > 40:
            return ""
        value, level = self.raw(key, below)
        if value is None:
            return self.builtins.get(key, "")
        value = INHERITED.sub(lambda _: self._resolve(key, level, depth + 1), normalize_value(value))
        return self.expand(value, depth)

    def expand(self, value, depth=0):
        def repl(m):
            return self._resolve((m.group(1) or m.group(2)).split(":", 1)[0], None, depth + 1)
        for _ in range(10):
            new = VARIABLE.sub(repl, value)
            if new == value:
                break
            value = new
        return value

    def list(self, key):
        """A list setting split like a shell (Xcode also reads `\\ ` inside
        quotes as a space)."""
        value = self.get(key)
        try:
            items = shlex.split(value)
        except ValueError:
            items = value.split()
        out = []
        for item in items:
            item = item.replace("\\ ", " ").strip('[],"\\').strip()
            if item:
                out.append(item)
        return out

    def yes(self, key):
        return self.get(key).upper() == "YES"
