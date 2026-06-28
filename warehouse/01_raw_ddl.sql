-- =====================================================================
-- 01_raw_ddl.sql  (DuckDB dialect)
-- RAW LAYER: per-partner, schema-as-delivered. Deliberately heterogeneous.
-- No constraints enforced (mirrors Snowflake/DuckDB warehouse semantics).
-- Loaded directly from the generator's CSVs in warehouse/_data/.
--
-- Lineage note: each partner delivers a DIFFERENT shape (column names,
-- grain, product-id vocabulary). Conforming these is the raw->stage job.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS raw;

-- ---- E-COMMERCE PARTNER (national, ~50% of volume) -------------------
-- Monthly snapshot grain. Uses the PARTNER'S OWN product part numbers
-- (CHEWY_PRODUCT_PART_NUMBER), NOT the manufacturer SKU. Mapping to the
-- manufacturer SKU happens at transform via the crosswalk -- and a gap in
-- that crosswalk is injected cause C2.
CREATE OR REPLACE TABLE raw.ecom_sales (
    CHEWY_PRODUCT_PART_NUMBER  VARCHAR,   -- partner product id (not mfr sku)
    report_month               VARCHAR,   -- 'YYYY-MM'
    ship_region                VARCHAR,
    units_sold                 INTEGER,
    ext_sales_amt              DOUBLE,
    unit_price                 DOUBLE
);

-- E-commerce fill-rate / inventory snapshot (only this partner shares it).
-- Demand-vs-supply signal: forecast_units vs fill_rate. Injected cause C6
-- (stockout) lives here.
CREATE OR REPLACE TABLE raw.ecom_fillrate (
    CHEWY_PRODUCT_PART_NUMBER  VARCHAR,
    reporting_week_month       VARCHAR,   -- 'YYYY-MM' (rolled from weekly)
    region                     VARCHAR,
    fill_rate                  DOUBLE,    -- 0..1
    on_hand_units              INTEGER,
    forecast_units             INTEGER
);

-- ---- RETAIL PARTNER (~30%) ------------------------------------------
-- Monthly POS grain. Uses the manufacturer/vendor part number directly.
CREATE OR REPLACE TABLE raw.retail_sales (
    vendor_part_no             VARCHAR,   -- = manufacturer sku_id
    period_month               VARCHAR,   -- 'YYYY-MM'
    store_region               VARCHAR,
    qty                        INTEGER,
    sales_dollars              DOUBLE,
    avg_price                  DOUBLE
);

-- ---- VET-CLINIC / DISTRIBUTOR PARTNER (~20%) ------------------------
-- TRANSACTIONAL invoice-line grain (day-level, multiple lines per
-- product/region/month). Must be AGGREGATED to monthly at stage --
-- a real conformance step the lineage-aware arm can reason about.
CREATE OR REPLACE TABLE raw.vetclinic_sales (
    TRANSACTION_PRODUCT_ID       VARCHAR, -- = manufacturer sku_id
    DATE_POSTING                 VARCHAR, -- 'YYYY-MM-DD'
    SHIP_STATE                   VARCHAR, -- region code
    TRANSACTION_QUANTITY         INTEGER,
    TRANSACTION_UNIT_PRICE       DOUBLE,
    TRANSACTION_EXTENDED_AMOUNT  DOUBLE
);

-- ---- RETURNS (multi-channel) ----------------------------------------
-- Cross-channel returns feed. product_ref is the PARTNER id for ecom rows
-- and the manufacturer sku_id for retail/vet rows -- another conforming
-- challenge. Injected cause C5 (returns spike) lives here.
CREATE OR REPLACE TABLE raw.returns (
    channel        VARCHAR,
    product_ref    VARCHAR,   -- ecom_pid for ecom, sku_id otherwise
    return_month   VARCHAR,
    region         VARCHAR,
    return_units   INTEGER,
    return_amount  DOUBLE
);

-- ---- MASTERS (reference data used at transform) ---------------------
CREATE OR REPLACE TABLE raw.master_sku (
    sku_id          VARCHAR,
    species         VARCHAR,
    family          VARCHAR,
    modality        VARCHAR,   -- Rx / OTC
    variant         INTEGER,
    base_unit_price DOUBLE
);

-- Vendor(ecom) <-> manufacturer crosswalk. Intentionally does NOT contain
-- the 'CW_NEW_*' ids emitted during C2's window -> those ecom rows cannot
-- be mapped and fall out at transform (the lineage gap).
CREATE OR REPLACE TABLE raw.master_crosswalk (
    ecom_pid   VARCHAR,
    sku_id     VARCHAR
);

-- ---- LOADERS (DuckDB read_csv_auto) ---------------------------------
-- Paths are relative to warehouse/. Adjust if running from elsewhere.
COPY raw.ecom_sales        FROM '_data/raw_ecom_sales.csv'      (HEADER, AUTO_DETECT TRUE);
COPY raw.ecom_fillrate     FROM '_data/raw_ecom_fillrate.csv'   (HEADER, AUTO_DETECT TRUE);
COPY raw.retail_sales      FROM '_data/raw_retail_sales.csv'    (HEADER, AUTO_DETECT TRUE);
COPY raw.vetclinic_sales   FROM '_data/raw_vetclinic_sales.csv' (HEADER, AUTO_DETECT TRUE);
COPY raw.returns           FROM '_data/raw_returns.csv'         (HEADER, AUTO_DETECT TRUE);
COPY raw.master_sku        FROM '_data/master_sku.csv'          (HEADER, AUTO_DETECT TRUE);
COPY raw.master_crosswalk  FROM '_data/master_crosswalk.csv'    (HEADER, AUTO_DETECT TRUE);
