"""Why a speculative method reached 100 % acceptance: keep the answers, not just the counters.

WHY THIS EXISTS
Measured 2026-08-05 (#29): with --spec-type ngram-mod the same prompt, same seed and same
operating point produced 10.22, 12.17, 12.67, 25.67 and 27.12 tok/s over five consecutive
requests. The throughput ordering follows the per-run acceptance rate without exception, and
the two fastest runs accepted EVERY drafted token with a mean accepted length of 58.5 and 59.0
tokens - roughly two verification steps for 128 generated tokens.

That is the point at which a speed number stops being interpretable on its own. An n-gram
method drafts by looking up repetitions of its context, so near-total acceptance has two very
different possible causes:

  * the answer was largely REPEATED from something already in the context - an earlier answer,
    or the prompt itself. Then the 2x is a cache effect for recurring output, or worse, a
    degenerate answer that is fast because it says nothing new;
  * the answer was genuinely produced and simply predictable. Then it is a real lever.

The counters cannot tell those apart. The answer texts can, so this probe keeps them.

WHAT IT RECORDS PER REQUEST
position, throughput, drafted and accepted tokens, acceptance rate, finish reason, completion
tokens, the full answer text, its sha256, and the prompt hash. Mean accepted length lives only
in the server log (server-context.cpp:670) and is folded in from there when --server-log is
given, matched by order of appearance.

WHAT IT COMPARES
Each answer against every earlier one, on three levels, because they fail differently:
  1. byte equality - the whole answer appeared before;
  2. longest common run of characters with any earlier answer - large passages reused;
  3. longest repeated run WITHIN the answer, and the overlap with the prompt - degeneration and
     prompt mirroring, which need no earlier answer at all.

WHAT IT DOES NOT DO
It does not judge whether the code is correct. That is what #46 is for, and mixing the two would
let a speed probe issue quality verdicts it has not earned. The full texts are written out so
the judgement is made on the text, not on a score this tool invented.

Usage:
  probe-spec-drafting.py --url http://127.0.0.1:8081 --out runs/<date>/spec-mod-text \\
      [--repeats 6] [--tokens 128] [--server-log <path>]
Exit 0 = every request answered.
"""

import argparse
import difflib
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request

# The prompt is the one measure-slot-scaling.ps1 sends at one slot (index 0). Changing it would
# measure a different workload than the run this probe exists to explain.
PROMPT = "Write a Python function that reverses a linked list. Code only."


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def classify(answer, reasoning, finish_reason):
    """Five classes, because a throughput number over each of them means something different.

    Measured 2026-08-05: at max_tokens 128 this prompt produced six consecutive
    `reasoning-only` runs - a real generation rate over work that never reached an answer.
    Reading that as answer throughput is what nearly turned a repeated thinking sequence into
    a 2x lever on #29.
    """
    if answer.strip():
        return "truncated-answer" if finish_reason == "length" else "complete-answer"
    if reasoning.strip():
        return "reasoning-only"
    return "empty-generation"


def longest_common(a, b):
    """Length and text of the longest run of characters present in both strings."""
    if not a or not b:
        return 0, ""
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size, a[m.a:m.a + m.size]


def longest_self_repeat(text):
    """Longest run of characters that occurs at least twice inside one text.

    A loop or a copied block shows up here without needing any earlier answer to compare
    against. Binary search on the length over a set of substrings - the texts are short.
    """
    n = len(text)
    if n < 8:
        return 0, ""
    lo, hi, best = 1, n // 2, ""
    while lo <= hi:
        mid = (lo + hi) // 2
        seen, found = set(), ""
        for i in range(n - mid + 1):
            s = text[i:i + mid]
            if s in seen:
                found = s
                break
            seen.add(s)
        if found:
            best, lo = found, mid + 1
        else:
            hi = mid - 1
    return len(best), best


def ask(url, prompt, max_tokens, timeout):
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


