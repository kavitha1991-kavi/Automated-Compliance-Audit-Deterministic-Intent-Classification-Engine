-- =====================================================================
-- SQL-11: PII Assurance Check
--
-- Business question: does ANY stored analytical record still contain
-- text matching a UK PII pattern? The expected result is ZERO ROWS -
-- that is the entire point of this check (it is the SQL-layer mirror
-- of tests/test_pii_scrubber.py::test_contains_unmasked_pii, run
-- against what's actually persisted rather than in isolation).
--
-- Checks BOTH persisted text columns: interactions.masked_text (the
-- clean analytical layer) and stg_raw_interactions.masked_text (the
-- staging layer) - remember both were masked in Python BEFORE they
-- were ever written to either table (see src/etl_pipeline.py -
-- PIIScrubber.scrub() runs before any INSERT), so this check should
-- never find anything to report.
--
-- SQL features used: pattern matching (regex operator), UNION ALL.
--
-- IMPORTANT - engine note: SQLite has no built-in regex operator. This
-- project registers one (function name REGEXP) via
-- src/db.py::get_engine(), so running this file through
-- src/run_sql.py (or the ETL/tests, which use the same engine factory)
-- works out of the box. Opening the .db file directly with the plain
-- `sqlite3` CLI will NOT have REGEXP available. On PostgreSQL 15+,
-- REGEXP is unnecessary - PostgreSQL's native `~` operator works out
-- of the box; just replace `x REGEXP 'pattern'` with `x ~ 'pattern'`.
--
-- The 5 patterns below are kept in lock-step with
-- config/pii_patterns.json (single source of truth for the Python
-- scrubber); if you change one there, mirror the change here.
-- =====================================================================
WITH combined AS (
    SELECT 'interactions' AS source_table, interaction_id AS record_id, masked_text
    FROM interactions
    UNION ALL
    SELECT 'stg_raw_interactions', CAST(stg_row_id AS TEXT), masked_text
    FROM stg_raw_interactions
)
SELECT
    source_table,
    record_id,
    CASE
        WHEN masked_text REGEXP '\b[A-Za-z]{2}\s*\d{2}\s*\d{2}\s*\d{2}\s*[A-Da-d]\b'                                        THEN 'NI_NUMBER'
        WHEN masked_text REGEXP '(?:\d[ -]?){13,19}'                                                                        THEN 'CARD_NUMBER'
        WHEN masked_text REGEXP '(?:\+44\s?\(0\)\s?\d{2,4}|\+44\s?\d{2,4}|\(?0\d{2,4}\)?)[\s-]?\d{3,4}[\s-]?\d{3,4}'         THEN 'PHONE'
        WHEN masked_text REGEXP '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'                                             THEN 'EMAIL'
        WHEN masked_text REGEXP '\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b'                                                      THEN 'POSTCODE'
    END AS leaked_entity_type,
    masked_text
FROM combined
WHERE masked_text REGEXP '\b[A-Za-z]{2}\s*\d{2}\s*\d{2}\s*\d{2}\s*[A-Da-d]\b'
   OR masked_text REGEXP '(?:\d[ -]?){13,19}'
   OR masked_text REGEXP '(?:\+44\s?\(0\)\s?\d{2,4}|\+44\s?\d{2,4}|\(?0\d{2,4}\)?)[\s-]?\d{3,4}[\s-]?\d{3,4}'
   OR masked_text REGEXP '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
   OR masked_text REGEXP '\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b';
