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
  3. identical texts (whitespace aside) are printed once and listed with every
     crate that uses them; an Apache-2.0 file is split into the licence terms,
     printed once per wording, and what the crate adds -- its filled-in copyright
     line, or text before or after the terms (split_apache, apache_own).

Checked here: the file is what the current Cargo.lock produces, every crate has
at least one text and its SPDX expression is covered by them ("A AND B" needs
both texts), every MPL-2.0 crate gets a source line (MPL 3.2(a)), the vendored
texts are unchanged, the WebView2 loader is still the library the vendored
Microsoft texts belong to, and NOTICE points at the file.

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


# What a licence text is, by phrases from the licence itself. A crate's SPDX
# expression is then evaluated against the kinds its texts show: "A AND B" needs
# both, "A OR B" one of them. ISC and 0BSD share their grant sentence, MIT and
# MIT-0 theirs; an id with no entry here fails the check until it gets one.
KINDS = (
    ("Apache-2.0", (r"Apache License", r"Version 2\.0")),
    ("MIT", (r"Permission is hereby granted, free of charge",)),
    ("ISC", (r"Permission to use, copy, modify, and(/or)? distribute this software for any",)),
    ("BSD", (r"Redistribution and use in source and binary forms",)),
    ("Zlib", (r"Permission is granted to anyone to use this software for any purpose",
              r"(?i)altered source versions must be plainly marked")),
    ("Unicode", (r"(?i)unicode,? inc|UNICODE LICENSE",)),
    ("Unlicense", (r"This is free and unencumbered software released into the public domain",)),
    ("CC0", (r"(?i)Creative Commons", r"CC0")),
    ("CDLA-Permissive", (r"Community Data License Agreement", r"(?i)permissive")),
    ("bzip2", (r"bzip2", r"Redistribution and use in source and binary forms")),
    ("MPL-2.0", (r"Mozilla Public License,? [Vv]ersion 2\.0",)),
)
SPDX_KIND = {
    "Apache-2.0": "Apache-2.0", "MIT": "MIT", "MIT-0": "MIT", "ISC": "ISC", "0BSD": "ISC",
    "BSD-2-Clause": "BSD", "BSD-3-Clause": "BSD", "Zlib": "Zlib", "Unicode-3.0": "Unicode",
    "Unicode-DFS-2016": "Unicode", "Unlicense": "Unlicense", "CC0-1.0": "CC0",
    "CDLA-Permissive-2.0": "CDLA-Permissive", "bzip2-1.0.6": "bzip2", "MPL-2.0": "MPL-2.0",
}
# MPL-2.0 is file-level copyleft, not a notice licence: section 3.2(a) has whoever
# distributes the Executable Form tell its recipients where to get the Source Code
# Form. The notices therefore name, for every MPL crate, the unmodified .crate on
# crates.io and its sha256 from Cargo.lock.
CRATE_URL = "https://static.crates.io/crates/{name}/{name}-{version}.crate"


def lock_checksums(installer):
    """{(name, version): sha256 of the .crate} from installer/Cargo.lock."""
    with open(os.path.join(installer, "Cargo.lock"), encoding="utf-8") as fh:
        lock = fh.read()
    out = {}
    for block in lock.split("[[package]]"):
        n = re.search(r'^name = "([^"]+)"', block, re.M)
        v = re.search(r'^version = "([^"]+)"', block, re.M)
        c = re.search(r'^checksum = "([0-9a-f]{64})"', block, re.M)
        if n and v and c:
            out[(n.group(1), v.group(1))] = c.group(1)
    return out


def kinds_of(text):
    return {k for k, needles in KINDS if all(re.search(n, text) for n in needles)}


def covered(expr, kinds):
    """True when the SPDX expression is satisfied by texts of these kinds.
    Raises ValueError on an id SPDX_KIND does not know."""
    tokens = re.findall(r"\(|\)|[A-Za-z0-9.+-]+", expr.replace("/", " OR "))
    pos = 0

    def atom():
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            v = disj()
            pos += 1                         # ")"
            return v
        if tok.endswith("+"):
            tok = tok[:-1]
        if tok not in SPDX_KIND:
            raise ValueError("unknown licence id %r in %r" % (tok, expr))
        return SPDX_KIND[tok] in kinds

    def conj():
        nonlocal pos
        v = atom()
        while pos < len(tokens) and tokens[pos] == "AND":
            pos += 1
            v = atom() and v
        return v

    def disj():
        nonlocal pos
        v = conj()
        while pos < len(tokens) and tokens[pos] == "OR":
            pos += 1
            v = conj() or v
        return v

    return disj()


