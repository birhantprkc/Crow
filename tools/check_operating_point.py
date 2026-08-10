"""Hold every written copy of the operating point against manifests/operating-point.json.

WHY THIS EXISTS: the server command line was spelled out in three places and they
disagreed. On 2026-08-10 the vault page carried neither --slot-save-path nor
--moe-stream-l2 32 while README.md and install.ps1 carried both. Nothing had been
changed wrongly; the copies drifted. A measurement taken against the older line
is not comparable to one taken against the newer, and nothing said so.

THE FAILURE MODE THIS TOOL MUST NOT HAVE, and it is the obvious one: reading a
second copy of the manifest and comparing it to the first. That checker is green
forever. So this reads the RAW TEXT of README.md and install.ps1 - the prose a
human follows and the line the installer actually prints - and extracts the flags
out of it. If someone edits the README by hand, this goes red.

--moe-stream-l2 is special and says so in the manifest: install.ps1 prints it with
a value computed from the machine's RAM. The flag must be there; the number is
whatever that machine got. Requiring 32 would make the check fail on every
machine other than this one.

Usage:  check_operating_point.py [--repo <dir>] [--extra <file> ...]
        --extra takes further files to check, e.g. a vault page. They are held to
        the same standard, and a file that does not exist is an error rather than
        a skip - a check that silently passes on a missing file checks nothing.

Exit 0 = every copy agrees.  1 = at least one differs.  2 = setup error.
"""

import argparse
import json
import os
import re
import sys

FLAG_SPECS = [
    # key in manifest.server,  regex against the raw text,             how to read it
    ("ctx", r"-c\s+(\d+)", "int"),
    ("ngl", r"-ngl\s+(\d+)", "int"),
    ("parallel", r"-np\s+(\d+)", "int"),
    ("port", r"--port\s+(\d+)", "int"),
    ("jinja", r"(--jinja)\b", "flag"),
    ("moe_stream", r"(--moe-stream)(?![-\w])", "flag"),
    ("moe_stream_cache", r"--moe-stream-cache\s+(\S+)", "str"),
    ("moe_stream_io_threads", r"--moe-stream-io-threads\s+(\d+)", "int"),
    ("moe_stream_direct", r"(--moe-stream-direct)\b", "flag"),
    # Value deliberately not compared - see the module docstring.
    ("moe_stream_l2", r"(--moe-stream-l2)\s+\S+", "flag"),
    # Same treatment: the flag must be there, the path differs per install.
    # 0731 ships no template and its GGUF embeds one that fails golden vector 4.
    ("chat_template_file", r"(--chat-template-file)\s+\S+", "flag"),
    # The capture demands something path-shaped: a separator or a variable marker.
    # Measured on the first run of this tool: a plain \S+ matched the PROSE at
    # install.ps1:1126 -- "# --slot-save-path refuses to start against a path
    # that is not an existing directory" -- and reported the operating point's
    # session directory as "refuses". A checker that reads the comment explaining
    # a flag instead of the flag is the failure this whole stage is about.
    ("slot_save_path", r"--slot-save-path\s+(\S*[\\/%$]\S*)", "path"),
]


def read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


# How far past "llama-server.exe" a command line can reach.
#
# MEASURED, not chosen: in install.ps1 the printed line is a run of Write-Host
# calls with explanatory comments between them, and from the binary name to
# --moe-stream-l2 is 1471 characters. A first guess of 1200 cut the last four
# flags off and reported the installer as broken. README.md's block is about 420.
REGION_CHARS = 2000


def command_regions(text):
    """The stretches of text that ARE a server command line.

    Measured 2026-08-10: searching the whole document reported `-c 65536` for the
    vault's operating-point page, because that page QUOTES the old value in order
    to warn against it. Prose that cites a wrong line is not a wrong line, and a
    checker that cannot tell them apart makes the correct page permanently red --
    which teaches everyone to ignore it.

    Anchored on the binary name, because every copy starts there whatever else it
    does with paths and quoting.
    """
    starts = [m.start() for m in re.finditer(r"llama-server(?:\.exe)?", text)]
    out = []
    for i, s in enumerate(starts):
        # Stop at the next occurrence as well as at the length limit, so a long
        # region can never merge two command lines into one that satisfies the
        # manifest with halves of both.
        end = min(s + REGION_CHARS, starts[i + 1] if i + 1 < len(starts) else len(text))
        out.append(text[s:end])
    return out


