#!/usr/bin/env python3
"""Pack the Windows release on a machine that cannot build the Windows engine.

WHAT THIS IS. tools/pack-release.ps1 stages bin/ from a Windows CUDA build tree,
resolves every DLL import, adds cli/, templates/, manifests/operating-point.json,
LICENSE, NOTICE, README.md, writes MANIFEST.json (path, bytes, sha256) and zips
crow-<version>-win-x64.zip. install.ps1 downloads that asset by version and
verifies every file against MANIFEST.json, so a tag without the asset breaks
every Windows install in the minutes raw.githubusercontent.com caches the
script (vault, 2026-08-08: pack, push, cut the release at once).

WHY IT EXISTS. The Linux port (2.2.0) changes cli/, manifests/ and the docs and
leaves the engine untouched: bin/ of the previous release is bit for bit the
engine the operating point was measured with. On Linux there is no dumpbin, no
MSVC runtime and no Windows build tree, but there is the previous asset. So
this takes bin/ FROM THE PREVIOUS PACKAGE, verified against that package's own
MANIFEST.json before a byte is reused, and stages everything else from the
checkout exactly as pack-release.ps1 does: cli/ without test_*.py and
__pycache__, no stray cli/runs logs (2.1.0 shipped ten of them by accident),
templates/0731-chat-template.jinja, manifests/operating-point.json, the three
root files. MANIFEST.json is written in the shape install.ps1 reads: a JSON
array of {path, bytes, sha256}, backslash paths, upper-case hex.

HOW IT IS CHECKED. --verify re-reads the finished zip the way install.ps1 does
(walk the manifest, hash every named file, then the other direction: every
file in the package is named) and refuses to print a result line otherwise.
When the engine DOES change, this tool is the wrong one: use pack-release.ps1
on Windows, which resolves the imports.

    python tools/repack-release.py --previous dist/crow-2.1.0-win-x64.zip \
        --version 2.2.0 --out dist/
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ROOT_FILES = ("LICENSE", "NOTICE", "README.md")
CLI_SKIP_DIRS = {"__pycache__", "runs"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def read_manifest(z: zipfile.ZipFile) -> list[dict]:
    raw = z.read("MANIFEST.json").decode("utf-8-sig")
    return json.loads(raw)


def previous_bin(path: str) -> dict[str, bytes]:
    """bin\\* of the previous package, every byte checked against its manifest."""
    with zipfile.ZipFile(path) as z:
        by_path = {e["path"]: e for e in read_manifest(z)}
        out = {}
        for name in z.namelist():
            key = name.replace("/", "\\")
            if not key.startswith("bin\\"):
                continue
            data = z.read(name)
            want = by_path.get(key)
            if want is None:
                raise SystemExit("previous package: %s is not in its MANIFEST.json" % key)
            if sha256_bytes(data) != want["sha256"].upper() or len(data) != want["bytes"]:
                raise SystemExit("previous package: %s does not match its own manifest" % key)
            out[key] = data
    if not out:
        raise SystemExit("previous package carries no bin/")
    return out


def version_literal() -> str:
    src = open(os.path.join(REPO, "cli", "crow.py"), encoding="utf-8").read()
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        raise SystemExit("no VERSION literal in cli/crow.py")
    return m.group(1)


def stage_from_checkout() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    cli = os.path.join(REPO, "cli")
    for dirpath, dirnames, filenames in os.walk(cli):
        dirnames[:] = [d for d in dirnames if d not in CLI_SKIP_DIRS]
        for f in sorted(filenames):
            if f.startswith("test_") and f.endswith(".py"):
                continue
            if f.endswith((".pyc", ".pyo", ".log")):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, REPO).replace("/", "\\")
            files[rel] = open(full, "rb").read()
    for f in ROOT_FILES:
        files[f] = open(os.path.join(REPO, f), "rb").read()
    files["templates\\0731-chat-template.jinja"] = open(
        os.path.join(REPO, "manifests", "0731-chat-template.jinja"), "rb").read()
    op = open(os.path.join(REPO, "manifests", "operating-point.json"), "rb").read()
    json.loads(op.decode("utf-8-sig"))  # must survive as readable JSON
    files["manifests\\operating-point.json"] = op
    if "cli\\fonts\\OFL.txt" not in files:
        raise SystemExit("cli/fonts/OFL.txt missing -- the typeface may not ship without it")
    return files


def write_package(out_zip: str, files: dict[str, bytes]) -> list[dict]:
    manifest = [{"path": p, "bytes": len(b), "sha256": sha256_bytes(b)}
                for p, b in sorted(files.items())]
    body = "﻿" + json.dumps(manifest, indent=4)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p, b in sorted(files.items()):
            z.writestr(p, b)
        z.writestr("MANIFEST.json", body.encode("utf-8"))
    return manifest


def verify(out_zip: str) -> tuple[int, list[str]]:
    """install.ps1's check, both directions."""
    problems = []
    with zipfile.ZipFile(out_zip) as z:
        manifest = read_manifest(z)
        names = {n.replace("/", "\\") for n in z.namelist()}
        for e in manifest:
            if e["path"] not in names:
                problems.append("missing: " + e["path"]); continue
            data = z.read(e["path"])
            if sha256_bytes(data) != e["sha256"] or len(data) != e["bytes"]:
                problems.append("corrupt: " + e["path"])
        named = {e["path"] for e in manifest} | {"MANIFEST.json"}
        for n in names:
            if n not in named:
                problems.append("unlisted: " + n)
    return len(manifest), problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--previous", required=True, help="the previous release's crow-*-win-x64.zip")
    ap.add_argument("--version", default=None, help="defaults to cli/crow.py's VERSION")
    ap.add_argument("--out", default=os.path.join(REPO, "dist"))
    a = ap.parse_args(argv)
    version = a.version or version_literal()
    if a.version and a.version != version_literal():
        print("NOTE: --version %s but cli/crow.py says %s" % (a.version, version_literal()))
    files = previous_bin(a.previous)
    print("bin/ reused from %s: %d files, every byte matched its manifest" % (a.previous, len(files)))
    staged = stage_from_checkout()
    print("staged from the checkout: %d files" % len(staged))
    files.update(staged)
    os.makedirs(a.out, exist_ok=True)
    out_zip = os.path.join(a.out, "crow-%s-win-x64.zip" % version)
    manifest = write_package(out_zip, files)
    n, problems = verify(out_zip)
    for p in problems:
        print("  FAILED   " + p)
    if problems:
        return 1
    total = sum(e["bytes"] for e in manifest)
    print("RESULT: %d files in the manifest (+ MANIFEST.json = %d in the package), %.1f MB staged, %.1f MB zipped"
          % (n, n + 1, total / 1e6, os.path.getsize(out_zip) / 1e6))
    print("  " + out_zip)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
