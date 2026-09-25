#!/usr/bin/env python3
"""Hold the vendored path-tracing kit together (#298).

kits/pathtracer ships a 958 KB minified bundle nobody reads. What makes it
trustworthy is the chain around it, and every link is checked here:

  1. the bundle's bytes and size are the ones kits/pathtracer/kit.json records
     (tools/build-pathtracer-kit.sh wrote both in one run);
  2. the three licence texts are present and byte-identical to what kit.json
     recorded -- MIT permits redistribution only WITH the notice;
  3. NOTICE names each package at the version kit.json pins, so the third-party
     list cannot drift from what actually ships;
  4. the pins are exact versions (no ranges) and the licences are MIT;
  5. the voxel kit keeps its measured rules: it imports only the local bundle,
     never enables vertexColors, and caps emission at 1;
  6. the kit's SKILL.md has a description the prompt can carry (one line, at
     most 200 characters, says WHEN).

Usage:  check_pathtracer_kit.py [REPO]
Exit 0 = all green.  1 = at least one check failed.  2 = setup error.
"""

import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DESC_CHARS = 200          # crow_core.SKILL_DESC_CHARS
PINNED = ("three", "three-mesh-bvh", "three-gpu-pathtracer")


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def checks(repo):
    """[(name, ok, detail)] for one repository root."""
    kit = os.path.join(repo, "kits", "pathtracer")
    out = []
    try:
        with open(os.path.join(kit, "kit.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as exc:
        return [("kit.json readable", False, str(exc))]

    bundle = os.path.join(kit, meta["bundle"]["file"])
    if not os.path.isfile(bundle):
        out.append(("bundle present", False, bundle))
    else:
        have = sha256(bundle)
        out.append(("bundle sha256 matches kit.json", have == meta["bundle"]["sha256"],
                    "kit.json %s, file %s" % (meta["bundle"]["sha256"], have)))
        size = os.path.getsize(bundle)
        out.append(("bundle size matches kit.json", size == meta["bundle"]["bytes"],
                    "kit.json %d, file %d" % (meta["bundle"]["bytes"], size)))

    for name in PINNED:
        lic = meta["licenses"].get(name)
        path = os.path.join(kit, lic or "(none)")
        ok = bool(lic) and os.path.isfile(path) and \
            sha256(path) == meta["license_sha256"].get(lic)
        out.append(("licence text %s present and unchanged" % name, ok, path))
        pkg = meta["packages"].get(name) or {}
        exact = bool(re.fullmatch(r"\d+\.\d+\.\d+", pkg.get("version", "")))
        out.append(("%s pinned to an exact version" % name, exact, pkg.get("version")))
        out.append(("%s is MIT" % name, pkg.get("license") == "MIT", pkg.get("license")))

    try:
        with open(os.path.join(repo, "NOTICE"), encoding="utf-8") as fh:
            notice = fh.read()
    except OSError as exc:
        return out + [("NOTICE readable", False, str(exc))]
    for name in PINNED:
        version = (meta["packages"].get(name) or {}).get("version", "?")
        out.append(("NOTICE names %s %s" % (name, version),
                    re.search(r"\b%s %s\b" % (re.escape(name), re.escape(version)), notice)
                    is not None, "NOTICE"))

    try:
        with open(os.path.join(kit, "voxel-kit.js"), encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        return out + [("voxel-kit.js readable", False, str(exc))]
    imports = re.findall(r"^\s*(?:import|export)\b[^;]*?\bfrom\s+['\"]([^'\"]+)['\"]", src, re.M)
    out.append(("voxel-kit.js imports only ./crow-pathtracer.js",
                bool(imports) and set(imports) == {"./crow-pathtracer.js"}, ", ".join(imports)))
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("//"))
    out.append(("voxel-kit.js never enables vertexColors",
                re.search(r"vertexColors\s*[:=]\s*true", code) is None, "vertexColors: true"))
    out.append(("voxel-kit.js caps emission at 1",
                re.search(r"MAX_EMISSIVE\s*=\s*1(\.0)?\s*;", code) is not None, "MAX_EMISSIVE"))

    try:
        with open(os.path.join(kit, "SKILL.md"), encoding="utf-8") as fh:
            skill = fh.read()
    except OSError as exc:
        return out + [("SKILL.md readable", False, str(exc))]
    desc = re.search(r"^description:\s*(.*)$", skill, re.M)
    desc = desc.group(1).strip() if desc else ""
    out.append(("SKILL.md description says when and fits",
                desc.startswith("When") and 0 < len(desc) <= SKILL_DESC_CHARS,
                "%d chars: %s" % (len(desc), desc[:60])))
    return out


def main(argv):
    repo = os.path.abspath(argv[1]) if len(argv) > 1 else os.path.dirname(HERE)
    if not os.path.isdir(os.path.join(repo, "kits", "pathtracer")):
        print("SETUP ERROR: no kits/pathtracer under %s" % repo)
        return 2
    failed = 0
    for name, ok, detail in checks(repo):
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
