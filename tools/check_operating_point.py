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

THE SAMPLING TRIPLE IS CHECKED DIFFERENTLY, and the difference is the point:
temperature / top_p / min_p are not flags on a command line, they are written
into the client. There "agrees with the manifest" is NOT "written down once" -
two clients that both hard-write 0.95 agree with the manifest and with each
other, and a comparing check reports them green right up to the day one of them
is edited. So that side is COUNTED (check_sampling below), not compared.

Usage:  check_operating_point.py [--repo <dir>] [--extra <file> ...]
        --extra takes further files to check, e.g. a vault page. They are held to
        the same standard, and a file that does not exist is an error rather than
        a skip - a check that silently passes on a missing file checks nothing.

Exit 0 = every copy agrees.  1 = at least one differs.  2 = setup error.
"""

import argparse
import io
import json
import os
import re
import sys
import tokenize

FLAG_SPECS = [
    # key in manifest.server,  regex against the raw text,             how to read it
    ("ctx", r"-c\s+(\d+)", "int"),
    ("ngl", r"-ngl\s+(\d+)", "int"),
    ("parallel", r"-np\s+(\d+)", "int"),
    ("port", r"--port\s+(\d+)", "int"),
    # -ctk / -ctv belong to the Qwen line, and they are half of what keeps the
    # two keys apart. A dense model that fits on the card whole has no expert
    # stream to declare, so its entry declares the cache types instead -- and a
    # region without them cannot satisfy that key, just as a region without
    # --moe-stream cannot satisfy 0731's. The ports differ too, which is the
    # backstop if a later pair of models ever declares the same flags.
    ("cache_type_k", r"-ctk\s+(\S+)", "str"),
    ("cache_type_v", r"-ctv\s+(\S+)", "str"),
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
    # EVERY NORMALISATION BELOW IS CONDITIONAL, AND THAT IS THE WHOLE OF #111's
    # CHANGE HERE. One server block could be assumed to carry all three keys.
    # A map of them cannot: the Qwen entry declares no expert stream and no
    # template file, and an unconditional `want["moe_stream_l2"] = True` would
    # ADD a requirement the manifest never made -- inventing a flag for a line
    # that must not have it, and failing the correct copy.
    if "slot_save_path" in want:
        want["slot_save_path"] = want["slot_save_path"].replace("\\", "/").split("/")[-1]
    # The manifest records 32 because that is what this machine gets. install.ps1
    # computes it from the detected RAM, so a copy is correct as long as the flag
    # is there at all. Comparing the number would make the check fail on every
    # machine except one, which is a checker nobody can keep green.
    if "moe_stream_l2" in want:
        want["moe_stream_l2"] = True
    if "chat_template_file" in want:
        want["chat_template_file"] = True
    return want


def server_keys(manifest):
    """The model keys that declare a server line, in manifest order.

    Underscore entries are notes, the same convention as everywhere else here.
    A key with no matching entry in models.entries is a SETUP ERROR rather than
    a skip: the point of keying the lines by model is that a line and the GGUF
    it starts are named by the same word, and a line naming a model the table
    does not have is exactly the drift this file exists to catch.
    """
    servers = manifest.get("servers") or {}
    keys = [k for k in servers if not k.startswith("_")]
    known = set((manifest.get("models") or {}).get("entries") or {})
    unknown = [k for k in keys if k not in known]
    return keys, unknown


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


# ---------------------------------------------------------------------------
# The sampling triple: temperature 1.0 / top_p 0.95 / min_p 0.01.
#
# WHY THIS RULE COUNTS AND DOES NOT COMPARE. The triple is not a flag in a
# command line a human copies; it is written into the client. On 2026-08-12 it
# stood in three places: cli/crow.py's stream_reply signature, cli/crow.py's
# argparse defaults, and the manifest. A second client would make it five.
# A comparing predicate - "every copy equals the manifest" - reports two clients
# that BOTH hard-write 0.95 as green, because both do equal the manifest. It
# only goes red after one of them has been edited, i.e. after the damage: the
# same prompt drawing from a different distribution in the two clients, with
# nothing on screen saying so. So the code side is COUNTED: a sampling default
# may be written EXACTLY ONCE across all client sources, and that once has to
# sit in the core. For README.md and install.ps1 repetition is the purpose; for
# shared code it is the defect.
#
# THE SECOND HALF IS THE OMISSION, and it is the failure the GUI draft already
# had: a request body that leaves min_p out does not fall back to the manifest,
# it falls back to llama.cpp's server default of 0.05 - a third value nobody in
# this repo chose. Silence is not agreement, so a file that assembles a request
# body has to name every sampling field the manifest declares.

# Where a client lives. DISCOVERED, not listed by hand: a hand-kept list is
# green about exactly the file nobody remembered to add to it, and that file -
# the second client - is what this rule exists for. A directory that does not
# exist yet (gui/) is not an error; a client that carries the value is caught
# the moment it lands in one of these directories.
CLIENT_DIRS = ("cli", "gui")

# The one file the single copy has to sit in. First entry that exists wins, so
# this survives the core extraction without an edit: while cli/crow_core.py does
# not exist the core IS cli/crow.py, and the day it appears, a value left behind
# in cli/crow.py stops being "the one copy, in the core" and goes red.
CORE_FILES = ("cli/crow_core.py", "cli/crow.py")

# Signed, because a sampling value may legitimately be 0 and a future one may
# not be a float at all.
NUM = r"(-?\d+(?:\.\d+)?)"


def client_sources(repo):
    """Every client source file, test files excluded.

    A test that asserts what goes on the wire HAS to spell the value out - that
    is the test doing its job, and it announces itself by going red when the
    value moves. Counting it as a place that writes the default would make the
    rule permanently red at the one file whose duplication is intentional.
    """
    out = []
    for d in CLIENT_DIRS:
        dpath = os.path.join(repo, d)
        if not os.path.isdir(dpath):
            continue
        for name in sorted(os.listdir(dpath)):
            if not name.endswith(".py"):
                continue
            if name.startswith("test_") or name.endswith("_test.py"):
                continue
            out.append(("%s/%s" % (d, name), os.path.join(dpath, name)))
    return out


def core_source(repo):
    for rel in CORE_FILES:
        if os.path.exists(os.path.join(repo, *rel.split("/"))):
            return rel
    return None


def code_only(text):
    """The file with its prose blanked out, offsets and line numbers intact.

    Case 4 of the self-test is why this exists at all: the checker once read the
    COMMENT explaining --slot-save-path as the flag itself. The same failure
    here would be a comment saying "the card runs agentic work at top_p 0.95"
    counted as a place that WRITES 0.95 - the rule would report a duplicate that
    is a sentence, and a rule that is red at prose gets ignored.

    Comments and triple-quoted strings are replaced by spaces of the same
    length, so every offset and line number reported afterwards still points at
    the real file. Ordinary quoted strings are LEFT ALONE on purpose: `"top_p":
    0.95` in a request body is a place that writes the value and has to count.
    """
    lines = text.splitlines(keepends=True)
    starts, off = [], 0
    for ln in lines:
        starts.append(off)
        off += len(ln)
    buf = list(text)

    def blank(start, end):
        a = starts[start[0] - 1] + start[1]
        b = starts[end[0] - 1] + end[1]
        for i in range(max(a, 0), min(b, len(buf))):
            if buf[i] != "\n":
                buf[i] = " "

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                blank(tok.start, tok.end)
            elif tok.type == tokenize.STRING and tok.string[:3] in ('"""', "'''"):
                blank(tok.start, tok.end)
        return "".join(buf)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file the tokenizer cannot finish still gets the cheap half. A line
        # whose first non-space character is # is a comment in every Python that
        # ever parsed, and losing the docstring half here can only produce a
        # false RED, which someone reads, and not a false green, which nobody
        # does.
        out = []
        for ln in lines:
            # Space for space, so the line numbers reported afterwards still
            # count the same newlines.
            out.append(re.sub(r"[^\n]", " ", ln) if ln.lstrip().startswith("#") else ln)
        return "".join(out)