def extract(text):
    """Pull the operating-point flags out of one command region."""
    found = {}
    for key, pattern, kind in FLAG_SPECS:
        m = re.search(pattern, text)
        if not m:
            continue
        if kind == "flag":
            found[key] = True
        elif kind == "int":
            found[key] = int(m.group(1))
        elif kind == "path":
            # Every copy writes the install directory differently -
            # %LOCALAPPDATA%\Crow, $InstallTo, an absolute path. Only the leaf
            # matters: the flag must point at the session directory.
            found[key] = m.group(1).replace("\\", "/").rstrip("`").split("/")[-1]
        else:
            found[key] = m.group(1).rstrip("`")
    return found


def expected(server):
    # Keys starting with _ are documentation riding inside the manifest, the
    # same convention as everywhere else in the file. Treating one as a flag
    # made every copy red the moment a note moved into the server block.
    want = {k: v for k, v in server.items() if not k.startswith("_")}
    want["slot_save_path"] = want["slot_save_path"].replace("\\", "/").split("/")[-1]
    # The manifest records 32 because that is what this machine gets. install.ps1
    # computes it from the detected RAM, so a copy is correct as long as the flag
    # is there at all. Comparing the number would make the check fail on every
    # machine except one, which is a checker nobody can keep green.
    want["moe_stream_l2"] = True
    want["chat_template_file"] = True
    return want


def compare(label, text, want):
    """A file passes if ONE of its command lines is the operating point.

    Not "some flag somewhere in the file matches": a document may legitimately
    show other invocations -- a minimal example, a counter-example, an older
    series it warns about. What it may not do is fail to contain the real one.
    The problems reported are those of the closest region, so the message names
    what is actually wrong instead of the worst match.
    """
    regions = command_regions(text)
    if not regions:
        return {}, ["no llama-server command line found in this file"]

    best, best_problems = {}, None
    for region in regions:
        got = extract(region)
        problems = []
        for key, value in want.items():
            if key not in got:
                problems.append("%s: missing" % key)
            elif got[key] != value:
                problems.append("%s: %r, manifest says %r" % (key, got[key], value))
        if not problems:
            return got, []
        if best_problems is None or len(problems) < len(best_problems):
            best, best_problems = got, problems
    return best, best_problems


def check_versions(repo, manifest_version):
    """The version literal, everywhere it is written down.

    install.ps1's copy is NOT redundant: it is the fallback used when the GitHub
    API cannot be reached (install.ps1 resolves the newest release at runtime and
    overwrites it). It cannot be removed without breaking a run through iex, which
    has no repository to read. So it is checked rather than deleted.
    """
    out = []
    crow = read(os.path.join(repo, "cli", "crow.py"))
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', crow, re.M)
    out.append(("cli/crow.py", m.group(1) if m else None))

    inst = read(os.path.join(repo, "install.ps1"))
    m = re.search(r'\[string\]\s*\$Version\s*=\s*"([^"]+)"', inst)
    out.append(("install.ps1 (fallback)", m.group(1) if m else None))

    readme = read(os.path.join(repo, "README.md"))
    for m in re.finditer(r"version-(\d+\.\d+\.\d+)-brightgreen", readme):
        out.append(("README.md badge", m.group(1)))

    bad = [(w, v) for w, v in out if v != manifest_version]
    return out, bad


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--extra", action="append", default=[])
    args = ap.parse_args(argv[1:])

    mpath = os.path.join(args.repo, "manifests", "operating-point.json")
    if not os.path.exists(mpath):
        print("SETUP ERROR: no manifest at %s" % mpath)
        return 2
    manifest = json.loads(read(mpath))
    want = expected(manifest["server"])

    copies = [
        ("README.md", os.path.join(args.repo, "README.md")),
        ("install.ps1", os.path.join(args.repo, "install.ps1")),
    ]
    for e in args.extra:
        copies.append((os.path.basename(e), e))

    failed = 0
    for label, path in copies:
        if not os.path.exists(path):
            print("  FAILED   %-34s does not exist" % label)
            failed += 1
            continue
        got, problems = compare(label, read(path), want)
        if problems:
            failed += 1
            print("  FAILED   %-34s %d of %d flags differ" % (label, len(problems), len(want)))
            for p in problems:
                print("             %s" % p)
        else:
            print("  OK       %-34s all %d flags match" % (label, len(want)))

    versions, badv = check_versions(args.repo, manifest["version"])
    if badv:
        failed += 1
        print("  FAILED   %-34s manifest says %s" % ("version literal", manifest["version"]))
        for where, value in versions:
            mark = " <-- differs" if value != manifest["version"] else ""
            print("             %-24s %s%s" % (where, value, mark))
    else:
        print("  OK       %-34s %s in all %d places" % ("version literal", manifest["version"], len(versions)))

    print()
    total = len(copies) + 1
    print("RESULT: %d of %d sources agree with manifests/operating-point.json"
          % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
