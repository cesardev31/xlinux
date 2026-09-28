"""Compile Xcode targets (static frameworks and libraries) without Xcode.

What Xcode does for each target, the way CocoaPods sets them up: stage the
framework's public headers and module map, compile Swift (whole module) and
then C / Objective-C / C++ in parallel, and archive the objects. Each target
is skipped when its inputs didn't change (flags, sources, headers and the
fingerprints of its dependencies).

Prebuilt React Native (React.xcframework) needs two rules Xcode doesn't:
  - its headers exist twice, as sources under Pods/Headers and inside the
    xcframework (reached through a VFS overlay). Only the xcframework copy is
    kept on the search path.
  - Clang assigns a header to a module by the folder it was found in, and the
    overlay's virtual folders don't match React's module map. A header pulled
    into one module then hides its declarations and macros from the next, so
    Objective-C++ and C++ are compiled without Clang modules (C and
    Objective-C keep them; Swift imports modules through its own importer).
"""

import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .. import config
from ..util import log

TOOLS = config.SUPPORT / "core/cocoapods/bin"  # ditto & co. for CocoaPods' script phases
LANGUAGES = {".m": "objective-c", ".mm": "objective-c++", ".c": "c",
             ".cpp": "c++", ".cc": "c++", ".cxx": "c++"}
BUILDER = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class BuildError(Exception):
    pass


def run(cmd):
    cmd = [str(c) for c in cmd if c != ""]
    if os.environ.get("XLINUX_VERBOSE"):
        print("   $ " + shlex.join(cmd), flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True, env=config.tool_env())
    if result.returncode != 0:
        errors = "\n".join(line for line in result.stderr.splitlines()
                           if "does not support linking for platform iOS" not in line)
        raise BuildError(f"{Path(cmd[0]).name} failed:\n{errors.strip()[-6000:]}")
    return result


# --- Prebuilt React Native -----------------------------------------------------

def react_duplicates(pods_root):
    """Pods/Headers/{Public,Private}/<pod> folders React.xcframework already ships."""
    headers = pods_root / "React-Core-prebuilt/React.xcframework/Headers"
    if not headers.is_dir():
        return set()
    shipped = {d.name for d in headers.iterdir()}
    dups = set()
    for kind in ("Public", "Private"):
        base = pods_root / "Headers" / kind
        if base.is_dir():
            dups |= {str(base / d.name) for d in base.iterdir() if d.name.replace("-", "_") in shipped}
    return dups


def react_overlay(pods_root):
    """The flags exposing React.xcframework's headers (React Native adds them to some pods only)."""
    vfs = pods_root / "React-Core-prebuilt/React-VFS.yaml"
    headers = pods_root / "React-Core-prebuilt/React.xcframework/Headers"
    return ["-ivfsoverlay", str(vfs), "-isystem", str(headers)] if vfs.exists() else []


def _under(path, dups):
    path = os.path.normpath(path)
    return any(path == d or path.startswith(d + "/") for d in dups)


def drop_duplicates(flags, dups, overlay):
    """Remove search paths into `dups`; if any was removed, add the xcframework's instead."""
    out, skip = [], False
    flags = [str(f) for f in flags]
    for i, f in enumerate(flags):
        if skip:
            skip = False
        elif f in ("-I", "-isystem", "-iquote") and i + 1 < len(flags) and _under(flags[i + 1], dups):
            skip = True
        elif f.startswith("-I/") and _under(f[2:], dups):
            pass
        else:
            out.append(f)
    if len(out) != len(flags) and overlay and overlay[1] not in out:
        out += overlay
    return out


# --- One target ------------------------------------------------------------------

