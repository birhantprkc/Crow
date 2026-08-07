# Determinism and reasoning-share analyser for stored probe-suite runs.
#
# WHY THIS EXISTS
# The gate (#46) reports one number per run: k of 10. Across three runs of the SAME
# configuration that number moved 9/8/9, and the obvious reading is "the gate is noisy,
# average it away". That reading is wrong, and this tool is what proves it wrong: the
# variation is not spread evenly over the ten tasks. Five tasks reproduce BYTE-IDENTICALLY
# in every run and five differ in every run, with nothing in between. A failure that lands
# on the identical side is a property of the build; one on the other side is a coin flip.
# Averaging mixes the two and destroys exactly the distinction that makes k usable.
#
# It also measures how much of what the model generates is reasoning rather than answer.
# probe-suite.py already learned (its own header, measured 2026-08-04) that a harness
# reading `content` alone misreads a cut-off turn as "the model wrote nothing". This tool
# quantifies the same field on runs that were NOT cut off, which is what makes the number
# transferable to any other consumer of the endpoint.
#
# WHAT IT IS NOT
# Not a measurement. It runs no server, loads no model, and spends nothing - it only reads
# JSON that a probe-suite run already wrote. Every number it prints is recomputable from
# files under runs/ and nothing else.
# It also does not judge correctness. probe-suite.py owns that verdict; reading the same
# files twice with two opinions is how two sources of truth start.
#
# THE COMPARISON IS OVER reasoning + content, NOT over the extracted .py
# Two runs can emit different reasoning and converge on the same code. Hashing the .py
# alone would call that "identical" and hide the divergence. Hashing reasoning and content
# together with a separator answers the question actually being asked - did the model take
# the same path - and the separator stops a shift of text across the boundary from looking
# like a match.
#
# SELF-TEST BEFORE ANY VERDICT
# A determinism detector that always answers "identical" would confirm every hypothesis put
# to it. `selftest` builds synthetic runs and requires BOTH colours: identical inputs must
# report identical, inputs differing by a SINGLE character must report divergence, and the
# divergence point must land on that character. If any of those fail the tool exits 3
# (DETECTOR BROKEN) rather than reporting a determinism it did not establish.
#
# Usage:
#   analyze-run-determinism.py selftest
#   analyze-run-determinism.py analyze runs/2026-08-07/quality-stability/run-1 ... run-3
#   analyze-run-determinism.py analyze <dirs...> --json out.json

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

# probe-suite writes one JSON per task plus this aggregate; the aggregate carries no
# choices[] and would silently become a zero-length "task" if it were not excluded.
SUMMARY_NAME = "summary.json"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DETECTOR_BROKEN = 3

# The separator is a NUL because it cannot occur in JSON string content. Joining with a
# printable character would let reasoning ending in that character and content starting
# with it collide.
_SEP = "\x00"


class Response:
    """One stored assistant turn, reduced to what determinism depends on."""

    __slots__ = ("task", "run", "reasoning", "content", "finish_reason", "predicted_n",
                 "prompt_ms", "predicted_ms")

    def __init__(self, task, run, reasoning, content, finish_reason,
                 predicted_n, prompt_ms, predicted_ms):
        self.task = task
        self.run = run
        self.reasoning = reasoning
        self.content = content
        self.finish_reason = finish_reason
        self.predicted_n = predicted_n
        self.prompt_ms = prompt_ms
        self.predicted_ms = predicted_ms

    @property
    def reasoning_chars(self) -> int:
        return len(self.reasoning)

    @property
    def content_chars(self) -> int:
        return len(self.content)

    @property
    def reasoning_share(self) -> float:
        total = self.reasoning_chars + self.content_chars
        return 0.0 if total == 0 else 100.0 * self.reasoning_chars / total

    def fingerprint(self) -> str:
        blob = (self.reasoning + _SEP + self.content).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16].upper()


