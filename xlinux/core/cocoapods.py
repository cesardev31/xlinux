"""CocoaPods → SwiftPM, sin Ruby ni `pod install`.

Para plugins/dependencias que solo tienen podspec:

- podspecs locales (Ruby, p. ej. los de plugins de Flutter): se leen con un
  intérprete mínimo de su DSL (asignaciones `s.x = …` y `s.dependency …`).
- pods de terceros: el podspec en JSON se baja del CDN de CocoaPods, se
  resuelve la versión (`~>`, `>=`, `=`…) y el código se baja sin historial.
- cada pod se convierte en un paquete SwiftPM: fuentes explícitas, frameworks
  del sistema, `binaryTarget` para xcframeworks y `COCOAPODS` definido.
- los `resource_bundles` se arman como los arma CocoaPods (`<nombre>.bundle` en
  la raíz del .app; los catálogos pasan por nuestro actool), porque el código
  de los pods los busca ahí y no en los bundles de SwiftPM.

Soporta pods en Swift (con xcframeworks binarios). Pods con Objective-C/C
todavía no.
"""

import ast
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from . import config
from .util import log, run

CDN = "https://cdn.cocoapods.org"
SOURCE_EXT = {".swift"}
UNSUPPORTED_EXT = {".m", ".mm", ".c", ".cc", ".cpp"}
# Dependencias que resuelve el framework (no son pods a bajar).
PROVIDED = {"Flutter", "React", "React-Core"}


# --------------------------------------------------------------------------- podspec Ruby

def _ruby_literal(text):
    """Convierte un literal de Ruby sencillo (strings, arrays, hashes, símbolos) a Python."""
    t = text.strip().rstrip(",")
    t = re.sub(r"%w[\[(](.*?)[\])]", lambda m: repr(m.group(1).split()), t, flags=re.S)
    t = re.sub(r"(?<![\w\"']):(\w+)", r"'\1'", t)          # :ios → 'ios'
    t = t.replace("=>", ":").replace("nil", "None").replace("true", "True").replace("false", "False")
    try:
        return ast.literal_eval(t)
    except (ValueError, SyntaxError):
        return t.strip("'\"")


def parse_ruby_podspec(path):
    """Lee un podspec Ruby típico de plugin. Devuelve el mismo formato que el JSON del CDN."""
    text = Path(path).read_text()
    text = re.sub(r"<<[-~]?(['\"]?)(\w+)\1.*?^\s*\2\s*$", "''", text, flags=re.S | re.M)  # heredocs
    spec = {"dependencies": {}}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].split("#", 1)[0] if not re.search(r"['\"][^'\"]*#", lines[i]) else lines[i]
        i += 1
        m = re.match(r"\s*\w+\.(?:ios\.)?(\w+)\s*=\s*(.+)$", line)
        dep = re.match(r"\s*\w+\.(?:ios\.)?dependency\s+(.+)$", line)
        if not m and not dep:
            continue
        value = (m.group(2) if m else dep.group(1)).strip()
        while value.count("[") > value.count("]") or value.count("{") > value.count("}"):
            if i >= len(lines):
                break
            value += " " + lines[i].split("#", 1)[0].strip()
            i += 1
        if dep:
            parts = [p for p in (_ruby_literal(x) for x in _split_args(value)) if p]
            spec["dependencies"][parts[0]] = parts[1:]
            continue
        key, parsed = m.group(1), _ruby_literal(value)
        if key == "platform" and isinstance(parsed, tuple):
            spec["platforms"] = {parsed[0]: parsed[1]}
        elif key == "deployment_target":
            spec["platforms"] = {"ios": parsed}
        elif key in ("framework", "library", "weak_framework"):
            spec.setdefault(key + "s", []).append(parsed)
        else:
            spec[key] = parsed
    return spec


def _split_args(text):
    out, depth, cur, quote = [], 0, "", None
    for ch in text:
        if quote:
            cur += ch
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote, cur = ch, cur + ch
        elif ch in "[{(":
            depth, cur = depth + 1, cur + ch
        elif ch in "]})":
            depth, cur = depth - 1, cur + ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + ([cur] if cur.strip() else [])


# --------------------------------------------------------------------------- CDN y versiones

def _shard(name):
    return list(hashlib.md5(name.encode()).hexdigest()[:3])


