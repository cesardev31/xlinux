"""Read an Xcode project (.xcodeproj) without Xcode.

The project file is parsed by CocoaPods' `xcodeproj` Ruby gem
(support/core/xcodeproj_dump.rb): targets, their source/header/resource
build phases, script phases and build settings. This module resolves those
settings per target (see settings.py) with the builtins Xcode provides.
"""

import json
import shlex
import shutil
import subprocess
from pathlib import Path

from .. import config
from ..util import output
from .settings import Settings, project_layer, read_xcconfig

DUMP_SCRIPT = config.SUPPORT / "core/xcodeproj_dump.rb"


def dump(xcodeproj, configuration, ruby_env):
    """JSON description of `xcodeproj` for `configuration` (Debug/Release)."""
    text = output([ruby_env["XLINUX_RUBY"], "-EUTF-8", DUMP_SCRIPT, xcodeproj, configuration], env=ruby_env)
    return json.loads(text)


class XcodeProject:
    """Targets of one project and how Xcode would resolve their settings."""

    def __init__(self, description, build_dir, configuration):
        self.description = description
        self.root = Path(description["project_dir"])  # SRCROOT
        self.build_dir = Path(build_dir).resolve()
        self.configuration = configuration
        self.targets = {t["name"]: t for t in description["targets"]}
        self.project_xcconfig = read_xcconfig(description.get("project_xcconfig"), configuration)
        self.project_settings = project_layer(description["project_settings"], configuration)

    def settings(self, target):
        name = target["name"]
        own = project_layer(target["settings"], self.configuration)
        sdk = config.IPHONE_SDK
        builtins = {
            "SRCROOT": str(self.root), "PROJECT_DIR": str(self.root),
            "BUILD_DIR": str(self.build_dir), "CONFIGURATION": self.configuration,
            "EFFECTIVE_PLATFORM_NAME": "-iphoneos", "PLATFORM_NAME": "iphoneos",
            "SDKROOT": str(sdk), "SDK_DIR": str(sdk), "PLATFORM_DIR": str(sdk.parent.parent.parent),
            "TARGET_NAME": name, "PRODUCT_NAME": own.get("PRODUCT_NAME", "$(TARGET_NAME)"),
            "DEVELOPMENT_LANGUAGE": "en", "ARCHS": "arm64", "CURRENT_ARCH": "arm64",
            "LOCAL_LIBRARY_DIR": "/Library", "DT_TOOLCHAIN_DIR": "",
            # Xcode's default, which settings extend with $(inherited).
            "OTHER_CPLUSPLUSFLAGS": "$(OTHER_CFLAGS)",
        }
        layers = [self.project_xcconfig, self.project_settings,
                  read_xcconfig(target.get("xcconfig"), self.configuration), own]
        s = Settings(layers, builtins)
        products = s.get("CONFIGURATION_BUILD_DIR") or \
            f"{self.build_dir}/{self.configuration}-iphoneos/{name}"
        temp = self.build_dir / "Intermediates" / self.configuration / name
        s.builtins.update(CONFIGURATION_BUILD_DIR=products, BUILT_PRODUCTS_DIR=products,
                          TARGET_TEMP_DIR=str(temp), DERIVED_FILE_DIR=str(temp / "DerivedSources"))
        return s

    def script_env(self, target, s, extra=None):
        """Environment for a target's script phase: Xcode exports every build
        setting to the scripts it runs."""
        env = config.tool_env()
        keys = set(s.builtins)
        for layer in s.layers:
            keys |= set(layer)
        for key in keys:
            env[key] = s.get(key)
        env.update(PODS_TARGET_SRCROOT=s.get("PODS_TARGET_SRCROOT"), TARGET_NAME=target["name"],
                   BUNDLE_FORMAT="shallow", ACTION="build")
        node = shutil.which("node", path=env.get("PATH"))
        if node:
            env["NODE_BINARY"] = node
        env.update(extra or {})
        return env

    def run_script(self, target, s, script, extra_env=None):
        env = self.script_env(target, s, extra_env)
        result = subprocess.run(["/bin/bash", "-c", script["script"]], cwd=self.root, env=env,
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"script phase '{script['name']}' of {target['name']} failed:\n"
                               f"{(result.stdout + result.stderr)[-4000:]}")
        return result

    def prepare_xcframeworks(self, target, s):
        """What <Pod>-xcframeworks.sh does: expose each vendored XCFramework's
        iPhone slice under PODS_XCFRAMEWORKS_BUILD_DIR."""
        script = self.root / "Target Support Files" / target["name"] / f"{target['name']}-xcframeworks.sh"
        if not script.exists():
            return
        dest_root = Path(s.get("PODS_XCFRAMEWORKS_BUILD_DIR"))
        for line in script.read_text().splitlines():
            if not line.strip().startswith("install_xcframework "):
                continue
            args = shlex.split(line.strip())[1:]
            xcframework, dest = Path(s.expand(args[0])), dest_root / args[1]
            for slice_id in (a for a in args[3:] if a.startswith("ios-") and "simulator" not in a):
                src = xcframework / slice_id
                if not src.is_dir():
                    continue
                dest.mkdir(parents=True, exist_ok=True)
                for item in src.iterdir():
                    link = dest / item.name
                    if item.name not in ("dSYMs", "BCSymbolMaps") and not (link.exists() or link.is_symlink()):
                        link.symlink_to(item)

    def prepare_all_xcframeworks(self):
        for target in self.targets.values():
            self.prepare_xcframeworks(target, self.settings(target))
