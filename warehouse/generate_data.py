"""
generate_data.py
================
Deterministic synthetic data generator for a multi-partner animal-health
distribution warehouse, with a *hidden injected-cause ledger* that serves as
the ground truth for diagnostic ("why") evaluation.

DESIGN PRINCIPLES (these protect scientific validity):
  1. FULLY DETERMINISTIC. A fixed SEED makes every run reproducible. No real
     data is used; the *shape* (grains, channel archetypes, crosswalk friction)
     is inspired by real animal-health feeds but every value here is fabricated.
  2. CAUSES ARE FIXED BEFORE ANY MODEL RUNS. The injected causes + decoys are
     declared in CAUSE_LEDGER below as a fixed protocol. We do NOT tune them to
     make any experimental arm win. If the lineage arm fails to find an injected
     cause, that is reported as a finding.
  3. GROUND TRUTH IS STRUCTURED, NOT A STRING. Each cause has a machine-checkable
     signature (scope dims + period + mechanism + expected direction/magnitude),
     so scoring checks whether the agent localized the real driver -- not whether
     its prose matches a gold sentence.
  4. DECOYS EXIST. Some metric movements are benign (seasonality) or are
     correlated-but-not-causal, so a correct agent must distinguish signal from
     noise and avoid over-attribution.

OUTPUTS (written to ../warehouse/_data/ as CSVs, loaded by the *_ddl.sql files):
  raw_ecom_*.csv, raw_retail_*.csv, raw_vetclinic_*.csv   (heterogeneous raw)
  and the generator also emits ground_truth/causes.json (the hidden ledger).

The raw layer is intentionally heterogeneous across partners (different column
names, grains, return conventions, product-id vocabularies) to create the
raw->stage->transform lineage depth the experiment depends on.
"""

import csv
import json
import math
import os
import random
from datetime import date, timedelta

