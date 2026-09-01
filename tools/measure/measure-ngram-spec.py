"""#182: bringt n-gram-Spekulation ohne Draftmodell Decode auf Flash-Next?

DIE REGELN, NACH DENEN DIESE REIHE GEBAUT IST (serving-aenderung-messen):

  - EINE VARIABLE je Lauf. Die Serverzeile ist die des Manifests, unveraendert,
    plus GENAU ein `--spec-type`. Der Kontrollarm ist dieselbe Zeile ohne ihn.
  - VERSCHRAENKT, nicht nacheinander. Diese Maschine driftete an einem Tag um
    9,4 % -- mehr als jeder gesuchte Effekt. A/B/A/B, ein Boot je Lauf.
  - EIN PROMPT, GROSS GENUG. Unter ein paar tausend Token misst man Overhead und
    nennt es Prefill. Hier ~32k, dieselbe Zeichenkette in jedem Lauf.
  - KALTER CACHE je Lauf, weil ein Boot ihn ohnehin leert.
  - EIN LOG JE LAUF. Ein Prozess, der ohne Log stirbt, hinterlaesst nichts.
  - `/health ok` heisst NICHT antwortbereit -- der erste Zug zahlt den Prefill,
    und genau der ist die Messung.

Rohzeilen nach `runs/<stamp>-ngram/`, eine JSON je Lauf plus eine CSV.
"""
import json, os, re, subprocess, sys, time, urllib.request, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "cli"))
import crow_core as core

PORT = 8083
BASE = "http://127.0.0.1:%d" % PORT
ARMS = ["control", "ngram-mod"]          # eine Variable: der Typ
# `ngram_mod.n_match` steht per Vorgabe auf 24: es verlangt eine
# 24-Token-Uebereinstimmung, bevor es etwas vorschlaegt. Fuer eine Aufgabe, die
# abschreibt, ist ein kuerzeres Fenster die ehrlichere Probe.
EXTRA = ["--spec-ngram-mod-n-match", "8"]
ROUNDS = 3                                # A/B/A/B/A/B
PROMPT_TOKENS = 32000
MAX_NEW = 200

# DIE AUFGABE SCHREIBT AB, und das ist der Punkt: ein Agentenzug zitiert staendig
# aus seinem Kontext -- gelesene Dateien, Werkzeugausgaben, Code mit einer
# kleinen Aenderung. Genau dort kann ein n-gram-Entwurf treffen. Eine Aufgabe,
# die NEUEN Text erzeugt, kann er grundsaetzlich nicht vorhersagen -- der erste
# Anlauf fragte nach einer Zusammenfassung und bekam null Entwurfstoken.
TASK = (chr(10) * 2 + "Reproduce the function `def _rooted(` from the code "
        "above, verbatim, exactly as it appears, including its docstring. "
        "Output only the code.")


def wait_ready(proc, timeout=420):
    """Antwortbereit heisst: /health ok UND ein Zug ist durchgekommen."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return "died with exit %s" % proc.returncode
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
                if json.loads(r.read()).get("status") == "ok":
                    return None
        except Exception:
            pass
        time.sleep(2)
    return "not ready after %d s" % timeout


def big_prompt():
    """Derselbe Text in jedem Lauf. Aus Crows eigener Quelle, weil ein
    wiederholter Lorem-Block ein n-gram-Verfahren geschenkt bekaeme -- echter
    Code hat Wiederholung, aber keine triviale."""
    src = os.path.join(os.path.dirname(os.path.dirname(HERE)), "cli", "crow_core.py")
    text = open(src, encoding="utf-8", errors="replace").read()
    # ~4 Zeichen je Token, grob, danach am Server gegengezaehlt
    return text[: PROMPT_TOKENS * 4]


def one_run(arm, outdir, idx):
    argv = core.server_command("flash-next-q2-k-xl")
    if arm != "control":
        argv = argv + ["--spec-type", arm] + EXTRA
    log = os.path.join(outdir, "%02d-%s.log" % (idx, arm))
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(log, "w", encoding="utf-8") as sink:
        proc = subprocess.Popen(argv, stdout=sink, stderr=subprocess.STDOUT,
                                creationflags=flags)
        try:
            bad = wait_ready(proc)
            if bad:
                return dict(arm=arm, idx=idx, error=bad, log=log)
            body = json.dumps({
                "model": "crow",
                "messages": [{"role": "user",
                              "content": big_prompt() + TASK}],
                "max_tokens": MAX_NEW, "temperature": 1.0, "top_p": 0.95,
                "stream": False,
            }).encode("utf-8")
            req = urllib.request.Request(BASE + "/v1/chat/completions", body,
                                         {"Content-Type": "application/json"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=1800) as r:
                doc = json.loads(r.read())
            wall = time.time() - t0
            tm = doc.get("timings") or {}
            return dict(arm=arm, idx=idx, wall_s=round(wall, 2),
                        prompt_n=tm.get("prompt_n"),
                        prompt_tps=tm.get("prompt_per_second"),
                        decode_n=tm.get("predicted_n"),
                        decode_tps=tm.get("predicted_per_second"),
                        draft_n=tm.get("draft_n"), accepted_n=tm.get("draft_n_accepted"),
                        log=log)
        finally:
            proc.kill()
            proc.wait(timeout=30)
            time.sleep(5)


def main():
    # DIE AUSGABE IST DEUTSCH UND NICHT cp1252-SAUBER. `text=True` faellt darauf
    # herein und liefert None -- die Prozesszaehlung waere damit still ausgefallen,
    # und genau die ist die harte Vorbedingung dieser Reihe.
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/NH"],
                             capture_output=True).stdout.decode("utf-8", "replace")
    if "llama-server" in running:
        print("ABBRUCH: es laeuft schon ein llama-server. Eine Zahl daneben "
              "beschreibt das Paar, nicht die Aenderung.")
        return 2
    stamp = time.strftime("%Y-%m-%d-%H%M")
    outdir = os.path.join(HERE, "runs", stamp + "-ngram")
    os.makedirs(outdir, exist_ok=True)
    rows, i = [], 0
    for _ in range(ROUNDS):
        for arm in ARMS:
            i += 1
            r = one_run(arm, outdir, i)
            rows.append(r)
            print(json.dumps(r), flush=True)
            with open(os.path.join(outdir, "rows.json"), "w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=1)
    print()
    for arm in ARMS:
        ok = [r for r in rows if r["arm"] == arm and "error" not in r]
        if not ok:
            print("%-10s kein gueltiger Lauf" % arm); continue
        d = [r["decode_tps"] for r in ok if r.get("decode_tps")]
        p = [r["prompt_tps"] for r in ok if r.get("prompt_tps")]
        print("%-10s n=%d  decode %.2f tok/s (%.2f-%.2f)  prefill %.2f"
              % (arm, len(ok), statistics.mean(d), min(d), max(d), statistics.mean(p)))
    print("\nRoh:", outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
