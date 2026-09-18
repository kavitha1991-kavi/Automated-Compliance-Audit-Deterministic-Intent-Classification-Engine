"""Tests for src/etl_pipeline.py: timestamp parsing, channel
canonicalisation, dedup logic, and a small end-to-end run proving
idempotent re-runs load zero duplicate rows."""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from db import ensure_schema, get_engine  # noqa: E402
from etl_pipeline import (  # noqa: E402
    canonical_channel,
    dedupe_and_select,
    LoadStats,
    parse_timestamp,
    run,
)


class TestParseTimestamp:
    def test_standard_uk_format(self):
        ts, valid = parse_timestamp("16/05/2026 19:02")
        assert valid is True
        assert ts == datetime(2026, 5, 16, 19, 2)

    def test_epoch_seconds(self):
        ts, valid = parse_timestamp("1770709049")
        assert valid is True
        assert isinstance(ts, datetime)

    def test_none(self):
        ts, valid = parse_timestamp(None)
        assert valid is False
        assert ts is None

    def test_empty_string(self):
        ts, valid = parse_timestamp("")
        assert valid is False

    def test_garbage_string(self):
        ts, valid = parse_timestamp("not-a-date")
        assert valid is False

    def test_nan_float(self):
        ts, valid = parse_timestamp(float("nan"))
        assert valid is False


class TestCanonicalChannel:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("CALL", "call"),
            ("call_transcript", "call"),
            ("Call Transcript", "call"),
            ("email", "email"),
            ("EMAIL", "email"),
            ("Email", "email"),
            ("E-Mail", "email"),
            ("webchat", "webchat"),
            ("web_chat", "webchat"),
            ("Web Chat", "webchat"),
            (None, "unknown"),
            ("", "unknown"),
            ("sms", "unknown"),  # unrecognised value
        ],
    )
    def test_variants(self, raw, expected):
        assert canonical_channel(raw) == expected


class TestDedupeAndSelect:
    def test_rejects_invalid_timestamp_rows(self):
        df = pd.DataFrame(
            [
                {"interaction_id": "A", "timestamp_valid": True, "interaction_ts": datetime(2026, 1, 1), "has_dup_marker": False, "stg_row_id": 1},
                {"interaction_id": "B", "timestamp_valid": False, "interaction_ts": None, "has_dup_marker": False, "stg_row_id": 2},
            ]
        )
        stats = LoadStats()
        out = dedupe_and_select(df, stats)
        assert list(out["interaction_id"]) == ["A"]
        assert stats.interactions_rejected_bad_timestamp == 1

    def test_prefers_row_without_dup_marker(self):
        df = pd.DataFrame(
            [
                {"interaction_id": "A", "timestamp_valid": True, "interaction_ts": datetime(2026, 1, 5), "has_dup_marker": True, "stg_row_id": 1},
                {"interaction_id": "A", "timestamp_valid": True, "interaction_ts": datetime(2026, 1, 1), "has_dup_marker": False, "stg_row_id": 2},
            ]
        )
        stats = LoadStats()
        out = dedupe_and_select(df, stats)
        assert len(out) == 1
        assert out.iloc[0]["stg_row_id"] == 2  # the non-marker row wins
        assert stats.interactions_skipped_duplicate == 1

    def test_earliest_timestamp_wins_when_no_marker_present(self):
        df = pd.DataFrame(
            [
                {"interaction_id": "A", "timestamp_valid": True, "interaction_ts": datetime(2026, 1, 5), "has_dup_marker": False, "stg_row_id": 1},
                {"interaction_id": "A", "timestamp_valid": True, "interaction_ts": datetime(2026, 1, 1), "has_dup_marker": False, "stg_row_id": 2},
            ]
        )
        out = dedupe_and_select(df, LoadStats())
        assert out.iloc[0]["stg_row_id"] == 2


class TestEndToEndIdempotency:
    def test_rerun_loads_zero_new_rows(self, tmp_path):
        csv_path = tmp_path / "mini.csv"
        csv_path.write_text(
            "interaction_id,customer_id,timestamp,channel,customer_tier,region,agent_id,raw_interaction_text\n"
            "INT-X-001,CUST-X-1,01/03/2026 09:00,EMAIL,VIP,Wales,AGT-001,This is a formal complaint about billing.\n"
            "INT-X-002,CUST-X-2,02/03/2026 10:00,call,Standard,Scotland,AGT-002,I lost my job and cannot afford this.\n"
            "INT-X-002,CUST-X-2,02/03/2026 10:00,call,Standard,Scotland,AGT-002,I lost my job and cannot afford this. [DUPLICATE SUBMISSION]\n"
            "INT-X-003,CUST-X-3,not-a-date,email,Standard,Wales,AGT-003,Some text with a bad timestamp.\n",
            encoding="utf-8",
        )
        db_path = tmp_path / "mini.db"
        db_url = f"sqlite:///{db_path}"

        stats1 = run(csv_path=csv_path, database_url=db_url)
        assert stats1.interactions_loaded == 2  # X-003 rejected (bad ts), one X-002 dup skipped
        assert stats1.interactions_rejected_bad_timestamp == 1
        assert stats1.interactions_skipped_duplicate == 1

        stats2 = run(csv_path=csv_path, database_url=db_url)
        assert stats2.interactions_loaded == 0
        assert stats2.interactions_skipped_already_loaded == 2

        engine = get_engine(db_url)
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM interactions")).scalar()
        assert count == 2  # still exactly 2 after two pipeline runs
