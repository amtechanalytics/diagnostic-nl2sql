# Diagnostic NL-to-SQL: A Controlled Benchmark over a Multi-Layer Warehouse

A research harness for studying diagnostic ("why") natural-language-to-SQL over
constraint-free, multi-layer enterprise data warehouses. It builds a
deterministic synthetic warehouse with **injected, documented causal events**
(structured ground truth), a benchmark of diagnostic questions, and an
experiment that compares four agent configurations across two models, scored
against the ground truth with paired statistics.

Unlike retrieval NL-to-SQL, a diagnostic question has no single gold query:
answering "why did this metric move" requires decomposing the question into a
battery of probes, running them, and synthesizing a verdict that localizes a
cause. This harness makes such verdicts checkable by scoring the structured
claim (mechanism + scope + real-vs-benign + grounding) against an injected cause
signature, rather than by string similarity.

All data is synthetic. Nothing here discloses any real company, customer, or
dataset; the *shape* (channel archetypes, lineage friction) is inspired by
generic multi-partner distribution feeds, but every value is fabricated and
seeded.

## Layout

```
warehouse/
  generate_data.py      # deterministic data generator + counterfactual override
  01_raw_ddl.sql        # raw: 3 heterogeneous partners (ecom / retail / vet-clinic)
  02_stage_ddl.sql      # stage: per-partner conformed (vet invoice-lines -> monthly)
  03_transform_ddl.sql  # transform: consolidated star; ecom crosswalk resolves here
  04_dp_views.sql       # data-product: business-facing aggregated views
  lineage.json          # machine-readable raw->stage->transform->dp graph + drill hints
  _data/*.csv           # generated CSVs (created by generate_data.py)
ground_truth/
  causes.json           # 6 injected causes + 2 decoys (FIXED protocol, hidden from model)
benchmark/
  questions.jsonl       # tagged diagnostic NL questions
harness/
  prompts.py            # the 4 arm prefixes (+ cache-breakpoint structure)
  execute_sql.py        # DuckDB executor (read-only guardrails) for the exec arm
  run_experiment.py     # the API loop
  run_with_monitor.py   # monitored entry point (canary + cost/parse gates)
  score.py              # rubric scoring vs ground truth
  stats.py              # McNemar exact + Wilson CIs (stdlib only)
  analyze.py            # scores responses.jsonl, emits result tables
results/                # outputs (responses.jsonl, scored.jsonl, *.csv, summary.txt)
make_figures.py         # cost-accuracy figure from results/scored.jsonl
```

## The four experimental arms

| Arm | Context given to the model | Loop |
|-----|----------------------------|------|
| `naive` | DP-layer views only (`dp.*`) | single-shot |
| `schema_aware` | DP + full transform star (`xform.*`) | single-shot |
| `lineage_aware` | schema + lineage graph + drill hints + upstream tables | single-shot |
| `lineage_iterative` | same context as `lineage_aware` | **execution-feedback** tool loop |

The three single-pass arms vary **only** in context depth; the fourth adds an
execution loop while holding context identical to `lineage_aware`. The task,
output contract, and scoring are identical across all arms, so any accuracy
difference is attributable to context depth (across the single-pass arms) or to
the execution loop (`lineage_aware` vs `lineage_iterative`).

## Run order

```bash
# 1. generate the warehouse data + ground truth (deterministic, seed-locked)
cd warehouse && python generate_data.py && cd ..

# 2. install deps
pip install -r requirements.txt

# 3. set your key
export ANTHROPIC_API_KEY=sk-...

# 4. run the experiment THROUGH THE MONITOR (recommended)
python harness/run_with_monitor.py        # writes results/responses.jsonl

# 5. score + stats (no API calls; re-runnable)
python harness/analyze.py                 # writes results/*.csv + summary.txt
```

### Live validation gates (built into the runner)

The runner self-protects so a broken output format or runaway cost cannot burn a
full budget:

1. **Strict canary (3/3).** Before the full matrix, it runs `CANARY_N` (default
   3) questions on one arm and parses each immediately. If **any** fail to yield
   a valid verdict block, it **aborts before the full run** (exit 2). Cheap
   insurance.