def read_mean_lengths(path):
    """Pull 'mean len' out of the server log, in order of appearance.

    The server prints one such line per request that drafted anything. It is matched by order,
    not by id, so a log carrying requests from an earlier probe would shift the alignment - the
    caller is expected to point at a log written for this run.
    """
    if not path or not os.path.isfile(path):
        return []
    raw = open(path, "rb").read()
    # Decode with errors="replace", never strictly. A server log at -lv 5 carries raw
    # token bytes, and one invalid sequence made strict utf-8 AND utf-16 both fail, which
    # returned [] and dropped every mean-len value without a word. Measured 2026-08-05:
    # byte 0xef at offset 7339 of a 1,938,549-byte log, 0 of 3 values extracted while the
    # lines were plainly present. The pattern is ASCII; a mangled byte elsewhere in the
    # file is no reason to abandon it.
    text = raw.decode("utf-8", errors="replace")
    values = [float(m) for m in re.findall(r"mean len\s*=\s*([0-9.]+)", text)]
    if not values and re.search(r"draft acceptance\s*=", text):
        # Acceptance lines are there but no mean len parsed - say so instead of
        # returning an empty list that reads like "the server never drafted".
        print("  NOTE: server log has 'draft acceptance' lines but no parsable 'mean len'.")
    return values


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8081")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--tokens", type=int, default=128)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--server-log", default="")
    ap.add_argument("--label", default="", help="recorded verbatim, e.g. the --spec-type in use")
    ap.add_argument("--prompt-files", nargs="*", default=None,
                    help="run each of these once, in order, instead of repeating one prompt. "
                         "This separates 'the same request again' from 'a new task'.")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    runs = []

    if args.prompt_files:
        prompts = []
        for p in args.prompt_files:
            with io.open(p, encoding="utf-8") as fh:
                prompts.append((os.path.splitext(os.path.basename(p))[0], fh.read()))
        # A shared instruction frame is context every prompt carries, so a method that drafts
        # from repetitions could feed on it. It does not appear in a function definition, but
        # the number belongs in the record rather than in an assumption.
        head = os.path.commonprefix([t for _, t in prompts])
        tail = os.path.commonprefix([t[::-1] for _, t in prompts])[::-1]
        print("%d distinct prompts, shared frame: %d chars leading, %d trailing"
              % (len(prompts), len(head), len(tail)))
    else:
        prompts = [("repeat", PROMPT)] * args.repeats

    print("max_tokens %d, %s" % (args.tokens, args.url))
    print()

    for i, (name, prompt) in enumerate(prompts, start=1):
        try:
            body, secs = ask(args.url, prompt, args.tokens, args.timeout)
        except Exception as e:
            print("  run %d FAILED: %s" % (i, e))
            return 2
        choice = body["choices"][0]
        text = choice["message"].get("content") or ""
        reasoning = choice["message"].get("reasoning_content") or ""
        usage = body.get("usage", {})
        timings = body.get("timings", {}) or {}
        drafted = int(timings.get("draft_n") or 0)
        accepted = int(timings.get("draft_n_accepted") or 0)
        completion = usage.get("completion_tokens")

        with io.open(os.path.join(args.out, "run-%d.txt" % i), "w", encoding="utf-8") as fh:
            fh.write(text)

        runs.append({
            "position": i,
            "task": name,
            "label": args.label,
            "seconds": round(secs, 2),
            "tok_per_s": round((completion or 0) / secs, 2) if secs > 0 else None,
            "completion_tokens": completion,
            "finish_reason": choice.get("finish_reason"),
            "drafted": drafted,
            "accepted": accepted,
            "accept_rate": round(accepted / drafted, 4) if drafted else None,
            "answer_sha": sha(text),
            "answer_chars": len(text),
            "reasoning_chars": len(reasoning),
            "prompt_sha": sha(prompt),
            "budget": args.tokens,
            "class": classify(text, reasoning, choice.get("finish_reason")),
        })
        acc = ("%5.1f%%" % (100.0 * accepted / drafted)) if drafted else " no draft"
        print("  %-22s %6.2f tok/s  %4s tokens  draft %4d/%-4d %s  %-16s %4d chars  sha %s"
              % (name, runs[-1]["tok_per_s"] or 0, completion, accepted, drafted, acc,
                 runs[-1]["class"], len(text), runs[-1]["answer_sha"]))

    # Mean accepted length, folded in from the server log where available.
    means = read_mean_lengths(args.server_log)
    drafting = [r for r in runs if r["drafted"] > 0]
    if means and len(means) == len(drafting):
        for r, m in zip(drafting, means):
            r["mean_accepted_len"] = m
    elif means:
        print("\n  NOTE: %d 'mean len' lines for %d drafting runs - not aligned, left out."
              % (len(means), len(drafting)))

    # ---------------------------------------------------------------- comparisons
    texts = []
    for i, r in enumerate(runs):
        with io.open(os.path.join(args.out, "run-%d.txt" % r["position"]), encoding="utf-8") as fh:
            texts.append(fh.read())

    print()
    print("Against everything that came before:")
    print("  %-22s %-10s %-9s %-24s %s" % ("task", "identical", "self-rep", "longest reuse", "vs prompt"))
    for i, r in enumerate(runs):
        earlier = texts[:i]
        ident = next((j + 1 for j, t in enumerate(earlier) if t == texts[i]), None)
        best_len, best_j = 0, None
        for j, t in enumerate(earlier):
            n, _ = longest_common(texts[i], t)
            if n > best_len:
                best_len, best_j = n, j + 1
        self_len, _ = longest_self_repeat(texts[i])
        prompt_len, _ = longest_common(texts[i], prompts[i][1])
        r["identical_to"] = ident
        r["longest_reuse_chars"] = best_len
        r["longest_reuse_from"] = best_j
        r["longest_self_repeat_chars"] = self_len
        r["longest_prompt_overlap_chars"] = prompt_len
        # Against EACH earlier answer, not only the best match: three sources of reuse fail
        # differently, and a single "best" number cannot say whether the overlap comes from
        # one particular earlier answer or from all of them alike.
        r["overlap_with_each_previous"] = [longest_common(texts[i], t)[0] for t in earlier]
        r["byte_equal_to_previous"] = bool(earlier) and texts[i] == earlier[-1]
        share = (100.0 * best_len / len(texts[i])) if texts[i] else 0.0
        print("  %-22s %-10s %-9s %-24s %s" % (
            r["task"],
            ("run %d" % ident) if ident else "-",
            "%d ch" % self_len,
            ("%d ch (%.0f%%) from run %s" % (best_len, share, best_j)) if best_j else "-",
            "%d ch" % prompt_len))

    with io.open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"tasks": [n for n, _ in prompts], "url": args.url,
                   "max_tokens": args.tokens, "runs": runs}, fh, indent=2, ensure_ascii=False)

    print()
    print("Answers and summary.json: %s" % args.out)
    print("The texts decide this, not the counters - read them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
