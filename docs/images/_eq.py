"""Render Crow's equations to white-background PNGs via matplotlib mathtext.

One image per equation: the formula large, a plain caption underneath. No LaTeX
install needed -- mathtext covers everything here.

Every number in these equations is measured on this machine and carries its issue
number in the caption. A figure without a source does not belong in this file.

    python docs/images/_eq.py
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

OUT = Path(__file__).resolve().parent


def render(name: str, formula: str, caption: str) -> None:
    fig = plt.figure(figsize=(8.4, 2.0), dpi=200)
    fig.patch.set_facecolor("white")
    fig.text(0.5, 0.60, formula, fontsize=24, ha="center", va="center", color="#1f2937")
    fig.text(0.5, 0.16, caption, fontsize=11.5, ha="center", va="center", color="#6b7280")
    fig.savefig(OUT / f"{name}.png", facecolor="white", bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    print(f"  rendered {name}.png")


EQUATIONS = {
    "eq_naive_memory": (
        r"$\mathrm{bytes} = P \cdot b = 304.18\times10^{9} \cdot 2 = 608\ \mathrm{GB}$",
        "Every parameter resident at bf16 -- eighteen times what the card holds",
    ),
    "eq_sparsity": (
        r"$\frac{\mathrm{active}}{\mathrm{total}} = \frac{6}{256} = 2.34\%\ \mathrm{per\ layer}$",
        "Six of 256 experts fire per layer, so 92.9 % of the file can stay on disk",
    ),
    "eq_resident_set": (
        r"$\mathrm{resident} = \mathrm{attention} + \mathrm{norms} + \mathrm{shared} = 6.88\ \mathrm{GiB}$",
        "7.08 % of the 97.05 GiB 0731 file, and every token touches all of it. 90.17 GiB are routed experts",
    ),
    "eq_bytes_per_token": (
        r"$90.17\ \mathrm{GiB} \cdot \frac{6}{256} = 2.11\ \mathrm{GiB}\ \mathrm{per\ token}$",
        "Routed experts of 0731 UD-IQ3_XXS; 6 x 378,208,256 B agrees. The 376.9 MiB the router pulled is preview-only (#28)",
    ),
    "eq_kv_cost": (
        r"$200\,192 \cdot 6.92\ \mathrm{KiB} = 1\,353.5\ \mathrm{MiB} = 1.32\ \mathrm{GiB}$",
        "Measured at n_ctx = 200192 in every paired run, not extrapolated from 64k",
    ),
    "eq_wave_cap": (
        r"$\mathrm{cap} = \frac{n_{\mathrm{slots}} - n_{\mathrm{used}}}{2} = \frac{58-6}{2} = 26$",
        "Experts one pass may hold. Batch 4 asks 19.8 and fits; batch 8 asks 31.2 and splits (#31)",
    ),
    "eq_queue_depth": (
        r"$1.60 \longrightarrow 4.31\ \mathrm{outstanding\ reads}$",
        "One work item per weight tensor instead of per expert. Same bytes, same request size",
    ),
    "eq_wait_share": (
        r"$\frac{32\,500.31\ \mathrm{ms}}{46\,434.23\ \mathrm{ms}} = 70.0\%$",
        "Decode spent waiting on the drive, with the host tier (58 slots, median arm r3-l2). 78.6-81.0 % without it",
    ),
}


def main() -> int:
    print(f"rendering {len(EQUATIONS)} equations")
    for name, (formula, caption) in EQUATIONS.items():
        render(name, formula, caption)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
