"""Negative control for check_shared_core.py.

A single-source checker that cannot go red is a decoration, and this one has a
second way to be useless: it can go red at the wrong thing and get skipped. So
every case below breaks exactly one thing and requires the checker to NAME it,
plus the cases that must NOT fire.

CASES 2 AND 3 ARE THE PAIR THIS FILE EXISTS FOR, and it is two cases rather than
one on purpose. The same core function is duplicated in a throwaway copy of a
surface - once as a top-level `def` and once as a METHOD INSIDE A CLASS. The
second shape is the likely one: cli/crow_gui.py will be widget- and class-heavy,
and a second implementation appears there indented, not at column 0. A checker
that only sees the first form checks the shape that will never occur.

CASE 4 IS THE ONE THE DEF PREDICATES CANNOT REACH. The duplicate is renamed -
`_write_file` inside a panel class - so no def predicate anchored on
`tool_write_file` can see it. What gives it away is the WORDING it carries, which
is why the manifest has a second class at all.

CASE 5 IS THE OTHER HALF: a docstring, and then a comment, that QUOTE the
write_file rule must not make anything red. A checker that is red at prose gets
ignored, and an ignored checker is worse than none.

Usage:  test_check_shared_core.py
Exit 0 = all cases behaved as required.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(HERE, "check_shared_core.py")
REAL_MANIFEST = os.path.join(REPO, "manifests", "shared-core.json")

# THE FIXTURE'S REFUSAL WORDING IS READ, NEVER TYPED. Case 9 of the neighbouring
# suite went quietly vacuous once because it carried a hard-coded version literal
# that stopped matching the day the version moved: the replace missed, both
# copies stayed equal, and the case that existed to catch a stale literal was
# itself the stale literal. A typed "without reading it first" here would stop
# exercising anything the day the real sentence is reworded, and say nothing.
WORDING_ID = "write_file refuses an unread file"


def refusal():
    src = json.loads(open(REAL_MANIFEST, encoding="utf-8-sig").read())
    for entry in src["wordings"]:
        if entry["id"] == WORDING_ID:
            return entry["text"]
    raise SystemExit("the real manifest no longer carries the wording %r -- this "
                     "suite would test nothing" % WORDING_ID)


REFUSAL = refusal()

CORE = '''"""A core, small enough to read and shaped like the real one."""


TOOL_IMPL = {"write_file": None}


class Conversation:
    def __init__(self, system):
        self.system = system


def tool_write_file(path, content=""):
    if path not in _READ:
        return "error: refusing to overwrite %s ''' + REFUSAL + ''' this turn." % path
    return "wrote %d bytes to %s" % (len(content), path)


def run_turn(conversation):
    return conversation
'''

CLI = '''"""The terminal surface."""

from crow_core import (
    Conversation,
    TOOL_IMPL,
    run_turn,
    tool_write_file,
)


def repl(system):
    conversation = Conversation(system)
    return run_turn(conversation)
'''

GUI = '''"""The window surface."""

from crow_core import (
    Conversation,
    TOOL_IMPL,
    run_turn,
    tool_write_file,
)


class Window:
    def send(self, system):
        conversation = Conversation(system)
        return run_turn(conversation)
'''


def manifest(surfaces=None):
    """The hand-written manifest, in the shape the real one has."""
    surfaces = surfaces if surfaces is not None else ["cli/crow.py", "gui/crow_gui.py"]
    return {
        "_what": "a fixture",
        "core": "cli/crow_core.py",
        "core_module": "crow_core",
        "surfaces": [{"path": p, "label": p.split("/")[0], "why": "a fixture surface"}
                     for p in surfaces],
        "names": [
            {"name": "tool_write_file", "kind": "def",
             "why": "the tool whose drift costs data"},
            {"name": "TOOL_IMPL", "kind": "binding",
             "why": "a dict, so the manifest needs a kind"},
            {"name": "Conversation", "kind": "class", "user_entry": True,
             "evidence": ["Conversation("], "why": "the append-only context"},
            {"name": "run_turn", "kind": "def", "user_entry": True,
             "evidence": ["run_turn("], "why": "the tool loop"},
        ],
        "wordings": [
            {"id": WORDING_ID, "text": REFUSAL, "why": "the first refusal with teeth"},
        ],
    }


def fixture(tmp, core=None, cli=None, gui=None, man=None, drop_gui=False):
    """A minimal repository the checker can be pointed at."""
    root = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(root, "manifests"), exist_ok=True)
    os.makedirs(os.path.join(root, "cli"), exist_ok=True)
    src = manifest() if man is None else man
    with open(os.path.join(root, "manifests", "shared-core.json"), "w",
              encoding="utf-8") as fh:
        json.dump(src, fh, indent=1)
    with open(os.path.join(root, "cli", "crow_core.py"), "w", encoding="utf-8") as fh:
        fh.write(CORE if core is None else core)
    with open(os.path.join(root, "cli", "crow.py"), "w", encoding="utf-8") as fh:
        fh.write(CLI if cli is None else cli)
    if not drop_gui:
        os.makedirs(os.path.join(root, "gui"), exist_ok=True)
        with open(os.path.join(root, "gui", "crow_gui.py"), "w", encoding="utf-8") as fh:
            fh.write(GUI if gui is None else gui)
    return root


def run(repo):
    p = subprocess.run([sys.executable, TOOL, "--repo", repo],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def all_green(out):
    """RESULT: N of N -- every rule, not most of them."""
    m = re.search(r"RESULT: (\d+) of (\d+) rules hold", out)
    return bool(m) and m.group(1) == m.group(2) and int(m.group(1)) > 0


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
        def clean():
            shutil.rmtree(os.path.join(tmp, "repo"), ignore_errors=True)

        # 1 - positive control. Without it a checker that says no to everything
        #     would pass every case below.
        code, out = run(fixture(tmp))
        check("1 a repo with two wired surfaces passes",
              code == 0 and all_green(out), out.strip()[-300:])
        clean()

        # 2 - THE first shape: the same function, duplicated at column 0.
        dup = GUI + '''

def tool_write_file(path, content=""):
    return "wrote %d bytes to %s" % (len(content), path)
'''
        code, out = run(fixture(tmp, gui=dup))
        check("2 a top-level duplicate goes red and names it",
              code == 1 and "tool_write_file" in out and "gui/crow_gui.py" in out,
              out.strip()[-300:])
        clean()

        # 3 - THE second shape, and the likely one: the duplicate is a METHOD in
        #     a class, indented, so a column-0 anchor never sees it.
        dup = GUI + '''

class FilePanel:
    def tool_write_file(self, path, content=""):
        return "wrote %d bytes to %s" % (len(content), path)
'''
        code, out = run(fixture(tmp, gui=dup))
        check("3 a duplicate as a method in a class goes red and names it",
              code == 1 and "tool_write_file" in out and "gui/crow_gui.py" in out,
              out.strip()[-300:])
        clean()

        # 4 - the duplicate no def predicate can reach: renamed, indented, and
        #     carrying the wording. Class (b) is the only thing that sees it.
        dup = GUI + '''

class FilePanel:
    def _write_file(self, path, content=""):
        if path not in self._read:
            return "error: refusing to overwrite %s ''' + REFUSAL + ''' this turn." % path
        return "wrote"
'''
        code, out = run(fixture(tmp, gui=dup))
        check("4 a renamed duplicate is caught by its wording",
              code == 1 and WORDING_ID in out and "gui/crow_gui.py" in out,
              out.strip()[-300:])
        clean()

        # 5 - and the half that must NOT fire: a docstring quoting the rule,
        #     including a def line inside it.
        quoting = GUI + '''

def help_text():
    """What write_file does, for the panel's help pane.

    It returns "error: refusing to overwrite PATH ''' + REFUSAL + ''' this turn."
    and the definition it comes from reads

        def tool_write_file(path, content=""):
            ...

    which is why the panel never writes without reading.
    """
    return "see the core"
'''
        code, out = run(fixture(tmp, gui=quoting))
        check("5 a docstring quoting the rule is not a second copy",
              code == 0 and all_green(out), out.strip()[-400:])
        clean()

        # 6 - the same in a comment, which is the other shape prose takes.
        quoting = GUI + '''

# def tool_write_file(path, content) lives in the core, and it answers
# "error: refusing to overwrite PATH ''' + REFUSAL + ''' this turn."
'''
        code, out = run(fixture(tmp, gui=quoting))
        check("6 a comment quoting the rule is not a second copy either",
              code == 0 and all_green(out), out.strip()[-400:])
        clean()

        # 7 - a star import. Not wrong, but not EVIDENCE: it binds whatever is
        #     there, so the checker cannot say the name came from the core.
        star = GUI.replace(
            "from crow_core import (\n    Conversation,\n    TOOL_IMPL,\n"
            "    run_turn,\n    tool_write_file,\n)",
            "from crow_core import *")
        code, out = run(fixture(tmp, gui=star))
        check("7 a star import is not proof and goes red",
              code == 1 and "star import" in out, out.strip()[-300:])
        clean()

        # 8 - the name is used and never imported from the core at all.
        loose = GUI.replace("    run_turn,\n", "")
        code, out = run(fixture(tmp, gui=loose))
        check("8 a used name with no named import goes red",
              code == 1 and "run_turn" in out and "by name" in out, out.strip()[-300:])
        clean()

        # 9 - THE honesty rule. One surface cannot demonstrate single-sourcing:
        #     the core has one consumer and every predicate holds by
        #     construction. Green here would be the checker measuring itself.
        code, out = run(fixture(tmp, man=manifest(surfaces=["cli/crow.py"]),
                                drop_gui=True))
        check("9 one surface is reported as one surface, not as green",
              code == 1 and "not 2" in out, out.strip()[-300:])
        clean()

        # 10 - a file the manifest names and that is not there is an ERROR. A
        #      checker that skips it passes by having nothing to read.
        code, out = run(fixture(tmp, drop_gui=True))
        check("10 a missing declared surface is an error, not a skip",
              code == 2 and "does not exist" in out, out.strip()[-300:])
        clean()

        # 11 - a surface nobody declared. The half-state: a second client lands,
        #      the manifest never learns about it, and the predicates go on
        #      holding over one file.
        root = fixture(tmp)
        with open(os.path.join(root, "gui", "crow_panel.py"), "w", encoding="utf-8") as fh:
            fh.write(GUI)
        code, out = run(root)
        check("11 an undeclared surface goes red",
              code == 1 and "crow_panel.py" in out, out.strip()[-300:])
        clean()

        # 12 - the symmetric half: a tool in the core the manifest does not
        #      declare. Without it a half-read manifest checks a subset and
        #      still prints "all".
        extra = CORE + '''

def tool_delete_file(path):
    return "deleted %s" % path
'''
        code, out = run(fixture(tmp, core=extra))
        check("12 a tool in the core that the manifest omits goes red",
              code == 1 and "tool_delete_file" in out, out.strip()[-300:])
        clean()

        # 13 - THE OTHER DIRECTION, and the predicate the first draft did not
        #      have: the GUI simply does not offer the behaviour. Every other
        #      predicate is green here BY DEFINITION - there is nothing to
        #      duplicate and nothing to import - and this is #90's second
        #      failure shape, "the CLI stops getting the next feature".
        silent = GUI.replace("        return run_turn(conversation)",
                             "        return conversation")
        code, out = run(fixture(tmp, gui=silent))
        check("13 a surface that never wires a user entry goes red",
              code == 1 and "run_turn" in out and "never wires it" in out,
              out.strip()[-400:])
        clean()

        # 14 - and its escape, which has to be WRITTEN DOWN rather than assumed:
        #      the manifest carries an explicit "only cli, because ...".
        man = manifest()
        for entry in man["names"]:
            if entry["name"] == "run_turn":
                entry["only"] = {"surface": "cli/crow.py",
                                 "because": "the window reports tool calls and runs none"}
        code, out = run(fixture(tmp, gui=silent, man=man))
        check("14 a declared 'only cli, because ...' makes it green again",
              code == 0 and all_green(out), out.strip()[-400:])
        clean()

        # 15 - a wording written twice. Two surfaces that write the SAME sentence
        #      agree with each other and with the manifest; the count does not.
        twice = CLI + '''

def refusal_banner(path):
    return "error: refusing to overwrite %s ''' + REFUSAL + ''' this turn." % path
'''
        code, out = run(fixture(tmp, cli=twice))
        check("15 a wording written twice goes red and names both files",
              code == 1 and "cli/crow.py" in out and "cli/crow_core.py" in out
              and "written 2 times" in out, out.strip()[-300:])
        clean()

        # 16 - written once, but in a surface. "Exactly one" alone would be green
        #      here, and the surface would own a sentence the core is supposed to
        #      hold - which is the state the abort line is in today.
        moved_core = CORE.replace(
            '        return "error: refusing to overwrite %s ' + REFUSAL + ' this turn." % path',
            '        return "error: refused"')
        # Said out loud rather than assumed. A replace that misses leaves the
        # wording in the core, the case reports "written 2 times" instead of
        # "not in the core", and the failure reads like the checker's rather
        # than the fixture's.
        if moved_core == CORE:
            raise SystemExit("case 16's fixture no longer removes the wording from "
                             "the core -- it would be testing case 15 twice")
        code, out = run(fixture(tmp, core=moved_core, cli=twice))
        check("16 the one copy has to sit in the core",
              code == 1 and "not in the core" in out, out.strip()[-300:])
        clean()

        # 17 - a manifest entry with no class behind it. An unknown kind is a
        #      SETUP ERROR: otherwise the declaration grows past the check, which
        #      is the exact half-state the two classes exist to prevent.
        man = manifest()
        man["names"][0]["kind"] = "behaviour"
        code, out = run(fixture(tmp, man=man))
        check("17 an entry with an unknown kind is a setup error, not a skip",
              code == 2 and "kind" in out, out.strip()[-300:])
        clean()

        # 18 - the same at the top level: a key nobody checks is a promise nobody
        #      keeps.
        man = manifest()
        man["invariants"] = ["the three refusals"]
        code, out = run(fixture(tmp, man=man))
        check("18 an unknown manifest key is a setup error",
              code == 2 and "invariants" in out, out.strip()[-300:])
        clean()

        # 19 - a declared name the core does not carry. The list is written by
        #      hand precisely so it can outlive the code and say so.
        man = manifest()
        man["names"].append({"name": "load_session", "kind": "def",
                             "why": "declared but never written"})
        code, out = run(fixture(tmp, man=man))
        check("19 a declared name the core does not define goes red",
              code == 1 and "load_session" in out and "defined nowhere" in out,
              out.strip()[-300:])
        clean()

    print()
    print("RESULT: PASS - the single-source checker sees both duplication shapes, "
          "and does not go red at prose"
          if not fail else
          "RESULT: FAIL - see the FAILED lines above")
    return fail


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
