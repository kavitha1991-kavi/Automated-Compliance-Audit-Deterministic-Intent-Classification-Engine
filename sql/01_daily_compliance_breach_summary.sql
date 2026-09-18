-- =====================================================================
-- SQL-01: Daily Compliance Breach Summary
--
-- Business question: for each audit date, channel and intent category,
-- what is the total interaction count, high-risk ("breach") count,
-- high-risk percentage and average risk score?
--
-- Assumptions:
--   * "Breach" = an interaction scored into the High risk tier.
--   * A date with zero interactions for a given channel/category simply
--     has no row here (this is a breakdown of activity that happened,
--     not a padded calendar - see SQL-07 for a calendar-filled trend).
--
-- SQL features used: CTE, GROUP BY, conditional aggregation,
-- division-safe rounding (NULLIF), date casting.
--
-- Portability: date(i.interaction_ts) is SQLite's date-truncation
-- function. On PostgreSQL 15+, replace it with
-- CAST(i.interaction_ts AS DATE) in both the CTE and the GROUP BY.
--
-- Reusability: this exact logic is also exposed as the view
-- v_daily_compliance_summary (see sql/00_schema_ddl.sql) and is the
-- data source for the Executive Summary dashboard.
-- =====================================================================
WITH daily AS (
    SELECT
        date(i.interaction_ts)   AS audit_date,
        i.channel                AS channel,
        rs.primary_category      AS intent_category,
        rs.risk_tier              AS risk_tier,
        rs.total_score               AS total_score
    FROM interactions i
    JOIN risk_scores rs
        ON rs.interaction_id = i.interaction_id
)
SELECT
    audit_date,
    channel,
    intent_category,
    COUNT(*)                                                              AS total_interactions,
    SUM(CASE WHEN risk_tier = 'High' THEN 1 ELSE 0 END)                   AS high_risk_count,
    ROUND(
        100.0 * SUM(CASE WHEN risk_tier = 'High' THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0),
        2
    )                                                                      AS high_risk_pct,
    ROUND(AVG(total_score), 2)                                            AS avg_risk_score
FROM daily
GROUP BY audit_date, channel, intent_category
ORDER BY audit_date, channel, intent_category;
