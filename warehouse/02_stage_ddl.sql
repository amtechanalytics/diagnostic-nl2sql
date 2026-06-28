-- =====================================================================
-- 02_stage_ddl.sql  (DuckDB dialect)
-- STAGE LAYER: per-partner, cleaned and conformed to a COMMON column
-- vocabulary, but still at per-partner grain. One stage table per partner.
--
-- Key conformance work done here:
--   * vet-clinic: AGGREGATE transactional invoice lines -> monthly grain
--   * all: rename partner-specific columns to a shared vocabulary
--          (product_key, month, region, units, revenue, price)
--   * ecom: product_key stays the PARTNER id (mapping deferred to transform)
--   * note: stage does NOT resolve the crosswalk -> C2 rows still present here
--           (they exist in raw & stage, and only disappear at transform).
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS stage;

-- ---- E-COMMERCE: snapshot already monthly; conform names only --------
CREATE OR REPLACE TABLE stage.ecom_sales AS
SELECT
    CHEWY_PRODUCT_PART_NUMBER          AS product_key,   -- still partner id
    report_month                       AS month,
    ship_region                        AS region,
    'ecom_national'                    AS channel,
    units_sold                         AS units,
    ext_sales_amt                      AS revenue,
    unit_price                         AS price
FROM raw.ecom_sales;

-- ---- E-COMMERCE fill-rate: conform names -----------------------------
CREATE OR REPLACE TABLE stage.ecom_fillrate AS
SELECT
    CHEWY_PRODUCT_PART_NUMBER          AS product_key,
    reporting_week_month               AS month,
    region                             AS region,
    fill_rate                          AS fill_rate,
    on_hand_units                      AS on_hand_units,
    forecast_units                     AS forecast_units
FROM raw.ecom_fillrate;

-- ---- RETAIL: already monthly; uses manufacturer sku directly ---------
CREATE OR REPLACE TABLE stage.retail_sales AS
SELECT
    vendor_part_no                     AS product_key,   -- = sku_id
    period_month                       AS month,
    store_region                       AS region,
    'retail'                           AS channel,
    qty                                AS units,
    sales_dollars                      AS revenue,
    avg_price                          AS price
FROM raw.retail_sales;

-- ---- VET-CLINIC: AGGREGATE transactional lines -> monthly ------------
-- This is the non-trivial conformance step: invoice-line -> month/region.
CREATE OR REPLACE TABLE stage.vetclinic_sales AS
SELECT
    TRANSACTION_PRODUCT_ID                         AS product_key,  -- = sku_id
    strftime(CAST(DATE_POSTING AS DATE), '%Y-%m')  AS month,
    SHIP_STATE                                     AS region,
    'vet_clinic'                                   AS channel,
    SUM(TRANSACTION_QUANTITY)                       AS units,
    SUM(TRANSACTION_EXTENDED_AMOUNT)                AS revenue,
    -- volume-weighted average price
    CASE WHEN SUM(TRANSACTION_QUANTITY) > 0
         THEN SUM(TRANSACTION_EXTENDED_AMOUNT) / SUM(TRANSACTION_QUANTITY)
         ELSE NULL END                              AS price
FROM raw.vetclinic_sales
GROUP BY 1, 2, 3, 4;

-- ---- RETURNS: conform; keep product_ref + channel for later mapping --
CREATE OR REPLACE TABLE stage.returns AS
SELECT
    product_ref                        AS product_key,   -- ecom_pid OR sku_id
    return_month                       AS month,
    region                             AS region,
    channel                            AS channel,
    return_units                       AS return_units,
    return_amount                      AS return_amount
FROM raw.returns;
