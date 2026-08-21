"""Hold every surface against manifests/shared-core.json: shared behaviour, once.

WHY THIS EXISTS. #90's third done criterion is "shared behaviour lives in exactly
one place, and there is a check that says so". The extraction into cli/crow_core.py
does NOT prove it: while cli/crow.py is the only caller, the core has exactly one
consumer and any checker pointed at that arrangement is measuring itself. What
this file measures is the arrangement AFTER a second surface lands - and it says
so out loud until one does, rather than reporting green at a repository that
cannot yet be wrong.

RAW TEXT, NOT AN IMPORT. Importing cli/crow.py and asking whether it has a
`tool_write_file` cannot tell `def tool_write_file` from
`from crow_core import tool_write_file` - and that is precisely the distinction
this checker exists to make. So both are read as text.

THE MANIFEST HAS TWO CLASSES, AND THE SPLIT IS NOT TIDINESS. The first draft of
this stage listed the three refusal sentences, "every failure is a result", the
four loop rules and the reasoning/content split, and hung the def predicates on
all of them. None of those behaviours IS a def name. The manifest would have
declared them, nothing would have checked them, and this tool would have printed
"all". So:

  * NAMES (class a) - functions, classes and module bindings. Four predicates,
    below, all of which have to hold.
  * WORDINGS (class b) - user-visible sentences and literal values. The counting
    predicate from E2: the wording occurs exactly ONCE across the core and every
    surface, and that once is in the core. Comparing would be useless here for
    the same reason it is useless for the sampling triple: two surfaces that
    write the SAME sentence agree with each other and with the manifest, right up
    to the day one of them is edited.

  * AN ENTRY THAT BELONGS TO NEITHER CLASS IS A SETUP ERROR, not a skip. That is
    what keeps the declaration from growing past the check.

THE FOUR PREDICATES FOR A NAME:

  (1) exactly ONE definition in column 0 across the core and every surface, and
      it is in the core;
  (2) ZERO definitions in any surface, ANCHORED WITH INDENTATION so a method
      inside a class counts. This is the likely shape: a GUI is widget- and
      class-heavy, and a second implementation appears there as an indented
      method, not at column 0. A checker that only sees column 0 checks the shape
      that will not occur;
  (3) every surface that USES the name imports it BY NAME from the core. A star
      import is red rather than accepted - not because it is wrong, but because
      it is not evidence: it binds whatever happens to be there;
  (4) THE OTHER DIRECTION. For a name the manifest marks as a user entry, EVERY
      surface has to show a wiring - or the manifest carries an explicit
      "only cli, because ..." beside it. Missing both is red.

WHY (4) MAY NOT BE DROPPED, and it was missing from the first draft: #90 names
two shapes of its failure - "a bug is fixed in one client and not the other" AND
"the CLI stops getting the next feature". Predicates (1)-(3) answer only the
first. Worse, (2) and (3) bind only a surface THAT USES THE NAME, so a surface
that does not offer a core behaviour at all is green by definition. That is the
second shape, and it passes silently.

COLUMN 0 FOR THE UNIQUENESS COUNT, INDENTATION FOR THE SURFACES. Predicate (1)
is looking for the one canonical definition, which sits at column 0. Predicates
(2) and (4) have to see a method. Comments and docstrings are blanked out first
(code_only, borrowed from check_operating_point rather than copied - see below),
so a docstring QUOTING the write_file rule cannot make this red. A checker that
is permanently red gets skipped; one that cannot see the common shape gets
believed, and that is worse.

THE EXPECTED LIST IS NOT GENERATED FROM THE CORE. If it were, it would shrink
with its input and could never go red when a behaviour LEAVES the core. For the
same reason a file the manifest names and that is not there is an ERROR, not a
skip. The symmetric half: a `def tool_*` in the core that the manifest does not
declare is red as well, because otherwise a half-read manifest checks a subset
and still reports "all".

Usage:  check_shared_core.py [--repo <dir>] [--manifest <file>]
Exit 0 = shared behaviour is single-source and every surface is wired.
     1 = at least one rule does not hold.  2 = setup error.
"""

import argparse
import json
import os
import re
import sys

# BORROWED, NOT COPIED, and in this tool of all tools that matters: `code_only`
# and `read` already exist next door. A second copy here would be the very thing
# the file is built to report - shared behaviour written twice - and the comment
# in code_only records a measurement (case 4 of the neighbour's self-test) that
# would go stale in a duplicate.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_operating_point import code_only, read  # noqa: E402

