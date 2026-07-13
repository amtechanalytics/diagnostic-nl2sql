#!/usr/bin/env python3
"""
Figure generation for TEMSMET 2026 Paper B.
Reads ONLY from scored.jsonl. No hand-entered numbers.
Outputs PNG (300 dpi) into figures/.
Fig. 2 was retired: it became Table III (per-type numbers) to fit the 6-page cap.

Colors chosen luminance-distinct so figures survive B&W printing.
"""
import json, math, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FIGDIR = os.path.join(HERE, "figures")
os.makedirs(FIGDIR, exist_ok=True)

ARMS = ["naive", "schema_aware", "lineage_aware", "lineage_iterative"]
ARM_LABEL = {
    "naive": "Naive\n(no context)",
    "schema_aware": "Schema\n(+star)",
    "lineage_aware": "Lineage\n(+graph)",
    "lineage_iterative": "Lineage\n+ LOOP",
}
MODELS = ["haiku", "sonnet"]
MODEL_LABEL = {"haiku": "Economy tier", "sonnet": "Premium tier"}

# luminance-distinct: dark navy vs mid orange
C = {"haiku": "#1f3b73", "sonnet": "#e07b1a"}
MARK = {"haiku": "o", "sonnet": "s"}


def load():
    rows = [json.loads(l) for l in open(os.path.join(HERE, "results", "scored.jsonl"))]
    # Q51-Q56 lack diagnostic_type; derive from category exactly as
    # breakdown_by_type.csv does (verified: sums to 56/cell).
    for r in rows:
        if "diagnostic_type" not in r:
            r["diagnostic_type"] = (
                "compound_multi_cause" if r["category"] == "compound"
                else "healthy_no_issue"
            )
    return rows


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def cell(rows, m, a):
    sub = [r for r in rows if r["model"] == m and r["arm"] == a]
    k = sum(1 for r in sub if r["cause_identified"])
    n = len(sub)
    cost = sum(r["cost_usd"] for r in sub)
    return k, n, cost / n


# ---------------------------------------------------------------- Figure 1
def fig1_frontier(rows):
    """Cost-accuracy frontier. THE central exhibit: the allocation decision."""
    fig, ax = plt.subplots(figsize=(7.0, 4.3))

    for m in MODELS:
        xs, ys, los, his = [], [], [], []
        for a in ARMS:
            k, n, cpq = cell(rows, m, a)
            lo, hi = wilson(k, n)
            xs.append(cpq * 1000)          # cost in tenths of a cent -> $ per 1000 q
            ys.append(k / n * 100)
            los.append((k / n - lo) * 100)
            his.append((hi - k / n) * 100)
        ax.errorbar(
            xs, ys, yerr=[los, his], marker=MARK[m], color=C[m],
            markersize=8, linewidth=1.8, capsize=3, elinewidth=1.1,
            label=MODEL_LABEL[m], zorder=3,
        )
        for a, x, y in zip(ARMS, xs, ys):
            dy = 5.5 if m == "sonnet" else -8.5
            ax.annotate(
                ARM_LABEL[a].replace("\n", " "),
                (x, y), textcoords="offset points", xytext=(0, dy),
                ha="center", fontsize=7.2, color=C[m],
            )

    # The substitution: economy+loop vs premium+context
    hk, hn, hc = cell(rows, "haiku", "lineage_iterative")
    sk, sn, sc = cell(rows, "sonnet", "schema_aware")
    ax.annotate(
        "",
        xy=(hc * 1000, hk / hn * 100), xytext=(sc * 1000, sk / sn * 100),
        arrowprops=dict(arrowstyle="<->", color="#444444", lw=1.4,
                        linestyle=(0, (4, 2))),
        zorder=2,
    )
    ax.text(
        (hc + sc) / 2 * 1000, (hk / hn + sk / sn) / 2 * 100 + 1.0,
        "same spend,\n+7.1 pp",
        ha="center", va="bottom", fontsize=7.6, color="#222222",
        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#888888", lw=0.6),
        zorder=4,
    )

    ax.set_xlabel("Inference spend (US dollars per 1,000 diagnostic questions)")
    ax.set_ylabel("Cause-identified rate (percent)")
    ax.set_title(
        "Buying the loop dominates buying the context",
        fontsize=10.5, pad=8,
    )
    ax.set_ylim(30, 100)
    ax.grid(alpha=0.25, linewidth=0.6)
    ax.legend(loc="lower right", fontsize=8.5, framealpha=0.95)
    fig.tight_layout()
    out = os.path.join(FIGDIR, "fig1_frontier.png")
    fig.savefig(out, dpi=300)
    plt.close(fig)
    print("wrote", out)


if __name__ == "__main__":
    rows = load()
    assert len(rows) == 448, len(rows)
    fig1_frontier(rows)

    # echo the numbers the manuscript cites, so drift is caught here
    print("\n--- numbers used in manuscript (verify against DATA_FACTS.md) ---")
    for m in MODELS:
        for a in ARMS:
            k, n, cpq = cell(rows, m, a)
            lo, hi = wilson(k, n)
            print(f"{m:7} {a:18} {k:2}/{n} = {k/n*100:5.1f}%  "
                  f"[{lo:.3f},{hi:.3f}]  ${cpq:.5f}/q  ${cpq*n/k:.5f}/correct")
