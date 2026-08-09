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
# THE FOUR VERDICTS PER TASK, and why the fourth is not a failure
#   RUNS AND CORRECT   - extracted, imported, all cases passed
#   RUNS BUT WRONG     - extracted and imported, a case failed
#   DOES NOT RUN       - no function extracted, or it raised at import or at call time
#   UNDECIDED          - the budget ran out before the model reached its answer
# Measured 2026-08-04: with a tight token budget the server returns finish_reason="length",
# an EMPTY content and a full reasoning_content. A harness reading content alone records
# that as "the model wrote nothing", which is indistinguishable from a real refusal. It is
# a harness fault, not a model fault.
#
# Until 2026-08-05 that case was a reason inside DOES NOT RUN - it was named, but it still
# counted as a failed task. Two runs of the SAME configuration then differed by two tasks
# (#46), and the two flips had different causes: top-k-frequent was a real model error
# (NameError on Counter), two-sum blew even the 4096 budget. Folding a budget artefact into
# the quality score mixes two measurements.
#
# THE ANSWER IS TO ASK AGAIN, NOT TO DROP THE TASK
# A cut-off turn carries an EMPTY content: there is no code, so right-or-wrong is not
# determinable from that turn at all. The only thing that settles it is a larger budget, so
# the runner asks again at twice the budget and judges the answer it gets. The denominator
# stays at ten. UNDECIDED is what remains when even the rerun is cut off - a state the
# harness admits to rather than a verdict it invents.
#
# THE DANGER IN THAT CHANGE, NAMED SO IT CANNOT BE FORGOTTEN
# A shrinking denominator makes any gate look better. Three rules keep it honest: the
# denominator printed is the number of tasks actually judged, any task still undecided is
# COUNTED AND NAMED, and a run with anything undecided cannot exit 0. The tasks that needed
# a rerun are named too - how often the reference budget was too small is a fact about the
# harness, and a silent rerun would hide it.
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
#   probe-suite.py compare <run-dir-a> <run-dir-b>
#
# Exit codes: 0 every task judged and correct, 1 at least one judged task not correct,
#             2 harness could not run, 3 CHECKER BROKEN,
#             4 every judged task correct BUT at least one task still undecided after its
#               rerun - an incomplete measurement, deliberately not 0, so a caller cannot
#               read it as all green.

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


# ---------------------------------------------------------------- verdicts

CORRECT = "RUNS AND CORRECT"
WRONG = "RUNS BUT WRONG"
DEAD = "DOES NOT RUN"
UNDECIDED = "UNDECIDED"

# A cut-off turn is asked again at a larger budget instead of leaving a hole in the
# denominator. ONE rerun, at TWICE the budget:
#   * two-sum is the only task observed to blow a budget, and its reasoning length for the
#     same prompt and seed has been seen at 2582, 3942 and >4096 tokens. Doubling is the
#     smallest step that clears that spread.
#   * it is also the most expensive task in the set (342 s at 3942 tokens), so every further
#     attempt is paid on the slowest turn. A second rerun would cost more than it decides.
# If the rerun is cut off too, the task stays UNDECIDED - the harness says it could not
# decide rather than inventing a verdict.
RERUN_ATTEMPTS = 1
RERUN_FACTOR = 2


