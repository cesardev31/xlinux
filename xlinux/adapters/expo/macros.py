"""Expo's Swift macro plugin (@expo/expo-modules-macros-plugin) for Linux.

Expo modules load it with `-load-plugin-executable <path>#ExpoModulesMacros`,
and npm ships only a macOS binary. Its sources are in the package: build them
once per version into the data directory.
"""

import json
import re
import shutil
from pathlib import Path

from ...core import config
from ...core.util import log, run

TOOL = "ExpoModulesMacros-tool"


def ensure(project_dir):
    """{plugin executable name: Linux build}, or {} if the project doesn't use it."""
    package = Path(project_dir) / "node_modules/@expo/expo-modules-macros-plugin"
    if not (package / "apple/Package.swift").exists():
        return {}
    version = json.loads((package / "package.json").read_text())["version"]
    work = config.data_dir() / "expo" / f"expo-modules-macros-{version}"
    tool = work / TOOL
    if tool.exists():
        return {TOOL: str(tool)}
    log(f"Building Expo's Swift macro plugin {version} for Linux (once, a few minutes)")
    source = work / "src"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(package / "apple", source, ignore=shutil.ignore_patterns(TOOL, ".build"))
    # The npm package lacks the test sources its test target points at.
    manifest = source / "Package.swift"
    manifest.write_text(re.sub(r",\s*\.testTarget\(.*?\n    \),\n", ",\n", manifest.read_text(), flags=re.S))
    swift = config.swift_bin()
    scratch = work / "build"
    env = config.tool_env()
    env["PATH"] = f"{swift}:/usr/bin:/bin"  # a plain Linux build, without our iOS stand-ins
    # Swift Build builds nothing for a package without products; the native
    # build system compiles the macro target (linked by hand below).
    run([swift / "swift", "build", "-c", "release", "--build-system", "native", "--target", "ExpoModulesMacros",
         "--package-path", source, "--scratch-path", scratch], env=env, capture_output=True, text=True)
    objects = sorted(str(o) for o in (scratch / "release").resolve().glob("*-tool.build/*.o"))
    # SwiftPM renames an executable module's main to <module>_main.
    run([swift / "swiftc", "-Xlinker", "--defsym", "-Xlinker", "main=ExpoModulesMacros_main",
         *objects, "-o", tool], env=env, capture_output=True, text=True)
    return {TOOL: str(tool)}
