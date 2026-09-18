"""Synthetic positive-case proof for src/etl_pipeline.py::apply_repeat_escalation.

The supplied 5,000-row sample dataset has customer interaction dates
spread widely enough that MOD-REPEAT never fires on it (see the header
comment in sql/03_repeat_escalation_detection.sql). This test builds a
small, deliberately-clustered dataset directly against a temp SQLite
database to prove the +15 point / re-tier logic is correct."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from db import ensure_schema, get_engine  # noqa: E402
from etl_pipeline import apply_repeat_escalation, load_rules_metadata, LoadStats  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / "test_repeat.db"
    eng = get_engine(f"sqlite:///{db_path}")
    ensure_schema(eng)
    load_rules_metadata(eng)
    return eng


def _insert_interaction(engine, interaction_id, customer_id, ts, primary_category, primary_rule_id, base_weight, tier):
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO interactions
                   (interaction_id, customer_id, interaction_ts, channel, customer_tier, region,
                    agent_id, masked_text, text_char_count, load_batch_id, load_ts)
                   VALUES (:iid, :cid, :ts, 'email', 'Standard', 'Wales', 'AGT-001', 'synthetic test text',
                           20, 'TEST-BATCH', :ts)"""
            ),
            {"iid": interaction_id, "cid": customer_id, "ts": ts},
        )
        conn.execute(
            text(
                """INSERT INTO risk_scores
                   (interaction_id, primary_category, primary_rule_id, base_weight, modifier_total,
                    repeat_escalation_bonus, total_score, risk_tier, scored_ts)
                   VALUES (:iid, :cat, :rid, :bw, 0, 0, :bw, :tier, :ts)"""
            ),
            {"iid": interaction_id, "cat": primary_category, "rid": primary_rule_id, "bw": base_weight, "tier": tier, "ts": ts},
        )
        conn.execute(
            text(
                """INSERT INTO flag_actions (interaction_id, priority, flagged_ts, reviewed, reviewer_id, action_ts)
                   VALUES (:iid, :priority, :ts, FALSE, NULL, NULL)"""
            ),
            {"iid": interaction_id, "priority": "Priority 2 (48 Hours)" if tier == "Medium" else "Priority 1 (< 2 Hours)", "ts": ts},
        )


class TestRepeatEscalationFires:
    def test_third_flagged_interaction_in_7_days_gets_bonus(self, engine):
        base_ts = datetime(2026, 3, 1, 9, 0, 0)
        # 3 Medium-risk (score 40) interactions for the same customer,
        # all within a 7-day window
        _insert_interaction(engine, "INT-T-01", "CUST-TEST-1", base_ts, "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")
        _insert_interaction(engine, "INT-T-02", "CUST-TEST-1", base_ts + timedelta(days=2), "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")
        _insert_interaction(engine, "INT-T-03", "CUST-TEST-1", base_ts + timedelta(days=4), "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")

        stats = LoadStats()
        apply_repeat_escalation(engine, stats)

        with engine.connect() as conn:
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute(text("SELECT interaction_id, total_score, risk_tier FROM risk_scores ORDER BY interaction_id"))
            }

        # First two interactions are NOT the 3rd-in-window yet at their own timestamp
        assert rows["INT-T-01"] == (40, "Medium")
        assert rows["INT-T-02"] == (40, "Medium")
        # The third interaction IS the 3rd flagged interaction within the
        # trailing 7-day window ending on its own timestamp -> +15, re-tiered
        assert rows["INT-T-03"] == (55, "Medium")  # 40 + 15 = 55, still within 31-60

        with engine.connect() as conn:
            match = conn.execute(
                text("SELECT rule_id, score_contribution FROM rule_matches WHERE interaction_id = 'INT-T-03' AND rule_id = 'MOD-REPEAT'")
            ).fetchone()
        assert match is not None
        assert match[1] == 15
        assert stats.repeat_escalations_applied == 1

    def test_bonus_can_push_medium_into_high_tier(self, engine):
        base_ts = datetime(2026, 3, 1, 9, 0, 0)
        # base weight 50 (Formal Complaint) x3 within 7 days: 3rd one is
        # 50 + 15 = 65 -> crosses into High (61+)
        _insert_interaction(engine, "INT-T-11", "CUST-TEST-2", base_ts, "Formal Complaint", "CAT-COMPLAINT", 50, "Medium")
        _insert_interaction(engine, "INT-T-12", "CUST-TEST-2", base_ts + timedelta(days=1), "Formal Complaint", "CAT-COMPLAINT", 50, "Medium")
        _insert_interaction(engine, "INT-T-13", "CUST-TEST-2", base_ts + timedelta(days=2), "Formal Complaint", "CAT-COMPLAINT", 50, "Medium")

        apply_repeat_escalation(engine, LoadStats())

        with engine.connect() as conn:
            row = conn.execute(text("SELECT total_score, risk_tier FROM risk_scores WHERE interaction_id='INT-T-13'")).fetchone()
        assert row == (65, "High")

    def test_interactions_outside_7day_window_do_not_count(self, engine):
        base_ts = datetime(2026, 3, 1, 9, 0, 0)
        _insert_interaction(engine, "INT-T-21", "CUST-TEST-3", base_ts, "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")
        _insert_interaction(engine, "INT-T-22", "CUST-TEST-3", base_ts + timedelta(days=10), "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")
        _insert_interaction(engine, "INT-T-23", "CUST-TEST-3", base_ts + timedelta(days=20), "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")

        stats = LoadStats()
        apply_repeat_escalation(engine, stats)

        assert stats.repeat_escalations_applied == 0
        with engine.connect() as conn:
            scores = [r[0] for r in conn.execute(text("SELECT total_score FROM risk_scores ORDER BY interaction_id"))]
        assert scores == [40, 40, 40]

    def test_idempotent_rerun_does_not_double_apply(self, engine):
        base_ts = datetime(2026, 3, 1, 9, 0, 0)
        _insert_interaction(engine, "INT-T-31", "CUST-TEST-4", base_ts, "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")
        _insert_interaction(engine, "INT-T-32", "CUST-TEST-4", base_ts + timedelta(days=1), "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")
        _insert_interaction(engine, "INT-T-33", "CUST-TEST-4", base_ts + timedelta(days=2), "Vulnerable / Hardship", "CAT-VULNERABLE", 40, "Medium")

        apply_repeat_escalation(engine, LoadStats())
        with engine.connect() as conn:
            first_pass = conn.execute(text("SELECT total_score FROM risk_scores WHERE interaction_id='INT-T-33'")).scalar()

        stats2 = LoadStats()
        apply_repeat_escalation(engine, stats2)  # rerun
        with engine.connect() as conn:
            second_pass = conn.execute(text("SELECT total_score FROM risk_scores WHERE interaction_id='INT-T-33'")).scalar()

        assert first_pass == second_pass == 55
        assert stats2.repeat_escalations_applied == 0  # nothing new to apply
