"""Render Crow's measurement plots to white-background PNGs.

Every series here is measured on one machine -- RTX 5090, 63.4 GB RAM, one NVMe --
and carries the issue it was measured on. Nothing is estimated, interpolated or
smoothed; where a figure is a single run it says so in the plot.

    python docs/images/_plots.py
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from pathlib import Path

OUT = Path(__file__).resolve().parent

INK    = "#1f2937"
MUTED  = "#6b7280"
RED    = "#c92a2a"
ORANGE = "#e8590c"
AMBER  = "#d9821a"
GREEN  = "#2f9e5e"
BLUE   = "#2c5bac"
GREY   = "#cbd5e1"

plt.rcParams.update({
    "font.family":     ["Segoe UI", "DejaVu Sans"],
    "axes.edgecolor":  "#d1d5db",
    "axes.labelcolor": MUTED,
    "xtick.color":     MUTED,
    "ytick.color":     MUTED,
    "text.color":      INK,
})


def save(fig, name):
    fig.savefig(OUT / f"{name}.png", facecolor="white", bbox_inches="tight", pad_inches=0.3, dpi=170)
    plt.close(fig)
    print(f"  rendered {name}.png")


def slot_ladder():
    """What VRAM buys, measured across four cache sizes. Issue #25."""
    slots = [18, 32, 48, 64]
    vram  = [14.11, 18.97, 24.21, 29.61]
    toks  = [6.22, 7.27, 9.17, 10.89]

    fig, ax = plt.subplots(figsize=(9.6, 5.2))
    ax.plot(vram, toks, "-o", color=BLUE, lw=2.4, ms=9, zorder=3)
    for v, t, s in zip(vram, toks, slots):
        ax.annotate(f"{s} slots\n{t:.2f} tok/s", xy=(v, t), xytext=(0, 16),
                    textcoords="offset points", ha="center", fontsize=10.5, color=INK)

    ax.set_xlabel("peak VRAM (GiB)")
    ax.set_ylabel("decode (tok/s)")
    ax.set_xlim(12, 32.6)
    ax.set_ylim(5.4, 12.4)
    ax.grid(color="#eef1f4", zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    ax.axvspan(12, 16, color="#fdecea", zorder=0)
    ax.text(14, 11.6, "below 16 GB:\nnever measured,\nunsupported", ha="center", fontsize=10, color=RED)

    ax.set_title("More VRAM keeps buying throughput -- there is no knee", fontsize=15, fontweight="bold", pad=14)
    ax.text(0.5, -0.19, "UD-Q2_K_XL at -c 4096, no host tier, four fresh servers, one evaluated request each -- a capacity "
                        "corridor, not a ranking, and not the operating point: the same 64-slot cache measures "
                        "14.53-15.89 tok/s there. Measured on #25.",
            transform=ax.transAxes, ha="center", fontsize=9.5, color=MUTED)
    save(fig, "slot_ladder")


def quant_ladder():
    """Speed against quality across the quantisation rungs. Issue #28."""
    names   = ["UD-IQ1_S\n82.5 GB", "UD-Q2_K_XL\n90.2 GiB", "UD-IQ3_XXS\n95.9 GiB"]
    decode  = [8.01, 11.58, 9.89]
    quality = [0, 8.67, 10.0]
    colours = [RED, AMBER, GREEN]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.4, 4.9))

    a1.bar(names, decode, color=colours, width=0.55, zorder=3)
    for i, d in enumerate(decode):
        a1.text(i, d + 0.28, f"{d:.2f}", ha="center", fontsize=12, fontweight="bold")
    a1.set_ylabel("decode (tok/s)")
    a1.set_ylim(0, 13.6)
    a1.set_title("speed", fontsize=13, color=MUTED)

    a2.bar(names, quality, color=colours, width=0.55, zorder=3)
    for i, q in enumerate(quality):
        label = "writes no\ncode at all" if q == 0 else f"{q:.2f}"
        a2.text(i, q + 0.3 if q else 0.4, label, ha="center", fontsize=12,
                fontweight="bold" if q else "normal", color=INK if q else RED)
    a2.set_ylabel("coding gate, mean of k of 10")
    a2.set_ylim(0, 12.6)
    a2.set_title("quality", fontsize=13, color=MUTED)

    for ax in (a1, a2):
        ax.grid(axis="y", color="#eef1f4", zorder=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=10)

    fig.suptitle("The harshest quantisation is the fastest, and it is worthless",
                 fontsize=15, fontweight="bold", y=1.0)
    fig.text(0.5, -0.06, "IQ3 decodes 15 % slower per token than Q2 and needs 15 % fewer tokens -- 4.5 s difference over "
                         "29 minutes, and it is right more often. IQ1_S was run at 40 slots, the other two at 64 and "
                         "-c 65536; all three without the host tier and without --jinja. Measured on #28.",
             ha="center", fontsize=9.5, color=MUTED)
    save(fig, "quant_ladder")


