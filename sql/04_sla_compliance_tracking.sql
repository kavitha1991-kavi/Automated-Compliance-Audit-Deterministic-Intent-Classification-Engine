-- =====================================================================
-- SQL-04: SLA Compliance Tracking
--
-- Business question: what percentage of Priority 1 flags were actioned
-- within 2 hours, and Priority 2 flags within 48 hours, reported by
-- week?
--
-- Assumptions:
--   * Only reviewed flags (flag_actions.reviewed = TRUE, i.e. they have
--     an action_ts) can be judged against the SLA; unreviewed flags are
--     still open and belong in the auditor work queue (SQL-05), not in
--     a completed-SLA percentage.
--   * "Week" = the ISO-ish week the flag was RAISED (flagged_ts), so a
--     flag raised late on a Friday and actioned the following Monday is
--     reported against the week it was raised, matching how a
--     compliance team would review "how did we do on flags raised this
--     week".
--   * flag_actions.flagged_ts and .action_ts are synthesized by the ETL
--     (src/etl_pipeline.py::_synthesize_flag_action) since the source
--     brief provides no ground-truth review timestamps - see README
--     Design Decisions.
--
-- SQL features used: timestamp arithmetic, CASE logic, weekly grouping.
--
-- Portability: strftime('%Y-W%W', ...) and julianday(...) are SQLite
-- functions. On PostgreSQL 15+, replace the week label with
-- to_char(flagged_ts, 'IYYY-"W"IW') and the hours calculation with
-- EXTRACT(EPOCH FROM (action_ts - flagged_ts)) / 3600.
-- =====================================================================
WITH actioned AS (
    SELECT
        fa.interaction_id                                               AS interaction_id,
        fa.priority                                                     AS priority,
        fa.flagged_ts                                                   AS flagged_ts,
        fa.action_ts                                                    AS action_ts,
        CASE fa.priority
            WHEN 'Priority 1 (< 2 Hours)' THEN 2
            WHEN 'Priority 2 (48 Hours)'  THEN 48
        END                                                              AS sla_hours,
        (julianday(fa.action_ts) - julianday(fa.flagged_ts)) * 24        AS hours_to_action
    FROM flag_actions fa
    WHERE fa.reviewed = TRUE
)
SELECT
    strftime('%Y-W%W', flagged_ts)                                      AS iso_week,
    priority,
    COUNT(*)                                                            AS actioned_count,
    SUM(CASE WHEN hours_to_action <= sla_hours THEN 1 ELSE 0 END)       AS within_sla_count,
    ROUND(
        100.0 * SUM(CASE WHEN hours_to_action <= sla_hours THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0),
        2
    )                                                                    AS sla_compliance_pct,
    ROUND(AVG(hours_to_action), 2)                                      AS avg_hours_to_action
FROM actioned
GROUP BY iso_week, priority
ORDER BY iso_week, priority;
