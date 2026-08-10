"""Does replaying `reasoning_content` hold the prompt cache? The prefill number decides.

Three modes, in the order they were needed on 2026-08-08 (#60):

  pair     one turn 1, then two turn-2 requests that differ ONLY in the
           reasoning field. The tightest comparison: same cache, same history.
  runs     two runs, four fresh tasks, one per arm. Nothing shared but the
           server, so a result cannot be an artefact of one task or one order.
  session  ten turns per arm, no token budget. Covers what the two above
           cannot: does the cost grow with the session, does the model still
           recall turn 1 at turn 10, and does replaying thoughts start a loop.

WHY THE `tools` ARRAY IS NOT OPTIONAL HERE. This model's chat template keeps a
past turn's thoughts only when the request carries tools; without them it
renders `<think></think>` and both arms come out byte for byte identical
(measured via /apply-template: 132 characters either way, against 1197 vs 1215
with tools). A run without tools measures nothing at all.

WHY THE FIRST VERSION OF `pair` WAS WORTHLESS, kept here as a warning: turn 1
generated 20 tokens, so the tail behind the divergence point was a dozen tokens
and BOTH arms looked cached. The signal scales with what turn 1 produced, so
turn 1 has to be substantial or the negative control cannot go red.

Read-only against a running server. Costs decode time and nothing else.

  python tools/probe-prefix-cache.py pair    --url http://127.0.0.1:8081
  python tools/probe-prefix-cache.py runs    --url http://127.0.0.1:8081
  python tools/probe-prefix-cache.py session --url http://127.0.0.1:8081
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SYSTEM = ("You are Crow, a local coding assistant. "
          "Always reply in the same language the user wrote in.")

# The CLI's default, read from the manifest rather than repeated here. 0.0 is the
# greedy attractor that made the model loop inside its reasoning block on
# 2026-08-07; a probe at 0.0 would measure that instead of the cache. The reason
# travels with the value in manifests/operating-point.json -- this comment used to
# be the only copy of it outside cli/crow.py, and the other four probes had the
# number without it.
import crow_manifest

TEMPERATURE = crow_manifest.sampling("temperature")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file from disk and return its contents.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    },
}]

MARK_A = "ZINNOBER-7"
MARK_B = "4711"


def ask(url: str, messages: list[dict], max_tokens: int | None = None) -> dict:
    """One non-streaming turn. Returns the fields the probe reasons about."""
    body = {"model": "crow", "messages": messages, "tools": TOOLS,
            "temperature": TEMPERATURE, "stream": False}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=3600) as response:
        out = json.loads(response.read().decode("utf-8"))
    wall = time.monotonic() - started
    choice = out["choices"][0]
    message = choice["message"]
    timings = out.get("timings", {})
    return {
        "content": message.get("content") or "",
        "reasoning": message.get("reasoning_content") or "",
        "tool_calls": message.get("tool_calls"),
        "finish": choice.get("finish_reason"),
        "prompt_n": timings.get("prompt_n"),
        "prompt_ms": timings.get("prompt_ms"),
        "predicted_n": timings.get("predicted_n"),
        "wall": round(wall, 2),
    }


def assistant_message(reply: dict, replay: bool) -> dict:
    """The history entry for a finished turn -- the one thing the arms differ in."""
    message = {"role": "assistant", "content": reply["content"]}
    if replay and reply["reasoning"]:
        message["reasoning_content"] = reply["reasoning"]
    return message


def shingles(text: str, size: int = 40, step: int = 8) -> set[str]:
    text = " ".join(text.split())
    return {text[i:i + size] for i in range(0, max(0, len(text) - size), step)}


def overlap(previous: str, current: str) -> float:
    """How much of the previous answer reappears in this one, in percent.

    The loop robin saw repeated itself rather than progressing, so repetition
    is measured rather than eyeballed. Zero means every turn said something new.
    """
    before, after = shingles(previous), shingles(current)
    return 100.0 * len(before & after) / len(before) if before else 0.0


def ratio(prompt_n: int | None, generated: int | None) -> float:
    """prompt_n of turn 2 against what turn 1 produced.

    Near 0 = the cache held. Near 1 = the whole of turn 1 was read again. It is
    a ratio and not a raw count because arms with different tasks generate
    different amounts, and the raw numbers are then not comparable.
    """
    if not generated:
        return float("nan")
    return (prompt_n or 0) / generated


# ---------------------------------------------------------------- pair

PAIR_TURN1 = ("Explain how merge sort works, step by step, with a complete Python "
              "implementation and a worked example on the list [5, 2, 9, 1, 7]. "
              "Be thorough.")
PAIR_TURN2 = "Now do the same for insertion sort, but keep it brief."


def mode_pair(url: str) -> int:
    print("=" * 74)
    print("Turn 1 -- large enough that the tail behind the break is visible")
    print("=" * 74, flush=True)
    base = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": PAIR_TURN1}]
    first = ask(url, base, max_tokens=3000)
    print(f"  prompt_n={first['prompt_n']}  predicted_n={first['predicted_n']}  "
          f"finish={first['finish']}  wall={first['wall']}s")
    print(f"  reasoning {len(first['reasoning'])} chars | "
          f"content {len(first['content'])} chars", flush=True)
    if not first["reasoning"]:
        print("\nABORT: turn 1 carried no reasoning_content -- nothing to measure.")
        return 1

    with_reasoning = base + [assistant_message(first, True),
                             {"role": "user", "content": PAIR_TURN2}]
    without = base + [assistant_message(first, False),
                      {"role": "user", "content": PAIR_TURN2}]

    # Arm B first, against the pristine cache. Arm A still measures the break
    # afterwards: the slot's cached head is unchanged, and both requests share
    # the prefix only up to where turn 1's <think> opened.
    arm_b = ask(url, with_reasoning, max_tokens=1200)
    arm_a = ask(url, without, max_tokens=1200)

    print("\n" + "=" * 74)
    print("RESULT")
    print("=" * 74)
    print(f"  turn 1 generated       {first['predicted_n']} tokens")
    print(f"  arm B  reasoning back  prompt_n = {arm_b['prompt_n']:<6} "
          f"({arm_b['prompt_ms']} ms prefill)")
    print(f"  arm A  reasoning kept  prompt_n = {arm_a['prompt_n']:<6} "
          f"({arm_a['prompt_ms']} ms prefill)")
    print("\n--- repetition check, arm B turn 2 ---")
    print(repr(arm_b["reasoning"][:300]))
    print(repr(arm_b["content"][:300]))
    return 0


# ---------------------------------------------------------------- runs

RUNS = [
    ("run 1", [
        ("A drop", False,
         "Explain how a hash map handles collisions, with a Python implementation "
         "using separate chaining and a worked example. Be thorough.",
         "Now explain open addressing instead, briefly."),
        ("B back", True,
         "Explain how binary search works, with a Python implementation, the loop "
         "invariant, and a worked example on a 9-element list. Be thorough.",
         "Now show the variant that finds the first index where a predicate flips, briefly."),
    ]),
    ("run 2", [
        ("A drop", False,
         "Explain how to reverse a linked list, both iteratively and recursively, "
         "in Python, with a worked example. Be thorough.",
         "Now do the same for reversing only the first k nodes, briefly."),
        ("B back", True,
         "Explain how Lomuto partitioning works in quicksort, with a Python "
         "implementation and a worked example on [8, 3, 5, 1, 9, 2]. Be thorough.",
         "Now do the same for Hoare partitioning, briefly."),
    ]),
]


def mode_runs(url: str) -> int:
    rows = []
    for run_name, arms in RUNS:
        for arm_name, replay, turn1, turn2 in arms:
            print(f"\n{'=' * 74}\n{run_name} / arm {arm_name}\n{'=' * 74}", flush=True)
            base = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": turn1}]
            first = ask(url, base, max_tokens=1400)
            print(f"  turn 1: prompt_n={first['prompt_n']} "
                  f"predicted_n={first['predicted_n']} finish={first['finish']}", flush=True)
            history = base + [assistant_message(first, replay),
                              {"role": "user", "content": turn2}]
            second = ask(url, history, max_tokens=500)
            r = ratio(second["prompt_n"], first["predicted_n"])
            print(f"  turn 2: prompt_n={second['prompt_n']} "
                  f"({second['prompt_ms']} ms)  ratio={r:.3f}", flush=True)
            rows.append((run_name, arm_name, replay, first["predicted_n"],
                         second["prompt_n"], second["prompt_ms"], r))

    print("\n" + "=" * 74)
    print("RESULT")
    print("=" * 74)
    print(f"  {'run':<7} {'arm':<8} {'replay':<7} {'gen t1':>7} {'prompt_n t2':>12} "
          f"{'prefill s':>10} {'ratio':>7}")
    for run_name, arm_name, replay, gen, pn, ms, r in rows:
        print(f"  {run_name:<7} {arm_name:<8} {str(replay):<7} {gen:>7} {pn:>12} "
              f"{ms / 1000.0:>10.1f} {r:>7.3f}")
    return 0


# ---------------------------------------------------------------- session

SESSION_TURNS = [
    f"Remember two things for the rest of this conversation: the project code name "
    f"is {MARK_A} and the check number is {MARK_B}. Confirm briefly, then tell me in "
    f"two sentences what a hash map is good for.",
    "What is the difference between a list and a tuple in Python? Briefly.",
    "When is quicksort worse than mergesort? Two sentences.",
    "What does the Python decorator @staticmethod do? Briefly.",
    "Why is (lo + hi) // 2 dangerous in some languages during a binary search? Briefly.",
    "What is the difference between a process and a thread? Two sentences.",
    "What is a Bloom filter good for, and what can it not do? Briefly.",
    "What does amortised complexity mean, using a dynamic array as the example? Briefly.",
    "Why is comparing floats with == risky? Two sentences.",
    "Last question: which project code name and which check number did you memorise "
    "from my first message? Just the two values.",
]


def mode_session(url: str) -> int:
    results = {}
    # Arm B first: arm A pays minutes of prefill in the later turns.
    for arm_name, replay in (("B back", True), ("A drop", False)):
        print(f"\n{'=' * 78}\nARM {arm_name} "
              f"(reasoning_content {'REPLAYED' if replay else 'DROPPED'})\n{'=' * 78}",
              flush=True)
        history = [{"role": "system", "content": SYSTEM}]
        turns = []
        for index, prompt in enumerate(SESSION_TURNS, start=1):
            history.append({"role": "user", "content": prompt})
            # No max_tokens on purpose: a turn cut off at a budget looks from
            # the outside exactly like the symptom being tested for.
            reply = ask(url, history)
            history.append(assistant_message(reply, replay))
            reply["overlap"] = round(
                overlap(turns[-1]["content"], reply["content"]) if turns else 0.0, 1)
            turns.append(reply)
            flag = "" if reply["finish"] == "stop" else f"  <<< finish={reply['finish']}"
            call = "  <<< TOOL_CALL" if reply["tool_calls"] else ""
            print(f"  turn {index:>2}: prompt_n={reply['prompt_n']:>6} "
                  f"({reply['prompt_ms'] / 1000:>7.1f}s)  gen={reply['predicted_n']:>5}  "
                  f"overlap={reply['overlap']:>5.1f}%{flag}{call}", flush=True)
        results[arm_name] = turns

    print("\n" + "=" * 78)
    print("ACCEPTANCE")
    print("=" * 78)
    for arm_name, turns in results.items():
        last = turns[-1]["content"]
        counts = [t["prompt_n"] for t in turns]
        print(f"\n  arm {arm_name}")
        print(f"    prompt_n per turn      : {counts}")
        print(f"    total prefill          : {sum(t['prompt_ms'] for t in turns) / 1000:.1f} s")
        print(f"    every turn finish=stop : {all(t['finish'] == 'stop' for t in turns)}")
        print(f"    max overlap            : {max(t['overlap'] for t in turns):.1f} %")
        print(f"    {MARK_A} recalled in turn 10 : {MARK_A in last}")
        print(f"    {MARK_B} recalled in turn 10      : {MARK_B in last}")
    return 0


MODES = {"pair": mode_pair, "runs": mode_runs, "session": mode_session}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=sorted(MODES))
    parser.add_argument("--url", default="http://127.0.0.1:8081",
                        help="server root, without /v1 (default: %(default)s)")
    args = parser.parse_args(argv)
    return MODES[args.mode](args.url)


if __name__ == "__main__":
    sys.exit(main())