def norm(text):
    return " ".join(text.split())


APACHE_END = "END OF TERMS AND CONDITIONS"


def split_apache(text):
    """(before, terms, appendix) of an Apache-2.0 licence file, or None.

    Apache-2.0 asks for one copy of the licence with the work (4a) and the
    work's NOTICE file (4d). Crates ship the same terms over and over, so the
    notices print each wording of the terms once; what a crate adds before or
    after them stays with that crate (apache_own)."""
    i = text.find("Apache License")
    j = text.find(APACHE_END)
    if i < 0 or j < i or "TERMS AND CONDITIONS FOR USE" not in text[i:j]:
        return None
    j += len(APACHE_END)
    return text[:i].strip("\n"), text[i:j], text[j:].strip("\n")


def apache_own(appendix):
    """What of an Apache APPENDIX is the crate's own. The appendix is the
    licence's template for applying it ("Copyright [yyyy] [name of copyright
    owner]"); when it is only that, the crate's own part is the copyright line
    it filled in, if any. An appendix that carries more (ring appends further
    licences) is kept whole."""
    n = norm(appendix)
    if n.startswith("APPENDIX: How to apply the Apache License") and \
            n.endswith("limitations under the License."):
        return "\n".join(line.strip() for line in appendix.splitlines()
                         if re.match(r"\s*Copyright\b", line)
                         and not re.search(r"[\[{]yyyy[\]}]", line))
    return appendix


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
    checksums = lock_checksums(installer)
    mpl = []                                 # [(name, version, repository, sha256)]

    texts = {}                               # whitespace-normalised text -> index
    order = []                               # [(title, text, users)]
    rows = []
    missing = []
    uncovered = []

    def add(title, text):
        key = norm(text)                     # texts that differ only in spacing are one
        if key not in texts:
            texts[key] = len(order) + 1
            order.append((title, text, []))
        return texts[key]

    def add_licence(title, text):
        parts = split_apache(text)
        if parts is None:
            return [add(title, text)]
        before, terms, appendix = parts
        refs = [add("Apache License 2.0, terms (as in %s)" % title, terms)]
        own = "\n\n".join(x for x in (before, apache_own(appendix)) if x.strip())
        if own:
            refs.append(add("%s -- besides the Apache-2.0 terms" % title, own))
        return refs

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
        found = [("%s %s: %s" % (name, version, f.replace("\\", "/")), read_text(os.path.join(root, f)))
                 for f in files]
        if not found:
            found = [("%s (installer/licenses/%s)" % (name, f), read_text(os.path.join(licdir, f)))
                     for f in vendored.get(name, [])]
        if not found:
            missing.append("%s %s (%s)" % (name, version, p.get("license")))
        refs = []
        for title, text in found:
            refs += [r for r in add_licence(title, text) if r not in refs]
        kinds = set().union(*(kinds_of(text) for _, text in found))
        try:
            if not covered(p.get("license") or "", kinds):
                uncovered.append("%s %s: %s, texts show %s"
                                 % (name, version, p.get("license"), sorted(kinds) or "nothing"))
        except (ValueError, IndexError) as exc:
            uncovered.append("%s %s: %s" % (name, version, exc))
        if "MPL" in (p.get("license") or ""):
            mpl.append((name, version, p.get("repository") or "-", checksums.get((name, version))))
        for r in refs:
            order[r - 1][2].append("%s %s" % (name, version))
        rows.append((name, version, p.get("license") or "?", crates[(name, version)], refs))
    checks.append(("every shipped crate has a licence text", not missing, "; ".join(missing)))
    checks.append(("every crate's licence expression is covered by its texts", not uncovered,
                   "; ".join(uncovered)))
    checks.append(("every MPL-2.0 crate has a checksum in Cargo.lock for its source line",
                   all(m[3] for m in mpl), ", ".join("%s %s" % m[:2] for m in mpl if not m[3])))

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
    if mpl:
        lines += ["", "=" * 80, "SOURCE CODE OF THE MPL-2.0 COMPONENTS", "=" * 80, "",
                  "These crates are under the Mozilla Public License 2.0 and are compiled in",
                  "unmodified. Their Source Code Form is the published crate, downloadable at",
                  "no charge from crates.io; the sha256 is the one Cargo.lock pins.", ""]
        for name, version, repo_url, digest in mpl:
            lines += ["%s %s" % (name, version),
                      "    %s" % CRATE_URL.format(name=name, version=version),
                      "    sha256 %s" % digest,
                      "    repository %s" % repo_url]
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