# Where a surface can live. DISCOVERED rather than taken from the manifest, and
# the reason is the one check_operating_point gives for CLIENT_DIRS: a hand-kept
# list is green about exactly the file nobody remembered to add to it, and that
# file - the second surface - is what this checker exists for. A directory that
# does not exist yet (gui/) is not an error; a surface that appears in one of
# these and is NOT declared in the manifest is.
SURFACE_DIRS = ("cli", "gui")

# A repository with one surface cannot demonstrate single-sourcing: the core has
# exactly one consumer, so every predicate below is satisfied by construction.
# Reporting green there would be the checker measuring itself.
MIN_SURFACES = 2

# The three shapes a declared name can take, and the two anchors each needs.
# `binding` is here because TOOL_IMPL is a dict, not a def - hanging a def
# predicate on it would declare it and check nothing.
ANCHORS = {
    "def": (r"^(?:async\s+)?def\s+%s\s*\(",
            r"^[ \t]*(?:async\s+)?def\s+%s\s*\("),
    "class": (r"^class\s+%s\b",
              r"^[ \t]*class\s+%s\b"),
    # `=` and not `==`, and an optional annotation between the two, so
    # `_READ: set[str] = set()` counts and `if TOOL_IMPL == other:` does not.
    "binding": (r"^%s\s*(?::[^=\n]+)?=(?!=)",
                r"^[ \t]*%s\s*(?::[^=\n]+)?=(?!=)"),
}

NAME_KEYS = {"name", "kind", "why", "user_entry", "evidence", "only"}
WORDING_KEYS = {"id", "text", "why"}
ONLY_KEYS = {"surface", "because"}
MANIFEST_KEYS = {"core", "core_module", "surfaces", "helpers", "names", "wordings"}
SURFACE_KEYS = {"path", "label", "why"}

# A HELPER IS NOT A SURFACE, AND THE DIFFERENCE IS EXACTLY ONE PREDICATE.
# It lives under cli/ and it is not a test, so this checker has to know a class
# for it or report it as a stray forever -- and forever-red is how a gate stops
# being read at all.
#
# Held to (1), (2) and (3) exactly like a surface: it may not carry a second
# definition of a core name, and it may not use one without taking it from the
# core by name. Those are the predicates that stop behaviour from being decided
# twice, and a helper can break them just as easily as a client.
#
# EXEMPT FROM (4), which is the whole distinction. "Offers every core behaviour"
# is what makes a thing a client of the core; a module a surface USES is not
# one, and demanding it wire run_turn and save_session would be red at every
# correctly written helper. The `only ..., because` escape cannot say this: it
# names ONE surface, not "every surface except this file".
#
# It does not count towards MIN_SURFACES either. Two helpers still cannot
# demonstrate single-sourcing.
HELPER_KEYS = SURFACE_KEYS


class SetupError(Exception):
    """The manifest cannot be read as written. Exit 2, never a quiet subset."""


def rel(repo, path):
    return os.path.join(repo, *path.split("/"))


