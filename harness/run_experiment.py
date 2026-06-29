"""
run_experiment.py
=================
Main experiment loop. Run this via Claude Code (it makes the Anthropic API
calls). It iterates ARMS x MODELS x QUESTIONS, captures each model response,
logs token usage + running cost, and writes everything to ../results/.

RUN ORDER (see README):
  1. cd warehouse && python generate_data.py      # build CSVs + ground truth
  2. (the runner builds the DuckDB warehouse in-process for the exec arm)
  3. export ANTHROPIC_API_KEY=...
  4. python harness/run_experiment.py              # this file
  5. python harness/analyze.py                     # scoring + stats (separate)

IMPORTANT (caching / concurrency):
  - Calls are made SEQUENTIALLY on purpose. A cache entry only becomes available
    after the first response begins, so sequential calls let the per-arm prefix
    warm once (via a max_tokens=0 pre-warm) and be read thereafter.
  - Small-arm prefixes (naive, schema_aware) fall below the model's minimum
    cacheable length, so they simply run uncached (no error). This is a
    deliberate, documented choice -- the prefixes are tiny, so uncached cost is
    negligible and padding them would cost more than it saves.

CONFIG via env or edit below:
  MODELS, ARMS, MAX_TOKENS, USE_EXEC_FEEDBACK_TURNS.

This file is defensive: if the anthropic package or API key is missing it prints
clear guidance and exits without crashing.
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
RESULTS = os.path.join(ROOT, "results")
os.makedirs(RESULTS, exist_ok=True)

sys.path.insert(0, HERE)
import prompts  # noqa: E402
import execute_sql  # noqa: E402

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
MODELS = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}
ARMS = ["naive", "schema_aware", "lineage_aware", "lineage_iterative"]
MAX_TOKENS = 2500          # raised: must fit reasoning + queries + closing JSON
# Single-pass arms get enough turns to FIX a mechanical query error (bad literal
# format, typo) and see real results -- they are NOT meant to fail on a typo.
# The contrast with iterative is about analytical re-decomposition depth, not
# about who survives a syntax slip.
SINGLE_PASS_ROUNDS = 3     # naive/schema/lineage_aware: query, fix if needed, commit
ITERATIVE_MAX_ROUNDS = 8   # lineage_iterative: deeper multi-round drilling
CACHE_TTL = "1h"           # 1h prefix cache (agentic gaps can exceed 5m)

# pricing per MTok (USD) for the running cost tally (from platform docs)
PRICE = {
    "haiku":  {"base": 1.0, "write_1h": 2.0, "read": 0.10, "out": 5.0},
    "sonnet": {"base": 3.0, "write_1h": 6.0, "read": 0.30, "out": 15.0},
}


def load_questions():
    path = os.path.join(ROOT, "benchmark", "questions.jsonl")
    return [json.loads(l) for l in open(path) if l.strip()]


def usage_cost(model_key, usage):
    """Compute USD cost for one response from its usage block."""
    p = PRICE[model_key]
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    ip = getattr(usage, "input_tokens", 0) or 0
    op = getattr(usage, "output_tokens", 0) or 0
    return (cr * p["read"] + cw * p["write_1h"] + ip * p["base"] + op * p["out"]) / 1e6


def make_client():
    try:
        import anthropic
    except ImportError:
        print("ERROR: `anthropic` package not installed. Run: pip install anthropic")
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: set ANTHROPIC_API_KEY in your environment.")
        sys.exit(1)
    return anthropic.Anthropic()


def prewarm(client, model_id, system_blocks):
    """max_tokens=0 pre-warm so the prefix cache is written before the loop."""
    try:
        r = client.messages.create(
            model=model_id, max_tokens=0, system=system_blocks,
            messages=[{"role": "user", "content": "warmup"}])
        return getattr(r, "usage", None)
    except Exception as e:
        # pre-warm is best-effort; a sub-threshold prefix may not cache.
        print(f"   (pre-warm note: {e})")
        return None


def cache_system(arm):
    """System blocks with cache_control on the last (only) static block."""
    blocks = prompts.build_system_prefix(arm)
    blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": CACHE_TTL}
    return blocks


# ---- unified execution loop (all arms execute; round cap distinguishes them) -
EXEC_TOOL = {
    "name": "execute_sql",
    "description": "Run a read-only SQL SELECT against the warehouse and get rows back.",
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single SELECT/WITH query."}},
        "required": ["sql"],
    },
}


def run_arm(client, model_id, model_key, system_blocks, question, con, max_rounds):
    """Unified executor. All arms can call execute_sql. `max_rounds` caps how many
    assistant turns may issue tool calls:
      max_rounds=1  -> single-pass (one batch of queries, then must commit)
      max_rounds>1  -> iterative (drill across rounds)
    After the cap, a final forced turn (no tools) elicits the committed verdict.
    Returns text, cost, turns, and a sql_log (list of {sql, ok, rows, error}) so
    failures are diagnosable from disk."""
    messages = [{"role": "user", "content": question["question"]}]
    total_cost = 0.0
    turns = 0
    last_text = ""
    sql_log = []

    for _ in range(max_rounds):
        msg = client.messages.create(
            model=model_id, max_tokens=MAX_TOKENS, system=system_blocks,
            tools=[EXEC_TOOL], messages=messages)
        total_cost += usage_cost(model_key, msg.usage)
        turns += 1
        last_text = "".join(b.text for b in msg.content if b.type == "text") or last_text
        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": msg.content})
        if not tool_uses:
            return {"text": last_text, "cost": total_cost, "turns": turns,
                    "sql_log": sql_log}
        results = []
        for tu in tool_uses:
            sql = tu.input.get("sql", "")
            res = execute_sql.run_sql(con, sql)
            sql_log.append({"sql": sql, "ok": res["ok"],
                            "rows": res["row_count"], "error": res.get("error")})
            results.append({
                "type": "tool_result", "tool_use_id": tu.id,
                "content": execute_sql.format_result_for_model(res)})
        messages.append({"role": "user", "content": results})

    messages.append({"role": "user", "content":
                     "You have used all query rounds. Based on the results you have "
                     "already seen, COMMIT now: output ONLY the json verdict block."})
    final = client.messages.create(
        model=model_id, max_tokens=MAX_TOKENS, system=system_blocks,
        messages=messages)   # no tools -> must answer
    total_cost += usage_cost(model_key, final.usage)
    turns += 1
    last_text = "".join(b.text for b in final.content if b.type == "text") or last_text
    return {"text": last_text, "cost": total_cost, "turns": turns,
            "sql_log": sql_log}


# ---------------------------------------------------------------------------
def main():
    client = make_client()
    questions = load_questions()
    # build the DuckDB warehouse once (ALL arms execute SQL now)
    print("Building DuckDB warehouse (all arms execute SQL)...")
    try:
        con = execute_sql.build_database()
        print("  warehouse ready.")
    except Exception as e:
        print(f"  FATAL: could not build DuckDB warehouse ({e}). Cannot run.")
        sys.exit(1)

    running_cost = 0.0
    out_path = os.path.join(RESULTS, "responses.jsonl")

    # ---- RESUME SUPPORT --------------------------------------------------
    # If RESUME=1, read any existing responses.jsonl, build a skip-set of
    # (model, arm, qid) already completed, SKIP the canary, and APPEND only the
    # missing cells. Conditions are identical to the original run (same harness,
    # seed, models), so a resumed segment is valid to merge.
    RESUME = os.environ.get("RESUME", "0") == "1"
    done_set = set()
    if RESUME and os.path.exists(out_path):
        with open(out_path) as rf:
            for line in rf:
                try:
                    d = json.loads(line)
                    done_set.add((d["model"], d["arm"], d["qid"]))
                except Exception:
                    pass
        print(f"RESUME mode: {len(done_set)} responses already on disk; "
              f"will skip those and append the rest. Canary skipped.")
    # ---- PREFLIGHT CANARY GATE (all arms, grounded) ---------------------
    # Run a canary across ALL FOUR ARMS (one model -- mechanics are model-
    # independent) on a couple of questions: one easy single-cause and one
    # execution-heavy. Refuse the full matrix unless every canary response is
    # (a) parseable AND (b) GROUNDED -- i.e. not a fabricated verdict from a
    # model that admits it never got query results. This catches the three
    # failure modes we have seen: no-verdict, fabricated-verdict, and the
    # turn-budget/execution break (which differs BY ARM, hence all arms).
    import score as _score
    if not RESUME:
      CANARY_QIDS = os.environ.get("CANARY_QIDS", "Q15,Q08").split(",")
      canary_model_key = list(MODELS)[0]               # cheapest model
      canary_model_id = MODELS[canary_model_key]
      qmap = {q["id"]: q for q in questions}
      canary_qs = [qmap[qid] for qid in CANARY_QIDS if qid in qmap]
      n_expected = len(ARMS) * len(canary_qs)
      print(f"\n=== PREFLIGHT CANARY: {canary_model_key}, all {len(ARMS)} arms x "
            f"{len(canary_qs)} questions = {n_expected} calls "
            f"(must be {n_expected}/{n_expected} parseable AND grounded) ===")

      def _is_grounded(parsed, sql_log):
          """Reject fabricated verdicts. Grounding requires:
            (a) at least one logged query that RAN OK and returned >0 rows
                (catches the 'successful-but-empty query -> fabricate' failure), and
            (b) the explanation does not admit it failed to execute/complete, and
            (c) evidence_queries contains a SELECT.
          Exception: an honest 'none' verdict that ran real non-empty queries and
          concluded no problem is still grounded even if evidence_queries is sparse."""
          expl = (parsed.get("explanation") or "").lower()
          admits_failure = any(s in expl for s in [
              "unable to", "could not", "couldn't", "no data", "without having",
              "syntax error", "did not run", "didn't run", "cannot complete",
              "no results returned", "failed to run", "failed to execute"])
          ran_nonempty = any(e.get("ok") and (e.get("rows") or 0) > 0 for e in sql_log)
          ev = parsed.get("evidence_queries") or []
          has_sql = any(isinstance(e, str) and "select" in e.lower() for e in ev)
          return ran_nonempty and has_sql and not admits_failure

      canary_ok = 0
      cpath = os.path.join(RESULTS, "canary.jsonl")
      with open(cpath, "w") as cf:
          for arm in ARMS:
              csys = cache_system(arm)
              crounds = ITERATIVE_MAX_ROUNDS if arm == "lineage_iterative" else SINGLE_PASS_ROUNDS
              for q in canary_qs:
                  try:
                      r = run_arm(client, canary_model_id, canary_model_key, csys, q, con, crounds)
                  except Exception as e:
                      r = {"text": f"__ERROR__ {e}", "cost": 0.0, "turns": 0}
                  running_cost += r["cost"]
                  parsed = _score.parse_json_block(r["text"])
                  parses = bool(parsed) and "root_cause_mechanism" in parsed
                  grounded = parses and _is_grounded(parsed, r.get("sql_log", []))
                  ok = parses and grounded
                  canary_ok += int(ok)
                  flag = ("OK" if ok else
                          "PARSES-BUT-UNGROUNDED" if parses else "NO-VERDICT")
                  print(f"   CANARY {canary_model_key}/{arm}/{q['id']}: {flag} "
                        f"turns={r['turns']} cost=${r['cost']:.4f}"
                        + (f" mech={parsed.get('root_cause_mechanism')}" if parses else ""))
                  cf.write(json.dumps({"model": canary_model_key, "arm": arm,
                                       "qid": q["id"], "parses": parses,
                                       "grounded": grounded, "ok": ok,
                                       "turns": r["turns"],
                                       "response_text": r["text"]}) + "\n")
      if canary_ok < n_expected:
          print(f"\nCANARY FAILED: {canary_ok}/{n_expected} parseable AND grounded. "
                f"Aborting BEFORE the full run. Inspect results/canary.jsonl, "
                f"fix the harness, and re-run. (spent ${running_cost:.3f})")
          sys.exit(2)
      print(f"CANARY PASSED: {canary_ok}/{n_expected} parseable and grounded. "
            f"Proceeding to full run. (canary cost ${running_cost:.3f})\n")

    # hard cost ceiling: abort the whole run if exceeded
    COST_CEILING = float(os.environ.get("COST_CEILING", "25"))

    fout = open(out_path, "a" if RESUME else "w")
    t0 = time.time()

    # live-validation state
    import score as _score2
    recent_parses = []          # rolling window of last-N parse_ok
    ROLL_N = 20
    PARSE_FLOOR = float(os.environ.get("PARSE_FLOOR", "0.85"))
    quiet_mode = False          # flips on once format proven stable
    stable_streak = 0
    status_path = os.path.join(RESULTS, "status.txt")
    total_planned = len(MODELS) * len(ARMS) * len(questions)
    done = 0

    def emit_status(msg):
        with open(status_path, "w") as sf:
            sf.write(msg + "\n")

    for model_key, model_id in MODELS.items():
        for arm in ARMS:
            # on resume, skip arms with no remaining questions (saves prewarm cost)
            if RESUME and all((model_key, arm, q["id"]) in done_set for q in questions):
                continue
            system_blocks = cache_system(arm)
            max_rounds = (ITERATIVE_MAX_ROUNDS if arm == "lineage_iterative"
                          else SINGLE_PASS_ROUNDS)
            print(f"\n=== {model_key} / {arm}  (max_rounds={max_rounds}) ===")
            pw = prewarm(client, model_id, system_blocks)
            if pw:
                print(f"   pre-warm: cache_creation={getattr(pw,'cache_creation_input_tokens',0)} "
                      f"cache_read={getattr(pw,'cache_read_input_tokens',0)}")
            for i, q in enumerate(questions, 1):
                if RESUME and (model_key, arm, q["id"]) in done_set:
                    continue  # already completed in the prior segment
                try:
                    r = run_arm(client, model_id, model_key, system_blocks,
                                q, con, max_rounds)
                except Exception as e:
                    print(f"   [{q['id']}] ERROR: {e}")
                    r = {"text": f"__ERROR__ {e}", "cost": 0.0, "turns": 0}
                running_cost += r["cost"]
                done += 1

                # ---- live parse validation ----
                parsed = _score2.parse_json_block(r["text"])
                ok = bool(parsed) and "root_cause_mechanism" in parsed
                recent_parses.append(int(ok))
                if len(recent_parses) > ROLL_N:
                    recent_parses.pop(0)
                roll_rate = sum(recent_parses) / len(recent_parses)
                stable_streak = stable_streak + 1 if ok else 0

                rec = {"model": model_key, "arm": arm, "qid": q["id"],
                       "question": q["question"], "cause_id": q["cause_id"],
                       "response_text": r["text"], "turns": r["turns"],
                       "cost_usd": round(r["cost"], 5), "parse_ok": ok,
                       "sql_log": r.get("sql_log", [])}
                fout.write(json.dumps(rec) + "\n")
                fout.flush()

                # ---- live status line (always written; cheap to tail) ----
                emit_status(f"{model_key}/{arm} {done}/{total_planned} | "
                            f"cost=${running_cost:.3f} | roll_parse={roll_rate:.0%} | "
                            f"{'QUIET' if quiet_mode else 'WATCH'} | last={q['id']} ok={ok}")

                # ---- verbose while proving format, quiet once stable ----
                if not quiet_mode:
                    print(f"   [{q['id']}] parse_ok={ok} roll={roll_rate:.0%} "
                          f"turns={r['turns']} cost=${running_cost:.3f}")
                    if stable_streak >= 30:
                        quiet_mode = True
                        print("   >>> format stable (30 clean parses); "
                              "switching to QUIET cost-only mode.")
                elif done % 10 == 0:
                    print(f"   {done}/{total_planned} | running cost ${running_cost:.3f} "
                          f"| roll_parse {roll_rate:.0%}")

                # ---- ABORT GATES ----
                if len(recent_parses) >= ROLL_N and roll_rate < PARSE_FLOOR:
                    emit_status(f"ABORTED: parse-rate {roll_rate:.0%} < {PARSE_FLOOR:.0%} "
                                f"at {done}/{total_planned}, cost=${running_cost:.3f}")
                    print(f"\n!!! ABORT: rolling parse-rate {roll_rate:.0%} fell below "
                          f"{PARSE_FLOOR:.0%}. Something regressed. Stopping to save budget. "
                          f"(spent ${running_cost:.3f})")
                    fout.close()
                    sys.exit(3)
                if running_cost > COST_CEILING:
                    emit_status(f"ABORTED: cost ${running_cost:.3f} > ceiling ${COST_CEILING}")
                    print(f"\n!!! ABORT: running cost ${running_cost:.3f} exceeded ceiling "
                          f"${COST_CEILING}. Stopping.")
                    fout.close()
                    sys.exit(4)
    fout.close()
    dt = time.time() - t0
    print(f"\nDONE in {dt:.0f}s. Total cost ${running_cost:.3f}. "
          f"Responses -> {out_path}")
    # write a small run summary. On resume, total_cost_usd reflects only the
    # resume segment; total_responses counts the full file so completeness is clear.
    try:
        total_responses = sum(1 for _ in open(out_path))
    except Exception:
        total_responses = None
    with open(os.path.join(RESULTS, "run_summary.json"), "w") as f:
        json.dump({"models": list(MODELS), "arms": ARMS,
                   "n_questions": len(questions),
                   "resume_segment": RESUME,
                   "segment_cost_usd": round(running_cost, 4),
                   "total_responses_on_disk": total_responses,
                   "expected_total": len(MODELS) * len(ARMS) * len(questions),
                   "seconds": round(dt, 1)}, f, indent=2)


if __name__ == "__main__":
    main()
