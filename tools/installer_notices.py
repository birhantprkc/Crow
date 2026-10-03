#!/usr/bin/env python3
"""CrowSetup's third-party notices: installer/THIRD-PARTY-NOTICES.txt.

CrowSetup compiles in Rust crates from crates.io, and on Windows also Microsoft's
WebView2 loader (webview2-com-sys links WebView2LoaderStatic.lib on MSVC). Their
MIT, BSD, ISC, Unicode and Apache terms allow a binary only together with the
notice. The exe travels alone as a release asset, so it carries the file itself:
`CrowSetup --licenses` prints it (installer/app/src/cli.rs, include_str!).

The file is generated, never written by hand:

  1. `cargo tree -e normal` for the release build (`-p crowsetup --features
     bundle`) on each target CrowSetup ships for -- the crates that end up in the
     binary, procedural macros included (their output is compiled in), build
     scripts and dev-dependencies excluded;
  2. every licence file the crate source carries (LICENSE*, COPYING*, NOTICE*,
     UNLICENSE*, the manifest's license-file), plus the texts named in EXTRA_FILES
     and installer/licenses/sources.json for what the crate sources lack;
  3. identical texts are printed once and listed with every crate that uses them.

Checked here: the file is what the current Cargo.lock produces, every crate has
at least one text, the vendored texts are unchanged, the WebView2 loader is still
the library the vendored Microsoft texts belong to, and NOTICE points at the file.

Usage:  installer_notices.py [--write] [REPO]
Exit 0 = all green (or written).  1 = at least one check failed.  2 = setup error.
"""

