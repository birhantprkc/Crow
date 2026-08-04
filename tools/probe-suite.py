# Quality gate for output-changing levers (issue #46).
#
# WHY THIS EXISTS
# Every lever in this project changes the model's output: quantisation, router bias, slot
# count, speculative decoding. Until now the whole quality axis rested on ONE prompt
# (probe-f, merge_intervals). On speed alone the harshest quantisation always wins, so a
# gate that a lever must clear is the only thing that keeps a speed number honest.
#
# WHAT IT IS NOT
# Not a benchmark of robin's domain. Ten algorithmic tasks prove "still writes correct,
# running code" - they do not prove "as good as MXFP4 on the real repositories". That set
# would come from the repos themselves and is a separate, more expensive decision.
#
# THE THREE VERDICTS PER TASK, and why the third exists separately
#   RUNS AND CORRECT   - extracted, imported, all cases passed
#   RUNS BUT WRONG     - extracted and imported, a case failed
#   DOES NOT RUN       - no function extracted, or it raised at import or at call time
# Truncation is reported as its own reason inside DOES NOT RUN. Measured 2026-08-04: with a
# tight token budget the server returns finish_reason="length", an EMPTY content and a full
# reasoning_content. A harness reading content alone records that as "the model wrote
# nothing", which is indistinguishable from a real refusal. It is a harness fault, not a
# model fault, and it must say so.
#
# THE EXTRACTION STEP IS WRITTEN ONCE, ON PURPOSE
# On 2026-08-04 a per-task regex `^\s*def merge_intervals` matched the INDENTED signature
# echoed back inside the prompt instead of the generated block. MXFP4 failed with an
# IndentationError and IQ1_S was nearly condemned with a broken tool. Ten copies of that
# regex would be ten chances to repeat it, so there is one extractor and it has its own test.
#
# SELF-TEST BEFORE EVERY VERDICT
# Each task carries a deliberately wrong implementation. `selftest` proves the harness can
# produce BOTH colours before any model touches it: all ten wrong implementations must be
# rejected, and the extractor must survive the prompt-echo sample. If any wrong
# implementation passes, the run aborts with CHECKER BROKEN (exit 3) instead of reporting a
# green it did not earn.
#
# Usage:
#   probe-suite.py selftest
#   probe-suite.py check <task-name> <generated.py>
#   probe-suite.py run --url http://127.0.0.1:8081 --out runs/2026-08-04/quality
#
# Exit codes: 0 all green, 1 at least one task not correct, 2 harness could not run,
#             3 CHECKER BROKEN.

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(HERE, "prompts", "tasks")


# ---------------------------------------------------------------- task table

