-- =====================================================================
-- SQL-02: Channel & Region High-Risk Ranking
--
-- Business question: rank the channels and regions generating the most
-- High-risk interactions on each audit date, so leadership can see the
-- single worst-performing channel and worst-performing region per day.
--
-- Assumptions:
--   * "Worst performer" = highest count of High-risk interactions that
--     day (not highest raw volume - a busy-but-low-risk channel should
--     not outrank a smaller channel that is throwing off breaches).
--   * Channel and region are ranked independently (two separate
--     dimensions), unioned into one result set tagged by dimension_type,
--     so both can be reviewed together without a second query.
--   * Days with zero High-risk interactions for a dimension value are
--     naturally excluded (nothing to rank).
--
-- SQL features used: window ranking (RANK, DENSE_RANK) with PARTITION BY.
--
-- Portability: date(i.interaction_ts) -> CAST(i.interaction_ts AS DATE)
-- on PostgreSQL 15+.
-- =====================================================================
WITH by_channel AS (
    SELECT
        date(i.interaction_ts)   AS audit_date,
        'channel'                 AS dimension_type,
        i.channel                  AS dimension_value,
        COUNT(*)                     AS high_risk_count
    FROM interactions i
    JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
    WHERE rs.risk_tier = 'High'
    GROUP BY date(i.interaction_ts), i.channel
),
by_region AS (
    SELECT
        date(i.interaction_ts)   AS audit_date,
        'region'                  AS dimension_type,
        i.region                   AS dimension_value,
        COUNT(*)                     AS high_risk_count
    FROM interactions i
    JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
    WHERE rs.risk_tier = 'High'
    GROUP BY date(i.interaction_ts), i.region
),
combined AS (
    SELECT audit_date, dimension_type, dimension_value, high_risk_count FROM by_channel
    UNION ALL
    SELECT audit_date, dimension_type, dimension_value, high_risk_count FROM by_region
)
SELECT
    audit_date,
    dimension_type,
    dimension_value,
    high_risk_count,
    RANK()       OVER (PARTITION BY audit_date, dimension_type ORDER BY high_risk_count DESC) AS rank_strict,
    DENSE_RANK() OVER (PARTITION BY audit_date, dimension_type ORDER BY high_risk_count DESC) AS rank_dense
FROM combined
ORDER BY audit_date, dimension_type, rank_dense;
