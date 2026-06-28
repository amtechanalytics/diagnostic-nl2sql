"""
prompts.py
==========
Builds the system-prompt context for each experimental ARM. The four arms are
the experimental treatment; they differ ONLY in how much warehouse context the
model is given. Everything else (task instructions, output contract, tool loop)
is held constant.

ARMS
  naive          : sees only the DP-layer views (dp.*). No transform, no lineage.
  schema_aware   : sees the full transform star schema (xform.*) + DP views.
  lineage_aware  : schema_aware + the machine-readable lineage graph + drill hints.
  lineage_exec   : same context as lineage_aware; the DIFFERENCE is the runner
                   gives it an execution-feedback loop (multi-turn: run queries,
                   see results, revise) instead of single-shot.

CACHING (see README): the static context below is the cacheable PREFIX. The
caller marks the LAST static block with cache_control. The per-question text is
the VARYING SUFFIX placed AFTER the breakpoint, so the prefix is identical across
all questions within an (arm, model) and is written once then read N times.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
WH = os.path.join(HERE, "..", "warehouse")


# ---------------------------------------------------------------------------
# Static schema descriptions per layer (rendered into the prefix)
# ---------------------------------------------------------------------------
DP_SCHEMA = """\
DATA PRODUCT (DP) LAYER -- business-facing aggregated views you can query:

dp.sales_by_species_channel_month(species, channel, month, quarter, units, revenue)
dp.otc_rx_performance(species, modality, region, quarter, units, revenue, blended_price)
dp.net_sales_by_species_channel_quarter(species, channel, quarter, gross_units, return_units, net_units, gross_revenue)
dp.region_performance(species, region, region_name, quarter, units, revenue)

Dimensions of note: species in {equine,cattle,canine,feline,swine,poultry};
modality in {Rx,OTC}; channel in {ecom_national,retail,vet_clinic};
region in {NE,MA,SE,MW,TX,MTN,PSW,PNW}; months 'YYYY-MM' spanning 2024-01..2025-12.
"""

TRANSFORM_SCHEMA = """\
TRANSFORM LAYER -- consolidated star schema (more granular than DP):

xform.fct_sales(sku_id, month, region, channel, units, revenue)
xform.fct_returns(sku_id, month, region, channel, return_units, return_amount)
xform.fct_inventory_fillrate(sku_id, month, region, channel, fill_rate, on_hand_units, forecast_units)
xform.dim_product(sku_id, species, family, modality, variant, base_unit_price)
xform.dim_channel(channel, channel_name, target_share)
xform.dim_region(region, region_name)
xform.dim_date(month, year, month_num, quarter)

Notes:
 - fct_sales is at sku x month x region x channel grain. Join dim_product for
   species/family/modality/variant. There are 2 SKUs (variants) per (species,family).
 - revenue/units let you separate PRICE (revenue/units) from VOLUME.
 - fct_returns lets you compute NET = sales units - return units.
 - fct_inventory_fillrate (ecom only) carries fill_rate and forecast_units
   (a supply/availability signal vs demand).
 - No foreign-key constraints are enforced (warehouse semantics).
"""

# Rendered lineage graph (only the lineage_aware / lineage_exec arms see this)
def _render_lineage():
    with open(os.path.join(WH, "lineage.json")) as f:
        lin = json.load(f)
    lines = ["DATA LINEAGE (raw -> stage -> transform -> dp) -- how each table is built:"]
    for node, meta in lin["nodes"].items():
        frm = meta.get("from")
        t = meta.get("transform", meta.get("role", ""))
        risk = meta.get("lineage_risk", "")
        seg = f"  {node}"
        if frm:
            seg += f"  <- {', '.join(frm)}"
        if t:
            seg += f"   [{t}]"
        lines.append(seg)
        if risk:
            lines.append(f"      LINEAGE RISK: {risk}")
    lines.append("")
    lines.append("GRAIN NOTES:")
    for tbl, note in lin["grain_notes"].items():
        lines.append(f"  {tbl}: {note}")
    lines.append("")
    lines.append("DIAGNOSTIC DRILL HINTS (reusable navigation knowledge, not answers):")
    for h in lin["drill_hints"]["apparent_drop_in_dp_metric"]:
        lines.append(f"  - {h}")
    # also expose raw/stage tables so reconciliation across lineage is possible
    lines.append("")
    lines.append("UPSTREAM TABLES you may also query for reconciliation:")
    lines.append("  stage.ecom_sales(product_key, month, region, channel, units, revenue, price)")
    lines.append("  stage.retail_sales(product_key, month, region, channel, units, revenue, price)")
    lines.append("  stage.vetclinic_sales(product_key, month, region, channel, units, revenue, price)")
    lines.append("  stage.ecom_fillrate(product_key, month, region, fill_rate, on_hand_units, forecast_units)")
    lines.append("  stage.returns(product_key, month, region, channel, return_units, return_amount)")
    lines.append("  raw.master_crosswalk(ecom_pid, sku_id)  -- ecom product id -> manufacturer sku")
    lines.append("  (stage.ecom_* product_key is the PARTNER id; transform maps it to sku via crosswalk."
                 " Rows whose partner id is missing from the crosswalk are dropped at transform.)")
    return "\n".join(lines)


LINEAGE_BLOCK = _render_lineage()


# ---------------------------------------------------------------------------
# Constant task instructions + output contract (same for all arms)
# ---------------------------------------------------------------------------
TASK_INSTRUCTIONS = """\
You are a diagnostic analytics agent for an animal-health manufacturer's
commercial-excellence team. A business user asks a DIAGNOSTIC ("why") question
about sales movements. Your job is NOT just to fetch a number -- it is to find
and explain the ROOT CAUSE, grounded entirely in the data.