# Each entry: prompt file, the function name that must appear at column 0, the cases, a
# deliberately wrong implementation, and whether the input must survive the call unmodified.
TASKS = [
    {
        "name": "merge-intervals",
        "prompt": "01-merge-intervals.txt",
        "func": "merge_intervals",
        "immutable": True,
        "cases": [
            ("empty", ([],), []),
            ("touching", ([[1, 3], [3, 5]],), [[1, 5]]),
            ("unsorted overlap", ([[5, 8], [1, 4], [2, 6]],), [[1, 8]]),
            ("disjoint", ([[1, 2], [5, 6]],), [[1, 2], [5, 6]]),
        ],
        "wrong": lambda intervals: [list(p) for p in intervals],
    },
    {
        "name": "two-sum",
        "prompt": "02-two-sum.txt",
        "func": "two_sum",
        "immutable": True,
        "cases": [
            ("classic", ([2, 7, 11, 15], 9), [0, 1]),
            ("duplicate values", ([3, 3], 6), [0, 1]),
            ("no pair", ([1, 2, 3], 7), []),
            ("later pair", ([3, 2, 4], 6), [1, 2]),
        ],
        "wrong": lambda nums, target: [0, 1],
    },
    {
        "name": "is-balanced",
        "prompt": "03-is-balanced.txt",
        "func": "is_balanced",
        "immutable": False,
        "cases": [
            ("empty", ("",), True),
            ("nested with text", ("(a[b]{c})",), True),
            ("crossed", ("(]",), False),
            ("unclosed", ("((",), False),
            ("wrong order", ("a)b(",), False),
        ],
        # Counts bracket characters and calls an even count balanced: "(]" has two, so it
        # says True where the answer is False.
        "wrong": lambda text: sum(1 for c in text if c in "()[]{}") % 2 == 0,
    },
    {
        "name": "rotate-matrix",
        "prompt": "04-rotate-matrix.txt",
        "func": "rotate_matrix",
        "immutable": True,
        "cases": [
            ("empty", ([],), []),
            ("two by two", ([[1, 2], [3, 4]],), [[3, 1], [4, 2]]),
            ("wide", ([[1, 2, 3], [4, 5, 6]],), [[4, 1], [5, 2], [6, 3]]),
        ],
        # Transposes without reversing - the single most common way to get this wrong.
        "wrong": lambda matrix: [list(row) for row in zip(*matrix)],
    },
    {
        "name": "longest-common-prefix",
        "prompt": "05-longest-common-prefix.txt",
        "func": "longest_common_prefix",
        "immutable": False,
        "cases": [
            ("classic", (["flower", "flow", "flight"],), "fl"),
            ("empty list", ([],), ""),
            ("nothing shared", (["a", "b"],), ""),
            ("identical", (["same", "same"],), "same"),
        ],
        "wrong": lambda words: words[0] if words else "",
    },
    {
        "name": "group-anagrams",
        "prompt": "06-group-anagrams.txt",
        "func": "group_anagrams",
        "immutable": True,
        "cases": [
            ("empty", ([],), []),
            (
                "classic",
                (["eat", "tea", "tan", "ate", "nat", "bat"],),
                [["eat", "tea", "ate"], ["tan", "nat"], ["bat"]],
            ),
            ("no anagrams", (["ab", "cd"],), [["ab"], ["cd"]]),
        ],
        # Never groups: one word per group.
        "wrong": lambda words: [[w] for w in words],
    },
    {
        "name": "binary-search",
        "prompt": "07-binary-search.txt",
        "func": "binary_search",
        "immutable": False,
        "cases": [
            ("present", ([1, 3, 5, 7], 5), 2),
            ("absent", ([1, 3, 5], 4), -1),
            ("first of duplicates", ([2, 2, 2], 2), 0),
            ("empty", ([], 1), -1),
        ],
        # Textbook midpoint search without the first-occurrence adjustment: on [2,2,2] it
        # stops at index 1.
        "wrong": lambda sorted_nums, target: (
            _plain_bisect(sorted_nums, target)
        ),
    },
    {
        "name": "rle-encode",
        "prompt": "08-rle-encode.txt",
        "func": "rle_encode",
        "immutable": False,
        "cases": [
            ("empty", ("",), []),
            ("runs", ("aaabbc",), [["a", 3], ["b", 2], ["c", 1]]),
            ("no runs", ("abc",), [["a", 1], ["b", 1], ["c", 1]]),
        ],
        # Emits every character as its own run of one.
        "wrong": lambda text: [[c, 1] for c in text],
    },
    {
        "name": "flatten",
        "prompt": "09-flatten.txt",
        "func": "flatten",
        "immutable": True,
        "cases": [
            ("empty", ([],), []),
            ("nested", ([1, [2, [3, 4]], 5],), [1, 2, 3, 4, 5]),
            ("strings stay whole", (["ab", ["cd"]],), ["ab", "cd"]),
            ("only empties", ([[[]]],), []),
        ],
        # Flattens exactly one level.
        "wrong": lambda items: [
            x for item in items for x in (item if isinstance(item, list) else [item])
        ],
    },
    {
        "name": "top-k-frequent",
        "prompt": "10-top-k-frequent.txt",
        "func": "top_k_frequent",
        "immutable": True,
        "cases": [
            ("classic", (["a", "b", "a", "c", "b", "a"], 2), ["a", "b"]),
            ("empty", ([], 1), []),
            ("k too large", (["x", "y"], 5), ["x", "y"]),
            ("frequency beats order", (["c", "a", "a"], 1), ["a"]),
        ],
        # Returns the first k distinct strings in input order, ignoring frequency: on
        # ["c","a","a"] it answers ["c"] where the answer is ["a"].
        "wrong": lambda items, k: list(dict.fromkeys(items))[:k],
    },
]


# Reference implementations. They exist for ONE reason: ten rejected wrong implementations
# are equally consistent with a checker that rejects everything. Red proves nothing without
# a green beside it, so `selftest` requires both - every reference must pass its own cases,
# and every wrong implementation must fail them.
def _ref_merge_intervals(intervals):
    out = []
    for start, end in sorted([list(p) for p in intervals]):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def _ref_two_sum(nums, target):
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            if nums[i] + nums[j] == target:
                return [i, j]
    return []


def _ref_is_balanced(text):
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for c in text:
        if c in "([{":
            stack.append(c)
        elif c in pairs:
            if not stack or stack.pop() != pairs[c]:
                return False
    return not stack


