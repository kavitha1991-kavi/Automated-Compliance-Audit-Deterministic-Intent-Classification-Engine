-- =====================================================================
-- SQL-09: Agent Exposure Report
--
-- Business question: for agents handling a meaningful volume of
-- interactions, what is their High-risk rate, and which of them sit
-- above the overall company average (i.e. deserve coaching / review
-- attention)?
--
-- Assumptions:
--   * Minimum volume threshold = 10 interactions (HAVING clause) - below
--     that, a single bad interaction would swing an agent's rate
--     wildly and isn't a fair signal.
--   * agent_id = 'AGT-UNKNOWN' (the ETL's placeholder for a missing
--     source agent_id) is excluded - it isn't a real agent to coach.
--   * "Company average" is the simple average of the per-agent rates
--     already filtered to the minimum-volume cohort, not a global
--     interaction-weighted rate - this matches how a team lead
--     typically benchmarks "the average agent", not "the average
--     interaction".
--
-- SQL features used: GROUP BY with HAVING, subquery (company average),
-- NULL/placeholder handling.
-- =====================================================================
WITH agent_stats AS (
    SELECT
        i.agent_id                                                          AS agent_id,
        COUNT(*)                                                            AS total_interactions,
        SUM(CASE WHEN rs.risk_tier = 'High' THEN 1 ELSE 0 END)              AS high_risk_count,
        ROUND(
            100.0 * SUM(CASE WHEN rs.risk_tier = 'High' THEN 1 ELSE 0 END)
            / COUNT(*),
            2
        )                                                                    AS high_risk_rate_pct
    FROM interactions i
    JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
    WHERE i.agent_id IS NOT NULL
      AND i.agent_id <> 'AGT-UNKNOWN'
    GROUP BY i.agent_id
    HAVING COUNT(*) >= 10
),
company_avg AS (
    SELECT ROUND(AVG(high_risk_rate_pct), 2) AS avg_rate FROM agent_stats
)
SELECT
    a.agent_id                                                          AS agent_id,
    a.total_interactions                                                AS total_interactions,
    a.high_risk_count                                                   AS high_risk_count,
    a.high_risk_rate_pct                                                AS high_risk_rate_pct,
    ca.avg_rate                                                         AS company_avg_high_risk_rate_pct,
    CASE WHEN a.high_risk_rate_pct > ca.avg_rate THEN 'Above Average' ELSE 'At/Below Average' END AS exposure_flag
FROM agent_stats a
CROSS JOIN company_avg ca
ORDER BY a.high_risk_rate_pct DESC;
