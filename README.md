# Diagnostic NL-to-SQL over a Multi-Partner Animal-Health Warehouse

Research harness for the paper on **lineage-aware prompting for diagnostic
("why") natural-language-to-SQL** over constraint-free enterprise warehouses.
It builds a deterministic synthetic warehouse with **injected, documented
causal events** (ground truth), a benchmark of diagnostic questions, and an
experiment that compares four prompting arms across two models, scored against
the ground truth with paired statistics.

All data is synthetic. Nothing here discloses any real company, customer, or
dataset; the *shape* (channel archetypes, lineage friction) is inspired by real
animal-health distribution feeds but every value is fabricated and seeded.

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
  questions.jsonl       # 56 tagged diagnostic NL questions
harness/
  prompts.py            # the 4 arm prefixes (+ cache-breakpoint structure)
  execute_sql.py        # DuckDB executor (read-only guardrails) for the exec arm
  run_experiment.py     # the API loop (run via Claude Code)
  score.py              # rubric scoring vs ground truth
  stats.py              # McNemar exact + Wilson CIs (stdlib only)
  analyze.py            # scores responses.jsonl, emits result tables
results/                # outputs (responses.jsonl, scored.jsonl, *.csv, summary.txt)
```

## The four experimental arms

| Arm | Context given to the model | Loop |
|-----|----------------------------|------|
| `naive` | DP-layer views only (`dp.*`) | single-shot |
| `schema_aware` | DP + full transform star (`xform.*`) | single-shot |
| `lineage_aware` | schema + lineage graph + drill hints + upstream tables | single-shot |
| `lineage_exec` | same context as lineage_aware | **execution-feedback** tool loop |

The arms differ **only** in context (and the exec loop). The task, output
contract, and scoring are identical, so any accuracy difference is attributable
to lineage context / execution feedback.

## Run order

```bash
# 1. generate the warehouse data + ground truth (deterministic, seed-locked)
cd warehouse && python generate_data.py && cd ..

# 2. install deps (in your env; pip is fine locally)
pip install -r requirements.txt

# 3. set your key
export ANTHROPIC_API_KEY=sk-...

# 4. run the experiment (makes the API calls; SEQUENTIAL by design)
python harness/run_experiment.py        # writes results/responses.jsonl

# 5. score + stats (no API calls; re-runnable)
python harness/analyze.py               # writes results/*.csv + summary.txt
```

Load the DDL into DuckDB manually if you want to explore:
```bash
cd warehouse
duckdb mywarehouse.duckdb \
  ".read 01_raw_ddl.sql" ".read 02_stage_ddl.sql" \
  ".read 03_transform_ddl.sql" ".read 04_dp_views.sql"
```

## Cost

With the default config (2 models x 4 arms x 56 questions, 1h prefix cache),
one full run costs roughly **$9**; budget ~**$22** with iteration/reruns. The
runner prints a live running cost tally and writes `results/run_summary.json`
with the realized total. Per-response token usage is logged so you can verify.

### A note on caching
The static per-arm prefixes are small (~660–1,990 tokens). Two consequences:
- The `naive` and `schema_aware` prefixes fall **below the model's minimum
  cacheable length** (1,024 tok Sonnet; 4,096 tok Haiku), so they run
  **uncached** — no error, just normal input billing. This is a deliberate,
  documented choice: the prefixes are tiny, so uncached cost is negligible and
  padding them to hit the cache floor would cost more than it saves.
- `cache_control` is still set on every arm's prefix (1h TTL). It activates on
  Sonnet's lineage arms and no-ops elsewhere. Pre-warm (`max_tokens=0`) runs
  before each arm's loop. **Run sequentially** (the default) — a cache entry is
  only available after the first response begins.

## Reproducibility / scientific-validity notes

- **Deterministic**: `SEED = 20260715` in `generate_data.py`. Same seed → same
  warehouse → same ground truth.
- **Fixed protocol**: the 6 causes + 2 decoys are declared in
  `ground_truth/causes.json` **before** any model runs. They are not tuned to
  make any arm win. If an expected-to-win arm fails, that is reported.
- **Counterfactual override**: each injected cause is applied as an exact
  override on a clean counterfactual baseline, so realized magnitudes equal
  declared magnitudes (e.g. exactly −28%) up to the baseline's own ~±8% noise.
- **Decoys**: a recurring seasonal dip (D1) and a within-family SKU mix shift
  that nets flat (D2) test whether an arm **over-attributes** a real cause to a
  benign movement.
- **Scoring** checks the agent's **structured** claim (mechanism + scope +
  real-vs-benign + grounding) against the cause signature, not string similarity.
- **Stats**: McNemar's exact (paired) test on the headline `cause_identified`
  binary, with Wilson CIs. Underpowered comparisons are reported as directional,
  not significant.

## Dialect

DDL is written for **DuckDB**. A Snowflake-dialect version can be emitted from
the same logic if needed (the constraint-free, crosswalk-resolved star schema is
identical; only `strftime`/`VALUES` syntax differs). The offline validation in
development used SQLite as a proxy; run on DuckDB for the real experiment.
