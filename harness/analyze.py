"""
analyze.py
==========
Scores results/responses.jsonl against the ground truth and runs paired stats.
Separate from run_experiment.py so you can re-score without re-calling the API.

Outputs (to ../results/):
  scored.jsonl            : per-response scores
  accuracy_by_arm.csv     : cause-identified rate + Wilson CI per (model, arm)
  pairwise_mcnemar.csv    : McNemar exact p for each arm-vs-arm within a model
  breakdown_by_type.csv   : accuracy by diagnostic_type x arm (where the lift is)
  summary.txt             : human-readable headline

Run: python harness/analyze.py
"""

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
RESULTS = os.path.join(ROOT, "results")
sys.path.insert(0, HERE)
import score as scoring  # noqa: E402
import stats as st  # noqa: E402

ARMS = ["naive", "schema_aware", "lineage_aware", "lineage_exec"]


def load():
    qs = {json.loads(l)["id"]: json.loads(l)
          for l in open(os.path.join(ROOT, "benchmark", "questions.jsonl")) if l.strip()}
    resp = [json.loads(l) for l in open(os.path.join(RESULTS, "responses.jsonl")) if l.strip()]
    return qs, resp


def main():
    qs, resp = load()
    scored = []
    for r in resp:
        q = qs[r["qid"]]
        parsed = scoring.parse_json_block(r["response_text"])
        s = scoring.score_answer(q, parsed)
        scored.append({**r, **s, "parsed_ok": bool(parsed)})

    with open(os.path.join(RESULTS, "scored.jsonl"), "w") as f:
        for s in scored:
            f.write(json.dumps(s) + "\n")

    # index: (model, arm) -> {qid: cause_identified}
    from collections import defaultdict
    idx = defaultdict(dict)
    for s in scored:
        idx[(s["model"], s["arm"])][s["qid"]] = 1 if s["cause_identified"] else 0

    models = sorted({s["model"] for s in scored})
    qids = [q for q in qs]  # stable question order

    # ---- accuracy by arm ----
    with open(os.path.join(RESULTS, "accuracy_by_arm.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "arm", "n", "cause_identified", "rate", "wilson_lo", "wilson_hi"])
        for m in models:
            for a in ARMS:
                if (m, a) not in idx:
                    continue
                vec = [idx[(m, a)].get(qid, 0) for qid in qids if qid in idx[(m, a)]]
                k, n = sum(vec), len(vec)
                lo, hi = st.wilson_ci(k, n)
                w.writerow([m, a, n, k, round(k / n, 3) if n else 0,
                            round(lo, 3), round(hi, 3)])

    # ---- pairwise McNemar within model (aligned on shared qids) ----
    with open(os.path.join(RESULTS, "pairwise_mcnemar.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "arm_a", "arm_b", "acc_a", "acc_b",
                    "a_only", "b_only", "mcnemar_p", "favored"])
        for m in models:
            present = [a for a in ARMS if (m, a) in idx]
            for i in range(len(present)):
                for j in range(i + 1, len(present)):
                    a, b = present[i], present[j]
                    shared = [qid for qid in qids
                              if qid in idx[(m, a)] and qid in idx[(m, b)]]
                    va = [idx[(m, a)][qid] for qid in shared]
                    vb = [idx[(m, b)][qid] for qid in shared]
                    res = st.compare_arms(a, va, b, vb)
                    w.writerow([m, a, b, round(res["acc_a"], 3), round(res["acc_b"], 3),
                                res["table"]["a_only"], res["table"]["b_only"],
                                round(res["mcnemar_p"], 4), res["favored"]])

    # ---- breakdown by diagnostic type ----
    type_idx = defaultdict(lambda: defaultdict(list))
    for s in scored:
        dt = qs[s["qid"]].get("diagnostic_type", "?")
        type_idx[(s["model"], s["arm"])][dt].append(1 if s["cause_identified"] else 0)
    with open(os.path.join(RESULTS, "breakdown_by_type.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "arm", "diagnostic_type", "n", "rate"])
        for (m, a), d in sorted(type_idx.items()):
            for dt, vec in sorted(d.items()):
                w.writerow([m, a, dt, len(vec), round(sum(vec) / len(vec), 3)])

    # ---- headline summary ----
    lines = ["DIAGNOSTIC NL2SQL -- RESULTS SUMMARY", "=" * 40]
    for m in models:
        lines.append(f"\nModel: {m}")
        for a in ARMS:
            if (m, a) not in idx:
                continue
            vec = list(idx[(m, a)].values())
            k, n = sum(vec), len(vec)
            lo, hi = st.wilson_ci(k, n)
            lines.append(f"  {a:14} {k:2}/{n:2} = {k/n:5.1%}  (95% CI {lo:.2f}-{hi:.2f})")
    summary = "\n".join(lines)
    with open(os.path.join(RESULTS, "summary.txt"), "w") as f:
        f.write(summary + "\n")
    print(summary)
    print(f"\nWrote: accuracy_by_arm.csv, pairwise_mcnemar.csv, "
          f"breakdown_by_type.csv, scored.jsonl, summary.txt -> {RESULTS}/")


if __name__ == "__main__":
    main()