2. **Live parse monitoring.** Every response is parsed as it lands. While the
   format is still being proven it prints a per-response `parse_ok` flag and the
   rolling parse-rate. After 30 consecutive clean parses it switches to
   **quiet cost-only mode**.
3. **Parse-floor abort (exit 3).** If the rolling parse-rate (last 20) drops
   below `PARSE_FLOOR` (default 0.85), the run aborts to save budget.
4. **Cost-ceiling abort (exit 4).** If running cost exceeds `COST_CEILING`
   (default $25), the run aborts.

`results/status.txt` is rewritten every response with a compact line
(`model/arm done/total | cost | roll_parse | WATCH|QUIET | last qid`). Tail it
in another terminal: `tail -f results/status.txt`.

The monitor wrapper (`run_with_monitor.py`) streams the runner output, surfaces
the status heartbeat, and reports a plain-English meaning for the exit code. You
can also run `python harness/run_experiment.py` directly (same gates), but the
monitor is the intended entry point.

Env knobs: `CANARY_N`, `CANARY_ARM`, `PARSE_FLOOR`, `COST_CEILING`.

## Cost

With the default config (2 models x 4 arms x 56 questions, 1h prefix cache), one
full run costs roughly **$9**; budget ~**$22** with iteration/reruns. The runner
prints a live running cost tally and writes `results/run_summary.json` with the
realized total. Per-response token usage is logged so you can verify.

### A note on caching
The static per-arm prefixes are small (~660-1,990 tokens). Two consequences:
- The `naive` and `schema_aware` prefixes fall **below the model's minimum
  cacheable length** (1,024 tok Sonnet; 4,096 tok Haiku), so they run
  **uncached** — no error, just normal input billing. This is a deliberate,
  documented choice: the prefixes are tiny, so uncached cost is negligible and
  padding them to hit the cache floor would cost more than it saves.
- `cache_control` is still set on every arm's prefix (1h TTL). It activates on
  the larger model's lineage arms and no-ops elsewhere. Pre-warm
  (`max_tokens=0`) runs before each arm's loop. **Run sequentially** (the
  default) — a cache entry is only available after the first response begins.

## Reproducibility / scientific-validity notes

- **Deterministic**: `SEED = 20260715` in `generate_data.py`. Same seed → same
  warehouse → same ground truth.
- **Fixed protocol**: the 6 causes + 2 decoys are declared in
  `ground_truth/causes.json` **before** any model runs. They are not tuned to
  make any arm win. If an expected-to-win arm fails, that is reported.
- **Counterfactual override**: each injected cause is applied as an exact
  override on a clean counterfactual baseline, so realized magnitudes equal
  declared magnitudes up to the baseline's own noise.
- **Decoys**: a recurring seasonal dip (D1) and a within-family SKU mix shift
  that nets flat (D2) test whether an arm **over-attributes** a real cause to a
  benign movement.
- **Scoring** checks the agent's **structured** claim (mechanism + scope +
  real-vs-benign + grounding) against the cause signature, not string
  similarity.
- **Stats**: McNemar's exact (paired) test on the headline `cause_identified`
  binary, with Wilson CIs. Underpowered comparisons are reported as directional,
  not significant.

## Dialect

DDL is written for **DuckDB**. A Snowflake-dialect version can be emitted from
the same logic if needed (the constraint-free, crosswalk-resolved star schema is
identical; only `strftime`/`VALUES` syntax differs). Offline validation in
development used SQLite as a proxy; run on DuckDB for the real experiment.

## Figures

`make_figures.py` generates the cost-accuracy figure from the scored results.

    python3 make_figures.py

Reads `results/scored.jsonl` and writes `figures/fig1_frontier.png` — diagnostic
accuracy against measured inference spend, for both models across all four arms,
with 95% Wilson intervals. All numbers are read directly from the scored results
file and recomputed at run time; none are hand-entered. The script echoes every
accuracy and cost cell to stdout on each run, so any drift between the result
file and a published figure is caught immediately.

## License / data

All materials are released for research reproducibility. No real, proprietary,
or personal data is included; the warehouse is synthetic, its structure modeled
on but containing no values from real feeds.