def load_manifest(path):
    """Read the manifest, and refuse anything it cannot check.

    Keys starting with _ are documentation riding inside the manifest, the same
    convention manifests/operating-point.json uses. EVERY OTHER UNKNOWN KEY IS AN
    ERROR: a declaration this tool silently ignores is a promise nobody keeps.
    """
    try:
        manifest = json.loads(read(path))
    except OSError as exc:
        raise SetupError("cannot read %s: %s" % (path, exc))
    except ValueError as exc:
        raise SetupError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(manifest, dict):
        raise SetupError("%s must be a JSON object" % path)

    declared = {k for k in manifest if not k.startswith("_")}
    unknown = declared - MANIFEST_KEYS
    if unknown:
        raise SetupError("unknown key(s) in the manifest: %s -- an entry with no "
                         "class behind it is declared and never checked"
                         % ", ".join(sorted(unknown)))
    missing = MANIFEST_KEYS - declared
    if missing:
        raise SetupError("the manifest is missing %s" % ", ".join(sorted(missing)))

    for entry in manifest["surfaces"]:
        bad = {k for k in entry if not k.startswith("_")} - SURFACE_KEYS
        if bad:
            raise SetupError("surface %r carries unknown key(s): %s"
                             % (entry.get("path"), ", ".join(sorted(bad))))
        for key in ("path", "label", "why"):
            if not entry.get(key):
                raise SetupError("a surface entry has no %r" % key)

    for entry in manifest["helpers"]:
        bad = {k for k in entry if not k.startswith("_")} - HELPER_KEYS
        if bad:
            raise SetupError("helper %r carries unknown key(s): %s"
                             % (entry.get("path"), ", ".join(sorted(bad))))
        for key in ("path", "label", "why"):
            if not entry.get(key):
                raise SetupError("a helper entry has no %r" % key)

    seen = set()
    for entry in manifest["names"]:
        bad = {k for k in entry if not k.startswith("_")} - NAME_KEYS
        if bad:
            raise SetupError("name %r carries unknown key(s): %s"
                             % (entry.get("name"), ", ".join(sorted(bad))))
        name = entry.get("name")
        if not name:
            raise SetupError("a name entry has no 'name'")
        if name in seen:
            raise SetupError("%s is declared twice" % name)
        seen.add(name)
        if entry.get("kind") not in ANCHORS:
            raise SetupError("%s has kind %r -- one of %s is required, and a "
                             "missing one is an error rather than a skip"
                             % (name, entry.get("kind"), "/".join(sorted(ANCHORS))))
        if not entry.get("why"):
            raise SetupError("%s has no 'why' -- an undeclared reason is a line "
                             "nobody can review" % name)
        only = entry.get("only")
        if only is not None:
            bad = set(only) - ONLY_KEYS
            if bad or not only.get("surface") or not only.get("because"):
                raise SetupError("%s: 'only' needs a 'surface' and a 'because'" % name)

    ids = set()
    for entry in manifest["wordings"]:
        bad = {k for k in entry if not k.startswith("_")} - WORDING_KEYS
        if bad:
            raise SetupError("wording %r carries unknown key(s): %s"
                             % (entry.get("id"), ", ".join(sorted(bad))))
        for key in ("id", "text", "why"):
            if not entry.get(key):
                raise SetupError("a wording entry has no %r" % key)
        if entry["id"] in ids:
            raise SetupError("wording %s is declared twice" % entry["id"])
        ids.add(entry["id"])

    return manifest


def import_statements(text, module):
    """Every `from <module> import ...` statement, as (start, end, body).

    Parenthesised and continued forms both, because cli/crow.py's block is one
    statement ninety names long. The span is returned so the caller can blank it:
    a name that appears ONLY in the import list is imported, not used, and
    counting it as a use would make predicate (3) satisfy itself.
    """
    out = []
    pattern = re.compile(r"^[ \t]*from\s+%s\s+import\s+" % re.escape(module), re.M)
    for m in pattern.finditer(text):
        rest = text[m.end():]
        if rest.lstrip().startswith("("):
            close = rest.find(")")
            end = m.end() + (len(rest) if close < 0 else close + 1)
        else:
            # A backslash continuation keeps the statement open one more line.
            end = m.end()
            while True:
                nl = text.find("\n", end)
                if nl < 0:
                    end = len(text)
                    break
                end = nl + 1
                if not text[:nl].rstrip().endswith("\\"):
                    break
        out.append((m.start(), end, text[m.end():end]))
    return out


def imported_names(text, module):
    """The names a surface takes out of the core by name, plus the star flag."""
    names, star = set(), False
    for _, _, body in import_statements(text, module):
        if "*" in body:
            star = True
            continue
        for piece in body.replace("(", " ").replace(")", " ").split(","):
            piece = piece.strip()
            if not piece:
                continue
            # `x as y` binds y and proves x came from the core either way.
            names.add(piece.split()[0])
    return names, star


def without_imports(text, module):
    """The surface with its core-import statements blanked, offsets intact."""
    buf = list(text)
    for start, end, _ in import_statements(text, module):
        for i in range(start, min(end, len(buf))):
            if buf[i] != "\n":
                buf[i] = " "
    return "".join(buf)


def definitions(text, name, kind, indented):
    """Where `name` is DEFINED in this text, as a list of line numbers."""
    pattern = ANCHORS[kind][1 if indented else 0] % re.escape(name)
    return [text[:m.start()].count("\n") + 1
            for m in re.finditer(pattern, text, re.M)]


def occurrences(text, needle):
    """Line numbers of every occurrence of a literal wording."""
    out, at = [], text.find(needle)
    while at >= 0:
        out.append(text[:at].count("\n") + 1)
        at = text.find(needle, at + 1)
    return out


