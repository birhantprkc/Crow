"""Repack the ComfyUI portable .7z as a deterministic .zip for Crow's mirror (#340).

CrowSetup extracts zip only (installer/core/src/layout.rs); upstream ships the
Windows portable build as 7z. This script turns the pinned upstream archive into
a zip with the same files, so the media point's runtime can be a mirror file.

  repack_comfyui.py <ComfyUI_windows_portable_nvidia.7z> <out.zip> [--sha256 <hex>]

1. --sha256 (the release asset digest) must match the input, if given.
2. Extract with bsdtar (Windows: %SystemRoot%\\System32\\tar.exe; elsewhere
   `bsdtar`), which reads 7z through libarchive.
3. Write the zip: entries in sorted order, every timestamp 1980-01-01 00:00:00,
   deflate level 9, files only (folders are implied by the names), forward
   slashes. The same input and the same Python/zlib give the same bytes.
4. Read the zip back: every extracted file is in it with its size and CRC-32,
   and nothing else is.

Prints the zip's size and sha256 for stack.json. Exit 0 = written and verified,
1 = a check failed, 2 = setup error (missing input, no bsdtar).
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib

EPOCH = (1980, 1, 1, 0, 0, 0)
CHUNK = 1 << 20


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def crc_of(path: str) -> int:
    crc = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            crc = zlib.crc32(block, crc)
    return crc


def bsdtar() -> "str | None":
    if os.name == "nt":
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
        return exe if os.path.exists(exe) else None
    return shutil.which("bsdtar")


def tree(root: str) -> "dict[str, str]":
    """Archive name (forward slashes) -> path, for every file under root."""
    out = {}
    for d, _, names in os.walk(root):
        for n in names:
            full = os.path.join(d, n)
            out[os.path.relpath(full, root).replace(os.sep, "/")] = full
    return out


def write_zip(files: "dict[str, str]", out: str) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(files[name], "rb") as src, z.open(info, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, CHUNK)


def verify(files: "dict[str, str]", out: str) -> "list[str]":
    p = []
    with zipfile.ZipFile(out) as z:
        infos = {i.filename: i for i in z.infolist()}
    for name in sorted(set(infos) - set(files)):
        p.append("zip has %s, which the archive does not" % name)
    for name, path in sorted(files.items()):
        i = infos.get(name)
        if i is None:
            p.append("zip lacks %s" % name)
        elif i.file_size != os.path.getsize(path) or i.CRC != crc_of(path):
            p.append("zip entry %s differs from the extracted file" % name)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("out")
    ap.add_argument("--sha256", help="the release asset's sha256; refuse on a mismatch")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.archive):
        print("repack_comfyui: no such archive: %s" % a.archive, file=sys.stderr)
        return 2
    tar = bsdtar()
    if tar is None:
        print("repack_comfyui: bsdtar not found (it reads 7z)", file=sys.stderr)
        return 2
    if a.sha256:
        got = sha256_of(a.archive)
        if got != a.sha256.lower():
            print("FAIL input sha256 %s, expected %s" % (got, a.sha256.lower()))
            return 1
        print("input sha256 matches the release digest")
    work = tempfile.mkdtemp(prefix="comfyui-repack-", dir=os.path.dirname(os.path.abspath(a.out)))
    try:
        subprocess.run([tar, "-xf", a.archive, "-C", work], check=True)
        files = tree(work)
        if not files:
            print("FAIL the archive extracted to nothing")
            return 1
        tmp = a.out + ".part"
        write_zip(files, tmp)
        problems = verify(files, tmp)
        if problems:
            print("FAIL %d differences, first: %s" % (len(problems), problems[0]))
            return 1
        os.replace(tmp, a.out)
        print("files %d, extracted bytes %d" % (len(files), sum(os.path.getsize(f) for f in files.values())))
        print("zip %s bytes %d sha256 %s" % (a.out, os.path.getsize(a.out), sha256_of(a.out)))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
