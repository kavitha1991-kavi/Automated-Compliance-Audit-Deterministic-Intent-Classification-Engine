-- =====================================================================
-- SQL-06: Rule Effectiveness Report
--
-- Business question: how often does each compliance rule fire, what
-- share of the total risk points generated does it account for, and
-- which active rules never fired at all (so governance can review
-- whether they're still needed, mistyped, or genuinely rare)?
--
-- Assumptions:
--   * CAT-GENERAL (the fallback "General Enquiry" category) is expected
--     to show zero fires by design: it is assigned whenever NO taxonomy
--     keyword matches, so it never appears in rule_matches (which only
--     logs actual keyword hits). This is not a broken rule.
--
-- SQL features used: LEFT JOIN, aggregation, percentage of total using
-- a window SUM.
--
-- Reusability: this exact logic is also exposed as the view
-- v_rule_effectiveness (see sql/00_schema_ddl.sql).
-- =====================================================================
SELECT
    r.rule_id                                                           AS rule_id,
    r.rule_type                                                         AS rule_type,
    r.category_or_name                                                  AS category_or_name,
    r.active                                                            AS active,
    COUNT(rm.interaction_id)                                            AS times_fired,
    COALESCE(SUM(rm.score_contribution), 0)                             AS total_points_generated,
    ROUND(
        100.0 * COALESCE(SUM(rm.score_contribution), 0)
        / NULLIF(SUM(SUM(rm.score_contribution)) OVER (), 0),
        2
    )                                                                    AS pct_of_total_points_generated,
    CASE WHEN COUNT(rm.interaction_id) = 0 THEN 'NEVER FIRED' ELSE 'active' END AS coverage_flag
FROM rules r
LEFT JOIN rule_matches rm ON rm.rule_id = r.rule_id
GROUP BY r.rule_id, r.rule_type, r.category_or_name, r.active
ORDER BY times_fired DESC, r.rule_id;
