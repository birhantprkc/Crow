"""Can a slot's KV state be written to the SSD and restored cheaper than re-computing it?

#67 measured the symptom: after a 15-minute pause the server re-read a 23,400-token context, about
35 minutes of prefill. Raising `--cache-ram` only moves where the volatile copy lives -- it is still
lost on restart. `--slot-save-path` writes the state to disk instead, and this probe asks whether
that is worth doing.

Four numbers, in the order they decide things (#68):

  1. bytes per token of saved state -- extrapolates to any session size
  2. restore time against the re-prefill it replaces
  3. whether a restored slot actually HOLDS the prefix, read off `prompt_n` and not off the
     HTTP status, because a restore that succeeds and re-reads everything looks identical in a log
  4. what repeated saving costs, since this is the only place Crow writes to the SSD rather
     than reading it

WHY IT MEASURES SMALL AND EXTRAPOLATES. Building a 24k context costs ~35 minutes of prefill before
the interesting part begins. KV state is linear in tokens, so bytes/token taken at 2k answers the
size question for 24k at a twentieth of the wall clock. What is NOT extrapolated is the prefix
question -- that is a yes/no and is asked directly.

The server must be started with `--slot-save-path <dir>`; without it every action returns
"This server does not support slots action" and the probe stops rather than reporting zeros.

  python tools/probe-slot-persistence.py --url http://127.0.0.1:8081 --dir <slot-save-path>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SYSTEM = ("You are Crow, a local coding assistant. "
          "Always reply in the same language the user wrote in.")

TEMPERATURE = 0.6

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

SLOT_FILE = "probe-slot-persistence.bin"


def post(url: str, path: str, body: dict | None, timeout: float = 1800.0) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url: str, path: str, timeout: float = 30.0):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ask(url: str, messages: list[dict], max_tokens: int | None = None) -> dict:
    """One non-streaming turn. Returns only the fields this probe reasons about."""
    body = {"model": "crow", "messages": messages, "tools": TOOLS,
            "temperature": TEMPERATURE, "stream": False}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    started = time.monotonic()
    out = post(url, "/v1/chat/completions", body)
    timings = out.get("timings") or {}
    choice = (out.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {
        "content": msg.get("content") or "",
        "reasoning": msg.get("reasoning_content") or "",
        "prompt_n": timings.get("prompt_n"),
        "predicted_n": timings.get("predicted_n"),
        "prompt_ms": timings.get("prompt_ms") or 0.0,
        "wall_s": round(time.monotonic() - started, 2),
    }


def assistant_message(reply: dict) -> dict:
    """#60: a kept turn carries its reasoning, or the template renders an empty
    think block and the prefix diverges where the thoughts began."""
    msg = {"role": "assistant", "content": reply["content"]}
    if reply["reasoning"]:
        msg["reasoning_content"] = reply["reasoning"]
    return msg


def slot_action(url: str, action: str, filename: str) -> tuple[bool, dict | str, float]:
    """save/restore. Returns (ok, payload_or_error, seconds)."""
    started = time.monotonic()
    try:
        out = post(url, f"/slots/0?action={action}", {"filename": filename})
        return True, out, round(time.monotonic() - started, 3)
    except urllib.error.HTTPError as exc:
        return False, exc.read().decode("utf-8", "replace")[:300], round(time.monotonic() - started, 3)


def prefix_held(held: int, reread: int) -> bool:
    """Did the restore replace the prefill, or only claim to?

    A restore returns HTTP 200 and an `n_restored` count whether or not the
    state it wrote is usable, so the verdict has to come from the NEXT turn's
    `prompt_n`. Measured 2026-08-08: a working restore re-read 18 of 2,517
    tokens; a broken one (probe sent no `reasoning_content`) re-read 2,487 of
    2,650. The gap is three orders of magnitude, so the threshold is not
    delicate -- but zero is the wrong test, because the new user turn is always
    re-read and always non-zero.
    """
    if held <= 0:
        return False
    return reread < held * 0.2


def size_model(points: list[tuple[int, int]]) -> tuple[float, float]:
    """(fixed_bytes, bytes_per_token) from (tokens, filesize) pairs.

    Two points, not one. Extrapolating the first measurement linearly gave
    5 GiB for a 200k context; the second point revealed a ~17 MiB fixed part
    and put the real figure at ~1.3 GiB. A single point cannot see a constant.
    """
    if len(points) < 2:
        raise ValueError("need at least two points -- one cannot separate the fixed part")
    (t1, s1), (t2, s2) = min(points), max(points)
    if t2 == t1:
        raise ValueError("two points at the same context size say nothing")
    per_token = (s2 - s1) / (t2 - t1)
    return s1 - t1 * per_token, per_token


def context_tokens(url: str) -> int | None:
    """How many tokens the slot currently holds, straight from the server."""
    try:
        slots = get(url, "/slots")
    except Exception:
        return None
    if not slots:
        return None
    return slots[0].get("n_prompt_tokens")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://127.0.0.1:8081")
    p.add_argument("--dir", required=True,
                   help="the server's --slot-save-path, so the probe can size the file")
    p.add_argument("--turns", type=int, default=3,
                   help="turns used to build the context before saving")
    p.add_argument("--max-tokens", type=int, default=400,
                   help="cap per building turn -- this probe measures persistence, not answers")
    p.add_argument("--saves", type=int, default=3,
                   help="repeated saves, for the write-cost question")
    args = p.parse_args(argv)

    print(f"probe-slot-persistence against {args.url}")
    print(f"slot dir: {args.dir}\n")

    # --- does this server support the action at all -------------------------
    ok, payload, _ = slot_action(args.url, "save", SLOT_FILE)
    if not ok and "does not support" in str(payload):
        print("FAIL  the server was not started with --slot-save-path")
        print(f"      {payload}")
        return 2

    # --- build a context ----------------------------------------------------
    prompts = [
        "In one paragraph: what is a hash map good for?",
        "And when is a B-tree the better choice? One paragraph.",
        "What does 'amortised' mean, using a dynamic array? One paragraph.",
        "Why is comparing floats with == risky? One paragraph.",
        "What is a Bloom filter good for? One paragraph.",
    ]
    history = [{"role": "system", "content": SYSTEM}]
    print("building context")
    for i in range(args.turns):
        prompt = prompts[i % len(prompts)]
        history.append({"role": "user", "content": prompt})
        r = ask(args.url, history, max_tokens=args.max_tokens)
        history.append(assistant_message(r))
        print(f"  turn {i+1}: prompt_n={r['prompt_n']:>6}  gen={r['predicted_n']:>5}  {r['wall_s']:>7.1f}s")

    held = context_tokens(args.url)
    print(f"\ncontext now: {held} tokens\n")

    # --- 1 + 4: save, size, repeated write cost -----------------------------
    print("saving")
    save_times = []
    for i in range(args.saves):
        ok, payload, secs = slot_action(args.url, "save", SLOT_FILE)
        if not ok:
            print(f"  FAIL  {payload}")
            return 1
        save_times.append(secs)
        print(f"  save {i+1}: {secs:>7.3f}s")

    path = os.path.join(args.dir, SLOT_FILE)
    size = os.path.getsize(path) if os.path.exists(path) else None
    if size is None:
        print(f"  FAIL  no file at {path} -- the server may write elsewhere")
        return 1

    per_token = size / held if held else 0.0
    print(f"\n  file      : {size:,} bytes ({size / 1024 / 1024:.1f} MiB)")
    print(f"  per token : {per_token:,.0f} bytes")
    print(f"  → 24k ctx : {per_token * 24000 / 1024 / 1024:,.0f} MiB")
    print(f"  → 200k ctx: {per_token * 200000 / 1024 / 1024 / 1024:,.1f} GiB")

    # --- 2 + 3: restore, and does the prefix survive ------------------------
    # The KV is dropped first, so the restore has something to prove. Without
    # this the slot still holds the state and a no-op would look like a success.
    print("\ndropping the slot's state, then restoring")
    try:
        post(args.url, "/slots/0?action=erase", {})
    except Exception:
        # Not every build has erase; a throwaway request with a different prefix
        # evicts the state just as well.
        ask(args.url, [{"role": "system", "content": "unrelated"},
                       {"role": "user", "content": "hi"}], max_tokens=1)

    ok, payload, restore_s = slot_action(args.url, "restore", SLOT_FILE)
    if not ok:
        print(f"  FAIL  {payload}")
        return 1
    print(f"  restore: {restore_s:.3f}s  {json.dumps(payload)[:160]}")

    # THE number. A restore that reports success and still re-reads everything
    # is indistinguishable from a working one until this is read.
    history.append({"role": "user", "content": "One more: what is a trie good for? One paragraph."})
    after = ask(args.url, history, max_tokens=args.max_tokens)
    print(f"\n  next turn: prompt_n={after['prompt_n']}  ({after['prompt_ms']/1000:.1f}s prefill)")

    reused = held - (after["prompt_n"] or 0)
    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    print(f"  context held            : {held} tokens")
    print(f"  re-read after restore   : {after['prompt_n']} tokens")
    print(f"  reused                  : {reused} tokens")
    print(f"  restore time            : {restore_s:.3f}s")
    print(f"  save time (median of {len(save_times)}): {sorted(save_times)[len(save_times)//2]:.3f}s")
    print(f"  state size              : {size / 1024 / 1024:.1f} MiB for {held} tokens")
    if prefix_held(held or 0, after["prompt_n"] or 0):
        print("\n  PREFIX HELD -- the restore replaced the prefill.")
    else:
        print("\n  PREFIX DID NOT HOLD -- the restore returned success and the")
        print("  server re-read the context anyway. This is the failure mode #68 names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
