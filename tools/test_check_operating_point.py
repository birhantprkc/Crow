"""Negative control for check_operating_point.py.

A drift checker that cannot go red is a decoration. Every case here breaks one
thing and requires the checker to name it, plus the two cases that must NOT fire.

Case 4 is the one this file exists for. On its first run the checker reported the
session directory as "refuses", because a bare \\S+ after --slot-save-path matched
the PROSE at install.ps1:1126 explaining what the flag does. A checker that reads
the comment about a flag instead of the flag is exactly the failure the stage was
written to prevent, so it gets a permanent case.

Usage:  test_check_operating_point.py
Exit 0 = all cases behaved as required.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(HERE, "check_operating_point.py")

# The fixture below builds its README and install.ps1 out of this line and holds them against the
# REAL manifest, so every literal here has to track manifests/operating-point.json. When the
# operating point moves, this line moves with it -- --moe-stream-cache went 64s -> 62s -> 58s on
# 2026-08-11 (#87) and tests 1, 4, 6 and 8 went red both times until it did.
GOOD_LINE = (
    "llama-server.exe -m %LOCALAPPDATA%\\Crow\\models\\UD-IQ3_XXS\\x.gguf "
    "--port 8081 -c 200000 -ngl 99 -np 1 --jinja "
    "--slot-save-path %LOCALAPPDATA%\\Crow\\session "
    "--chat-template-file %LOCALAPPDATA%\\Crow\\templates\\0731-chat-template.jinja "
    "--moe-stream --moe-stream-cache 58s --moe-stream-io-threads 8 --moe-stream-direct "
    "--moe-stream-l2 32\n"
)


def run(repo, extra=None):
    cmd = [sys.executable, TOOL, "--repo", repo]
    for e in extra or []:
        cmd += ["--extra", e]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def fixture(tmp, readme=None, install=None, manifest_patch=None):
    """A minimal repo the checker can be pointed at."""
    root = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(root, "manifests"), exist_ok=True)
    os.makedirs(os.path.join(root, "cli"), exist_ok=True)
    src = json.load(open(os.path.join(REPO, "manifests", "operating-point.json"), encoding="utf-8-sig"))
    if manifest_patch:
        manifest_patch(src)
    with open(os.path.join(root, "manifests", "operating-point.json"), "w", encoding="utf-8") as fh:
        json.dump(src, fh, indent=1)
    ver = src["version"]
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("version-%s-brightgreen\n\n```\n%s```\n" % (ver, readme if readme is not None else GOOD_LINE))
    with open(os.path.join(root, "install.ps1"), "w", encoding="utf-8") as fh:
        fh.write('param([string] $Version = "%s")\n%s' % (ver, install if install is not None else GOOD_LINE))
    with open(os.path.join(root, "cli", "crow.py"), "w", encoding="utf-8") as fh:
        fh.write('VERSION = "%s"\n' % ver)
    return root


def main():
    fail = 0

    def check(label, cond, detail=""):
        nonlocal fail
        if cond:
            print("  OK       %s" % label)
        else:
            fail = 1
            print("  FAILED   %s%s" % (label, (": " + detail) if detail else ""))

    with tempfile.TemporaryDirectory() as tmp:
        # 1 - positive control on a fixture, so a red real repo cannot mask a
        #     checker that says no to everything.
        code, out = run(fixture(tmp, ))
        check("1 a repo that agrees passes", code == 0 and "3 of 3" in out, out.strip()[-200:])
        shutil.rmtree(os.path.join(tmp, "repo"))

        # 2 - a changed value must be named, not just counted.
        bad = GOOD_LINE.replace("--moe-stream-cache 58s", "--moe-stream-cache 48s")
        code, out = run(fixture(tmp, readme=bad))
        check("2 a changed cache size goes red and is named",
              code == 1 and "moe_stream_cache" in out and "48s" in out, out.strip()[-200:])
        shutil.rmtree(os.path.join(tmp, "repo"))

        # 3 - a dropped flag. This is the actual vault drift: the page carried
        #     neither --slot-save-path nor --moe-stream-l2.
        bad = GOOD_LINE.replace("--slot-save-path %LOCALAPPDATA%\\Crow\\session ", "")
        code, out = run(fixture(tmp, readme=bad))
        check("3 a dropped flag goes red as missing",
              code == 1 and "slot_save_path: missing" in out, out.strip()[-200:])
        shutil.rmtree(os.path.join(tmp, "repo"))

        # 4 - THE case. Prose ABOUT the flag must not be read as the flag.
        prose = ("# --slot-save-path refuses to start against a path that is not\n"
                 "# an existing directory, so the server would fail.\n") + GOOD_LINE
        code, out = run(fixture(tmp, install=prose))
        check("4 a comment explaining a flag is not the flag",
              code == 0 and "refuses" not in out, out.strip()[-260:])
        shutil.rmtree(os.path.join(tmp, "repo"))

        # 5 - and the half that must still fire: prose alone, with no real line,
        #     is a copy that lost the flag.
        only_prose = ("# --slot-save-path refuses to start against a path that is\n"
                      "# not an existing directory.\n") + GOOD_LINE.replace(
                          "--slot-save-path %LOCALAPPDATA%\\Crow\\session ", "")
        code, out = run(fixture(tmp, install=only_prose))
        check("5 prose alone does not satisfy the flag",
              code == 1 and "slot_save_path: missing" in out, out.strip()[-200:])
        shutil.rmtree(os.path.join(tmp, "repo"))

        # 6 - --moe-stream-l2 carries a machine-dependent value. A different
        #     number is fine, a missing flag is not.
        code, out = run(fixture(tmp, install=GOOD_LINE.replace("--moe-stream-l2 32", "--moe-stream-l2 48")))
        check("6 a different L2 value is accepted", code == 0, out.strip()[-200:])
        shutil.rmtree(os.path.join(tmp, "repo"))

        code, out = run(fixture(tmp, install=GOOD_LINE.replace(" --moe-stream-l2 32", "")))
        check("7 a missing L2 flag is not", code == 1 and "moe_stream_l2: missing" in out, out.strip()[-200:])
        shutil.rmtree(os.path.join(tmp, "repo"))

        # 8 - the version literal, drifting in one place only.
        def bump(m):
            m["version"] = "9.9.9"
        code, out = run(fixture(tmp, manifest_patch=bump))
        check("8 a version that agrees everywhere passes", code == 0, out.strip()[-200:])
        shutil.rmtree(os.path.join(tmp, "repo"))

        root = fixture(tmp)
        inst = open(os.path.join(root, "install.ps1"), encoding="utf-8").read()
        # The manifest's version, read rather than spelled: this line carried a
        # hard-coded "0.0.6" and went quietly vacuous the day the version moved
        # to 0.1.0 -- the replace missed, both copies stayed equal, and the case
        # that exists to catch a stale literal was itself the stale literal.
        current = json.load(open(os.path.join(root, "manifests", "operating-point.json"),
                                 encoding="utf-8-sig"))["version"]
        open(os.path.join(root, "install.ps1"), "w", encoding="utf-8").write(
            inst.replace('$Version = "%s"' % current, '$Version = "0.0.5"'))
        code, out = run(root)
        check("9 one stale version literal goes red",
              code == 1 and "0.0.5" in out and "differs" in out, out.strip()[-260:])
        shutil.rmtree(root)

        # 10 - a file that is not there must be an error. A checker that skips a
        #      missing file passes by having nothing to read.
        code, out = run(fixture(tmp), extra=[os.path.join(tmp, "nope.md")])
        check("10 a missing --extra file is an error, not a skip",
              code == 1 and "does not exist" in out, out.strip()[-200:])

    print()
    print("RESULT: PASS - the drift checker can go red, and does not go red at prose"
          if not fail else
          "RESULT: FAIL - see the FAILED lines above")
    return fail


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
