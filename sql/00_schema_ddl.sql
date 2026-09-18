-- =====================================================================
-- Automated Compliance Audit & Intent Classification Engine
-- Master schema creation script
-- Builds every object from an empty database. Executes unmodified on
-- SQLite 3.35+ and PostgreSQL 15+.
--
-- Design notes (see README "Design Decisions" for the full rationale):
--   * No AUTOINCREMENT/SERIAL surrogate keys anywhere. Every primary key
--     is either a natural key from the source system (interaction_id) or
--     a deterministic composite key (interaction_id, rule_id) or an
--     application-assigned integer (stg_row_id). This is what makes the
--     script portable across both database engines without an IF/ELSE.
--   * A two-layer load: stg_raw_interactions (lightly-typed staging,
--     keeps raw non-text field variants and load-quality flags so
--     SQL-10's data-quality report has something to report on) feeding
--     interactions (deduplicated, canonicalised fact grain). The
--     free-text column is ALREADY MASKED in both layers - raw unmasked
--     text is never written to the database, per the brief's zero-PII-
--     persistence requirement (masking happens in Python, pipeline
--     Step 2, before any DB write).
--   * flag_actions simulates the operational review/action lifecycle
--     (reviewed_ts, reviewer) needed for the SLA-tracking report
--     (SQL-04). The source brief provides no ground-truth review data,
--     so these timestamps are synthesized by the ETL at load time using
--     a documented, reproducible distribution - see README.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. stg_raw_interactions - staging layer, one row per SOURCE row
--    (duplicates, blanks and bad timestamps are kept here deliberately)
-- ---------------------------------------------------------------------
CREATE TABLE stg_raw_interactions (
    stg_row_id          INTEGER      NOT NULL,
    interaction_id       VARCHAR(30)  NOT NULL,
    customer_id_raw       VARCHAR(30),
    raw_timestamp         VARCHAR(40),
    timestamp_valid       BOOLEAN      NOT NULL,
    channel_raw            VARCHAR(40),
    customer_tier_raw       VARCHAR(40),
    region_raw               VARCHAR(60),
    agent_id_raw               VARCHAR(30),
    masked_text                  TEXT,               -- already PII-masked; never raw
    text_blank                     BOOLEAN NOT NULL,
    load_batch_id                    VARCHAR(20) NOT NULL,
    load_ts                            TIMESTAMP   NOT NULL,
    PRIMARY KEY (stg_row_id)
);

CREATE INDEX idx_stg_raw_interaction_id ON stg_raw_interactions (interaction_id);
CREATE INDEX idx_stg_raw_channel ON stg_raw_interactions (channel_raw);

-- ---------------------------------------------------------------------
-- 2. interactions - clean, deduplicated fact grain (one row per
--    genuine customer interaction)
-- ---------------------------------------------------------------------
CREATE TABLE interactions (
    interaction_id     VARCHAR(30)  NOT NULL,
    customer_id         VARCHAR(30),
    interaction_ts        TIMESTAMP    NOT NULL,
    channel                  VARCHAR(20)  NOT NULL,   -- canonical: call, email, webchat
    channel_raw                VARCHAR(40),
    customer_tier                 VARCHAR(20)  NOT NULL DEFAULT 'Unknown',
    region                           VARCHAR(60)  NOT NULL,
    agent_id                           VARCHAR(20)  NOT NULL,
    masked_text                          TEXT,
    text_char_count                        INTEGER      NOT NULL DEFAULT 0,
    load_batch_id                            VARCHAR(20)  NOT NULL,
    load_ts                                    TIMESTAMP    NOT NULL,
    PRIMARY KEY (interaction_id),
    CHECK (channel IN ('call', 'email', 'webchat', 'unknown'))
);

CREATE INDEX idx_interactions_ts ON interactions (interaction_ts);
CREATE INDEX idx_interactions_customer ON interactions (customer_id);
CREATE INDEX idx_interactions_channel ON interactions (channel);
CREATE INDEX idx_interactions_region ON interactions (region);
CREATE INDEX idx_interactions_agent ON interactions (agent_id);

-- ---------------------------------------------------------------------
-- 3. rules - taxonomy + modifier metadata (mirrors config/rules_config.json
--    so the database itself is self-describing for auditors, independent
--    of the JSON file on disk)
-- ---------------------------------------------------------------------
CREATE TABLE rules (
    rule_id             VARCHAR(20)  NOT NULL,
    rule_type             VARCHAR(20)  NOT NULL,
    category_or_name        VARCHAR(60)  NOT NULL,
    compliance_mandate         VARCHAR(80),
    weight                        INTEGER      NOT NULL,
    active                          BOOLEAN      NOT NULL DEFAULT TRUE,
    description                      VARCHAR(200),
    PRIMARY KEY (rule_id),
    CHECK (rule_type IN ('base_category', 'modifier'))
);

-- ---------------------------------------------------------------------
-- 4. rule_matches - full audit lineage: which rule(s) fired on which
--    interaction, and on what trigger phrase
-- ---------------------------------------------------------------------
CREATE TABLE rule_matches (
    interaction_id     VARCHAR(30)  NOT NULL,
    rule_id               VARCHAR(20)  NOT NULL,
    trigger_phrase          VARCHAR(100) NOT NULL,
    score_contribution         INTEGER      NOT NULL,
    matched_ts                   TIMESTAMP    NOT NULL,
    PRIMARY KEY (interaction_id, rule_id),
    FOREIGN KEY (interaction_id) REFERENCES interactions (interaction_id),
    FOREIGN KEY (rule_id) REFERENCES rules (rule_id)
);

CREATE INDEX idx_rule_matches_rule ON rule_matches (rule_id);

-- ---------------------------------------------------------------------
-- 5. risk_scores - one row per interaction: final deterministic score
-- ---------------------------------------------------------------------
CREATE TABLE risk_scores (
    interaction_id       VARCHAR(30)  NOT NULL,
    primary_category        VARCHAR(60)  NOT NULL,
    primary_rule_id            VARCHAR(20)  NOT NULL,
    base_weight                   INTEGER      NOT NULL,
    modifier_total                   INTEGER      NOT NULL DEFAULT 0,
    repeat_escalation_bonus             INTEGER      NOT NULL DEFAULT 0,
    total_score                            INTEGER      NOT NULL,
    risk_tier                                 VARCHAR(10)  NOT NULL,
    scored_ts                                   TIMESTAMP    NOT NULL,
    PRIMARY KEY (interaction_id),
    FOREIGN KEY (interaction_id) REFERENCES interactions (interaction_id),
    FOREIGN KEY (primary_rule_id) REFERENCES rules (rule_id),
    CHECK (risk_tier IN ('Low', 'Medium', 'High'))
);

CREATE INDEX idx_risk_scores_tier ON risk_scores (risk_tier);
CREATE INDEX idx_risk_scores_score ON risk_scores (total_score);

-- ---------------------------------------------------------------------
-- 6. flag_actions - operational review/action lifecycle for Medium/High
--    risk interactions only (Low risk needs no manual audit per the
--    brief's risk-tier table, so no row is created for them)
-- ---------------------------------------------------------------------
CREATE TABLE flag_actions (
    interaction_id     VARCHAR(30)  NOT NULL,
    priority              VARCHAR(25)  NOT NULL,
    flagged_ts               TIMESTAMP    NOT NULL,
    reviewed                   BOOLEAN      NOT NULL DEFAULT FALSE,
    reviewer_id                   VARCHAR(20),
    action_ts                        TIMESTAMP,
    PRIMARY KEY (interaction_id),
    FOREIGN KEY (interaction_id) REFERENCES interactions (interaction_id)
);

CREATE INDEX idx_flag_actions_reviewed ON flag_actions (reviewed);
CREATE INDEX idx_flag_actions_priority ON flag_actions (priority);

-- =====================================================================
-- VIEWS  (at least 3 required by the brief; every dashboard metric
-- traces back to one of the SQL-01..SQL-12 queries or these views)
-- =====================================================================

-- v_daily_compliance_summary  (backs SQL-01 and the Executive Summary
-- dashboard view)
-- Date truncation uses SQLite's date() function. On PostgreSQL 15+,
-- replace date(i.interaction_ts) with CAST(i.interaction_ts AS DATE)
-- (or i.interaction_ts::date) in this view and in SQL-01/02/04/07/10.
CREATE VIEW v_daily_compliance_summary AS
SELECT
    date(i.interaction_ts)                                               AS audit_date,
    i.channel                                                            AS channel,
    rs.primary_category                                                  AS intent_category,
    COUNT(*)                                                             AS total_interactions,
    SUM(CASE WHEN rs.risk_tier = 'High' THEN 1 ELSE 0 END)               AS high_risk_count,
    ROUND(
        100.0 * SUM(CASE WHEN rs.risk_tier = 'High' THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 2
    )                                                                     AS high_risk_pct,
    ROUND(AVG(rs.total_score), 2)                                        AS avg_risk_score
FROM interactions i
JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
GROUP BY date(i.interaction_ts), i.channel, rs.primary_category;

-- v_auditor_work_queue  (backs SQL-05 and the Operational Auditor Work
-- Queue dashboard view)
CREATE VIEW v_auditor_work_queue AS
SELECT
    i.interaction_id                                                     AS interaction_id,
    i.customer_id                                                        AS customer_id,
    i.channel                                                            AS channel,
    i.region                                                             AS region,
    rs.primary_category                                                  AS intent_category,
    rs.total_score                                                       AS total_score,
    rs.risk_tier                                                         AS risk_tier,
    fa.priority                                                          AS priority,
    fa.flagged_ts                                                        AS flagged_ts,
    fa.reviewed                                                          AS reviewed
FROM flag_actions fa
JOIN interactions i ON i.interaction_id = fa.interaction_id
JOIN risk_scores rs ON rs.interaction_id = fa.interaction_id
WHERE fa.reviewed = FALSE;

-- v_rule_effectiveness  (backs SQL-06)
CREATE VIEW v_rule_effectiveness AS
SELECT
    r.rule_id                                                            AS rule_id,
    r.rule_type                                                          AS rule_type,
    r.category_or_name                                                   AS category_or_name,
    COUNT(rm.interaction_id)                                             AS times_fired,
    COALESCE(SUM(rm.score_contribution), 0)                              AS total_points_generated
FROM rules r
LEFT JOIN rule_matches rm ON rm.rule_id = r.rule_id
GROUP BY r.rule_id, r.rule_type, r.category_or_name;
