"""
stats.py
========
Paired statistical tests for comparing experimental arms, implemented purely
from the Python standard library (no statsmodels/scipy -- pip is unavailable in
some environments and these are exact/closed-form anyway).

Provides:
  mcnemar_exact(b, c)        : exact (binomial) McNemar test for paired binary
                               outcomes. b, c are the discordant-pair counts.
  wilson_ci(k, n, z)         : Wilson score interval for a binomial proportion.
  paired_table(arm_a, arm_b) : build the 2x2 discordant counts from two arms'
                               per-question cause_identified vectors.

McNemar is the right test here because each question is answered by BOTH arms,
so outcomes are PAIRED. We compare arms on the headline binary CAUSE_IDENTIFIED.
With ~56 paired items the exact binomial form is preferred over the chi-square
approximation (which is unreliable when b+c is small).
"""

import math
from math import comb


def wilson_ci(k, n, z=1.96):
    """Wilson score interval for k successes in n trials. Returns (lo, hi).
    z=1.96 -> 95%. Handles n=0 gracefully."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def mcnemar_exact(b, c):
    """Exact McNemar test (two-sided binomial) for paired binary data.
    b = # pairs where arm A correct & arm B wrong
    c = # pairs where arm A wrong   & arm B correct
    Returns dict with n_discordant, statistic, p_value, and the direction.
    Under H0 each discordant pair is a fair coin; we sum binomial tail mass.
    """
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "n_discordant": 0, "p_value": 1.0,
                "note": "no discordant pairs"}
    k = min(b, c)
    # two-sided exact p = 2 * sum_{i=0}^{k} C(n,i) (0.5)^n, capped at 1.0
    tail = sum(comb(n, i) for i in range(0, k + 1)) * (0.5 ** n)
    p = min(1.0, 2 * tail)
    return {"b": b, "c": c, "n_discordant": n, "p_value": p,
            "favored": ("A" if b > c else "B" if c > b else "tie")}


def paired_table(vec_a, vec_b):
    """Given two equal-length lists of 0/1 (cause_identified) aligned by
    question, return (a_only, b_only, both, neither)."""
    assert len(vec_a) == len(vec_b), "vectors must align by question"
    both = sum(1 for x, y in zip(vec_a, vec_b) if x and y)
    a_only = sum(1 for x, y in zip(vec_a, vec_b) if x and not y)
    b_only = sum(1 for x, y in zip(vec_a, vec_b) if y and not x)
    neither = sum(1 for x, y in zip(vec_a, vec_b) if not x and not y)
    return {"both": both, "a_only": a_only, "b_only": b_only, "neither": neither}


def compare_arms(name_a, vec_a, name_b, vec_b):
    """Full paired comparison of two arms on the cause_identified vectors."""
    n = len(vec_a)
    ka, kb = sum(vec_a), sum(vec_b)
    tab = paired_table(vec_a, vec_b)
    mc = mcnemar_exact(tab["a_only"], tab["b_only"])
    return {
        "arm_a": name_a, "arm_b": name_b, "n": n,
        "acc_a": ka / n if n else 0, "acc_b": kb / n if n else 0,
        "ci_a": wilson_ci(ka, n), "ci_b": wilson_ci(kb, n),
        "table": tab,
        "mcnemar_p": mc["p_value"],
        "discordant": mc["n_discordant"],
        "favored": mc.get("favored"),
    }


if __name__ == "__main__":
    # sanity: a clear improvement of B over A
    a = [1, 0, 0, 0, 1, 0, 0, 1, 0, 0]   # 3/10
    b = [1, 1, 1, 1, 1, 0, 1, 1, 0, 1]   # 8/10
    r = compare_arms("naive", a, "lineage", b)
    print("acc_a=%.2f ci_a=(%.2f,%.2f)" % (r["acc_a"], *r["ci_a"]))
    print("acc_b=%.2f ci_b=(%.2f,%.2f)" % (r["acc_b"], *r["ci_b"]))
    print("table:", r["table"])
    print("mcnemar exact p=%.4f favored=%s" % (r["mcnemar_p"], r["favored"]))
    # wilson sanity
    print("wilson 8/10:", tuple(round(x, 3) for x in wilson_ci(8, 10)))
    print("wilson 28/56:", tuple(round(x, 3) for x in wilson_ci(28, 56)))
