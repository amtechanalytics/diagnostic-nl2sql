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
ARMS = ["naive", "schema_aware", "lineage_aware", "lineage_exec"]
MAX_TOKENS = 1500
EXEC_MAX_TURNS = 6          # tool-loop cap for the lineage_exec arm
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


# ---- single-shot arms ------------------------------------------------------
def run_single_shot(client, model_id, model_key, system_blocks, question):
    msg = client.messages.create(
        model=model_id, max_tokens=MAX_TOKENS, system=system_blocks,
        messages=[{"role": "user", "content": question["question"]}])
    text = "".join(b.text for b in msg.content if b.type == "text")
    return {"text": text, "usage": msg.usage,
            "cost": usage_cost(model_key, msg.usage), "turns": 1}


# ---- execution-feedback arm (tool loop) -----------------------------------
EXEC_TOOL = {
    "name": "execute_sql",
    "description": "Run a read-only SQL SELECT against the warehouse and get rows back.",
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single SELECT/WITH query."}},
        "required": ["sql"],
    },
}


def run_exec_loop(client, model_id, model_key, system_blocks, question, con):
    messages = [{"role": "user", "content": question["question"]}]
    total_cost = 0.0
    turns = 0
    last_text = ""
    for _ in range(EXEC_MAX_TURNS):
        msg = client.messages.create(
            model=model_id, max_tokens=MAX_TOKENS, system=system_blocks,
            tools=[EXEC_TOOL], messages=messages)
        total_cost += usage_cost(model_key, msg.usage)
        turns += 1
        last_text = "".join(b.text for b in msg.content if b.type == "text") or last_text
        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        # record assistant turn
        messages.append({"role": "assistant", "content": msg.content})
        if not tool_uses:
            break  # model gave a final answer
        # execute each requested query, return results
        results = []
        for tu in tool_uses:
            sql = tu.input.get("sql", "")
            res = execute_sql.run_sql(con, sql)
            results.append({
                "type": "tool_result", "tool_use_id": tu.id,
                "content": execute_sql.format_result_for_model(res)})
        messages.append({"role": "user", "content": results})
    return {"text": last_text, "usage": None, "cost": total_cost, "turns": turns}


# ---------------------------------------------------------------------------
def main():
    client = make_client()
    questions = load_questions()
    # build the DuckDB warehouse once (only needed for the exec arm)
    print("Building DuckDB warehouse for the execution-feedback arm...")
    try:
        con = execute_sql.build_database()
        print("  warehouse ready.")
    except Exception as e:
        print(f"  WARNING: could not build DuckDB warehouse ({e}). "
              f"The lineage_exec arm will be skipped.")
        con = None

    running_cost = 0.0
    out_path = os.path.join(RESULTS, "responses.jsonl")
    fout = open(out_path, "w")
    t0 = time.time()

    for model_key, model_id in MODELS.items():
        for arm in ARMS:
            if arm == "lineage_exec" and con is None:
                continue
            system_blocks = cache_system(arm)
            print(f"\n=== {model_key} / {arm} ===")
            pw = prewarm(client, model_id, system_blocks)
            if pw:
                print(f"   pre-warm: cache_creation={getattr(pw,'cache_creation_input_tokens',0)} "
                      f"cache_read={getattr(pw,'cache_read_input_tokens',0)}")
            for i, q in enumerate(questions, 1):
                try:
                    if arm == "lineage_exec":
                        r = run_exec_loop(client, model_id, model_key,
                                          system_blocks, q, con)
                    else:
                        r = run_single_shot(client, model_id, model_key,
                                            system_blocks, q)
                except Exception as e:
                    print(f"   [{q['id']}] ERROR: {e}")
                    r = {"text": f"__ERROR__ {e}", "cost": 0.0, "turns": 0}
                running_cost += r["cost"]
                rec = {"model": model_key, "arm": arm, "qid": q["id"],
                       "question": q["question"], "cause_id": q["cause_id"],
                       "response_text": r["text"], "turns": r["turns"],
                       "cost_usd": round(r["cost"], 5)}
                fout.write(json.dumps(rec) + "\n")
                fout.flush()
                if i % 10 == 0 or i == len(questions):
                    print(f"   {i}/{len(questions)} done | running cost ${running_cost:.3f}")
    fout.close()
    dt = time.time() - t0
    print(f"\nDONE in {dt:.0f}s. Total cost ${running_cost:.3f}. "
          f"Responses -> {out_path}")
    # write a small run summary
    with open(os.path.join(RESULTS, "run_summary.json"), "w") as f:
        json.dump({"models": list(MODELS), "arms": ARMS,
                   "n_questions": len(questions),
                   "total_cost_usd": round(running_cost, 4),
                   "seconds": round(dt, 1)}, f, indent=2)


if __name__ == "__main__":
    main()