def check_name(entry, core_rel, core_code, surfaces, module, helpers=()):
    """The four predicates for one declared name. Returns a list of problems.

    `helpers` join the surfaces for (1), (2) and (3) and are left out of (4).
    See HELPER_KEYS for why that one predicate is the whole difference.
    """
    name, kind = entry["name"], entry["kind"]
    problems = []
    consumers = list(surfaces) + list(helpers)

    # (1) exactly one definition in column 0, over the core and every surface.
    sites = [(core_rel, line) for line in definitions(core_code, name, kind, False)]
    for surf_rel, _, code, _ in consumers:
        sites += [(surf_rel, line) for line in definitions(code, name, kind, False)]
    if not sites:
        problems.append("defined nowhere -- the manifest declares a %s the core "
                        "does not carry" % kind)
    elif len(sites) > 1:
        problems.append("defined %d times (%s) -- exactly once, and in %s"
                        % (len(sites), ", ".join("%s:%d" % s for s in sites), core_rel))
    elif sites[0][0] != core_rel:
        problems.append("the one definition sits in %s:%d, not in the core %s"
                        % (sites[0][0], sites[0][1], core_rel))

    for surf_rel, _, code, _ in consumers:
        # (2) zero definitions in a surface, methods included.
        for line in definitions(code, name, kind, True):
            problems.append("%s:%d defines it -- a second implementation, whether "
                            "at column 0 or as a method" % (surf_rel, line))

    for surf_rel, _, code, star in consumers:
        bare = without_imports(code, module)
        uses = re.search(r"\b%s\b" % re.escape(name), bare) is not None
        if not uses:
            continue
        # (3) named import, or a qualified reference through the core module.
        by_name = name in imported_names(code, module)[0]
        qualified = re.search(r"\b%s\.%s\b" % (re.escape(module), re.escape(name)),
                              bare) is not None
        if by_name or qualified:
            continue
        if star:
            problems.append("%s uses it behind `from %s import *` -- a star import "
                            "binds whatever is there and proves nothing"
                            % (surf_rel, module))
        else:
            problems.append("%s uses it without taking it from %s by name"
                            % (surf_rel, module))

    # (4) the other direction: a user entry has to be wired in EVERY surface.
    if entry.get("user_entry"):
        evidence = entry.get("evidence") or [name]
        only = entry.get("only")
        for surf_rel, _, code, _ in surfaces:
            if only and only["surface"] != surf_rel:
                continue
            bare = without_imports(code, module)
            if any(token in bare for token in evidence):
                continue
            problems.append("%s never wires it (looked for %s) -- a surface that "
                            "does not offer a core behaviour is the second half of "
                            "#90's failure, and the manifest carries no "
                            "\"only ..., because\" line for it"
                            % (surf_rel, " or ".join(repr(t) for t in evidence)))
    return problems


def check_wording(entry, core_rel, core_code, surfaces):
    """The counting predicate: written once, and that once in the core."""
    text = entry["text"]
    sites = [(core_rel, line) for line in occurrences(core_code, text)]
    for surf_rel, _, code, _ in surfaces:
        sites += [(surf_rel, line) for line in occurrences(code, text)]
    if not sites:
        problems = ["written nowhere -- the manifest declares a wording that is "
                    "not in any source, so nothing carries it"]
    elif len(sites) > 1:
        problems = ["written %d times (%s) -- exactly once, and in %s"
                    % (len(sites), ", ".join("%s:%d" % s for s in sites), core_rel)]
    elif sites[0][0] != core_rel:
        problems = ["the one copy sits in %s:%d, not in the core %s -- the next "
                    "surface writes its own" % (sites[0][0], sites[0][1], core_rel)]
    else:
        problems = []
    return problems