def against_cpu():
    """Streaming against llama.cpp's CPU offload, and where the operating point sits.

    THREE BARS AND NOT TWO, and the middle one has a hole in it on purpose. The CPU-offload
    comparison is #24, measured WITHOUT the host tier - one binary, one quantisation, placement the
    only variable. The operating point ships WITH the tier, and only two of its three figures are
    measured: decode (paired, 2026-08-09) and peak host memory (live). Nobody has run prefill at
    the operating point, so that bar is absent rather than carried over from a different
    configuration. A missing bar is a statement; a borrowed one would be a mistake.
    """
    metrics = ["prefill\n(tok/s)", "decode\n(tok/s)", "peak host RAM\n(GiB)"]
    base    = [9.47, 10.54, 26.99]   # decode paired, host peak measured at the operating point
    op      = [None, 14.73, 33.73]   # operating point, tier on
    cpu     = [4.43, 8.18, 51.79]    # #24, experts on the CPU

    labels  = ["Crow\nno tier", "Crow\n+ tier", "CPU\noffload"]
    fig, axes = plt.subplots(1, 3, figsize=(12.4, 4.6))
    for ax, m, b, o, p in zip(axes, metrics, base, op, cpu):
        vals   = [b, o, p]
        colors = [GREY, GREEN, "#e2e8f0"]
        for i, (v, col) in enumerate(zip(vals, colors)):
            if v is None:
                ax.text(i, max(x for x in vals if x) * 0.10, "not\nmeasured", ha="center",
                        fontsize=9.5, color=MUTED, style="italic")
                continue
            ax.bar(i, v, color=col, width=0.55, zorder=3)
            ax.text(i, v * 1.04, f"{v:,.2f}".rstrip("0").rstrip("."), ha="center",
                    fontsize=12, fontweight="bold")
        ax.set_xticks(range(3)); ax.set_xticklabels(labels, fontsize=9.5)
        ax.set_title(m, fontsize=12, color=MUTED)
        ax.set_ylim(0, max(x for x in vals if x) * 1.3)
        ax.grid(axis="y", color="#eef1f4", zorder=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=10.5)

    axes[2].text(0.5, 0.62, "1.5x less than\nCPU offload", transform=axes[2].transAxes,
                 ha="center", fontsize=11, fontweight="bold", color=GREEN)

    fig.suptitle("Against CPU offload, and what the operating point costs instead",
                 fontsize=14.5, fontweight="bold", y=1.02)
    fig.text(0.5, -0.08, "Left and right are #24: the same executable with the experts on the CPU, no host tier on "
                         "either side, two evaluated requests each. The middle bar is what ships -- decode paired "
                         "2026-08-09, host memory measured live. It buys throughput by giving back the memory "
                         "advantage: 1.9x less than CPU offload without the tier, 1.5x with it. Decode and host peak for "
                         "both Crow bars are 2026-08-09 at the operating point; prefill is #24, at -c 4096, and has "
                         "not been re-taken.",
             ha="center", fontsize=9.5, color=MUTED)
    save(fig, "against_cpu")


def batch_curve():
    """Aggregate throughput against batch depth, and where the expert union stops paying. #31."""
    batch = [1, 2, 4, 8]
    agg   = [8.88, 12.47, 16.00, 18.52]
    per   = [8.88, 6.24, 4.00, 2.32]
    hit   = [68.87, 70.07, 70.14, 67.75]

    fig, ax = plt.subplots(figsize=(9.8, 5.2))
    ax.plot(batch, agg, "-o", color=BLUE,  lw=2.4, ms=9, label="aggregate", zorder=3)
    ax.plot(batch, per, "-o", color=ORANGE, lw=2.4, ms=9, label="per request", zorder=3)
    ax.set_xlabel("concurrent requests")
    ax.set_ylabel("tok/s")
    ax.set_xticks(batch)
    ax.set_ylim(0, 21)
    ax.grid(color="#eef1f4", zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False, fontsize=11, loc="center left")

    ax2 = ax.twinx()
    ax2.plot(batch, hit, ":s", color=GREEN, lw=1.8, ms=7, zorder=3)
    ax2.set_ylabel("cache hit rate (%)", color=GREEN)
    ax2.set_ylim(60, 76)
    ax2.tick_params(colors=GREEN)
    ax2.spines["top"].set_visible(False)

    ax.axvline(8, color=RED, ls="--", lw=1.4, zorder=1)
    ax.text(7.86, 19.4, "cap = (64-6)/2 = 29 experts.\nbatch 8 asks 31.2 and splits\ninto two waves",
            ha="right", fontsize=10, color=RED)

    ax.set_title("Batching buys aggregate throughput and spends per-request latency",
                 fontsize=15, fontweight="bold", pad=14)
    ax.text(0.5, -0.19, "The CLI runs -np 1 by construction: one user, one stream. Harness case: -c 8192, a 6-token prompt, "
                        "32 tokens generated, one run per depth, no host tier. The short prompt is why the hit rate "
                        "reads 68 % where the ten-task gate reads 82 %. Measured on #31.",
            transform=ax.transAxes, ha="center", fontsize=9.5, color=MUTED)
    save(fig, "batch_curve")