def truncation_reason(finish_reason, content, reasoning_content):
    """Return a reason string when the turn was cut off before the answer, else None.

    The trigger is exactly the shape measured on 2026-08-04 and nothing wider:
    finish_reason "length" TOGETHER WITH an empty content. A cut-off turn that still
    carries a complete function is judged normally - it earned its verdict before the cap.
    Widening this to every finish_reason="length" would let the harness declare answers
    undecided that it could perfectly well judge, which is the same sin as scoring them.

    It is a separate function so it can be proven without a server; `selftest` does that.
    """
    if finish_reason == "length" and not (content or "").strip():
        return ("truncated: finish_reason=length, content empty, "
                "reasoning_content %d chars" % len(reasoning_content or ""))
    return None


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
    if spec is None or spec.loader is None:
        # Measured 2026-08-05: pointed at a stored answer with a .txt extension, this raised
        # "AttributeError: 'NoneType' object has no attribute 'loader'" - twelve times in a
        # row, which reads like twelve broken answers and was twelve broken invocations.
        # A raw transcript is not the input to this step; `run` writes the EXTRACTED source to
        # a .py file first, and a caller doing it by hand has to do the same.
        raise ImportError(
            "%r has no Python loader (extension %r). Extract the function first and pass a "
            ".py file - `run` does this with extract_function()."
            % (os.path.basename(path), os.path.splitext(path)[1] or "none"))
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
        print("RESULT: %s - unknown task %r" % (DEAD, task_name))
        return 2
    try:
        fn = load_function(path, task["func"])
    except Exception as e:
        print("RESULT: %s - %s: %s" % (DEAD, type(e).__name__, e))
        return 2
    try:
        err = run_cases(task, fn)
    except Exception as e:
        # Code that references names which were never bound fails here. That is the model's
        # failure, not the harness's, and it must not surface as a traceback.
        print("RESULT: %s - %s: %s" % (DEAD, type(e).__name__, e))
        return 2
    if err:
        print("RESULT: %s - %s" % (WRONG, err))
        return 1
    print("RESULT: %s - %d cases" % (CORRECT, len(task["cases"])))
    return 0


# ---------------------------------------------------------------- the run