SEED = 20260715  # fixed; do not change between runs reported in the paper
random.seed(SEED)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "_data")
GT_DIR = os.path.join(HERE, "..", "ground_truth")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(GT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 1. DIMENSIONS  (the conformed business vocabulary)
# ---------------------------------------------------------------------------
SPECIES = ["equine", "cattle", "canine", "feline", "swine", "poultry"]
MODALITY = ["Rx", "OTC"]
CHANNELS = ["ecom_national", "retail", "vet_clinic"]      # ~50 / 30 / 20
CHANNEL_SHARE = {"ecom_national": 0.50, "retail": 0.30, "vet_clinic": 0.20}
REGIONS = ["NE", "MA", "SE", "MW", "TX", "MTN", "PSW", "PNW"]  # 8 US regions

# product families per species (realistic animal-health structure)
FAMILIES = {
    "equine":  ["dewormer", "joint_supplement", "vaccine"],
    "cattle":  ["antibiotic_otc", "vaccine", "parasiticide"],
    "canine":  ["flea_tick", "heartworm_rx", "derm_otc"],
    "feline":  ["flea_tick", "renal_rx", "derm_otc"],
    "swine":   ["vaccine", "antibiotic_otc"],
    "poultry": ["vaccine", "antibiotic_otc"],
}
# modality by family (Rx vs OTC) -- some families are clearly Rx
FAMILY_MODALITY = {
    "dewormer": "OTC", "joint_supplement": "OTC", "vaccine": "Rx",
    "antibiotic_otc": "OTC", "parasiticide": "OTC", "flea_tick": "OTC",
    "heartworm_rx": "Rx", "derm_otc": "OTC", "renal_rx": "Rx",
}

# Build SKU master: 2 SKUs per (species, family)
SKUS = []  # list of dicts
_pid = 100000
for sp in SPECIES:
    for fam in FAMILIES[sp]:
        _shared_base = random.randint(200, 2800)  # used when variants must match
        for variant in (1, 2):
            _pid += 7  # non-contiguous internal ids (realistic)
            # The two canine derm_otc SKUs share an equal base so the D2 decoy's
            # symmetric -f/+f shift nets to an exactly flat family total.
            if sp == "canine" and fam == "derm_otc":
                base_units = _shared_base
            else:
                base_units = random.randint(200, 2800)
            sku = {
                "sku_id": f"BI{_pid}",          # manufacturer (internal) part number
                "species": sp,
                "family": fam,
                "modality": FAMILY_MODALITY[fam],
                "variant": variant,
                "base_unit_price": round(random.uniform(8, 220), 2),
                "base_monthly_units": base_units,
            }
            SKUS.append(sku)

# Partner-specific product-id crosswalk (the lineage friction!).
# The e-commerce partner uses its OWN product numbers; transform must map them
# back to the manufacturer sku_id via a crosswalk. A deliberate *gap* in this
# crosswalk is one of the injected causes.
ECOM_CROSSWALK = {}   # ecom_pid -> sku_id
_cw = 500000
for sku in SKUS:
    _cw += 3
    ECOM_CROSSWALK[f"CW{_cw}"] = sku["sku_id"]
ECOM_PID_BY_SKU = {v: k for k, v in ECOM_CROSSWALK.items()}

# ---------------------------------------------------------------------------
# 2. TIME  (24 months = 8 quarters, monthly grain)
# ---------------------------------------------------------------------------
START = date(2024, 1, 1)
MONTHS = []
for m in range(24):
    y = 2024 + (m // 12)
    mo = (m % 12) + 1
    MONTHS.append(date(y, mo, 1))


def quarter_of(d):
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def month_key(d):
    return d.strftime("%Y-%m")


# seasonality multiplier by species & month (e.g., parasiticides peak spring/summer)
def seasonality(species, d):
    m = d.month
    # generic warm-season lift for parasite/flea-tick/dewormer-heavy species
    warm = math.sin((m - 3) / 12 * 2 * math.pi) * 0.25 + 1.0  # peak ~ summer
    if species in ("equine", "cattle"):
        return warm
    if species in ("canine", "feline"):
        return 1.0 + (warm - 1.0) * 0.6
    return 1.0  # swine/poultry roughly flat


# mild underlying growth trend (units grow ~0.6%/month on average)
def trend(month_idx):
    return 1.0 + 0.006 * month_idx


# ---------------------------------------------------------------------------
# 3. INJECTED CAUSE LEDGER  (the hidden ground truth -- FIXED PROTOCOL)
# ---------------------------------------------------------------------------
# Each cause has:
#   id, mechanism, scope (filters), window (months), direction, magnitude,
#   layer_where_visible (which lineage layer reveals the true driver),
#   detectable_by (which decomposition surfaces it), narrative (gold rationale)
# Decoys have is_decoy=True and represent benign / non-causal movements.
CAUSE_LEDGER = [
    {
        "id": "C1_price_increase_equine_otc_TX",
        "is_decoy": False,
        "mechanism": "price_increase",
        "scope": {"species": "equine", "modality": "OTC", "region": "TX"},
        "window": ["2025-01", "2025-02", "2025-03"],
        "direction": "down",
        "metric": "units",
        "magnitude_pct": -28,
        "driver_detail": {"price_change_pct": +18},
        "layer_visible": "transform",   # need unit price vs units split
        "detectable_by": ["price_vs_volume_decomposition", "region_breakdown"],
        "narrative": ("A list-price increase of ~18% on equine OTC SKUs in TX "
                      "starting 2025Q1 suppressed unit volume ~28%; revenue held "
                      "roughly flat, so the drop is volume-driven, visible only "
                      "when price and volume are separated."),
    },
    {
        "id": "C2_crosswalk_gap_ecom_canine_fleatick",
        "is_decoy": False,
        "mechanism": "lineage_crosswalk_gap",
        "scope": {"species": "canine", "family": "flea_tick",
                  "channel": "ecom_national"},
        "window": ["2025-04", "2025-05"],
        "direction": "down",
        "metric": "units",
        "magnitude_pct": -100,   # appears as a total disappearance at DP layer
        "driver_detail": {"note": "new ecom product ids not added to crosswalk; "
                                   "sales fell out of transform mapping"},
        "layer_visible": "stage_vs_transform",  # raw ecom has rows; transform drops them
        "detectable_by": ["lineage_drilldown", "channel_breakdown",
                           "raw_vs_transform_reconciliation"],
        "narrative": ("Two new e-commerce product IDs for canine flea/tick were "
                      "absent from the vendor->manufacturer crosswalk, so their "
                      "April-May 2025 sales existed in raw/stage but were dropped "
                      "at transform. The 'drop' is a pipeline mapping gap, not a "
                      "real demand loss -- only visible by reconciling raw/stage "
                      "against transform along lineage."),
    },
    {
        "id": "C3_sku_discontinue_cattle_rx_vaccine",
        "is_decoy": False,
        "mechanism": "sku_discontinuation",
        "scope": {"species": "cattle", "family": "vaccine", "modality": "Rx"},
        "window": ["2025-07", "2025-12"],
        "direction": "down",
        "metric": "units",
        "magnitude_pct": -55,
        "driver_detail": {"note": "one of two cattle Rx vaccine SKUs discontinued"},
        "layer_visible": "transform",
        "detectable_by": ["sku_breakdown", "product_family_breakdown"],
        "narrative": ("One of the two cattle Rx vaccine SKUs was discontinued from "
                      "2025Q3; family-level sales fell ~55% concentrated in the "
                      "discontinued SKU, while the surviving SKU was stable. "
                      "Visible by drilling to SKU grain."),
    },
    {
        "id": "C4_distribution_loss_retail_feline_SE",
        "is_decoy": False,
        "mechanism": "distribution_loss",
        "scope": {"species": "feline", "channel": "retail", "region": "SE"},
        "window": ["2025-09", "2025-11"],
        "direction": "down",
        "metric": "units",
        "magnitude_pct": -40,
        "driver_detail": {"note": "retail partner de-listed feline derm in SE region"},
        "layer_visible": "transform",
        "detectable_by": ["channel_breakdown", "region_breakdown"],
        "narrative": ("The retail channel de-listed feline derm_otc in the SE region "
                      "for 2025Q3-Q4, a ~40% channel-region loss; other channels and "
                      "regions for feline were unaffected. Visible by channel x region."),
    },
    {
        "id": "C5_returns_spike_ecom_swine_vaccine",
        "is_decoy": False,
        "mechanism": "returns_spike",
        "scope": {"species": "swine", "family": "vaccine", "channel": "ecom_national"},
        "window": ["2025-05", "2025-06"],
        "direction": "down",
        "metric": "net_units",
        "magnitude_pct": -22,
        "driver_detail": {"note": "cold-chain complaint drove a returns spike; "
                                   "gross sales stable but net (sales - returns) fell"},
        "layer_visible": "transform",   # need fct_returns joined to fct_sales
        "detectable_by": ["returns_decomposition", "gross_vs_net"],
        "narrative": ("A cold-chain quality complaint caused a returns spike on swine "
                      "vaccine via e-commerce in May-Jun 2025. Gross sales were flat, "
                      "but net units (sales minus returns) fell ~22%. Visible only by "
                      "decomposing gross vs net using the returns fact."),
    },
    {
        "id": "C6_stockout_ecom_canine_heartworm",
        "is_decoy": False,
        "mechanism": "stockout_fillrate",
        "scope": {"species": "canine", "family": "heartworm_rx", "channel": "ecom_national"},
        "window": ["2025-08", "2025-09"],
        "direction": "down",
        "metric": "units",
        "magnitude_pct": -33,
        "driver_detail": {"fill_rate_drop_to": 0.55},
        "layer_visible": "transform",  # need inventory/fill-rate fact
        "detectable_by": ["fillrate_decomposition", "inventory_join"],
        "narrative": ("A supply constraint dropped e-commerce fill rate on canine "
                      "heartworm Rx to ~55% in Aug-Sep 2025, causing a ~33% unit "
                      "shortfall. The demand signal (forecast/POA) was intact; the "
                      "gap is supply-side, visible by joining the fill-rate fact."),
    },
    # ---- DECOYS (benign or non-causal; agent must NOT over-attribute) ----
    {
        "id": "D1_seasonality_equine_parasiticide_winter",
        "is_decoy": True,
        "mechanism": "seasonality",
        "scope": {"species": "equine"},
        "window": ["2024-11", "2025-01"],
        "direction": "down",
        "metric": "units",
        "magnitude_pct": -20,
        "driver_detail": {"note": "expected winter seasonal dip, recurs YoY"},
        "layer_visible": "transform",
        "detectable_by": ["yoy_comparison", "seasonality_check"],
        "narrative": ("The winter dip in equine parasiticide/dewormer is seasonal and "
                      "recurs year over year; it is NOT a problem. A correct diagnosis "
                      "identifies it as seasonality via YoY comparison, not a root-cause "
                      "event."),
    },
    {
        "id": "D2_skumix_canine_derm_variants",
        "is_decoy": True,
        "mechanism": "sku_mix_shift_benign",
        "scope": {"species": "canine", "family": "derm_otc"},
        "window": ["2025-02", "2025-04"],
        "direction": "flat",
        "metric": "units",
        "magnitude_pct": 0,
        "driver_detail": {"note": "within canine derm_otc, variant 1 declines and "
                                   "variant 2 rises by an offsetting amount; family "
                                   "total flat. Looks like a drop only if you view a "
                                   "single SKU."},
        "layer_visible": "transform",
        "detectable_by": ["sku_breakdown"],
        "narrative": ("Within canine derm_otc, one SKU (variant 1) fell while its "
                      "sibling SKU (variant 2) rose by an offsetting amount; the "
                      "family total was flat. A single-SKU view sees a 'drop' that "
                      "is actually a benign within-family SKU mix shift. Correct "
                      "diagnosis: no net decline."),
    },
]


def write_ground_truth():
    payload = {
        "seed": SEED,
        "protocol_note": ("Causes and decoys were fixed before any model run. "
                          "Scoring checks localization of the structured signature, "
                          "not string similarity. Decoys must not be over-attributed."),
        "dimensions": {
            "species": SPECIES, "modality": MODALITY, "channels": CHANNELS,
            "regions": REGIONS, "months": [month_key(m) for m in MONTHS],
        },
        "causes": CAUSE_LEDGER,
    }
    with open(os.path.join(GT_DIR, "causes.json"), "w") as f:
        json.dump(payload, f, indent=2)
    return payload


# ---------------------------------------------------------------------------
# 4. EFFECT APPLICATION  (apply each cause's effect to the base signal)
# ---------------------------------------------------------------------------
def _in_window(mk, window):
    return window[0] <= mk <= window[1]


def _matches(scope, species, family, modality, channel, region):
    if "species" in scope and scope["species"] != species:
        return False
    if "family" in scope and scope["family"] != family:
        return False
    if "modality" in scope and scope["modality"] != modality:
        return False
    if "channel" in scope and scope["channel"] != channel:
        return False
    if "region" in scope and scope["region"] != region:
        return False
    return True


def sales_multiplier(species, family, modality, channel, region, mk):
    """Return (unit_mult, price_mult, returns_extra, fillrate_override,
    crosswalk_drop) from injected causes for this cell+month."""
    unit_mult = 1.0
    price_mult = 1.0
    returns_extra = 0.0
    fillrate = None
    crosswalk_drop = False
    for c in CAUSE_LEDGER:
        if not _in_window(mk, c["window"]):
            continue
        if not _matches(c["scope"], species, family, modality, channel, region):
            continue
        mech = c["mechanism"]
        if mech == "price_increase":
            price_mult *= (1 + c["driver_detail"]["price_change_pct"] / 100.0)
            unit_mult *= (1 + c["magnitude_pct"] / 100.0)
        elif mech == "sku_discontinuation":
            # applied at SKU level later; here approximate family effect
            unit_mult *= (1 + c["magnitude_pct"] / 100.0)
        elif mech == "distribution_loss":
            unit_mult *= (1 + c["magnitude_pct"] / 100.0)
        elif mech == "returns_spike":
            returns_extra += abs(c["magnitude_pct"]) / 100.0
        elif mech == "stockout_fillrate":
            fillrate = c["driver_detail"]["fill_rate_drop_to"]
            unit_mult *= (1 + c["magnitude_pct"] / 100.0)
        elif mech == "lineage_crosswalk_gap":
            crosswalk_drop = True  # rows exist in raw/stage, dropped at transform
        elif mech == "seasonality":
            pass  # handled by seasonality(); decoy is just labeled, not re-applied
        elif mech == "mix_shift_benign":
            # offsetting within species OTC; applied via family-specific tweak below
            pass
    return unit_mult, price_mult, returns_extra, fillrate, crosswalk_drop


# benign within-family SKU mix shift (D2): canine derm_otc variant 1 down,
# variant 2 up by the SAME fraction. Because the two canine derm_otc SKUs are
# forced to share an equal base volume (see SKU master construction), a
# symmetric -f / +f multiplier nets to an exactly FLAT family total. Operates
# at SKU grain, off flea_tick (no C2 collision).
D2_SHIFT_FRAC = 0.45


def sku_mix_shift_adjust(species, family, variant, mk):
    if species != "canine" or family != "derm_otc":
        return 1.0
    if not ("2025-02" <= mk <= "2025-04"):
        return 1.0
    if variant == 1:
        return 1.0 - D2_SHIFT_FRAC   # this SKU falls
    if variant == 2:
        return 1.0 + D2_SHIFT_FRAC   # sibling rises by the same fraction
    return 1.0


# ---------------------------------------------------------------------------
# 5. GENERATE RAW ROWS (heterogeneous per partner)
# ---------------------------------------------------------------------------
def generate():
    gt = write_ground_truth()

    ecom_sales = []      # snapshot-ish weekly rolled to monthly; own product ids
    retail_sales = []    # POS-style monthly
    vet_sales = []       # invoice-line transactional
    returns_rows = []    # conformed-ish returns feed (per channel)
    fillrate_rows = []   # ecom fill-rate / inventory snapshots

    for sku in SKUS:
        sp, fam, mod = sku["species"], sku["family"], sku["modality"]
        # SKU discontinuation: kill variant 2 of cattle Rx vaccine from 2025-07
        discontinued_from = None
        if sp == "cattle" and fam == "vaccine" and sku["variant"] == 2:
            discontinued_from = "2025-07"

        for mi, d in enumerate(MONTHS):
            mk = month_key(d)
            if discontinued_from and mk >= discontinued_from:
                continue  # SKU gone -> its rows vanish at SKU grain

            for ch in CHANNELS:
                ch_share = CHANNEL_SHARE[ch]
                for rg in REGIONS:
                    # ---- (1) COUNTERFACTUAL CLEAN BASELINE ----
                    # what this cell would be with NO injected cause: realistic
                    # seasonality + stable region level + trend + modest noise.
                    base = sku["base_monthly_units"] * ch_share / len(REGIONS)
                    base *= seasonality(sp, d) * trend(mi)
                    base *= sku_mix_shift_adjust(sp, fam, sku["variant"], mk)
                    region_level = 0.75 + (hash((rg, sku["sku_id"])) % 50) / 100.0
                    base *= region_level
                    base *= random.uniform(0.92, 1.08)   # ~+/-8% noise floor
                    counterfactual_units = int(round(base))

                    # ---- (2) APPLY INJECTED CAUSE AS EXACT OVERRIDE ----
                    um, pm, rex, fr, cw_drop = sales_multiplier(
                        sp, fam, mod, ch, rg, mk)
                    # units: exact multiplicative override vs the clean baseline,
                    # so the realized magnitude equals the declared magnitude_pct
                    # (e.g. exactly -28%) plus only the baseline's own noise.
                    units = max(0, int(round(counterfactual_units * um)))
                    if units == 0:
                        continue
                    price = round(sku["base_unit_price"] * pm, 2)
                    gross_rev = round(units * price, 2)

                    # returns: small baseline + injected extra
                    base_ret = units * random.uniform(0.0, 0.03)
                    ret_units = int(round(base_ret + units * rex))

                    # ---- write into the partner-specific RAW shapes ----
                    if ch == "ecom_national":
                        # e-commerce: own product id; crosswalk gap drops mapping later.
                        ecom_pid = ECOM_PID_BY_SKU[sku["sku_id"]]
                        # crosswalk gap (C2): for canine flea_tick in window, emit rows
                        # under a NEW unmapped product id so transform can't map them.
                        if cw_drop:
                            ecom_pid = "CW_NEW_" + ecom_pid[-4:]  # not in crosswalk
                        ecom_sales.append({
                            "CHEWY_PRODUCT_PART_NUMBER": ecom_pid,
                            "report_month": mk,
                            "ship_region": rg,
                            "units_sold": units,
                            "ext_sales_amt": gross_rev,
                            "unit_price": price,
                        })
                        # fill-rate / inventory snapshot (ecom only)
                        fill = fr if fr is not None else random.uniform(0.93, 1.0)
                        fillrate_rows.append({
                            "CHEWY_PRODUCT_PART_NUMBER": ecom_pid,
                            "reporting_week_month": mk,
                            "region": rg,
                            "fill_rate": round(fill, 3),
                            "on_hand_units": int(units * random.uniform(0.5, 2.0)),
                            "forecast_units": int(units / max(fill, 0.4)),
                        })
                    elif ch == "retail":
                        retail_sales.append({
                            "vendor_part_no": sku["sku_id"],
                            "period_month": mk,
                            "store_region": rg,
                            "qty": units,
                            "sales_dollars": gross_rev,
                            "avg_price": price,
                        })
                    else:  # vet_clinic invoice-line transactional
                        # emit a few invoice lines summing to units (transactional grain)
                        remaining = units
                        n_lines = max(1, units // 12)
                        for _ln in range(n_lines):
                            q = max(1, remaining // (n_lines - _ln)) if (n_lines - _ln) else remaining
                            q = min(q, remaining)
                            remaining -= q
                            vet_sales.append({
                                "TRANSACTION_PRODUCT_ID": sku["sku_id"],
                                "DATE_POSTING": mk + "-15",
                                "SHIP_STATE": rg,
                                "TRANSACTION_QUANTITY": q,
                                "TRANSACTION_UNIT_PRICE": price,
                                "TRANSACTION_EXTENDED_AMOUNT": round(q * price, 2),
                            })
                            if remaining <= 0:
                                break

                    if ret_units > 0:
                        returns_rows.append({
                            "channel": ch,
                            "product_ref": (ECOM_PID_BY_SKU[sku["sku_id"]]
                                            if ch == "ecom_national" else sku["sku_id"]),
                            "return_month": mk,
                            "region": rg,
                            "return_units": ret_units,
                            "return_amount": round(ret_units * price, 2),
                        })

    # ---- write CSVs ----
    def dump(name, rows):
        if not rows:
            rows = [{}]
        path = os.path.join(DATA_DIR, name)
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return len(rows)

    counts = {
        "raw_ecom_sales.csv": dump("raw_ecom_sales.csv", ecom_sales),
        "raw_ecom_fillrate.csv": dump("raw_ecom_fillrate.csv", fillrate_rows),
        "raw_retail_sales.csv": dump("raw_retail_sales.csv", retail_sales),
        "raw_vetclinic_sales.csv": dump("raw_vetclinic_sales.csv", vet_sales),
        "raw_returns.csv": dump("raw_returns.csv", returns_rows),
    }
    # also dump SKU + crosswalk masters (used by transform)
    dump("master_sku.csv", [
        {"sku_id": s["sku_id"], "species": s["species"], "family": s["family"],
         "modality": s["modality"], "variant": s["variant"],
         "base_unit_price": s["base_unit_price"]} for s in SKUS])
    dump("master_crosswalk.csv",
         [{"ecom_pid": k, "sku_id": v} for k, v in ECOM_CROSSWALK.items()])

    summary = {"seed": SEED, "row_counts": counts,
               "n_skus": len(SKUS), "n_causes": len(CAUSE_LEDGER)}
    with open(os.path.join(DATA_DIR, "_generation_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    return summary


if __name__ == "__main__":
    s = generate()
    print(json.dumps(s, indent=2))