import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = (("x86_64-pc-windows-msvc", "Windows"), ("x86_64-unknown-linux-gnu", "Linux"))
OUT = "installer/THIRD-PARTY-NOTICES.txt"
LICENCE_FILE = re.compile(r"(?i)^(licen[cs]e|copying|notice|unlicense)([-._].*)?$")
# Texts a crate keeps below its root: zstd-sys compiles the zstd C library in,
# whose own licence is zstd/LICENSE (Meta); the crate's top-level files cover the
# bindings and the build glue.
EXTRA_FILES = {"zstd-sys": ["zstd/LICENSE"]}


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def cargo(installer, *args):
    r = subprocess.run(["cargo", *args, "--locked", "--manifest-path",
                        os.path.join(installer, "Cargo.toml")],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError("cargo %s: exit %d\n%s" % (" ".join(args), r.returncode, r.stderr[-2000:]))
    return r.stdout


def shipped(installer):
    """{(name, version): [target labels]} for the crates in the release binary."""
    out = {}
    for triple, label in TARGETS:
        tree = cargo(installer, "tree", "-p", "crowsetup", "--features", "bundle",
                     "--target", triple, "-e", "normal", "--prefix", "none",
                     "--no-dedupe", "--format", "{p}")
        for line in tree.splitlines():
            parts = line.split()
            if len(parts) < 2 or not parts[1].startswith("v"):
                continue
            key = (parts[0], parts[1][1:])
            labels = out.setdefault(key, [])
            if label not in labels:
                labels.append(label)
    return out


def read_text(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def build(repo):
    """(file text, [(check, ok, detail)])."""
    installer = os.path.join(repo, "installer")
    licdir = os.path.join(installer, "licenses")
    with open(os.path.join(licdir, "sources.json"), encoding="utf-8") as fh:
        sources = json.load(fh)
    checks = []
    vendored = {}                            # crate name -> vendored file
    for fname, meta in sources["files"].items():
        path = os.path.join(licdir, fname)
        ok = os.path.isfile(path)
        if ok:
            with open(path, "rb") as fh:
                ok = sha256_bytes(fh.read()) == meta["sha256"]
        checks.append(("vendored text %s present and unchanged" % fname, ok, path))
        for crate in meta["for"]:
            vendored.setdefault(crate, []).append(fname)

    cargo(installer, "fetch")
    meta = json.loads(cargo(installer, "metadata", "--format-version", "1"))
    packages = {(p["name"], p["version"]): p for p in meta["packages"]}
    crates = shipped(installer)

    texts = {}                               # text -> index
    order = []                               # [(title, text)]
    rows = []
    missing = []

    def add(title, text):
        if text not in texts:
            texts[text] = len(order) + 1
            order.append((title, text, []))
        return texts[text]

    for (name, version) in sorted(crates, key=lambda k: (k[0].lower(), k[1])):
        p = packages.get((name, version))
        if p is None:
            missing.append("%s %s (not in cargo metadata)" % (name, version))
            continue
        if p["source"] is None:
            continue                         # a path crate: CrowSetup's own code
        root = os.path.dirname(p["manifest_path"])
        files = sorted(f for f in os.listdir(root)
                       if LICENCE_FILE.match(f) and os.path.isfile(os.path.join(root, f)))
        if p.get("license_file") and p["license_file"] not in files:
            files.append(p["license_file"])
        files += [f for f in EXTRA_FILES.get(name, []) if os.path.isfile(os.path.join(root, f))]
        refs = []
        for f in files:
            refs.append(add("%s %s: %s" % (name, version, f.replace("\\", "/")),
                            read_text(os.path.join(root, f))))
        if not refs:
            for f in vendored.get(name, []):
                refs.append(add("%s (installer/licenses/%s)" % (name, f),
                                read_text(os.path.join(licdir, f))))
        if not refs:
            missing.append("%s %s (%s)" % (name, version, p.get("license")))
        for r in refs:
            order[r - 1][2].append("%s %s" % (name, version))
        rows.append((name, version, p.get("license") or "?", crates[(name, version)], refs))
    checks.append(("every shipped crate has a licence text", not missing, "; ".join(missing)))

    loader = sources["webview2_loader"]
    lp = packages.get((loader["crate"], loader["version"]))
    lib_ok = False
    if lp is not None:
        lib = os.path.join(os.path.dirname(lp["manifest_path"]), "x64", "WebView2LoaderStatic.lib")
        if os.path.isfile(lib):
            with open(lib, "rb") as fh:
                lib_ok = sha256_bytes(fh.read()) == loader["x64_static_lib_sha256"]
    checks.append(("%s %s is the locked version and its WebView2 loader is unchanged"
                   % (loader["crate"], loader["version"]), lib_ok,
                   "a new webview2-com-sys means a new WebView2 SDK: re-fetch its "
                   "LICENSE.txt/NOTICE.txt and update installer/licenses/sources.json"))
    ms = []
    for fname, m in sources["files"].items():
        if "WebView2LoaderStatic.lib" in m["for"]:
            ms.append(add("Microsoft WebView2 loader, %s (installer/licenses/%s)" % (loader["nuget"], fname),
                          read_text(os.path.join(licdir, fname))))
    for r in ms:
        order[r - 1][2].append("WebView2LoaderStatic.lib")

    lines = [
        "CrowSetup -- third-party notices",
        "",
        "Generated by tools/installer_notices.py from installer/Cargo.lock. Do not edit;",
        "run `python tools/installer_notices.py --write` after a dependency change.",
        "`CrowSetup --licenses` prints this file.",
        "",
        "CrowSetup's own code is MIT (LICENSE). The release binary compiles in the",
        "%d crates below (Windows and Linux builds together, procedural macros" % len(rows),
        "included) and, on Windows, Microsoft's WebView2 loader. Each keeps its own",
        "terms. Where a crate offers a choice (\"A OR B\"), CrowSetup takes it under",
        "any one of them; every text the crate ships is reproduced.",
        "",
        "The embedded Python (python.org's embeddable package) and get-pip.py are",
        "unpacked unchanged; they carry their own licence texts inside (LICENSE.txt",
        "in the Python folder, pip's and its vendored libraries' in pip's wheel).",
        "",
        "=" * 80,
        "COMPONENTS",
        "=" * 80,
        "",
    ]
    for name, version, lic, labels, refs in rows:
        targets = "" if len(labels) == len(TARGETS) else "  [%s only]" % "/".join(labels)
        lines.append("%s %s -- %s%s" % (name, version, lic, targets))
        lines.append("    text %s" % ", ".join("[%d]" % r for r in refs))
    lines.append("WebView2LoaderStatic.lib (%s) -- Microsoft, see the texts  [Windows only]"
                 % loader["nuget"])
    lines.append("    text %s" % ", ".join("[%d]" % r for r in ms))
    lines += ["", "=" * 80, "LICENCE TEXTS", "=" * 80]
    for i, (title, text, users) in enumerate(order, 1):
        lines += ["", "-" * 80, "[%d] %s" % (i, title)]
        if len(users) > 1:
            lines.append("    also used by: %s" % ", ".join(u for u in users if not title.startswith(u + ":")))
        lines += ["-" * 80, "", text]
    body = "\n".join(lines) + "\n"
    return body, checks


def main(argv):
    args = [a for a in argv[1:] if a != "--write"]
    write = "--write" in argv[1:]
    repo = os.path.abspath(args[0]) if args else os.path.dirname(HERE)
    if not os.path.isfile(os.path.join(repo, "installer", "Cargo.lock")):
        print("SETUP ERROR: no installer/Cargo.lock under %s" % repo)
        return 2
    try:
        body, checks = build(repo)
    except (OSError, ValueError, RuntimeError) as exc:
        print("SETUP ERROR: %s" % exc)
        return 2
    out = os.path.join(repo, *OUT.split("/"))
    if write:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        print("wrote %s (%d bytes)" % (OUT, len(body.encode("utf-8"))))
    try:
        with open(out, encoding="utf-8", newline="") as fh:
            have = fh.read()
    except OSError:
        have = None
    checks.append(("%s is what Cargo.lock produces" % OUT, have == body,
                   "run: python tools/installer_notices.py --write"))
    try:
        with open(os.path.join(repo, "NOTICE"), encoding="utf-8") as fh:
            notice = fh.read()
    except OSError as exc:
        notice = ""
        checks.append(("NOTICE readable", False, str(exc)))
    checks.append(("NOTICE points at installer/THIRD-PARTY-NOTICES.txt",
                   "installer/THIRD-PARTY-NOTICES.txt" in notice, "NOTICE"))
    failed = 0
    for name, ok, detail in checks:
        if ok:
            print("  OK       %s" % name)
        else:
            failed += 1
            print("  FAILED   %s\n             %s" % (name, detail))
    print()
    print("RESULT: %s" % ("PASS" if not failed else "%d FAILED" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