def ask_model(url, prompt, max_tokens, timeout, temperature=0.6):
    # 0.6 and not 0, changed 2026-08-09 after greedy decoding produced sixteen answers in a row
    # with finish_reason "length" and an EMPTY content field: 8,192 tokens of reasoning and no
    # reply, under the model's own chat template (--jinja). The CLI has defaulted to 0.6 since it
    # was written, for this exact reason, and the README states it. The suite kept 0 only because
    # the switch did not exist.
    #
    # WHY THE OLD DEFAULT IS NOT WORTH KEEPING: a measurement whose subject never answers measures
    # nothing, however reproducibly it does so. Reproducing an old series is still possible - pass
    # --temperature 0 - but that is now the deliberate act, not the accident.
    #
    # WHAT IT COSTS, and it is real: above 0 the runs are no longer byte-identical, so the
    # determinism half of this gate (five of ten tasks reproduced exactly at temperature 0) does
    # not carry over, and a k/N taken here may NOT be compared with one taken at 0. Different
    # measurement, not a better one.
    payload = {
        "model": "crow",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
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


def run_suite(url, out_dir, max_tokens, timeout, only=None, temperature=0.6):
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

        # A cut-off turn is not a hole in the denominator - it is a turn that was not asked
        # properly. It is asked again at a larger budget, and only a SECOND cut-off leaves
        # the task undecided. Attempt 0 is the reference budget, attempt 1 the rerun.
        budget = max_tokens
        outcome = None
        for attempt in range(RERUN_ATTEMPTS + 1):
            try:
                body, secs = ask_model(url, prompt, budget, timeout, temperature)
            except Exception as e:
                print("  %-22s %s - endpoint: %s" % (task["name"], DEAD, e))
                outcome = {"task": task["name"], "verdict": DEAD,
                           "reason": "endpoint: %s" % e, "max_tokens": budget}
                break

            choice = body["choices"][0]
            finish = choice.get("finish_reason")
            content = (choice["message"].get("content") or "")
            reasoning = (choice["message"].get("reasoning_content") or "")
            usage = body.get("usage", {})
            base = {"task": task["name"], "seconds": round(secs, 2),
                    "completion_tokens": usage.get("completion_tokens"),
                    "max_tokens": budget, "attempt": attempt + 1}

            # The discarded transcript is kept under its own name. A rerun that overwrote
            # the truncated one would erase the evidence that the budget was ever too small.
            suffix = "" if attempt == 0 else "-rerun"
            with open(os.path.join(out_dir, task["name"] + suffix + ".json"), "w",
                      encoding="utf-8") as fh:
                json.dump(body, fh, indent=2)

            reason = truncation_reason(finish, content, reasoning)
            if reason is not None:
                if attempt < RERUN_ATTEMPTS:
                    budget *= RERUN_FACTOR
                    print("  %-22s cut off at %s tokens (%s) - asking again at %d"
                          % (task["name"], base["max_tokens"], reason.split(",")[0],
                             budget))
                    continue
                # Even the larger budget did not reach an answer. Say so; do not score it.
                print("  %-22s %s - %s (also at %d tokens)"
                      % (task["name"], UNDECIDED, reason, budget))
                outcome = dict(base, verdict=UNDECIDED, reason=reason)
                break

            src = extract_function(content, task["func"])
            if src is None:
                reason = "no column-0 definition of %s in %d chars of content" % (
                    task["func"], len(content))
                print("  %-22s %s - %s" % (task["name"], DEAD, reason))
                outcome = dict(base, verdict=DEAD, reason=reason)
                break

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
            verdict_line = verdict_line[-1] if verdict_line else "RESULT: %s - no output" % DEAD
            if proc.returncode == 3:
                print("  %-22s %s" % (task["name"], verdict_line))
                print("\nCHECKER BROKEN - aborting")
                return 3

            verdict = (CORRECT if proc.returncode == 0 else
                       WRONG if proc.returncode == 1 else DEAD)
            note = "" if attempt == 0 else "  [rerun at %d tokens]" % budget
            print("  %-22s %s  (%.1fs, %s tokens)%s" % (
                task["name"], verdict_line, secs, usage.get("completion_tokens"), note))
            outcome = dict(base, verdict=verdict, reason=verdict_line)
            break

        results.append(outcome)

    if not results:
        print("RESULT: no task produced a verdict - refusing to report a clean run "
              "over nothing.")
        return 2

    green = sum(1 for r in results if r["verdict"] == CORRECT)
    undecided = [r["task"] for r in results if r["verdict"] == UNDECIDED]
    judged = len(tasks) - len(undecided)

    print()
    if judged == 0:
        # A zero without a denominator is not a statement.
        print("RESULT: NO TASK WAS JUDGED - all %d turns were cut off by the budget."
              % len(tasks))
    else:
        # The denominator is the number of tasks actually judged, and the undecided ones are
        # named on every run. A denominator that shrinks quietly is how a gate starts looking
        # better than it is.
        print("RESULT: %d of %d judged correct, %d undecided (of %d tasks)"
              % (green, judged, len(undecided), len(tasks)))
    for verdict in (WRONG, DEAD, UNDECIDED):
        names = [r["task"] for r in results if r["verdict"] == verdict]
        if names:
            print("  %-16s %s" % (verdict + ":", ", ".join(names)))
    # How often the reference budget was too small is a number about the HARNESS, not the
    # model, and it stays visible. A silent rerun would hide that the budget needs raising.
    reran = [r["task"] for r in results if r.get("attempt", 1) > 1]
    if reran:
        print("  %-16s %s (at %d tokens)"
              % ("rerun:", ", ".join(reran), max_tokens * RERUN_FACTOR))
    if undecided:
        print("  Still undecided after the rerun - not scored, and not counted as failed.")

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"total": len(tasks), "judged": judged, "correct": green,
                   "undecided": len(undecided), "undecided_tasks": undecided,
                   "reran_tasks": reran, "max_tokens": max_tokens,
                   "rerun_max_tokens": max_tokens * RERUN_FACTOR,
                   "results": results}, fh, indent=2)

    # Exit 0 means every task was judged AND correct. Anything undecided is an incomplete
    # measurement and gets its own code, so no caller can read it as all green.
    if judged == 0:
        return 4
    if green != judged:
        return 1
    return 4 if undecided else 0


# ---------------------------------------------------------------- comparison