class TargetBuild:
    def __init__(self, project, target, settings, context):
        self.project, self.t, self.s, self.ctx = project, target, settings, context
        s = settings
        self.name = target["name"]
        self.product = s.get("PRODUCT_NAME") or self.name
        self.module = s.get("PRODUCT_MODULE_NAME") or re.sub(r"\W", "_", self.product)
        self.is_framework = target.get("product_type") == "com.apple.product-type.framework"
        self.out = Path(s.get("CONFIGURATION_BUILD_DIR"))
        self.temp = Path(s.builtins["TARGET_TEMP_DIR"])
        self.derived = Path(s.builtins["DERIVED_FILE_DIR"])
        self.objs = self.temp / "Objects"
        self.fw = self.out / f"{self.product}.framework"
        self.triple = f"arm64-apple-ios{s.get('IPHONEOS_DEPLOYMENT_TARGET') or config.MIN_IOS}"
        self.sources = [src for src in target.get("sources", []) if not src["path"].endswith("-dummy.m")]
        self.archive = self.fw / self.product if self.is_framework else self.out / f"lib{self.product}.a"

    def path(self, value):
        return Path(value) if os.path.isabs(value) else self.project.root / value

    def search_flags(self):
        s, flags = self.s, []
        for path in s.list("HEADER_SEARCH_PATHS"):
            flags += ["-I", path]
        for path in s.list("USER_HEADER_SEARCH_PATHS"):
            flags += ["-iquote", path]
        # Xcode's header maps let a target find its own headers by bare name,
        # and its DerivedSources hold <Module>-Swift.h.
        for d in sorted({str(Path(h["path"]).parent) for h in self.t.get("headers", [])}):
            flags += ["-iquote", d]
        flags += ["-I", self.derived, "-I", self.out / "include", "-F", self.out]
        for path in s.list("FRAMEWORK_SEARCH_PATHS"):
            flags += ["-F", path]
        return drop_duplicates(flags, self.ctx.react_dups, self.ctx.react_overlay)

    def defines(self):
        return [f"-D{d}" for d in self.s.list("GCC_PREPROCESSOR_DEFINITIONS")]

    def clang_flags(self, lang):
        s = self.s
        use_modules = lang in ("c", "objective-c") and s.yes("CLANG_ENABLE_MODULES")
        flags = ["-target", self.triple, "-isysroot", config.IPHONE_SDK, "-Wno-everything",
                 "-O0" if s.get("GCC_OPTIMIZATION_LEVEL", "0") == "0" else "-Os"]
        if use_modules:
            flags += ["-fmodules", f"-fmodules-cache-path={self.project.build_dir / 'ModuleCache'}"]
            if self.is_framework:
                flags.append(f"-fmodule-name={self.module}")
        if lang.startswith("objective-c") and s.yes("CLANG_ENABLE_OBJC_ARC"):
            flags.append("-fobjc-arc")
        if lang in ("c++", "objective-c++"):
            flags += [f"-std={s.get('CLANG_CXX_LANGUAGE_STANDARD', 'gnu++20')}", "-stdlib=libc++"]
            other = s.list("OTHER_CPLUSPLUSFLAGS")
        else:
            flags.append(f"-std={s.get('GCC_C_LANGUAGE_STANDARD', 'gnu11')}")
            other = s.list("OTHER_CFLAGS")
        pch = s.get("GCC_PREFIX_HEADER")
        if pch:
            flags += ["-include", self.path(pch)]
        flags = drop_duplicates(flags + self.defines() + self.search_flags() + other,
                                self.ctx.react_dups, self.ctx.react_overlay)
        if not use_modules:
            flags = [f for f in flags if not f.startswith(("-fmodule-map-file", "-fmodules", "-fcxx-modules"))]
        return flags

    def per_file(self, src, lang):
        # Per-file flags are expanded like settings ($(inherited) is empty there);
        # `-x objective-c++` on a .m changes which language's settings apply.
        raw = re.sub(r"\$[({]inherited[)}]", "", src.get("flags") or "")
        flags = shlex.split(self.s.expand(raw)) if raw.strip() else []
        if "-x" in flags[:-1]:
            i = flags.index("-x")
            lang = flags[i + 1]
            flags = flags[:i] + flags[i + 2:]
        return lang, flags

    def compile_c(self, src):
        path = Path(src["path"])
        lang, extra = self.per_file(src, LANGUAGES[path.suffix])
        obj = self.objs / f"{path.name}-{hashlib.sha1(str(path).encode()).hexdigest()[:8]}.o"
        run(["clang", "-x", lang, *self.clang_flags(lang), *extra, "-c", path, "-o", obj])
        return obj

    def swift_flags(self):
        s, flags = self.s, []
        for f in s.list("OTHER_SWIFT_FLAGS"):
            exe, sep, module = f.partition("#")
            if sep and Path(exe).name in self.ctx.macro_plugins:  # macOS-only plugin binaries
                f = f"{self.ctx.macro_plugins[Path(exe).name]}#{module}"
            flags.append(f)
        for path in s.list("SWIFT_INCLUDE_PATHS"):
            flags += ["-I", path]
        for path in s.list("FRAMEWORK_SEARCH_PATHS"):
            flags += ["-F", path]
        flags += ["-F", self.out]
        for cond in s.list("SWIFT_ACTIVE_COMPILATION_CONDITIONS"):
            flags += ["-D", cond]
        bridging = s.get("SWIFT_OBJC_BRIDGING_HEADER")
        if bridging:
            flags += ["-import-objc-header", self.path(bridging)]
        headers = self.t.get("headers", [])
        if self.is_framework and any(h["visibility"] == "public" for h in headers):
            flags += ["-import-underlying-module"]
        elif not self.is_framework and s.get("MODULEMAP_FILE") and headers:
            flags += ["-import-underlying-module", "-Xcc", f"-fmodule-map-file={self.path(s.get('MODULEMAP_FILE'))}"]
        # Module maps and the React overlay from OTHER_CFLAGS matter to Swift's
        # Clang importer too (`import React`).
        cflags = s.list("OTHER_CFLAGS")
        for i, f in enumerate(cflags):
            if f.startswith("-fmodule-map-file="):
                flags += ["-Xcc", f]
            elif f == "-ivfsoverlay" and i + 1 < len(cflags):
                flags += ["-Xcc", "-ivfsoverlay", "-Xcc", cflags[i + 1]]
        for f in self.search_flags() + self.defines():
            flags += ["-Xcc", str(f)]
        return flags

    def compile_swift(self, files):
        s = self.s
        module_dir = (self.fw / "Modules" if self.is_framework else self.out) / f"{self.module}.swiftmodule"
        module_dir.mkdir(parents=True, exist_ok=True)
        header = (self.fw / "Headers" if self.is_framework else self.derived) / f"{self.module}-Swift.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        obj = self.objs / f"{self.module}-swift.o"
        run(["swiftc", "-target", self.triple, "-sdk", config.IPHONE_SDK, "-resource-dir", config.SWIFT_RESOURCES,
             "-module-name", self.module, "-swift-version", (s.get("SWIFT_VERSION") or "5").split(".")[0],
             "-parse-as-library", "-wmo", "-num-threads", "0",
             "-Onone" if s.get("SWIFT_OPTIMIZATION_LEVEL", "-Onone") == "-Onone" else "-O",
             "-enable-testing" if s.yes("ENABLE_TESTABILITY") else "",
             "-Xfrontend", "-enable-cross-import-overlays",
             "-module-cache-path", self.project.build_dir / "ModuleCache",
             "-emit-module", "-emit-module-path", module_dir / "arm64-apple-ios.swiftmodule",
             "-emit-objc-header", "-emit-objc-header-path", header,
             *self.swift_flags(), "-c", *files, "-o", obj])
        if header.parent != self.derived:  # `#import "Module-Swift.h"` inside the target
            self.derived.mkdir(parents=True, exist_ok=True)
            shutil.copy2(header, self.derived / header.name)
        if self.is_framework:
            mm = self.fw / "Modules/module.modulemap"
            if mm.exists() and f"{self.module}.Swift" not in mm.read_text():
                with open(mm, "a") as f:
                    f.write(f'\nmodule {self.module}.Swift {{\n  header "{header.name}"\n  requires objc\n}}\n')
        return obj

    def stage_headers(self):
        """Public headers and module map in place before compiling (Swift
        imports its own module)."""
        if not self.is_framework:
            return
        (self.fw / "Headers").mkdir(parents=True, exist_ok=True)
        for h in self.t.get("headers", []):
            if h["visibility"] == "public":
                shutil.copy2(h["path"], self.fw / "Headers" / Path(h["path"]).name)
        modulemap = self.s.get("MODULEMAP_FILE")
        if modulemap:
            (self.fw / "Modules").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.path(modulemap), self.fw / "Modules/module.modulemap")

    def run_compatibility_header_script(self):
        """CocoaPods' script for static libraries with Swift: module map, umbrella
        header and <Module>-Swift.h next to the library."""
        for script in self.t.get("scripts", []):
            if "compatibility header" in (script.get("name") or ""):
                env = config.tool_env()
                env.update(BUILT_PRODUCTS_DIR=str(self.out), PRODUCT_MODULE_NAME=self.module,
                           DERIVED_SOURCES_DIR=str(self.derived), PODS_ROOT=str(self.project.root),
                           PATH=f"{TOOLS}:{env['PATH']}")
                r = subprocess.run(["/bin/sh", "-c", script["script"]], env=env, capture_output=True, text=True)
                if r.returncode:
                    raise BuildError(f"script '{script['name']}' failed:\n{r.stderr}")

    def fingerprint(self, dependencies):
        h = hashlib.sha256(BUILDER.encode())
        langs = {LANGUAGES.get(Path(src["path"]).suffix) for src in self.sources} - {None}
        h.update(json.dumps({lang: self.clang_flags(lang) for lang in sorted(langs)}).encode())
        if any(src["path"].endswith(".swift") for src in self.sources):
            h.update(json.dumps(self.swift_flags()).encode())
        for item in self.sources + self.t.get("headers", []):
            st = os.stat(item["path"])
            h.update(f"{item['path']}|{item.get('flags')}|{st.st_mtime_ns}|{st.st_size}".encode())
        h.update(json.dumps(dependencies).encode())
        return h.hexdigest()

    def compile(self):
        """Compile every source; return the object files."""
        shutil.rmtree(self.objs, ignore_errors=True)
        self.objs.mkdir(parents=True)
        self.stage_headers()
        objects = []
        swift = [src["path"] for src in self.sources if src["path"].endswith(".swift")]
        if swift:
            objects.append(self.compile_swift(swift))
            self.run_compatibility_header_script()
        others = [src for src in self.sources if not src["path"].endswith(".swift")]
        with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as pool:
            objects += list(pool.map(self.compile_c, others))
        return objects

    def build(self, dependencies):
        """Build unless up to date. Returns (fingerprint, built?)."""
        stamp = self.temp / "fingerprint"
        fingerprint = self.fingerprint(dependencies)
        if self.archive.exists() and stamp.exists() and stamp.read_text() == fingerprint:
            return fingerprint, False
        log(f"{self.name} ({len(self.sources)} files)")
        objects = self.compile()
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.unlink(missing_ok=True)
        run([config.llvm_tool("ar"), "rcs", self.archive, *objects])
        stamp.write_text(fingerprint)
        return fingerprint, True


# --- Whole project -----------------------------------------------------------------

class Context:
    """What every target build shares: prebuilt React Native rules and
    Linux builds of macOS-only Swift macro plugins ({executable name: path})."""

    def __init__(self, pods_root, macro_plugins=None):
        self.react_dups = react_duplicates(pods_root)
        self.react_overlay = react_overlay(pods_root)
        self.macro_plugins = macro_plugins or {}


def compiles(target):
    return target["kind"] == "PBXNativeTarget" and any(
        not src["path"].endswith("-dummy.m") for src in target.get("sources", []))


def build_targets(project, names, context):
    """Build `names` and their dependencies (compilable targets only)."""
    fingerprints, built = {}, 0

    def build(name):
        nonlocal built
        if name in fingerprints:
            return fingerprints[name]
        target = project.targets[name]
        deps = [build(d) for d in target.get("dependencies", []) if d in project.targets]
        settings = project.settings(target)
        project.prepare_xcframeworks(target, settings)
        if not compiles(target):
            fingerprints[name] = "-"
            return "-"
        fingerprint, did_build = TargetBuild(project, target, settings, context).build(deps)
        built += did_build
        fingerprints[name] = fingerprint
        return fingerprint

    for name in names:
        build(name)
    return built
