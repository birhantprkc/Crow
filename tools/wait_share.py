"""Wie viel eines Laufs ist Warten auf die Platte?

Liest eine counters-CSV von sample-counters.ps1 und rechnet aus, was #39 fragt.

Die Zahl, die zaehlt, ist NICHT der Durchsatz. Es ist die Warteschlangentiefe:
disk_read_queue sagt, wie viele Anfragen die Platte gleichzeitig bearbeitet. Liegt
sie bei ~1, wird seriell gefragt und jede Anfrage wartet auf die vorige - genau der
Zustand, den ein Batch-Fetch aufloesen wuerde. Liegt sie hoch, ist die Platte schon
ausgelastet und ein Umbau bringt nichts.

NEGATIVKONTROLLE: dieselbe Rechnung ueber eine Leerlauf-CSV muss nahe null
Fault-Verkehr und eine Warteschlange nahe null ergeben. Tut sie das nicht, misst
das Skript nicht den Lauf, sondern Rauschen - dann gilt keine Zahl daraus.

Aufruf:  wait_share.py <lauf.csv> [<leerlauf.csv>]
Exit 0 = ausgewertet. Exit 2 = Setup-Fehler. Exit 1 = Negativkontrolle gehalten nicht.
"""
import csv
import os
import sys


def load(path):
    if not os.path.exists(path):
        print(f"SETUP ERROR: no such file: {path}")
        sys.exit(2)
    # utf-8-sig, nicht utf-8: sample-counters.ps1 schreibt ein BOM, und damit heisst
    # die ERSTE Spalte "﻿t_s". Nur sie ist betroffen, alle anderen lesen sich
    # normal - der Fehler sieht deshalb aus wie "Zeitspanne 0 s" und nicht wie ein
    # Lesefehler. Gemessen 2026-08-03.
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    if not rows:
        print(f"SETUP ERROR: no rows in {path}")
        sys.exit(2)
    return rows


def num(row, key):
    v = (row.get(key) or "").strip()
    if v in ("", "NaN", "nan"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def summarise(rows, label):
    ok = [r for r in rows if (r.get("status") or "").strip() == "ok"]
    other = len(rows) - len(ok)

    t = [num(r, "t_s") for r in ok]
    t = [x for x in t if x is not None]
    span = (max(t) - min(t)) if len(t) > 1 else 0.0

    fault = [num(r, "fault_mb_s") for r in ok]
    fault = [x for x in fault if x is not None]
    disk = [num(r, "disk_read_mb_s") for r in ok]
    disk = [x for x in disk if x is not None]
    queue = [num(r, "disk_read_queue") for r in ok]
    queue = [x for x in queue if x is not None]
    blk = [num(r, "bytes_per_fault_read_kb") for r in ok]
    blk = [x for x in blk if x is not None]

    print(f"=== {label} ===")
    print(f"  Zeilen gesamt      {len(rows)}   davon status=ok {len(ok)}, anders {other}")
    print(f"  Zeitspanne         {span:.1f} s")

    if not fault:
        print("  KEINE fault_mb_s-Werte - nichts auswertbar")
        return None

    # Wie viele Sekunden lief ueberhaupt Plattenverkehr ueber Seitenfehler?
    busy = [x for x in fault if x > 1.0]
    share = 100.0 * len(busy) / len(fault)
    total_gb = sum(fault) / 1024.0  # 1 s Takt -> MB/s == MB je Zeile

    # Median vor Mittel: die Ladephase enthaelt Ausreisser bis 23.700 MB/s, die als
    # physikalisch unmoeglich zurueckgezogen sind. Sie ziehen jedes Mittel hoch.
    print(f"  Fault-Durchsatz    Median {sorted(fault)[len(fault)//2]:8.2f} MB/s"
          f"   Mittel {sum(fault)/len(fault):8.2f}   Max {max(fault):8.2f}")
    print(f"  Zeilen mit >1 MB/s {len(busy)} von {len(fault)}  = {share:.1f} % der Messzeit")
    print(f"  Summe ueber Faults {total_gb:.1f} GB gelesen (1-s-Takt angenommen)")
    if disk:
        print(f"  Platte (Treiber)   Mittel {sum(disk)/len(disk):8.2f} MB/s"
              f"   Max {max(disk):8.2f}")
    if blk:
        print(f"  je Leseoperation   Mittel {sum(blk)/len(blk):8.2f} KB")
    if queue:
        q = sorted(queue)
        print(f"  WARTESCHLANGE      Mittel {sum(queue)/len(queue):8.3f}"
              f"   Median {q[len(q)//2]:8.3f}   Max {max(queue):8.3f}")
        print(f"                     Zeilen mit Tiefe < 2: "
              f"{sum(1 for x in queue if x < 2)} von {len(queue)}")
    print()
    return {"share": share, "fault_mean": sum(fault) / len(fault),
            "queue_mean": (sum(queue) / len(queue)) if queue else None}


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2

    run = summarise(load(argv[1]), f"LAUF  {os.path.basename(argv[1])}")
    if run is None:
        return 2

    if len(argv) < 3:
        print("KEINE NEGATIVKONTROLLE angegeben - die Zahlen oben stehen ohne Gegenprobe.")
        print("Ein Sampler, der nur Zahlen druckt, ist von einem, der Rauschen druckt,")
        print("nicht zu unterscheiden. Zweites Argument: eine Leerlauf-CSV.")
        return 0

    idle = summarise(load(argv[2]), f"LEERLAUF  {os.path.basename(argv[2])}")
    if idle is None:
        return 2

    print("=== Negativkontrolle ===")
    ok = True
    if idle["fault_mean"] >= 10.0:
        print(f"  ROT: Leerlauf zeigt {idle['fault_mean']:.2f} MB/s Fault-Verkehr, erwartet < 10")
        ok = False
    else:
        print(f"  gruen: Leerlauf {idle['fault_mean']:.2f} MB/s < 10")
    if idle["share"] >= 20.0:
        print(f"  ROT: Leerlauf ist zu {idle['share']:.1f} % beschaeftigt, erwartet < 20")
        ok = False
    else:
        print(f"  gruen: Leerlauf {idle['share']:.1f} % beschaeftigt < 20")
    if run["fault_mean"] <= idle["fault_mean"] * 5:
        print(f"  ROT: Lauf ({run['fault_mean']:.2f}) hebt sich nicht vom Leerlauf "
              f"({idle['fault_mean']:.2f}) ab")
        ok = False
    else:
        print(f"  gruen: Lauf {run['fault_mean']:.2f} MB/s hebt sich klar vom Leerlauf ab")

    print()
    if ok:
        print("RESULT: PASS - die Negativkontrolle haelt, die Zahlen des Laufs gelten.")
        return 0
    print("RESULT: FAIL - die Negativkontrolle haelt nicht. Keine Zahl oben gilt.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