def compare_runs(dir_a, dir_b):
    """Compare two runs task by task.

    THE POINT IS THE VERDICT COLUMN, NOT THE BYTES. Two runs of the same configuration
    may generate different code and still agree on every verdict; that is the property
    this gate needs, and it is weaker than determinism. Reporting only byte-equality
    would condemn a gate that works, and reporting only verdicts would hide that the
    model is not reproducible at all. Both columns are printed for that reason.

    A task undecided in either run is left out of the stability statement and counted
    separately. It was never judged there, so it can neither agree nor disagree - and the
    count is printed every time, because it is the part of the comparison that was not made.
    """
    def load(d):
        with open(os.path.join(d, "summary.json"), "r", encoding="utf-8") as fh:
            return {r["task"]: r for r in json.load(fh)["results"]}

    try:
        a, b = load(dir_a), load(dir_b)
    except Exception as e:
        print("RESULT: cannot compare - %s" % e)
        return 2

    shared = [t["name"] for t in TASKS if t["name"] in a and t["name"] in b]
    if not shared:
        print("RESULT: no task appears in both runs - refusing to compare nothing.")
        return 2

    print("  %-22s %-18s %-18s %-11s %s" % ("task", "A", "B", "verdict", "bytes"))
    same_verdict = same_bytes = 0
    comparable, undecided = [], []
    for name in shared:
        va, vb = a[name]["verdict"], b[name]["verdict"]

        # A task undecided in either run was never judged there, so it can neither agree
        # nor disagree. Counting it as a flip would blame the model for the budget;
        # counting it as agreement would invent a stability that was not measured.
        if UNDECIDED in (va, vb):
            undecided.append(name)
            print("  %-22s %-18s %-18s %-11s %s" % (name, va, vb, "undecided", "-"))
            continue

        comparable.append(name)
        agree = va == vb
        same_verdict += agree
        pa = os.path.join(dir_a, name + ".py")
        pb = os.path.join(dir_b, name + ".py")
        if os.path.isfile(pa) and os.path.isfile(pb):
            with open(pa, "rb") as fh1, open(pb, "rb") as fh2:
                identical = fh1.read() == fh2.read()
        else:
            identical = False
        same_bytes += identical
        print("  %-22s %-18s %-18s %-11s %s" % (
            name, va, vb, "same" if agree else "CHANGED",
            "identical" if identical else "differ"))

    n = len(comparable)
    print()
    if n == 0:
        print("RESULT: NOTHING COMPARABLE - all %d shared tasks were undecided in at "
              "least one run." % len(shared))
        print("  Rerun with a larger budget before comparing anything.")
        return 2

    print("RESULT: %d of %d verdicts unchanged, %d of %d outputs byte-identical"
          % (same_verdict, n, same_bytes, n))
    if undecided:
        # Printed every time, because this number is the part of the comparison that was
        # not made. Silence here would read as a clean sweep over ten tasks.
        print("  Outside the statement: %d of %d task(s) undecided in at least one run "
              "(%s)." % (len(undecided), len(shared), ", ".join(undecided)))
    if same_verdict == n:
        print("  The gate is verdict-stable over the tasks it judged in both runs. That is")
        print("  what a lever comparison needs; byte-equality is not required.")
    else:
        changed = [t for t in comparable if a[t]["verdict"] != b[t]["verdict"]]
        print("  NOT verdict-stable: %s" % ", ".join(changed))
        print("  A one-task difference between configurations cannot be attributed to")
        print("  the configuration while the gate moves on its own.")
    return 0 if same_verdict == n else 1


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
    p_run.add_argument("--temperature", type=float, default=0.6,
                       help="sampling temperature; 0.6 is the CLI's operating point. "
                            "0 is byte-reproducible but loops in the reasoning block under --jinja")
    p_run.add_argument("--only", nargs="*", default=None,
                       help="run only these task names (a rerun, never a replacement)")

    p_cmp = sub.add_parser("compare", help="two run directories: verdict stability")
    p_cmp.add_argument("dir_a")
    p_cmp.add_argument("dir_b")

    args = ap.parse_args()

    if args.cmd == "selftest":
        return 0 if selftest() else 3
    if args.cmd == "check":
        return check_file(args.task, args.path)
    if args.cmd == "compare":
        return compare_runs(args.dir_a, args.dir_b)
    return run_suite(args.url, args.out, args.max_tokens, args.timeout, args.only, args.temperature)


if __name__ == "__main__":
    sys.exit(main())
