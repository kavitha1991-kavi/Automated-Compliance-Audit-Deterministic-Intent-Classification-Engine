-- =====================================================================
-- SQL-10: Data Quality Report
--
-- Business question: what raw channel spelling variants map to which
-- canonical channel, and how many blank-text, invalid-timestamp and
-- duplicate-interaction-ID records did the source file contain?
--
-- Assumptions:
--   * All four checks read from stg_raw_interactions (the staging
--     layer that deliberately keeps every source row, including the
--     ones later rejected or deduplicated from the clean `interactions`
--     table) - this is exactly why that staging layer exists.
--   * "Blank text" and "invalid timestamp" are pre-computed flags set
--     by the ETL at staging time (text_blank, timestamp_valid), not
--     re-derived here, so this report always agrees with what the
--     pipeline actually decided.
--
-- SQL features used: DISTINCT, COUNT, GROUP BY, NULL/blank handling,
-- string normalisation.
-- =====================================================================
WITH channel_variants AS (
    SELECT
        channel_raw                                                         AS channel_raw,
        CASE
            WHEN LOWER(REPLACE(REPLACE(TRIM(COALESCE(channel_raw, '')), '_', ' '), '-', ' ')) IN ('call', 'call transcript')
                THEN 'call'
            WHEN LOWER(REPLACE(REPLACE(TRIM(COALESCE(channel_raw, '')), '_', ' '), '-', ' ')) IN ('email', 'e mail')
                THEN 'email'
            WHEN LOWER(REPLACE(REPLACE(TRIM(COALESCE(channel_raw, '')), '_', ' '), '-', ' ')) IN ('webchat', 'web chat')
                THEN 'webchat'
            ELSE 'unknown'
        END                                                                  AS mapped_canonical_channel,
        COUNT(*)                                                            AS record_count
    FROM stg_raw_interactions
    GROUP BY channel_raw
)
SELECT
    'channel_variant_mapping'   AS check_type,
    channel_raw                  AS detail,
    mapped_canonical_channel      AS mapped_value,
    record_count                    AS record_count
FROM channel_variants

UNION ALL

SELECT 'blank_text', NULL, NULL, COUNT(*)
FROM stg_raw_interactions
WHERE text_blank = TRUE

UNION ALL

SELECT 'invalid_timestamp', NULL, NULL, COUNT(*)
FROM stg_raw_interactions
WHERE timestamp_valid = FALSE

UNION ALL

SELECT 'duplicate_interaction_id_groups', NULL, NULL, COUNT(*)
FROM (
    SELECT interaction_id
    FROM stg_raw_interactions
    GROUP BY interaction_id
    HAVING COUNT(*) > 1
)

UNION ALL

SELECT 'duplicate_interaction_id_extra_rows', NULL, NULL, SUM(cnt - 1)
FROM (
    SELECT interaction_id, COUNT(*) AS cnt
    FROM stg_raw_interactions
    GROUP BY interaction_id
    HAVING COUNT(*) > 1
)

ORDER BY check_type, detail;