def _fetch(url):
    # El CDN rechaza (403) el User-Agent por defecto de urllib.
    request = urllib.request.Request(url, headers={"User-Agent": "xlinux (+https://github.com/cesardev31/xlinux)"})
    with urllib.request.urlopen(request, timeout=60) as r:
        return r.read()


def _version_key(v):
    main, _, pre = v.partition("-")
    nums = [int(x) if x.isdigit() else 0 for x in re.split(r"[.]", main)]
    return (nums + [0] * (4 - len(nums)), pre == "", pre)


def _satisfies(version, reqs):
    vk = _version_key(version)
    for req in reqs:
        m = re.match(r"\s*(~>|>=|<=|>|<|=)?\s*(\S+)", req)
        op, target = (m.group(1) or "="), m.group(2)
        tk = _version_key(target)
        if op == "=" and vk != tk:
            return False
        if op == ">=" and vk < tk or op == ">" and vk <= tk or op == "<=" and vk > tk or op == "<" and vk >= tk:
            return False
        if op == "~>":
            parts = target.split("-")[0].split(".")
            upper = parts[:-1] if len(parts) > 1 else parts
            upper[-1] = str(int(upper[-1]) + 1)
            if vk < tk or vk >= _version_key(".".join(upper)):
                return False
    return True


def resolve(name, reqs):
    a, b, c = _shard(name)
    listing = _fetch(f"{CDN}/all_pods_versions_{a}_{b}_{c}.txt").decode()
    for line in listing.splitlines():
        parts = line.split("/")
        if parts[0] == name:
            versions = [v for v in parts[1:] if v]
            break
    else:
        sys.exit(f"error: el pod {name} no existe en el CDN de CocoaPods")
    stable = [v for v in versions if "-" not in v] or versions
    ok = sorted((v for v in stable if _satisfies(v, reqs)), key=_version_key)
    if not ok:
        sys.exit(f"error: ninguna versión de {name} cumple {reqs}")
    return ok[-1]


def fetch_spec(name, version):
    a, b, c = _shard(name)
    cache = config.data_dir() / "pods/specs" / name / f"{version}.json"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(_fetch(f"{CDN}/Specs/{a}/{b}/{c}/{name}/{version}/{name}.podspec.json"))
    return json.loads(cache.read_text())


