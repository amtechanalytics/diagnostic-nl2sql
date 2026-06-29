-- =====================================================================
-- 03_transform_ddl.sql  (DuckDB dialect)
-- TRANSFORM LAYER: consolidated, conformed STAR SCHEMA across all partners.
-- This is where channels UNIFY and where the ecom crosswalk RESOLVES.
--
-- CRITICAL lineage behavior (injected cause C2):
--   ecom stage rows are mapped to manufacturer sku via master_crosswalk.
--   The 'CW_NEW_*' product ids emitted during C2's window are NOT in the
--   crosswalk, so an INNER JOIN drops them here. They exist in raw & stage
--   but vanish at transform -> the "drop" is a pipeline mapping gap, only
--   diagnosable by reconciling stage vs transform along lineage.
--
-- No FK constraints (warehouse semantics). Dimensions are conformed.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS xform;

-- ---- DIMENSIONS ------------------------------------------------------
CREATE OR REPLACE TABLE xform.dim_product AS
SELECT
    sku_id,
    species,
    family,
    modality,                 -- Rx / OTC
    variant,
    base_unit_price
FROM raw.master_sku;

CREATE OR REPLACE TABLE xform.dim_channel AS
SELECT * FROM (VALUES
    ('ecom_national', 'National E-Commerce', 0.50),
    ('retail',        'Retail',              0.30),
    ('vet_clinic',    'Vet Clinic / Distributor', 0.20)
) AS t(channel, channel_name, target_share);

CREATE OR REPLACE TABLE xform.dim_region AS
SELECT * FROM (VALUES
    ('NE','Northeast'),('MA','Mid-Atlantic'),('SE','Southeast'),
    ('MW','Midwest'),('TX','Texas'),('MTN','Mountain'),
    ('PSW','Pacific Southwest'),('PNW','Pacific Northwest')
) AS t(region, region_name);

CREATE OR REPLACE TABLE xform.dim_date AS
SELECT DISTINCT
    month,
    CAST(substr(month,1,4) AS INTEGER)                              AS year,
    CAST(substr(month,6,2) AS INTEGER)                              AS month_num,
    substr(month,1,4) || 'Q' ||
        CAST((CAST(substr(month,6,2) AS INTEGER)-1) // 3 + 1 AS VARCHAR) AS quarter
FROM (
    SELECT month FROM stage.retail_sales
    UNION SELECT month FROM stage.ecom_sales
    UNION SELECT month FROM stage.vetclinic_sales
);

-- ---- FACT: SALES (consolidated, sku-resolved) ------------------------
-- Build a partner-unioned sales stream where ecom is mapped to sku via the
-- crosswalk (INNER JOIN -> unmapped C2 rows drop), retail/vet already use sku.
CREATE OR REPLACE TABLE xform.fct_sales AS
WITH ecom_mapped AS (
    SELECT
        cw.sku_id        AS sku_id,
        e.month          AS month,
        e.region         AS region,
        e.channel        AS channel,
        e.units          AS units,
        e.revenue        AS revenue
    FROM stage.ecom_sales e
    JOIN raw.master_crosswalk cw      -- INNER JOIN: unmapped (C2) rows excluded
      ON e.product_key = cw.ecom_pid
),
retail_mapped AS (
    SELECT product_key AS sku_id, month, region, channel, units, revenue
    FROM stage.retail_sales
),
vet_mapped AS (
    SELECT product_key AS sku_id, month, region, channel, units, revenue
    FROM stage.vetclinic_sales
),
unioned AS (
    SELECT * FROM ecom_mapped
    UNION ALL SELECT * FROM retail_mapped
    UNION ALL SELECT * FROM vet_mapped
)
SELECT
    u.sku_id, u.month, u.region, u.channel,
    SUM(u.units)    AS units,
    SUM(u.revenue)  AS revenue
FROM unioned u
GROUP BY 1,2,3,4;

-- ---- FACT: RETURNS (sku-resolved across channels) --------------------
-- ecom returns carry partner ids -> map via crosswalk; others already sku.
CREATE OR REPLACE TABLE xform.fct_returns AS
WITH ecom_ret AS (
    SELECT cw.sku_id AS sku_id, r.month, r.region, r.channel,
           r.return_units, r.return_amount
    FROM stage.returns r
    JOIN raw.master_crosswalk cw ON r.product_key = cw.ecom_pid
    WHERE r.channel = 'ecom_national'
),
other_ret AS (
    SELECT product_key AS sku_id, month, region, channel,
           return_units, return_amount
    FROM stage.returns
    WHERE channel <> 'ecom_national'
),
unioned AS (
    SELECT * FROM ecom_ret UNION ALL SELECT * FROM other_ret
)
SELECT sku_id, month, region, channel,
       SUM(return_units)  AS return_units,
       SUM(return_amount) AS return_amount
FROM unioned
GROUP BY 1,2,3,4;

-- ---- FACT: INVENTORY / FILL-RATE (ecom only, sku-resolved) -----------
CREATE OR REPLACE TABLE xform.fct_inventory_fillrate AS
SELECT
    cw.sku_id          AS sku_id,
    f.month            AS month,
    f.region           AS region,
    'ecom_national'    AS channel,
    AVG(f.fill_rate)   AS fill_rate,
    SUM(f.on_hand_units)   AS on_hand_units,
    SUM(f.forecast_units)  AS forecast_units
FROM stage.ecom_fillrate f
JOIN raw.master_crosswalk cw ON f.product_key = cw.ecom_pid
GROUP BY 1,2,3,4;
