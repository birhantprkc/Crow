# Flash-Next Decode: der beste Stand nach der Nacht

**Kurz: `-ncmoe 30 -b 2048 -ub 2048` ist ein echter Gewinn -- +16,8 % Decode,
+34,8 % Prefill, 24 % weniger Wanduhr je Zug. Verifiziert, drei Laeufe je Arm.
Nicht 3x, aber auslieferbar.**

Alle Zahlen aus derselben Reihe, gleicher Prompt (33.494 Token), 200 Token Ausgabe,
ein Boot je Lauf, kalter Cache. Roh: `runs/one-runs.jsonl`, ein Log je Lauf.

## Die Empfehlung

```powershell
llama-server.exe `
  -m ...\Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf `
  --port 8083 -c 200000 `
  -b 2048 -ub 2048 `                <-- war 4096
  -ctk q8_0 -ctv q8_0 `
  -ncmoe 30 `                       <-- war 40
  --fit off --load-mode none -np 1 --jinja `
  --mmproj ...\mmproj-F16.gguf
```

Zwei geaenderte Werte. Drei Laeufe je Arm, verschraenkt, ein Boot je Lauf:

| | Betriebspunkt | `-ncmoe 30 -ub 2048` | |
|---|---|---|---|
| Decode | 35,74 (35,13-36,11) | **41,76 (40,34-42,76)** | **+16,8 %**, keine Ueberlappung |
| Prefill | 539,98 | **727,65** | **+34,8 %** |
| Wanduhr je Zug | 67,9 s | **51,3 s** | **-24,4 %** |

**Alles besser, nichts schlechter.**

## Die Konfiguration, die NICHT auslieferbar ist

`-ncmoe 24 -ub 1024` hat den hoechsten Decode und ist trotzdem ein Verlust:

| | Betriebspunkt | `-ncmoe 24 -ub 1024` |
|---|---|---|
| Decode | 34,73 | **46,59** (+34,2 %) |
| Prefill | 547,71 | **208,48** (-62 %) |
| Wanduhr je Zug | 67,2 s | **165,1 s** (2,5x langsamer) |

Der Prefill frisst den Decodegewinn und mehr. Ein einzelner frueherer Lauf zeigte
dort Prefill 616 und 58,7 s Wanduhr -- ein Ausreisser, einer von vier. Genau die
Sorte Zahl, wegen der eine Reihe gefahren wird und nicht ein Lauf.

## Warum es wirkt

Der Ubatch belegt VRAM, den Expertenschichten tragen könnten. Bei `-ub 4096`
passen nur 8 der 48 Expertenschichten auf die Karte; bei `-ub 1024` sind es 24.
Jede Schicht, die von der CPU auf die GPU wandert, nimmt **10 × 1,7883 MiB =
17,9 MiB je Token** vom RAM-Bus und legt sie auf VRAM mit ~20× der Bandbreite.

Das Manifest kannte beide Hälften einzeln und beide waren dort negativ bewertet:
`-ub 2048` „frees 6.3 GiB and wins nothing" (gemessen bei `-ncmoe 40`, wo VRAM
nicht die Grenze war) und `-ncmoe 32` „dies 2/2". **Zusammen ergeben sie den
Gewinn, den keines von beiden allein zeigt.**

## Der ganze abgesuchte Raum

Wanduhr je Zug ist das Mass, nicht Decode allein: ein Agent prefillt in jeder
Runde neu.

| `-ncmoe` | `-b/-ub` | Decode | Prefill | **Wanduhr** | |
|---|---|---|---|---|---|
| 40 | 4096 | 35,74 | 539,98 | 67,9 s | Betriebspunkt, 3 Laeufe |
| 40 | 4096 | 34,41 | 613,16 | 60,6 s | Einzellauf |
| 36 | 4096 | 37,48 | 302,06 | 116,4 s | |
| 34 | 4096 | 38,67 | 227,86 | 152,4 s | |
| 30 | 3072 | 35,59 | 360,24 | 98,8 s | |
| 32 | 2048 | 38,58 | 617,57 | 59,6 s | |
| **30** | **2048** | **41,76** | **727,65** | **51,3 s** | **Optimum, 3 Laeufe** |
| 28 | 2048 | 43,50 | 360,97 | 97,5 s | Kante -- scharf |
| 26 | 2048 | 45,46 | 167,88 | 204,0 s | |
| 24 | 2048 | 40,62 | 309,94 | 113,0 s | |
| 30 | 1536 | 40,37 | 664,42 | 55,5 s | |
| 24 | 1024 | 46,59 | 208,48 | 165,1 s | hoechster Decode, 3 Laeufe |
| 22 | 1024 | 33,23 | 119,59 | 286,1 s | WDDM-Spill |
| 20 | 1024 | — | ~27 | — | Spill |
| 20 | 512 | — | ~27 | — | Spill -- Ubatch hilft nicht mehr |
| 18 | 1024, KV q4_0 | — | ~25 | — | Spill -- KV-Cache auch nicht |

**Von beiden Seiten bestaetigt.** 32 ist schlechter, 28 ist schlechter, ub 1536
und ub 3072 sind schlechter. Der Punkt ist ein Optimum und kein Zufall.

**Unter `-ncmoe 24` ist Schluss**, an den Expertengewichten selbst: 24 Schichten
auf der GPU sind ~22 GiB. Weder ein kleinerer Ubatch noch ein q4-KV-Cache
verschieben diese Grenze.

## Spekulation: der Mechanismus läuft, er trägt hier nur nicht

| Typ | Entwürfe | akzeptiert | Decode |
|---|---|---|---|
| ohne | — | — | **46,59** |
| `ngram-mod`, Vorgaben | **0** | — | 34,71 |
| `ngram-mod`, `n-min 4 n-max 16` | 16 | 0 | 43,16 |
| `ngram-simple` | 0 | — | 45,94 |
| `ngram-map-k`, `n 8 m 12 h 1` | 24 | **23 (96 %)** | 46,51 |
| `ngram-map-k`, `n 6 m 16 h 2` | 48 | 26 (54 %) | 43,04 |
| `ngram-map-k`, `n 4 m 24 h 1` | 378 | 29 (7,7 %) | **21,06** |

Drei Befunde:

1. **`ngram_mod.n_min` steht per Vorgabe auf 48** — es ist eine MINDESTLÄNGE des
   Entwurfs, kein Startwert. Es schlägt nur vor, wenn es 48 Token am Stück
   findet, und das passiert praktisch nie. Deshalb null Entwürfe in zwei ganzen
   Messreihen.
2. **`ngram-map-k` mit `size-n 8` erreicht 96 % Annahme** — der Mechanismus
   funktioniert. Er feuert nur 24 Mal in 200 Token.
3. **Jeder `--spec-type` kostet Prefill** (~205 statt ~548). Achtung: die beste
   Platzierung kostet ihn ohnehin schon -- die beiden Ursachen sind hier nicht
   getrennt und muessen es noch werden.

Der Grund, warum es nicht mehr trägt, ist derselbe, den die OpenVINO-Leute für
ihre NPU gefunden haben: **Spekulation zahlt proportional dazu, wie teuer ein
Zieltoken ist.** Unseres kostet jetzt 21,5 ms. Da ist wenig zu amortisieren.

## Zwei weitere Befunde

**`-fa on` aendert nichts.** Decode 46,86 gegen 46,59, Prefill 206 gegen 208 --
Flash Attention laeuft ohnehin, die Vorgabe ist `auto`. Der Schalter ist kein
Hebel, er ist eine Bestaetigung.

**`--spec-type ngram-cache` mit `--lookup-cache-dynamic` STUERZT AB**, und zwar
reproduzierbar, wenn die Datei noch nicht existiert:

```
E spec common_specu: failed to open dynamic lookup cache: <pfad>
Prozess endet mit 0xC0000409 (STACK_BUFFER_OVERRUN / fast-fail)
```

Eine Datei, die noch nicht da ist, sollte angelegt werden -- oder der Start
sollte mit einer Meldung abbrechen. Stattdessen faellt der Prozess hart. Das ist
ein Fehler in llama.cpp und dieselbe Klasse Beitrag wie die zwei Defekte im
PR #27992: reproduzierbar, mit einem Einzeiler ausloesbar.

## Was NICHT geht, gemessen

- **Unter `-ncmoe 24`** — WDDM-Spill, Prefill fällt auf ~27 tok/s.
- **Kleinerer Ubatch als 1024** löst den Spill nicht; die Grenze sind die
  Gewichte, nicht der Puffer.
- **KV-Cache auf q4_0** verschiebt die Grenze ebenfalls nicht.
- **NPU als Drafter** — 1.037,7 ms fest je Aufruf, unabhängig bestätigt durch
  openvino Discussion #36484 (0,55–0,74× Durchsatz).

## Offen, fuer den Morgen

Das Ziel war **3x = ~107 tok/s**. Erreicht: **41,76 tok/s Decode bei 24 % weniger
Wanduhr**, also **1,17x** auf dem Decode und **1,32x** auf der Zugdauer.

**Der Flaggenraum ist abgesucht.** Platzierung, Ubatch, KV-Typ, Flash Attention
und alle fuenf Spekulationsarten sind gemessen; das Optimum ist von beiden Seiten
bestaetigt. Ein weiterer Lauf in diesem Raum waere Rauschen, keine Suche.

Was bleibt, ist keine Konfiguration mehr:

1. **Die ~16,7 ms des festen Terms (#159).** Von 30,74 ms je Token sind hoechstens
   8,4 ms Datentransport. Der Rest ist Rechnung, Kernel-Starts und 84 Graph-Splits
   je Token. Das ist der groesste benannte Hebel und er liegt in ggml.
2. **Der Prefill-Einbruch unter jeder Spekulation** (~205 statt ~548) ist nicht
   erklaert und nicht von der Platzierung getrennt. Er entscheidet, ob
   Spekulation fuer einen Agenten je in Frage kommt.
3. **`ngram-map-k` bei 96 % Annahme** feuert zu selten. Warum es nur 24 Mal in
   200 Token trifft, obwohl die Aufgabe woertlich abschreibt, ist offen.
4. **Der `ngram-cache`-Absturz** ist ein Upstream-Bug mit Einzeiler-Reproduktion.

## Was das Manifest lernen muesste

`_needs` fuer `flash-next-q2-k-xl` traegt heute `-b 4096 -ub 4096 -ncmoe 40`.
Zwei Notizen darin sind einzeln richtig und zusammen falsch:

- `-ub 2048` "frees 6.3 GiB of VRAM and wins nothing" -- gemessen bei `-ncmoe 40`,
  wo VRAM nicht die Grenze war.
- `-ncmoe 32` "dies 2/2" -- gemessen bei `-ub 4096`, wo es sie ist.

Der Ubatch haelt VRAM, den Expertenschichten tragen koennen. Wer nur eine Haelfte
misst, sieht in beiden Faellen nichts.