def sampling_sites(text, key):
    """Every place in one file that WRITES a value for `key`.

    Five shapes, because the value has been written in the first three already
    and the other two are one keystroke away:
      top_p: float = 0.95                     a default in a signature
      "top_p": 0.95                           a literal in a request body
      add_argument("--top-p", …, default=…)   a default on the command line
      body["top_p"] = 0.95                    the same, assembled field by field
      cfg.get("top_p", 0.95)                  a fallback for a missing setting
    Case-insensitive so a module constant (TOP_P = 0.95) counts as well - that
    is the shape a de-duplicated core will use, and it must not be able to hide
    a second copy from the count.

    A pass-through (`"top_p": top_p`) is deliberately NOT a site: it writes no
    value, it forwards one.
    """
    flag = "--" + key.replace("_", "-")
    patterns = [
        r"\b%s\s*(?::\s*[A-Za-z_][\w\.\[\]]*\s*)?=\s*%s" % (re.escape(key), NUM),
        r"[\"']%s[\"']\s*:\s*%s" % (re.escape(key), NUM),
        r"[\"']%s[\"'][^)]*?\bdefault\s*=\s*%s" % (re.escape(flag), NUM),
        r"\[\s*[\"']%s[\"']\s*\]\s*=\s*%s" % (re.escape(key), NUM),
        r"[\"']%s[\"']\s*,\s*%s\s*\)" % (re.escape(key), NUM),
    ]
    hits = []
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            hits.append((m.start(), m.end(), float(m.group(1))))
    hits.sort()
    out = []
    for start, end, value in hits:
        # Two patterns can cover the same stretch of text; that is one site
        # written once, not two copies.
        if out and start < out[-1][1]:
            continue
        out.append((start, end, value))
    return [(start, text[:start].count("\n") + 1, value) for start, _, value in out]