How to work:
 1. Decompose the question into the specific SQL queries needed to localize the
    cause (by region, channel, sku, price-vs-volume, gross-vs-net, fill-rate,
    or by reconciling upstream vs downstream tables along the data pipeline).
 2. Ground every claim in query results. Do NOT invent numbers.
 3. Distinguish a REAL problem from a BENIGN movement (seasonality that recurs
    year-over-year; a within-family SKU mix shift that nets flat; a pipeline/data
    issue that is not a true demand loss). Do not over-attribute: if there is no
    real problem, say so.
 4. If the movement is a DATA/PIPELINE artifact rather than a demand change, say
    that explicitly.

OUTPUT CONTRACT -- end your final answer with a single fenced ```json block:
```json
{
  "root_cause_mechanism": "<one of: price_increase | sku_discontinuation | distribution_loss | returns_spike | stockout_fillrate | lineage_crosswalk_gap | seasonality | sku_mix_shift_benign | none | other>",
  "scope": {"species": "...", "modality": "...", "family": "...", "channel": "...", "region": "..."},
  "is_real_problem": true,
  "explanation": "<2-3 sentence grounded explanation with the key numbers>",
  "evidence_queries": ["<the SQL you relied on>"]
}
```
Use only keys that apply in "scope". Set "is_real_problem" false for benign/seasonal/
data-artifact cases. "root_cause_mechanism":"none" means no real problem found.
"""

SINGLE_SHOT_NOTE = """\
You may reason step by step, but you will produce SQL and your final answer in a
single response. Assume the queries you write will be executed and you should
state the expected diagnostic logic and the answer based on the schema given.
"""

EXEC_LOOP_NOTE = """\
You have an execute_sql tool. Call it to run SQL against the warehouse, observe
the returned rows, and iterate: drill down, reconcile upstream vs downstream,
and revise your hypothesis based on what you see. When confident, give the final
answer with the required json block.
"""


# ---------------------------------------------------------------------------
# Public: build the cacheable system prefix for an arm
# ---------------------------------------------------------------------------
def build_system_prefix(arm):
    """Return a list of system content blocks (the cacheable PREFIX).
    The caller marks the LAST block with cache_control."""
    if arm == "naive":
        ctx = DP_SCHEMA
        loop_note = SINGLE_SHOT_NOTE
    elif arm == "schema_aware":
        ctx = DP_SCHEMA + "\n" + TRANSFORM_SCHEMA
        loop_note = SINGLE_SHOT_NOTE
    elif arm == "lineage_aware":
        ctx = DP_SCHEMA + "\n" + TRANSFORM_SCHEMA + "\n" + LINEAGE_BLOCK
        loop_note = SINGLE_SHOT_NOTE
    elif arm == "lineage_exec":
        ctx = DP_SCHEMA + "\n" + TRANSFORM_SCHEMA + "\n" + LINEAGE_BLOCK
        loop_note = EXEC_LOOP_NOTE
    else:
        raise ValueError(f"unknown arm: {arm}")

    full = TASK_INSTRUCTIONS + "\n" + loop_note + "\n\nWAREHOUSE CONTEXT\n" + ctx
    # Single cacheable block (the whole static prefix). The caller adds
    # cache_control to this block.
    return [{"type": "text", "text": full}]


ARMS = ["naive", "schema_aware", "lineage_aware", "lineage_exec"]

if __name__ == "__main__":
    for a in ARMS:
        blocks = build_system_prefix(a)
        n = len(blocks[0]["text"])
        print(f"{a:16} prefix chars={n:6}  (~{n//4} tokens)")