def _ref_rotate_matrix(matrix):
    return [list(row) for row in zip(*matrix[::-1])]


def _ref_longest_common_prefix(words):
    if not words:
        return ""
    out = words[0]
    for w in words[1:]:
        while not w.startswith(out):
            out = out[:-1]
            if not out:
                return ""
    return out


def _ref_group_anagrams(words):
    order, groups = [], {}
    for w in words:
        key = "".join(sorted(w))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(w)
    return [groups[k] for k in order]


def _ref_binary_search(sorted_nums, target):
    lo, hi, found = 0, len(sorted_nums) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if sorted_nums[mid] == target:
            found = mid
            hi = mid - 1
        elif sorted_nums[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return found


def _ref_rle_encode(text):
    out = []
    for c in text:
        if out and out[-1][0] == c:
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return out


def _ref_flatten(items):
    out = []
    for item in items:
        if isinstance(item, list):
            out.extend(_ref_flatten(item))
        else:
            out.append(item)
    return out


def _ref_top_k_frequent(items, k):
    order, counts = [], {}
    for it in items:
        if it not in counts:
            counts[it] = 0
            order.append(it)
        counts[it] += 1
    return sorted(order, key=lambda w: (-counts[w], order.index(w)))[:k]


REFERENCES = {
    "merge-intervals": _ref_merge_intervals,
    "two-sum": _ref_two_sum,
    "is-balanced": _ref_is_balanced,
    "rotate-matrix": _ref_rotate_matrix,
    "longest-common-prefix": _ref_longest_common_prefix,
    "group-anagrams": _ref_group_anagrams,
    "binary-search": _ref_binary_search,
    "rle-encode": _ref_rle_encode,
    "flatten": _ref_flatten,
    "top-k-frequent": _ref_top_k_frequent,
}


def _plain_bisect(sorted_nums, target):
    lo, hi = 0, len(sorted_nums) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if sorted_nums[mid] == target:
            return mid
        if sorted_nums[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


# ---------------------------------------------------------------- extraction

FENCE = "```"


def extract_function(text, func_name):
    """Return the source of the generated function, or None.

    Two rules, both earned the hard way:
      * a markdown fence wins, because a fenced block is unambiguous even when the model
        also wrote prose the prompt asked it not to write;
      * outside a fence only a definition at COLUMN 0 counts. The prompt echoes the wanted
        signature indented by four spaces, and that echo is what a naive regex finds.
    """
    body = text
    if FENCE in body:
        parts = body.split(FENCE)
        if len(parts) >= 3:
            block = parts[1]
            nl = block.find("\n")
            if nl != -1 and block[:nl].strip().isalpha():
                block = block[nl + 1:]      # drop a language tag such as ```python
            body = block

    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("def "):
            if start is None:
                start = i
    if start is None:
        return None

    src = "\n".join(lines[start:])
    if ("def " + func_name + "(") not in src:
        return None
    # A definition at column 0 is required; an indented one is the prompt echo.
    if not any(l.startswith("def " + func_name + "(") for l in src.splitlines()):
        return None
    return src


PROMPT_ECHO_SAMPLE = """Sure! Here is the function.

Write a Python function with exactly this signature:

    def merge_intervals(intervals):

def merge_intervals(intervals):
    return sorted(intervals)
"""


# ---------------------------------------------------------------- checking

def normalise(value):
    """Tuples compare equal to lists. The prompts ask for lists; a model that answers with
    tuples of the right contents got the algorithm right, and this gate measures the
    algorithm, not the container type. Everything else is compared strictly."""
    if isinstance(value, tuple):
        return [normalise(v) for v in value]
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


def run_cases(task, fn):
    """Return None when every case passed, else the first failure as a string."""
    for name, args, want in task["cases"]:
        call_args = [normalise_arg(a) for a in args]
        before = json.dumps(call_args, default=repr)
        got = fn(*call_args)
        if normalise(got) != normalise(want):
            return "%s: got %r, want %r" % (name, got, want)
        if task["immutable"] and json.dumps(call_args, default=repr) != before:
            return "%s: input was modified" % name
    return None


def normalise_arg(arg):
    """Deep-copy list arguments so a mutating implementation is caught rather than
    corrupting the next case."""
    if isinstance(arg, list):
        return [normalise_arg(a) for a in arg]
    return arg


def load_function(path, func_name):
    spec = importlib.util.spec_from_file_location("gen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, func_name)


# ---------------------------------------------------------------- self-test

def selftest(verbose=True):
    """Prove both colours before any model is involved. Returns True when the harness is
    trustworthy; prints the reason and returns False when it is not."""
    failures = []

    for task in TASKS:
        # GREEN half: the reference must pass. Without it, ten rejections are equally
        # consistent with a checker that rejects everything.
        ref = REFERENCES.get(task["name"])
        if ref is None:
            failures.append("%s: no reference implementation" % task["name"])
        else:
            try:
                ref_err = run_cases(task, ref)
            except Exception as e:
                ref_err = "%s: %s" % (type(e).__name__, e)
            if ref_err is not None:
                failures.append("%s: the REFERENCE implementation failed: %s"
                                % (task["name"], ref_err))

        # RED half: the deliberately wrong implementation must fail.
        err = None
        try:
            err = run_cases(task, task["wrong"])
        except Exception as e:            # a wrong implementation may legitimately raise
            err = "%s: %s" % (type(e).__name__, e)
        if err is None:
            failures.append(
                "%s: the deliberately wrong implementation PASSED its cases" % task["name"]
            )
        elif verbose:
            print("  %-22s reference ok / wrong impl rejected: %s" % (task["name"], err))

    extracted = extract_function(PROMPT_ECHO_SAMPLE, "merge_intervals")
    if extracted is None:
        failures.append("extractor: found nothing in the prompt-echo sample")
    elif extracted.splitlines()[0] != "def merge_intervals(intervals):":
        failures.append("extractor: did not start at the column-0 definition")
    elif "Write a Python function" in extracted:
        failures.append("extractor: dragged the prompt echo into the source")
    elif verbose:
        print("  %-22s prompt echo skipped, column-0 definition taken" % "extractor")

    missing = [t["name"] for t in TASKS
               if not os.path.isfile(os.path.join(PROMPT_DIR, t["prompt"]))]
    if missing:
        failures.append("missing prompt files: %s" % ", ".join(missing))

    if failures:
        print("RESULT: CHECKER BROKEN")
        for f in failures:
            print("  - %s" % f)
        return False
    if verbose:
        print("RESULT: HARNESS OK - %d references pass, %d wrong implementations rejected, extractor tested"
              % (len(TASKS), len(TASKS)))
    return True


# ---------------------------------------------------------------- single check

def check_file(task_name, path):
    task = next((t for t in TASKS if t["name"] == task_name), None)
    if task is None:
        print("RESULT: DOES NOT RUN - unknown task %r" % task_name)
        return 2
    try:
        fn = load_function(path, task["func"])
    except Exception as e:
        print("RESULT: DOES NOT RUN - %s: %s" % (type(e).__name__, e))
        return 2
    try:
        err = run_cases(task, fn)
    except Exception as e:
        # Truncated code references names that were never bound. That is the model's
        # failure, not the harness's, and it must not surface as a traceback.
        print("RESULT: DOES NOT RUN - %s: %s" % (type(e).__name__, e))
        return 2
    if err:
        print("RESULT: RUNS BUT WRONG - %s" % err)
        return 1
    print("RESULT: RUNS AND CORRECT - %d cases" % len(task["cases"]))
    return 0


# ---------------------------------------------------------------- the run

def ask_model(url, prompt, max_tokens, timeout):
    payload = {
        "model": "crow",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "seed": 1234,
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body, time.time() - started


def run_suite(url, out_dir, max_tokens, timeout, only=None):
    if not selftest(verbose=True):
        return 3
    print()

    os.makedirs(out_dir, exist_ok=True)
    results = []

    tasks = TASKS if only is None else [t for t in TASKS if t["name"] in only]
    if not tasks:
        print("RESULT: no task matched %r - refusing to report a clean run over nothing."
              % (only,))
        return 2

    for task in tasks:
        prompt_path = os.path.join(PROMPT_DIR, task["prompt"])
        with open(prompt_path, "r", encoding="utf-8") as fh:
            prompt = fh.read()

        try:
            body, secs = ask_model(url, prompt, max_tokens, timeout)
        except Exception as e:
            print("  %-22s DOES NOT RUN - endpoint: %s" % (task["name"], e))
            results.append({"task": task["name"], "verdict": "DOES NOT RUN",
                            "reason": "endpoint: %s" % e})
            continue

        choice = body["choices"][0]
        finish = choice.get("finish_reason")
        content = (choice["message"].get("content") or "")
        reasoning = (choice["message"].get("reasoning_content") or "")
        usage = body.get("usage", {})

        with open(os.path.join(out_dir, task["name"] + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)

        # Truncation first. An empty content with finish_reason "length" means the budget
        # ran out inside the reasoning block - a harness fault, and it says so rather than
        # blaming the model.
        if finish == "length" and not content.strip():
            reason = ("truncated: finish_reason=length, content empty, "
                      "reasoning_content %d chars" % len(reasoning))
            print("  %-22s DOES NOT RUN - %s" % (task["name"], reason))
            results.append({"task": task["name"], "verdict": "DOES NOT RUN",
                            "reason": reason, "seconds": round(secs, 2),
                            "completion_tokens": usage.get("completion_tokens")})
            continue

        src = extract_function(content, task["func"])
        if src is None:
            reason = "no column-0 definition of %s in %d chars of content" % (
                task["func"], len(content))
            print("  %-22s DOES NOT RUN - %s" % (task["name"], reason))
            results.append({"task": task["name"], "verdict": "DOES NOT RUN",
                            "reason": reason, "seconds": round(secs, 2),
                            "completion_tokens": usage.get("completion_tokens")})
            continue

        gen_path = os.path.join(out_dir, task["name"] + ".py")
        with open(gen_path, "w", encoding="utf-8") as fh:
            fh.write(src)

        # The verdict is taken in a separate process. A generated file may call sys.exit,
        # spin, or import something that rewrites state this runner depends on; in-process
        # execution would let the prüfling decide the prüfer's fate.
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "check", task["name"], gen_path],
            capture_output=True, text=True, timeout=120,
        )
        verdict_line = (proc.stdout or proc.stderr or "").strip().splitlines()
        verdict_line = verdict_line[-1] if verdict_line else "RESULT: DOES NOT RUN - no output"
        if proc.returncode == 3:
            print("  %-22s %s" % (task["name"], verdict_line))
            print("\nCHECKER BROKEN - aborting")
            return 3

        verdict = ("RUNS AND CORRECT" if proc.returncode == 0 else
                   "RUNS BUT WRONG" if proc.returncode == 1 else "DOES NOT RUN")
        print("  %-22s %s  (%.1fs, %s tokens)" % (
            task["name"], verdict_line, secs, usage.get("completion_tokens")))
        results.append({"task": task["name"], "verdict": verdict,
                        "reason": verdict_line, "seconds": round(secs, 2),
                        "completion_tokens": usage.get("completion_tokens")})

    if not results:
        print("RESULT: no task produced a verdict - refusing to report a clean run "
              "over nothing.")
        return 2

    green = sum(1 for r in results if r["verdict"] == "RUNS AND CORRECT")
    print()
    print("RESULT: %d of %d correct" % (green, len(tasks)))
    for verdict in ("RUNS BUT WRONG", "DOES NOT RUN"):
        names = [r["task"] for r in results if r["verdict"] == verdict]
        if names:
            print("  %-16s %s" % (verdict + ":", ", ".join(names)))

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"total": len(tasks), "correct": green, "max_tokens": max_tokens,
                   "results": results}, fh, indent=2)

    return 0 if green == len(tasks) else 1


# ---------------------------------------------------------------- entry point

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="prove the harness can go red without any model")

    p_check = sub.add_parser("check", help="judge one generated file")
    p_check.add_argument("task")
    p_check.add_argument("path")

    p_run = sub.add_parser("run", help="drive the endpoint over all tasks")
    p_run.add_argument("--url", default="http://127.0.0.1:8081")
    p_run.add_argument("--out", required=True)
    # 4096, not 1024. Measured 2026-08-04 on the first reference run: completion tokens
    # ranged from 184 (rle-encode) to 2582 (two-sum) across ten tasks of comparable size -
    # a factor of 14, with the maximum on the EASIEST task. At 1024 two-sum truncated and
    # the gate reported a red it had caused itself. A budget too small does not measure the
    # model, it measures the budget; the four extra minutes are cheaper than a false red.
    p_run.add_argument("--max-tokens", type=int, default=4096,
                       help="must cover reasoning_content AND the answer")
    p_run.add_argument("--timeout", type=int, default=600)
    p_run.add_argument("--only", nargs="*", default=None,
                       help="run only these task names (a rerun, never a replacement)")

    args = ap.parse_args()

    if args.cmd == "selftest":
        return 0 if selftest() else 3
    if args.cmd == "check":
        return check_file(args.task, args.path)
    return run_suite(args.url, args.out, args.max_tokens, args.timeout, args.only)


if __name__ == "__main__":
    sys.exit(main())