def builds_a_body(text):
    """Does this file assemble a chat request itself?

    Anchored on the two fields every such body has and no prose about one does.
    A client that calls the core instead of building its own body is not asked
    for the sampling fields - it cannot omit them.
    """
    return (re.search(r"[\"']messages[\"']\s*:", text) is not None
            and re.search(r"[\"']stream[\"']\s*:", text) is not None)


def check_sampling(repo, sampling):
    """Count the sampling defaults across the client sources.

    Keys starting with _ are documentation riding inside the manifest, the same
    convention as `expected`. The manifest's own _measurement_temperature is one
    of them: temperature 0 in a probe is a measurement decision, not a client
    default, and those literals stay where they are.
    """
    want = [(k, v) for k, v in sampling.items() if not k.startswith("_")]
    sources = client_sources(repo)
    core = core_source(repo)
    problems = []
    if not want:
        problems.append("the manifest declares no sampling defaults to check")
        return sources, core, problems
    if not sources:
        problems.append("no client source found in %s - nothing was checked"
                        % ", ".join(d + "/" for d in CLIENT_DIRS))
        return sources, core, problems
    if core is None:
        problems.append("no core source: none of %s exists" % ", ".join(CORE_FILES))

    texts = {}
    for rel, path in sources:
        texts[rel] = code_only(read(path))

    for key, value in want:
        sites = []
        for rel, _ in sources:
            for _, line, got in sampling_sites(texts[rel], key):
                sites.append((rel, line, got))
        if not sites:
            problems.append("%s: written nowhere - every client would inherit the "
                            "server's own default, manifest says %r" % (key, value))
            continue
        if len(sites) > 1:
            problems.append("%s: written %d times (%s) - exactly once, and in %s"
                            % (key, len(sites),
                               ", ".join("%s:%d" % (r, l) for r, l, _ in sites),
                               core or "the core"))
        for rel, line, got in sites:
            if got != value:
                problems.append("%s: %s:%d says %r, manifest says %r"
                                % (key, rel, line, got, value))
        if len(sites) == 1 and core is not None and sites[0][0] != core:
            problems.append("%s: the one copy sits in %s, not in the core %s"
                            % (key, sites[0][0], core))

    for rel, _ in sources:
        if not builds_a_body(texts[rel]):
            continue
        missing = [k for k, _ in want
                   if not re.search(r"[\"']%s[\"']\s*:" % re.escape(k), texts[rel])]
        if missing:
            problems.append("%s builds a request body without %s - an omitted field "
                            "is the server's default, not the manifest's"
                            % (rel, ", ".join(missing)))
    return sources, core, problems


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
    keys, unknown = server_keys(manifest)
    if not keys:
        print("SETUP ERROR: %s has no 'servers' block with at least one model key" % mpath)
        return 2
    if unknown:
        print("SETUP ERROR: servers declares %s, which models.entries does not have"
              % ", ".join(sorted(unknown)))
        return 2
    wants = [(key, expected(manifest["servers"][key])) for key in keys]

    copies = [
        ("README.md", os.path.join(args.repo, "README.md")),
        ("install.ps1", os.path.join(args.repo, "install.ps1")),
    ]
    for e in args.extra:
        copies.append((os.path.basename(e), e))

    failed = 0
    checked = 0
    for label, path in copies:
        if not os.path.exists(path):
            # One failure per key, not one per file: with a map of lines the
            # unit that can be right or wrong is (file, key), and counting a
            # missing file once would let the RESULT line read as though only
            # one thing was outstanding.
            print("  FAILED   %-34s does not exist" % label)
            failed += len(wants)
            checked += len(wants)
            continue
        # READ ONCE, COMPARED PER KEY. Every key gets the whole file and looks
        # for its OWN region in it; the old code took the first region that
        # matched the single manifest line and never looked further, which is
        # why a second command line would have been unchecked prose sitting
        # beside a checked one.
        text = read(path)
        for key, want in wants:
            checked += 1
            # THE KEY IS IN THE LABEL, so a failure says WHICH line is wrong.
            # Without it "3 of 12 flags differ" on a file with two command
            # lines sends the reader to the wrong one.
            where = "%s [%s]" % (label, key)
            got, problems = compare(where, text, want)
            if problems:
                failed += 1
                print("  FAILED   %-34s %d of %d flags differ" % (where, len(problems), len(want)))
                for p in problems:
                    print("             %s" % p)
            else:
                print("  OK       %-34s all %d flags match" % (where, len(want)))

    versions, badv = check_versions(args.repo, manifest["version"])
    if badv:
        failed += 1
        print("  FAILED   %-34s manifest says %s" % ("version literal", manifest["version"]))
        for where, value in versions:
            mark = " <-- differs" if value != manifest["version"] else ""
            print("             %-24s %s%s" % (where, value, mark))
    else:
        print("  OK       %-34s %s in all %d places" % ("version literal", manifest["version"], len(versions)))

    sources, core, sprob = check_sampling(args.repo, manifest.get("sampling", {}))
    if sprob:
        # Counted into `failed` in the same breath as it is printed. A rule class
        # that reports its finding and leaves the exit code alone is the half
        # state this one was written to avoid: four rules on screen and EXIT 0,
        # which every caller reads as agreement.
        failed += 1
        print("  FAILED   %-34s %d problem(s) across %d client file(s)"
              % ("sampling defaults", len(sprob), len(sources)))
        for p in sprob:
            print("             %s" % p)
    else:
        triple = " / ".join("%s %s" % (k, v) for k, v in manifest["sampling"].items()
                            if not k.startswith("_"))
        print("  OK       %-34s %s, once each in %s"
              % ("sampling defaults", triple, core))

    print()
    # `checked` and not len(copies): the unit is one (file, model key) pair, so
    # adding a model to the manifest raises this number and a copy that never
    # learned about it shows up as a missing pair rather than as nothing.
    total = checked + 2
    print("RESULT: %d of %d sources agree with manifests/operating-point.json"
          % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
