-- =====================================================================
-- SQL-08: Intent Cross-Tab (Customer Tier x Region)
--
-- Business question: how is each intent category distributed across
-- customer tiers and UK regions, in a single pivoted result set?
--
-- Assumptions:
--   * customer_tier is pivoted into columns (a fixed, known set of 4
--     tiers plus "Unknown" for missing source data); intent_category
--     and region form the row grain. This keeps the result readable as
--     a single wide table rather than a 3-dimensional cube.
--
-- SQL features used: conditional (pivot-style) aggregation, multi-level
-- grouping.
-- =====================================================================
SELECT
    rs.primary_category                                                         AS intent_category,
    i.region                                                                    AS region,
    SUM(CASE WHEN i.customer_tier = 'VIP'            THEN 1 ELSE 0 END)          AS vip_count,
    SUM(CASE WHEN i.customer_tier = 'Enterprise'     THEN 1 ELSE 0 END)          AS enterprise_count,
    SUM(CASE WHEN i.customer_tier = 'Small Business' THEN 1 ELSE 0 END)          AS small_business_count,
    SUM(CASE WHEN i.customer_tier = 'Standard'       THEN 1 ELSE 0 END)          AS standard_count,
    SUM(CASE WHEN i.customer_tier = 'Unknown'        THEN 1 ELSE 0 END)          AS unknown_tier_count,
    COUNT(*)                                                                     AS total_interactions
FROM interactions i
JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
GROUP BY rs.primary_category, i.region
ORDER BY intent_category, region;