def load_response(path: Path, run_label: str) -> Response | None:
    """Read one stored task response. Returns None for files that carry no turn.

    A malformed file is skipped with a warning rather than aborting: a single unreadable
    task must not make the other nine unavailable, but it must not vanish either.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! {path.name}: unreadable ({exc})", file=sys.stderr)
        return None

    choices = doc.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message") or {}
    timings = doc.get("timings") or {}

    # `reasoning_content` is absent on endpoints/builds that do not split it out, and is
    # explicitly null on some. Both mean "no reasoning", not "crash".
    return Response(
        task=path.stem,
        run=run_label,
        reasoning=message.get("reasoning_content") or "",
        content=message.get("content") or "",
        finish_reason=choices[0].get("finish_reason"),
        predicted_n=timings.get("predicted_n"),
        prompt_ms=timings.get("prompt_ms"),
        predicted_ms=timings.get("predicted_ms"),
    )


def load_run(directory: Path) -> list[Response]:
    if not directory.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")
    label = directory.name
    out = []
    for path in sorted(directory.glob("*.json")):
        if path.name == SUMMARY_NAME:
            continue
        resp = load_response(path, label)
        if resp is not None:
            out.append(resp)
    return out


def divergence_point(a: str, b: str) -> int:
    """Index of the first differing character; len of the shorter if one is a prefix."""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def group_by_task(responses):
    tasks = {}
    for r in responses:
        tasks.setdefault(r.task, []).append(r)
    return tasks


def analyse(run_dirs):
    """Return the full report as a plain dict, so callers can print or serialise it."""
    responses = []
    for d in run_dirs:
        responses.extend(load_run(Path(d)))
    if not responses:
        raise ValueError("no task responses found in the given directories")

    tasks = group_by_task(responses)

    total_reasoning = sum(r.reasoning_chars for r in responses)
    total_content = sum(r.content_chars for r in responses)
    total_all = total_reasoning + total_content

    task_reports = {}
    for name, group in sorted(tasks.items()):
        prints = [r.fingerprint() for r in group]
        unique = len(set(prints))
        report = {
            "runs": len(group),
            "unique_fingerprints": unique,
            "deterministic": unique == 1,
            "max_predicted_n": max((r.predicted_n or 0) for r in group),
            "finish_reasons": sorted({r.finish_reason for r in group}, key=str),
            "reasoning_share_pct": round(
                sum(r.reasoning_share for r in group) / len(group), 1),
        }
        # The divergence point only means something when the group actually diverges,
        # and only the first differing pair is reported - the rest is the same story.
        if unique > 1 and len(group) >= 2:
            first, second = group[0], group[1]
            a = first.reasoning + _SEP + first.content
            b = second.reasoning + _SEP + second.content
            idx = divergence_point(a, b)
            report["divergence_index"] = idx
            report["divergence_share_pct"] = round(
                100.0 * idx / min(len(a), len(b)), 1) if min(len(a), len(b)) else 0.0
            report["divergence_context"] = a[max(0, idx - 60):idx].replace("\n", " ")
            report["divergence_a"] = a[idx:idx + 40].replace("\n", " ")
            report["divergence_b"] = b[idx:idx + 40].replace("\n", " ")
        task_reports[name] = report

    deterministic = [n for n, r in task_reports.items() if r["deterministic"]]
    varying = [n for n, r in task_reports.items() if not r["deterministic"]]

    return {
        "runs_analysed": len(run_dirs),
        "responses": len(responses),
        "responses_with_reasoning": sum(1 for r in responses if r.reasoning_chars > 0),
        "reasoning_chars": total_reasoning,
        "content_chars": total_content,
        "reasoning_share_pct": round(100.0 * total_reasoning / total_all, 1) if total_all else 0.0,
        "finish_reasons": {
            fr: sum(1 for r in responses if r.finish_reason == fr)
            for fr in sorted({r.finish_reason for r in responses}, key=str)
        },
        "deterministic_tasks": sorted(deterministic),
        "varying_tasks": sorted(varying),
        # The two groups are only a clean split if the longest stable chain is shorter
        # than the shortest varying one. When they overlap, chain length is NOT the
        # explanation and saying so is the point of carrying these two numbers.
        "max_predicted_n_deterministic": max(
            (task_reports[n]["max_predicted_n"] for n in deterministic), default=None),
        "min_predicted_n_varying": min(
            (task_reports[n]["max_predicted_n"] for n in varying), default=None),
        "tasks": task_reports,
    }


def print_report(rep) -> None:
    print(f"runs analysed:  {rep['runs_analysed']}")
    print(f"responses:      {rep['responses']}")
    print(f"  with reasoning_content: {rep['responses_with_reasoning']} "
          f"of {rep['responses']}")
    print(f"reasoning chars: {rep['reasoning_chars']}   "
          f"content chars: {rep['content_chars']}")
    print(f"REASONING SHARE OF ALL GENERATED TEXT: {rep['reasoning_share_pct']} %")
    print(f"finish_reason:  " + ", ".join(
        f"{k}={v}" for k, v in rep["finish_reasons"].items()))
    print()

    print(f"{'task':24} {'runs':>4} {'uniq':>4}  {'verdict':<22} {'maxTok':>6} {'reas%':>6}")
    print("-" * 74)
    for name, t in sorted(rep["tasks"].items(),
                          key=lambda kv: (kv[1]["unique_fingerprints"], kv[0])):
        verdict = ("identical %d/%d" % (t["runs"], t["runs"]) if t["deterministic"]
                   else "varies (%d variants)" % t["unique_fingerprints"])
        print(f"{name:24} {t['runs']:>4} {t['unique_fingerprints']:>4}  {verdict:<22} "
              f"{t['max_predicted_n']:>6} {t['reasoning_share_pct']:>6}")
    print()

    det, var = rep["deterministic_tasks"], rep["varying_tasks"]
    print(f"deterministic: {len(det)}  ({', '.join(det) if det else '-'})")
    print(f"varying:       {len(var)}  ({', '.join(var) if var else '-'})")

    hi, lo = rep["max_predicted_n_deterministic"], rep["min_predicted_n_varying"]
    if hi is not None and lo is not None:
        if hi < lo:
            print(f"CLEAN SPLIT BY CHAIN LENGTH: every stable task <= {hi} decoded tokens, "
                  f"every varying task >= {lo}. No task falls between.")
        else:
            # Named explicitly: with an overlap, chain length does not separate the two
            # groups and any claim that it does would be reading a pattern into noise.
            print(f"NO CLEAN SPLIT: stable reaches {hi} tokens, varying starts at {lo} "
                  f"- the groups overlap, so chain length does not explain them.")

    if var:
        print()
        print("divergence points (first differing pair per varying task):")
        for name in var:
            t = rep["tasks"][name]
            if "divergence_index" not in t:
                continue
            print(f"  {name}: char {t['divergence_index']} "
                  f"({t['divergence_share_pct']} % into the shorter chain)")
            print(f"    ...{t['divergence_context']}")
            print(f"    A> {t['divergence_a']}")
            print(f"    B> {t['divergence_b']}")


def _write_fake_run(root: Path, name: str, tasks) -> Path:
    """Build a synthetic run directory. tasks: {name: (reasoning, content)}."""
    d = root / name
    d.mkdir(parents=True)
    for task, (reasoning, content) in tasks.items():
        doc = {
            "choices": [{
                "message": {"reasoning_content": reasoning, "content": content},
                "finish_reason": "stop",
            }],
            "timings": {"predicted_n": len(reasoning) + len(content),
                        "prompt_ms": 1.0, "predicted_ms": 2.0},
        }
        (d / f"{task}.json").write_text(json.dumps(doc), encoding="utf-8")
    # A summary must be ignored, not counted as an eleventh task.
    (d / SUMMARY_NAME).write_text(json.dumps({"total": len(tasks)}), encoding="utf-8")
    return d


def selftest(verbose=True) -> int:
    """Prove the detector produces BOTH colours. Returns an exit code."""
    failures = []

    def check(label, condition, detail=""):
        if condition:
            if verbose:
                print(f"  ok   {label}")
        else:
            failures.append(f"{label}{(' - ' + detail) if detail else ''}")
            print(f"  FAIL {label} {detail}", file=sys.stderr)

    root = Path(tempfile.mkdtemp(prefix="crow-determinism-selftest-"))
    try:
        if verbose:
            print("selftest: identical inputs must report identical")
        a = _write_fake_run(root, "run-1", {"stable": ("thinking hard", "def f(): pass")})
        b = _write_fake_run(root, "run-2", {"stable": ("thinking hard", "def f(): pass")})
        rep = analyse([a, b])
        check("identical pair -> deterministic", rep["tasks"]["stable"]["deterministic"])
        check("identical pair -> 1 fingerprint",
              rep["tasks"]["stable"]["unique_fingerprints"] == 1)
        check("summary.json ignored", rep["responses"] == 2,
              f"got {rep['responses']} responses, expected 2")

        # THE NEGATIVE PROBE. Without this the tool would confirm any determinism claim.
        if verbose:
            print("selftest: a single differing character must report divergence")
        c = _write_fake_run(root, "run-3", {"drift": ("abcdefghij", "same")})
        d = _write_fake_run(root, "run-4", {"drift": ("abcdefXhij", "same")})
        rep2 = analyse([c, d])
        check("one-char difference -> NOT deterministic",
              not rep2["tasks"]["drift"]["deterministic"])
        check("one-char difference -> 2 fingerprints",
              rep2["tasks"]["drift"]["unique_fingerprints"] == 2)
        check("divergence index lands on the changed character",
              rep2["tasks"]["drift"].get("divergence_index") == 6,
              f"got {rep2['tasks']['drift'].get('divergence_index')}, expected 6")

        # A difference that lives only in content must not be masked by equal reasoning.
        if verbose:
            print("selftest: a difference in content alone must still be seen")
        e = _write_fake_run(root, "run-5", {"tail": ("same reasoning", "return 1")})
        f = _write_fake_run(root, "run-6", {"tail": ("same reasoning", "return 2")})
        rep3 = analyse([e, f])
        check("content-only difference -> NOT deterministic",
              not rep3["tasks"]["tail"]["deterministic"])

        # Text moving across the reasoning/content boundary must not look identical.
        if verbose:
            print("selftest: text shifted across the boundary must not collide")
        g = _write_fake_run(root, "run-7", {"shift": ("ab", "cd")})
        h = _write_fake_run(root, "run-8", {"shift": ("a", "bcd")})
        rep4 = analyse([g, h])
        check("boundary shift -> NOT deterministic",
              not rep4["tasks"]["shift"]["deterministic"])

        if verbose:
            print("selftest: reasoning share and a missing field")
        i = _write_fake_run(root, "run-9", {"half": ("1234", "5678")})
        rep5 = analyse([i])
        check("50/50 -> 50.0 %", rep5["reasoning_share_pct"] == 50.0,
              f"got {rep5['reasoning_share_pct']}")

        j = root / "run-10"
        j.mkdir()
        (j / "noreason.json").write_text(json.dumps({
            "choices": [{"message": {"content": "only content"}, "finish_reason": "stop"}],
            "timings": {"predicted_n": 3},
        }), encoding="utf-8")
        rep6 = analyse([j])
        check("absent reasoning_content -> 0 %, no crash",
              rep6["reasoning_share_pct"] == 0.0 and rep6["responses"] == 1)

        # The overlap branch must be reachable, or the "clean split" line is unfalsifiable.
        if verbose:
            print("selftest: overlapping chain lengths must NOT claim a clean split")
        k1 = _write_fake_run(root, "run-11",
                             {"short_stable": ("x" * 50, ""), "long_varying": ("y" * 10, "")})
        k2 = _write_fake_run(root, "run-12",
                             {"short_stable": ("x" * 50, ""), "long_varying": ("z" * 10, "")})
        rep7 = analyse([k1, k2])
        hi7, lo7 = rep7["max_predicted_n_deterministic"], rep7["min_predicted_n_varying"]
        # Compared as a pair and only when both exist. A run in which every task is stable
        # (or every task varies) leaves one side None, and that is a legitimate outcome -
        # found by sabotaging fingerprint(), which made all tasks look deterministic and
        # turned this check into a TypeError instead of a verdict.
        check("stable longer than varying -> overlap detected",
              hi7 is not None and lo7 is not None and hi7 >= lo7,
              f"got max_stable={hi7}, min_varying={lo7}")

        # A one-sided report must be produced and printed, not crashed on.
        if verbose:
            print("selftest: an all-stable run must report, not crash")
        m1 = _write_fake_run(root, "run-13", {"only": ("same", "same")})
        m2 = _write_fake_run(root, "run-14", {"only": ("same", "same")})
        rep8 = analyse([m1, m2])
        check("all-stable -> min_predicted_n_varying is None",
              rep8["min_predicted_n_varying"] is None)
        try:
            import io
            import contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                print_report(rep8)
            check("all-stable -> print_report survives", True)
        except Exception as exc:  # noqa: BLE001 - any exception here is the failure
            check("all-stable -> print_report survives", False, repr(exc))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print(f"DETECTOR BROKEN - {len(failures)} check(s) failed:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return EXIT_DETECTOR_BROKEN
    print("selftest passed - the detector reports both colours.")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="prove the detector produces both colours")

    p_an = sub.add_parser("analyze", help="analyse stored probe-suite run directories")
    p_an.add_argument("dirs", nargs="+", help="run directories written by probe-suite run")
    p_an.add_argument("--json", default=None, help="also write the report as JSON")
    p_an.add_argument("--skip-selftest", action="store_true",
                      help="do not self-test first (for debugging the tool itself)")

    args = ap.parse_args()

    if args.cmd == "selftest":
        return selftest()

    # The detector runs against itself before it is allowed to judge stored runs - the
    # same order probe-suite.py uses, and for the same reason.
    if not args.skip_selftest:
        rc = selftest(verbose=False)
        if rc != EXIT_OK:
            print("refusing to analyse with a broken detector.", file=sys.stderr)
            return rc
        print("(selftest passed)\n")

    try:
        rep = analyse(args.dirs)
    except (NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print_report(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.json}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