def fetch_source(spec):
    """Baja el código del pod (sin historial) al directorio de datos. Pods del
    mismo repo y tag comparten la copia."""
    src = spec["source"]
    if "git" in src:
        ref = src.get("tag") or src.get("commit") or src.get("branch") or "HEAD"
        key = re.sub(r"[^A-Za-z0-9._-]", "_", src["git"].split("://")[-1].removesuffix(".git")) + "@" + ref
        dest = config.data_dir() / "pods/src" / key
        if not dest.exists():
            log(f"Descargando {spec['name']} {spec['version']} sin historial")
            tmp = dest.with_suffix(".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            if "commit" in src:
                run(["git", "init", "-q", tmp])
                run(["git", "-C", tmp, "fetch", "-q", "--depth", "1", src["git"], src["commit"]])
                run(["git", "-C", tmp, "checkout", "-q", "FETCH_HEAD"])
            else:
                run(["git", "clone", "-q", "--depth", "1", "--branch", ref, src["git"], tmp],
                    capture_output=True, text=True)
            tmp.rename(dest)
        return dest
    if "http" in src:
        url = src["http"]
        dest = config.data_dir() / "pods/src" / (spec["name"] + "-" + spec["version"])
        if not dest.exists():
            log(f"Descargando {spec['name']} {spec['version']}")
            archive = dest.with_suffix(".download")
            archive.write_bytes(_fetch(url))
            tmp = dest.with_suffix(".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as z:
                    z.extractall(tmp)
            else:
                with tarfile.open(archive) as t:
                    t.extractall(tmp, filter="data")
            archive.unlink()
            tmp.rename(dest)
        return dest
    sys.exit(f"error: fuente no soportada para el pod {spec['name']}: {src}")


# --------------------------------------------------------------------------- specs

def _as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def flatten(spec):
    """Mezcla las subspecs por defecto en la spec principal (como `pod 'X'`)."""
    spec = dict(spec)
    subs = {s["name"]: s for s in spec.get("subspecs", [])}
    wanted = _as_list(spec.get("default_subspecs") or spec.get("default_subspec")) or list(subs)
    seen = set()
    while wanted:
        name = wanted.pop(0)
        if name in seen or name not in subs:
            continue
        seen.add(name)
        sub = flatten(subs[name])
        for key in ("source_files", "resources", "vendored_frameworks", "frameworks", "libraries",
                    "weak_frameworks", "exclude_files"):
            spec[key] = _as_list(spec.get(key)) + _as_list(sub.get(key))
        spec.setdefault("resource_bundles", {}).update(sub.get("resource_bundles") or {})
        for dep, reqs in (sub.get("dependencies") or {}).items():
            if dep.split("/")[0] == spec["name"]:
                wanted.append(dep.split("/", 1)[1] if "/" in dep else dep)
            else:
                spec.setdefault("dependencies", {})[dep] = reqs
    return spec


def module_name(spec):
    return spec.get("module_name") or re.sub(r"[^A-Za-z0-9_]", "_", spec.get("header_dir") or spec["name"])


def _expand(pattern):
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    return [p for alt in m.group(1).split(",") for p in _expand(pattern[:m.start()] + alt + pattern[m.end():])]


def glob(root, patterns):
    """Globs de CocoaPods (`**`, `{a,b}`; un directorio incluye todo su contenido)."""
    found = set()
    for pattern in _as_list(patterns):
        for p in _expand(pattern):
            for match in root.glob(p):
                found.add(match)
    return sorted(found)


def _files(root, spec, key, exclude=()):
    out = []
    excluded = set(glob(root, spec.get("exclude_files")))
    for path in glob(root, spec.get(key)):
        candidates = [f for f in path.rglob("*") if f.is_file()] if path.is_dir() and path.suffix != ".xcassets" else [path]
        out += [f for f in candidates if f not in excluded]
    return out


# --------------------------------------------------------------------------- generación

class Pod:
    def __init__(self, spec, root, deps):
        self.spec = spec
        self.root = root
        self.name = spec["name"]
        self.module = module_name(spec)
        self.deps = deps          # [Pod]

    @property
    def package_dir_name(self):
        return f"{self.name}-{self.spec.get('version', 'local')}"


def load_graph(local_spec, local_root):
    """Resuelve el podspec local y todas sus dependencias. Devuelve (Pod raíz, [todos])."""
    pods = {}

    def load(spec, root):
        spec = flatten(spec)
        deps = []
        for dep, reqs in (spec.get("dependencies") or {}).items():
            base = dep.split("/")[0]
            if base in PROVIDED or base == spec["name"]:
                continue
            if base not in pods:
                version = resolve(base, _as_list(reqs))
                dep_spec = fetch_spec(base, version)
                pods[base] = None  # evita ciclos
                pods[base] = load(dep_spec, fetch_source(dep_spec))
            deps.append(pods[base])
        return Pod(spec, root, deps)

    root_pod = load(local_spec, local_root)
    ordered, seen = [], set()

    def visit(pod):
        if pod.name in seen:
            return
        seen.add(pod.name)
        for d in pod.deps:
            visit(d)
        ordered.append(pod)

    visit(root_pod)
    return root_pod, ordered


def write_package(pod, dest, deps_base="../", extra_deps=(), product=None):
    """Package.swift para un pod. `dest/src` es un symlink a su código;
    `deps_base` es la ruta (relativa a `dest`) donde están los paquetes de sus
    dependencias."""
    dest.mkdir(parents=True, exist_ok=True)
    link = dest / "src"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(pod.root)

    sources = _files(pod.root, pod.spec, "source_files")
    bad = [f for f in sources if f.suffix in UNSUPPORTED_EXT]
    if bad:
        sys.exit(f"error: el pod {pod.name} tiene Objective-C/C ({bad[0].name}); "
                 "xlinux todavía solo convierte pods en Swift.")
    swift = [f for f in sources if f.suffix in SOURCE_EXT]
    if not swift:
        sys.exit(f"error: el pod {pod.name} no tiene fuentes Swift que compilar")
    # La carpeta del target solo tiene enlaces a sus fuentes: si apuntara al
    # repo completo, SwiftPM tomaría como recursos los de las apps de ejemplo
    # (los recursos del pod los empaqueta pack_resources, como CocoaPods).
    target_dir = dest / "Sources" / pod.module
    shutil.rmtree(dest / "Sources", ignore_errors=True)
    for f in swift:
        link_path = target_dir / f.relative_to(pod.root)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(f)

    binaries = []
    for fw in glob(pod.root, pod.spec.get("vendored_frameworks")):
        if fw.suffix == ".xcframework":
            binaries.append((fw.stem, "src/" + fw.relative_to(pod.root).as_posix()))
        else:
            log(f"aviso: {pod.name}: {fw.name} no es un .xcframework; se omite")

    defines = {"COCOAPODS"}
    xc = pod.spec.get("pod_target_xcconfig") or {}
    for key, value in xc.items():
        if key.startswith("SWIFT_ACTIVE_COMPILATION_CONDITIONS") and "Release" not in key:
            defines |= {v for v in str(value).split() if v != "$(inherited)"}

    platform = max(config.MIN_IOS, str((pod.spec.get("platforms") or {}).get("ios") or config.MIN_IOS),
                   key=lambda v: [int(x) for x in v.split(".")])
    q = json.dumps
    packages = [f'.package(name: {q(d.name)}, path: {q(deps_base + d.package_dir_name)})' for d in pod.deps]
    target_deps = [f'.product(name: {q(d.module)}, package: {q(d.name)})' for d in pod.deps]
    target_deps += [q(name) for name, _ in binaries]
    for pkg_name, rel in extra_deps:
        packages.append(f'.package(name: {q(pkg_name)}, path: {q(rel)})')
        target_deps.append(f'.product(name: {q(pkg_name)}, package: {q(pkg_name)})')
    linker = [f'.linkedFramework({q(f)})' for f in _as_list(pod.spec.get("frameworks"))]
    linker += [f'.linkedLibrary({q(lib)})' for lib in _as_list(pod.spec.get("libraries"))]
    swift_settings = [f'.define({q(d)})' for d in sorted(defines)]
    product = product or pod.module
    binary_targets = "".join(f",\n        .binaryTarget(name: {q(n)}, path: {q(p)})" for n, p in binaries)
    (dest / "Package.swift").write_text(f"""// swift-tools-version: 5.9
// Generado por xlinux desde el podspec de {pod.name} {pod.spec.get('version', '')}. No editar.
import PackageDescription

let package = Package(
    name: {q(pod.name)},
    platforms: [.iOS({q(platform)})],
    products: [.library(name: {q(product)}, targets: [{q(pod.module)}])],
    dependencies: [{", ".join(packages)}],
    targets: [
        .target(
            name: {q(pod.module)},
            dependencies: [{", ".join(target_deps)}],
            path: {q("Sources/" + pod.module)},
            swiftSettings: [{", ".join(swift_settings)}],
            linkerSettings: [{", ".join(linker)}]
        ){binary_targets}
    ]
)
""")


def resource_manifest(pods):
    """[(bundle o None, [rutas])] de todos los pods, para empaquetar después."""
    out = []
    for pod in pods:
        for bundle, patterns in (pod.spec.get("resource_bundles") or {}).items():
            files = [p.as_posix() for p in glob(pod.root, patterns)]
            out.append({"pod": pod.name, "bundle": bundle, "files": files})
        loose = [p.as_posix() for p in glob(pod.root, pod.spec.get("resources"))]
        if loose:
            out.append({"pod": pod.name, "bundle": None, "files": loose})
    return out


def pack_resources(manifest, app):
    """Arma los bundles de recursos como CocoaPods: `<bundle>.bundle` en la raíz
    del .app (los .xcassets pasan por actool; .lproj conserva su carpeta)."""
    actool = config.SUPPORT / "core/bin/actool"
    for entry in manifest:
        target = app / f"{entry['bundle']}.bundle" if entry["bundle"] else app
        target.mkdir(parents=True, exist_ok=True)
        for f in map(Path, entry["files"]):
            if f.suffix == ".xcassets":
                run([actool, f, "--compile", target, "--platform", "iphoneos"], capture_output=True, text=True)
            elif f.suffix in (".storyboard", ".xib"):
                log(f"aviso: {entry['pod']}: {f.name} necesita ibtool; se omite")
            elif f.is_dir():
                shutil.copytree(f, target / f.name, dirs_exist_ok=True)
            else:
                lproj = next((p for p in f.parents if p.suffix == ".lproj"), None)
                dst = target / lproj.name / f.name if lproj else target / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
        if entry["bundle"]:
            info = target / "Info.plist"
            if not info.exists():
                info.write_bytes(plistlib.dumps({
                    "CFBundleIdentifier": f"org.cocoapods.{entry['bundle']}",
                    "CFBundleName": entry["bundle"], "CFBundlePackageType": "BNDL",
                    "CFBundleInfoDictionaryVersion": "6.0", "CFBundleVersion": "1",
                    "CFBundleShortVersionString": "1.0"}))
