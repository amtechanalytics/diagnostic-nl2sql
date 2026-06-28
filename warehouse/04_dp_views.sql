-- =====================================================================
-- 04_dp_views.sql  (DuckDB dialect)
-- DATA PRODUCT (DP) LAYER: aggregated, business-facing VIEWS on top of the
-- transform star. This is the surface the commercial-excellence consumer
-- (and the NAIVE prompt arm) sees. Lineage is abstracted away here.
--
-- The diagnostic trap: a metric MOVES at this layer, but its CAUSE often
-- lives below it -- in a price/volume split, a returns join, a fill-rate
-- join, a sku-level breakdown, or (worst) a stage-vs-transform mapping gap
-- that the DP view, by construction, cannot reveal.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS dp;

-- ---- DP1: sales by species x channel x month (the headline product) --
CREATE OR REPLACE VIEW dp.sales_by_species_channel_month AS
SELECT
    p.species,
    s.channel,
    s.month,
    d.quarter,
    SUM(s.units)    AS units,
    SUM(s.revenue)  AS revenue
FROM xform.fct_sales s
JOIN xform.dim_product p ON s.sku_id = p.sku_id
JOIN xform.dim_date    d ON s.month  = d.month
GROUP BY 1,2,3,4;

-- ---- DP2: OTC vs Rx performance by species x region x quarter --------
CREATE OR REPLACE VIEW dp.otc_rx_performance AS
SELECT
    p.species,
    p.modality,
    s.region,
    d.quarter,
    SUM(s.units)    AS units,
    SUM(s.revenue)  AS revenue,
    -- blended price at this grain (a tempting but lossy DP-level signal)
    CASE WHEN SUM(s.units) > 0 THEN SUM(s.revenue)/SUM(s.units) ELSE NULL END
                    AS blended_price
FROM xform.fct_sales s
JOIN xform.dim_product p ON s.sku_id = p.sku_id
JOIN xform.dim_date    d ON s.month  = d.month
GROUP BY 1,2,3,4;

-- ---- DP3: net sales (gross minus returns) by species x channel x qtr -
-- Pre-joins returns so the consumer sees NET; but the gross-vs-net split
-- (needed to diagnose a returns spike, C5) is collapsed here.
CREATE OR REPLACE VIEW dp.net_sales_by_species_channel_quarter AS
SELECT
    p.species,
    s.channel,
    d.quarter,
    SUM(s.units)                              AS gross_units,
    COALESCE(SUM(r.return_units), 0)          AS return_units,
    SUM(s.units) - COALESCE(SUM(r.return_units),0) AS net_units,
    SUM(s.revenue)                            AS gross_revenue
FROM xform.fct_sales s
JOIN xform.dim_product p ON s.sku_id = p.sku_id
JOIN xform.dim_date    d ON s.month  = d.month
LEFT JOIN xform.fct_returns r
       ON s.sku_id=r.sku_id AND s.month=r.month
      AND s.region=r.region AND s.channel=r.channel
GROUP BY 1,2,3;

-- ---- DP4: region performance by species x quarter --------------------
CREATE OR REPLACE VIEW dp.region_performance AS
SELECT
    p.species,
    s.region,
    rg.region_name,
    d.quarter,
    SUM(s.units)    AS units,
    SUM(s.revenue)  AS revenue
FROM xform.fct_sales s
JOIN xform.dim_product p  ON s.sku_id = p.sku_id
JOIN xform.dim_date    d  ON s.month  = d.month
JOIN xform.dim_region  rg ON s.region = rg.region
GROUP BY 1,2,3,4;
