-- =====================================================================
-- SQL-12: Customer Risk Trajectory
--
-- Business question: for each customer, what was their FIRST risk
-- score, their LATEST risk score, and the net movement between the
-- two - keeping only customers whose risk has genuinely worsened?
--
-- Assumptions:
--   * "First" and "latest" are ordered by interaction_ts.
--   * Customers with only a single interaction are excluded - there is
--     no trajectory to measure with one data point.
--   * customer_id IS NULL rows are excluded (nothing to group by).
--
-- SQL features used: ROW_NUMBER / FIRST_VALUE / LAG-style window
-- functions, filtering on a derived table.
-- =====================================================================
WITH customer_history AS (
    SELECT
        i.customer_id                                                       AS customer_id,
        i.interaction_id                                                    AS interaction_id,
        i.interaction_ts                                                    AS interaction_ts,
        rs.total_score                                                      AS total_score,
        ROW_NUMBER() OVER (PARTITION BY i.customer_id ORDER BY i.interaction_ts ASC)  AS rn_asc,
        ROW_NUMBER() OVER (PARTITION BY i.customer_id ORDER BY i.interaction_ts DESC) AS rn_desc,
        COUNT(*)     OVER (PARTITION BY i.customer_id)                                AS interaction_count,
        FIRST_VALUE(rs.total_score) OVER (
            PARTITION BY i.customer_id ORDER BY i.interaction_ts ASC
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        )                                                                              AS first_score,
        FIRST_VALUE(rs.total_score) OVER (
            PARTITION BY i.customer_id ORDER BY i.interaction_ts DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        )                                                                              AS latest_score,
        FIRST_VALUE(i.interaction_ts) OVER (
            PARTITION BY i.customer_id ORDER BY i.interaction_ts ASC
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        )                                                                              AS first_interaction_ts,
        FIRST_VALUE(i.interaction_ts) OVER (
            PARTITION BY i.customer_id ORDER BY i.interaction_ts DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        )                                                                              AS latest_interaction_ts
    FROM interactions i
    JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
    WHERE i.customer_id IS NOT NULL
),
trajectory AS (
    SELECT DISTINCT
        customer_id,
        interaction_count,
        first_interaction_ts,
        first_score,
        latest_interaction_ts,
        latest_score,
        (latest_score - first_score) AS net_movement
    FROM customer_history
    WHERE interaction_count > 1
)
SELECT
    customer_id,
    interaction_count,
    first_interaction_ts,
    first_score,
    latest_interaction_ts,
    latest_score,
    net_movement
FROM trajectory
WHERE net_movement > 0
ORDER BY net_movement DESC, customer_id;
