"""
score.py
========
Scores an agent's diagnostic answer against the hidden ground-truth ledger.

This is the EVALUATION METHODOLOGY contribution. Unlike text-to-SQL accuracy
(which compares one generated SQL to one gold SQL) or causal-QA (which fuzzy-
matches a generated explanation string to a gold sentence), diagnostic answers
have no single gold SQL. We instead score the agent's STRUCTURED claim against
the injected cause's structured signature:

  1. mechanism_correct : did the agent name the right root-cause mechanism?
                         (e.g. price_increase, returns_spike, lineage_crosswalk_gap)
  2. scope_correct     : did it localize to the right species/channel/region/family?
                         (Jaccard over the ground-truth scope keys; threshold)
  3. realness_correct  : did it correctly classify real-problem vs benign/decoy/
                         data-artifact? (the decoy-resistance / no-false-positive axis)
  4. grounded          : did it ground the claim in executed queries (evidence)?

The headline binary used for paired tests is CAUSE_IDENTIFIED:
  CAUSE_IDENTIFIED = mechanism_correct AND scope_correct AND realness_correct
For decoys/healthy items, CAUSE_IDENTIFIED = realness_correct AND mechanism in
{seasonality, sku_mix_shift_benign, lineage_crosswalk_gap, none} as appropriate
(i.e. correctly NOT over-attributing to a fake demand problem).

All thresholds are declared up front (fixed protocol).
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
GT_PATH = os.path.join(HERE, "..", "ground_truth", "causes.json")

SCOPE_KEYS = ["species", "modality", "family", "channel", "region"]
SCOPE_JACCARD_THRESHOLD = 0.6   # fraction of GT scope keys that must match

# mechanism synonyms the model might emit -> canonical ledger mechanism
MECH_CANON = {
    "price_increase": "price_increase", "price": "price_increase",
    "pricing": "price_increase",
    "sku_discontinuation": "sku_discontinuation",
    "discontinuation": "sku_discontinuation", "discontinued": "sku_discontinuation",
    "distribution_loss": "distribution_loss", "delisting": "distribution_loss",
    "de-listing": "distribution_loss", "distribution": "distribution_loss",
    "returns_spike": "returns_spike", "returns": "returns_spike",
    "stockout_fillrate": "stockout_fillrate", "stockout": "stockout_fillrate",
    "fill_rate": "stockout_fillrate", "supply": "stockout_fillrate",
    "lineage_crosswalk_gap": "lineage_crosswalk_gap",
    "crosswalk": "lineage_crosswalk_gap", "mapping_gap": "lineage_crosswalk_gap",
    "data_issue": "lineage_crosswalk_gap", "pipeline": "lineage_crosswalk_gap",
    "seasonality": "seasonality", "seasonal": "seasonality",
    "sku_mix_shift_benign": "sku_mix_shift_benign",
    "mix_shift": "sku_mix_shift_benign", "mix_shift_benign": "sku_mix_shift_benign",
    "none": "none", "no_cause": "none", "healthy": "none",
}


def _load_gt():
    with open(GT_PATH) as f:
        return {c["id"]: c for c in json.load(f)["causes"]}


GT = _load_gt()


def _canon_mech(m):
    if not m:
        return "other"
    return MECH_CANON.get(str(m).strip().lower(), str(m).strip().lower())


def _scope_jaccard(pred_scope, gt_scope):
    """Fraction of GT scope keys the prediction got right. (GT scope is the
    authoritative set; extra pred keys are ignored to avoid penalizing detail.)"""
    if not gt_scope:
        return 1.0  # compound/portfolio questions have empty scope
    hits = 0
    for k in gt_scope:
        pv = (pred_scope or {}).get(k)
        if pv is not None and str(pv).strip().lower() == str(gt_scope[k]).strip().lower():
            hits += 1
    return hits / len(gt_scope)


def score_answer(question, parsed_answer):
    """question: a benchmark dict. parsed_answer: the model's json block (dict)
    with keys root_cause_mechanism, scope, is_real_problem, explanation,
    evidence_queries. Returns a dict of per-axis booleans + the headline."""
    cause_id = question["cause_id"]
    is_decoy = question.get("is_decoy", False)
    diag_type = question["diagnostic_type"]

    pred_mech = _canon_mech(parsed_answer.get("root_cause_mechanism"))
    pred_scope = parsed_answer.get("scope") or {}
    pred_real = bool(parsed_answer.get("is_real_problem", False))
    evidence = parsed_answer.get("evidence_queries") or []
    grounded = len(evidence) > 0 and any(
        isinstance(e, str) and "select" in e.lower() for e in evidence)

    # ---- expected values by question category ----
    if cause_id in ("compound", "healthy"):
        if cause_id == "healthy":
            # correct = no real problem, mechanism none
            realness_correct = (pred_real is False) and (pred_mech in ("none",))
            mechanism_correct = pred_mech == "none"
            scope_correct = True  # not scored for healthy
            headline = realness_correct
        else:  # compound: scored separately (coverage), headline = grounded+some real
            realness_correct = pred_real is True
            mechanism_correct = pred_mech not in ("none",)
            scope_correct = True
            headline = mechanism_correct and grounded
        return {"mechanism_correct": mechanism_correct,
                "scope_correct": scope_correct,
                "realness_correct": realness_correct,
                "grounded": grounded,
                "cause_identified": bool(headline),
                "category": cause_id}

    gt = GT[cause_id]
    gt_mech = _canon_mech(gt["mechanism"])
    gt_scope = gt["scope"]

    mechanism_correct = (pred_mech == gt_mech)
    scope_correct = _scope_jaccard(pred_scope, gt_scope) >= SCOPE_JACCARD_THRESHOLD

    if is_decoy:
        # correct realness = NOT a real demand problem (benign/seasonal/mix-shift)
        realness_correct = (pred_real is False)
        # for decoys, mechanism should be the benign mechanism (seasonality / mix)
        headline = realness_correct and mechanism_correct
    elif gt["mechanism"] == "lineage_crosswalk_gap":
        # data-artifact: correct = flagged as NOT a real demand loss + right mech
        realness_correct = (pred_real is False)
        headline = mechanism_correct and scope_correct and realness_correct
    else:
        # genuine problem: correct = real problem, right mechanism + scope
        realness_correct = (pred_real is True)
        headline = mechanism_correct and scope_correct and realness_correct

    return {"mechanism_correct": mechanism_correct,
            "scope_correct": scope_correct,
            "realness_correct": realness_correct,
            "grounded": grounded,
            "cause_identified": bool(headline),
            "category": "decoy" if is_decoy else "real_cause",
            "diagnostic_type": diag_type}


def parse_json_block(text):
    """Extract the verdict JSON from a model response. Robust to:
       - fenced ```json ... ``` blocks (closed or unclosed)
       - JSON appearing first or last in the message
       - a trailing prose after the block
    Strategy: find the first '{' that starts a brace-balanced object containing
    'root_cause_mechanism' and parse it; fall back to any balanced object."""
    import re
    if not text:
        return {}

    # 1) try fenced json blocks first (closed)
    for m in re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL):
        try:
            obj = json.loads(m)
            if "root_cause_mechanism" in obj:
                return obj
        except Exception:
            pass

    # 2) brace-balanced scan: find every top-level {...} and try to parse
    candidates = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start:i + 1])
                    start = None
    # prefer a candidate that contains the verdict key
    ranked = sorted(candidates,
                    key=lambda c: ("root_cause_mechanism" in c, len(c)),
                    reverse=True)
    for c in ranked:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and "root_cause_mechanism" in obj:
                return obj
        except Exception:
            continue
    # 3) last resort: any parseable object
    for c in ranked:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return {}


if __name__ == "__main__":
    # self-test with synthetic answers
    qs = [json.loads(l) for l in open(os.path.join(HERE, "..", "benchmark",
                                                   "questions.jsonl")) if l.strip()]
    # perfect answer to Q01 (C1 price increase)
    q1 = next(q for q in qs if q["id"] == "Q01")
    perfect = {"root_cause_mechanism": "price_increase",
               "scope": {"species": "equine", "modality": "OTC", "region": "TX"},
               "is_real_problem": True, "explanation": "price up 18%, vol down 28%",
               "evidence_queries": ["SELECT ... FROM xform.fct_sales ..."]}
    print("Q01 perfect:", score_answer(q1, perfect))
    # decoy Q43 (seasonality) answered correctly as benign
    q43 = next(q for q in qs if q["id"] == "Q43")
    benign = {"root_cause_mechanism": "seasonality", "scope": {"species": "equine"},
              "is_real_problem": False, "explanation": "recurs YoY",
              "evidence_queries": ["SELECT ... yoy ..."]}
    print("Q43 benign-correct:", score_answer(q43, benign))
    # decoy Q43 answered WRONG (over-attributed to a real problem)
    over = {"root_cause_mechanism": "distribution_loss", "scope": {"species": "equine"},
            "is_real_problem": True, "explanation": "lost distribution",
            "evidence_queries": ["SELECT ..."]}
    print("Q43 over-attributed:", score_answer(q43, over))
