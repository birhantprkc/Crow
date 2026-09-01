"""Ein Lauf, eine Konfiguration, eine Zeile Ergebnis.

robins Regel vom 2026-09-01: erst EIN Lauf, lesen, und nur wenn er etwas zeigt,
die Reihe. Sechs Boots zu je 2,4 min, um etwas zu lernen, das ein einzelner Lauf
gesagt haette, sind fuenfzehn verlorene Minuten.

    python try-one.py --label ncmoe36 --set ncmoe=36
    python try-one.py --label spec-simple --add "--spec-type ngram-simple"

Jede Zeile landet in `runs/one-runs.jsonl`, damit die Reihe hinterher aus den
Einzellaeufen besteht und nicht aus dem Gedaechtnis.
"""
import argparse, json, os, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "cli"))
import crow_core as core

PORT = 8083
BASE = "http://127.0.0.1:%d" % PORT
LEDGER = os.path.join(HERE, "runs", "one-runs.jsonl")

TASK = (chr(10) * 2 + "Reproduce the function `def _rooted(` from the code above, "
        "verbatim, exactly as it appears, including its docstring. "
        "Output only the code.")


def prompt(n_chars=128000):
    src = os.path.join(os.path.dirname(os.path.dirname(HERE)), "cli", "crow_core.py")
    return open(src, encoding="utf-8", errors="replace").read()[:n_chars]


def build(sets, adds):
    argv = core.server_command("flash-next-q2-k-xl")
    for kv in sets:
        k, _, v = kv.partition("=")
        flag = "-" + k if len(k) <= 6 else "--" + k
        if flag in argv:
            argv[argv.index(flag) + 1] = v
        else:
            argv += [flag, v]
    for a in adds:
        argv += a.split()
    return argv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--add", action="append", default=[])
    ap.add_argument("--max-new", type=int, default=200)
    a = ap.parse_args()

    busy = subprocess.run(["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/NH"],
                          capture_output=True).stdout.decode("utf-8", "replace")
    if "llama-server" in busy:
        print(json.dumps(dict(label=a.label, error="a llama-server is already running")))
        return 2

    argv = build(a.set, a.add)
    os.makedirs(os.path.join(HERE, "runs"), exist_ok=True)
    log = os.path.join(HERE, "runs", "one-%s.log" % a.label)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    row = dict(label=a.label, at=time.strftime("%H:%M"),
               extra=[x for x in argv if x not in core.server_command("flash-next-q2-k-xl")])
    with open(log, "w", encoding="utf-8") as sink:
        p = subprocess.Popen(argv, stdout=sink, stderr=subprocess.STDOUT, creationflags=flags)
        try:
            t0 = time.time()
            while time.time() - t0 < 480:
                if p.poll() is not None:
                    row["error"] = "died with exit %s" % p.returncode
                    break
                try:
                    with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
                        if json.loads(r.read()).get("status") == "ok":
                            break
                except Exception:
                    pass
                time.sleep(2)
            else:
                row["error"] = "not ready after 480 s"
            if "error" not in row:
                row["boot_s"] = round(time.time() - t0, 1)
                body = json.dumps({
                    "model": "crow",
                    "messages": [{"role": "user", "content": prompt() + TASK}],
                    "max_tokens": a.max_new, "temperature": 1.0, "top_p": 0.95,
                }).encode("utf-8")
                req = urllib.request.Request(BASE + "/v1/chat/completions", body,
                                             {"Content-Type": "application/json"})
                t1 = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=1800) as r:
                        doc = json.loads(r.read())
                    tm = doc.get("timings") or {}
                    row.update(wall_s=round(time.time() - t1, 1),
                               prompt_n=tm.get("prompt_n"),
                               prefill=round(tm.get("prompt_per_second") or 0, 2),
                               decode=round(tm.get("predicted_per_second") or 0, 2),
                               decode_n=tm.get("predicted_n"),
                               draft_n=tm.get("draft_n"),
                               accepted=tm.get("draft_n_accepted"))
                except Exception as exc:
                    row["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        finally:
            p.kill()
            p.wait(timeout=30)
            time.sleep(4)
    row["log"] = os.path.basename(log)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
