-- =====================================================================
-- SQL-03: Repeat Escalation Detection
--
-- Business question: which customers had more than two flagged
-- (Medium/High risk) interactions inside any rolling 7-day window? For
-- each, report the first and last flag date in that qualifying window.
--
-- Assumptions:
--   * "Flagged" = Medium or High risk tier (Low risk needs no audit,
--     per the risk-tier table in the brief, so it can't count toward a
--     repeat-escalation pattern).
--   * Interactions with no customer_id (missing in source data) are
--     excluded - there's nothing to group repeats by.
--   * This is the same business rule the risk-scoring engine applies
--     as the MOD-REPEAT modifier (+15 points) - see
--     src/etl_pipeline.py::apply_repeat_escalation(). On the sample
--     5,000-row dataset supplied with this project, customer
--     interaction timestamps happen to be spread widely enough that
--     this query legitimately returns zero rows; see
--     tests/test_repeat_escalation.py for a synthetic positive-case
--     proof that the logic itself is correct.
--
-- SQL features used: self-join over a date range, HAVING, aggregation.
--
-- Portability: datetime(a.interaction_ts, '-6 days') is SQLite syntax.
-- On PostgreSQL 15+, replace with (a.interaction_ts - INTERVAL '6 days').
-- =====================================================================
WITH flagged AS (
    SELECT
        i.customer_id      AS customer_id,
        i.interaction_id    AS interaction_id,
        i.interaction_ts      AS interaction_ts
    FROM interactions i
    JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
    WHERE rs.risk_tier IN ('Medium', 'High')
      AND i.customer_id IS NOT NULL
),
window_counts AS (
    -- self-join: for every flagged interaction ("anchor"), count how
    -- many flagged interactions of the SAME customer fall in the
    -- trailing 7-day window ending on the anchor's own timestamp
    SELECT
        a.customer_id                AS customer_id,
        a.interaction_id              AS anchor_interaction_id,
        a.interaction_ts               AS anchor_ts,
        COUNT(*)                         AS interactions_in_window
    FROM flagged a
    JOIN flagged b
        ON b.customer_id = a.customer_id
       AND b.interaction_ts BETWEEN datetime(a.interaction_ts, '-6 days') AND a.interaction_ts
    GROUP BY a.customer_id, a.interaction_id, a.interaction_ts
)
SELECT
    customer_id,
    MIN(anchor_ts)                              AS first_flag_date,
    MAX(anchor_ts)                              AS last_flag_date,
    COUNT(*)                                       AS anchors_exceeding_threshold,
    MAX(interactions_in_window)                       AS max_interactions_in_any_7day_window
FROM window_counts
WHERE interactions_in_window > 2
GROUP BY customer_id
ORDER BY max_interactions_in_any_7day_window DESC, customer_id;