def host_tier():
    """The host-RAM tier against no tier, paired on identical tasks. 2026-08-09.

    Paired and not averaged, because the arrangement is half the result: two earlier attempts
    produced a 2.06x spread at identical configuration, once by repeating tasks across runs (which
    measures the cache the previous run warmed) and once by giving each arm different tasks (which
    measures how hard the tasks were). Same tasks within a pair, fresh tasks across pairs.
    """
    pairs = ["is-balanced\nrotate-matrix", "longest-common-prefix\ngroup-anagrams",
             "binary-search\nrle-encode"]
    tier  = [15.89, 14.73, 14.53]
    base  = [10.81, 10.54, 10.09]
    stall_tier = [0.728, 0.745, 0.752]
    stall_base = [1.307, 1.282, 1.345]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.8, 5.0))
    x = range(len(pairs))
    w = 0.36

    for i, (t, b) in enumerate(zip(tier, base)):
        a1.bar(i - w / 2, b, width=w, color=GREY,  zorder=3)
        a1.bar(i + w / 2, t, width=w, color=GREEN, zorder=3)
        a1.text(i - w / 2, b + 0.25, f"{b:.2f}", ha="center", fontsize=11)
        a1.text(i + w / 2, t + 0.25, f"{t:.2f}", ha="center", fontsize=11, fontweight="bold")
        a1.text(i, max(t, b) + 1.4, f"{t / b:.2f}x", ha="center", fontsize=12,
                fontweight="bold", color=GREEN)
    a1.set_xticks(list(x)); a1.set_xticklabels(pairs, fontsize=9)
    a1.set_ylabel("decode (tok/s)"); a1.set_ylim(0, 19)
    a1.set_title("throughput", fontsize=13, color=MUTED)

    for i, (t, b) in enumerate(zip(stall_tier, stall_base)):
        a2.bar(i - w / 2, b, width=w, color=GREY,  zorder=3)
        a2.bar(i + w / 2, t, width=w, color=GREEN, zorder=3)
        a2.text(i - w / 2, b + 0.03, f"{b:.3f}", ha="center", fontsize=11)
        a2.text(i + w / 2, t + 0.03, f"{t:.3f}", ha="center", fontsize=11, fontweight="bold")
    a2.set_xticks(list(x)); a2.set_xticklabels(pairs, fontsize=9)
    a2.set_ylabel("load stall per miss (ms)"); a2.set_ylim(0, 1.75)
    a2.set_title("what a miss costs", fontsize=13, color=MUTED)

    for ax in (a1, a2):
        ax.grid(axis="y", color="#eef1f4", zorder=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=10)

    a1.bar(0, 0, color=GREY,  label="no tier")
    a1.bar(0, 0, color=GREEN, label="32 GiB host tier")
    a1.legend(frameon=False, fontsize=10.5, loc="upper right")

    fig.suptitle("A host-RAM tier below the VRAM slots, paired on identical tasks",
                 fontsize=15, fontweight="bold", y=1.02)
    fig.text(0.5, -0.07, "Both arms solve the same two tasks within a pair and no task repeats across pairs; each arm "
                         "starts its own server, so the tier begins empty on both sides. Within-arm spread is 1.09x "
                         "(tier) and 1.07x (base) -- narrower than the difference, which is what makes it readable.",
             ha="center", fontsize=9.5, color=MUTED)
    save(fig, "host_tier")


def main() -> int:
    print("rendering plots")
    slot_ladder()
    quant_ladder()
    against_cpu()
    batch_curve()
    host_tier()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