def undeclared_surfaces(repo, core_rel, declared):
    """Surface files nobody put in the manifest.

    The half-state this closes: a second surface lands, the manifest never learns
    about it, and every predicate above goes on holding over one file.
    """
    known = set(declared) | {core_rel}
    out = []
    for d in SURFACE_DIRS:
        dpath = os.path.join(repo, d)
        if not os.path.isdir(dpath):
            continue
        for name in sorted(os.listdir(dpath)):
            if not name.endswith(".py"):
                continue
            if name.startswith("test_") or name.endswith("_test.py"):
                continue
            candidate = "%s/%s" % (d, name)
            if candidate not in known:
                out.append(candidate)
    return out


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--manifest", default=None)
    args = ap.parse_args(argv[1:])

    mpath = args.manifest or os.path.join(args.repo, "manifests", "shared-core.json")
    if not os.path.exists(mpath):
        print("SETUP ERROR: no manifest at %s" % mpath)
        return 2
    try:
        manifest = load_manifest(mpath)
    except SetupError as exc:
        print("SETUP ERROR: %s" % exc)
        return 2

    core_rel = manifest["core"]
    module = manifest["core_module"]
    core_path = rel(args.repo, core_rel)
    # A declared file that is not there is an ERROR. A checker that skips it
    # passes by having nothing to read.
    if not os.path.exists(core_path):
        print("SETUP ERROR: the manifest names %s as the core and it does not exist"
              % core_rel)
        return 2
    core_code = code_only(read(core_path))

    surfaces, failed, total = [], 0, 0
    for entry in manifest["surfaces"]:
        path = rel(args.repo, entry["path"])
        if not os.path.exists(path):
            print("SETUP ERROR: the manifest names the surface %s and it does not exist"
                  % entry["path"])
            return 2
        surfaces.append((entry["path"], entry["label"], code_only(read(path)), False))
    surfaces = [(r, l, c, imported_names(c, module)[1]) for r, l, c, _ in surfaces]

    helpers = []
    for entry in manifest["helpers"]:
        path = rel(args.repo, entry["path"])
        if not os.path.exists(path):
            print("SETUP ERROR: the manifest names the helper %s and it does not exist"
                  % entry["path"])
            return 2
        helpers.append((entry["path"], entry["label"], code_only(read(path)), False))
    helpers = [(r, l, c, imported_names(c, module)[1]) for r, l, c, _ in helpers]

    # --- the honesty rule, before any predicate -----------------------------
    total += 1
    if len(surfaces) < MIN_SURFACES:
        failed += 1
        print("  FAILED   %-34s %d declared (%s), not %d -- with one consumer the "
              "core cannot be shown to be single-source, and this checker would be "
              "measuring itself"
              % ("surfaces", len(surfaces), ", ".join(s[0] for s in surfaces),
                 MIN_SURFACES))
    else:
        print("  OK       %-34s %d declared: %s"
              % ("surfaces", len(surfaces), ", ".join(s[0] for s in surfaces)))

    total += 1
    stray = undeclared_surfaces(args.repo, core_rel,
                                [s[0] for s in surfaces] + [h[0] for h in helpers])
    if stray:
        failed += 1
        print("  FAILED   %-34s %s under %s"
              % ("undeclared surface", ", ".join(stray),
                 "/, ".join(SURFACE_DIRS) + "/"))
        for s in stray:
            print("             %s is neither a declared surface nor a declared "
                  "helper" % s)
    else:
        print("  OK       %-34s every file under %s is a surface, a helper or a test"
              % ("undeclared surface", "/, ".join(SURFACE_DIRS) + "/"))

    # --- class (a): the names ------------------------------------------------
    for entry in manifest["names"]:
        total += 1
        problems = check_name(entry, core_rel, core_code, surfaces, module, helpers)
        if problems:
            failed += 1
            print("  FAILED   %-34s %d problem(s)" % (entry["name"], len(problems)))
            for p in problems:
                print("             %s" % p)
        else:
            note = "user entry" if entry.get("user_entry") else entry["kind"]
            print("  OK       %-34s once in %s, %d surface(s) wired (%s)"
                  % (entry["name"], core_rel, len(surfaces), note))

    # --- the symmetric half --------------------------------------------------
    total += 1
    declared_names = {e["name"] for e in manifest["names"]}
    in_core = set(re.findall(r"^def\s+(tool_\w+)\s*\(", core_code, re.M))
    orphans = sorted(in_core - declared_names)
    if orphans:
        failed += 1
        print("  FAILED   %-34s %s" % ("undeclared tool in the core",
                                       ", ".join(orphans)))
        for o in orphans:
            print("             %s is shared behaviour that %s does not declare"
                  % (o, os.path.basename(mpath)))
    else:
        print("  OK       %-34s all %d in %s are declared"
              % ("undeclared tool in the core", len(in_core), core_rel))

    # --- class (b): the wordings --------------------------------------------
    for entry in manifest["wordings"]:
        total += 1
        # HELPERS COUNT HERE TOO. "Written once, and that once in the core" is a
        # counting rule, and a sentence copied into a helper is copied just as
        # surely as one copied into a client.
        problems = check_wording(entry, core_rel, core_code, surfaces + helpers)
        if problems:
            failed += 1
            print("  FAILED   %-34s %s" % (entry["id"], problems[0]))
        else:
            print("  OK       %-34s written once, in %s" % (entry["id"], core_rel))

    print()
    print("RESULT: %d of %d rules hold against %s"
          % (total - failed, total, os.path.relpath(mpath, args.repo).replace("\\", "/")))
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
