-- =====================================================================
-- SQL-05: Operational Auditor Work Queue
--
-- Business question: give the shift a prioritised list of unreviewed
-- flags - risk tier first, then score, then how long it's been
-- waiting - so the most urgent items are worked first.
--
-- Assumptions:
--   * "Unreviewed" = flag_actions.reviewed = FALSE (no action_ts yet).
--   * Ageing is measured against the current time of the query, since
--     that's genuinely how old the item is right now.
--   * "Top items for the shift" is implemented as a LIMIT; 50 is a
--     reasonable single-shift batch size for a compliance team and is
--     trivially adjustable.
--
-- SQL features used: multi-key ORDER BY, filtering, joins, LIMIT.
--
-- Reusability: this exact logic is also exposed as the view
-- v_auditor_work_queue (see sql/00_schema_ddl.sql) and is the data
-- source for the Operational Auditor Work Queue dashboard panel.
--
-- Portability: julianday('now') is SQLite syntax. On PostgreSQL 15+,
-- replace age_days with EXTRACT(DAY FROM (now() - fa.flagged_ts)).
-- =====================================================================
SELECT
    i.interaction_id                                                    AS interaction_id,
    i.customer_id                                                       AS customer_id,
    i.channel                                                           AS channel,
    i.region                                                            AS region,
    rs.primary_category                                                 AS intent_category,
    rs.total_score                                                      AS total_score,
    rs.risk_tier                                                        AS risk_tier,
    fa.priority                                                         AS priority,
    fa.flagged_ts                                                       AS flagged_ts,
    CAST(julianday('now') - julianday(fa.flagged_ts) AS INTEGER)        AS age_days
FROM flag_actions fa
JOIN interactions i ON i.interaction_id = fa.interaction_id
JOIN risk_scores rs ON rs.interaction_id = fa.interaction_id
WHERE fa.reviewed = FALSE
ORDER BY
    CASE rs.risk_tier WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END,
    rs.total_score DESC,
    age_days DESC
LIMIT 50;

-- =====================================================================
-- Performance note (brief Section 4.3 "Performance awareness")
--
-- EXPLAIN QUERY PLAN output captured against the loaded sample dataset:
--
--   SEARCH fa USING INDEX idx_flag_actions_reviewed (reviewed=?)
--   SEARCH i  USING INDEX sqlite_autoindex_interactions_1 (interaction_id=?)
--   SEARCH rs USING INDEX sqlite_autoindex_risk_scores_1 (interaction_id=?)
--   USE TEMP B-TREE FOR ORDER BY
--
-- Indexing decision: idx_flag_actions_reviewed lets SQLite seek
-- straight to the (typically small) set of unreviewed rows instead of
-- scanning all of flag_actions; both joins use the tables' own primary
-- key indexes automatically. The final sort still needs a temp B-tree
-- because the risk_tier ordering is a CASE expression, not a raw
-- column value, so no index can pre-sort it. A composite index on
-- (reviewed, risk_tier, total_score) could remove that sort - but with
-- only a few hundred unreviewed rows at any time and a LIMIT 50, the
-- sort is negligible, so the extra write-time cost of maintaining that
-- index on every flag_actions update wasn't judged worth it. This is a
-- deliberate "don't index" decision, not an oversight.
-- =====================================================================

