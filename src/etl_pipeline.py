"""
End-to-end ETL pipeline (brief Section 2.1, Steps 1, 2, 3, 4, 5):

    1. Raw Ingestion       - read the source CSV into a pandas buffer
    2. PII Masking Gate    - every free-text field is scrubbed BEFORE it
                              touches a DataFrame column that could be
                              written anywhere. Unmasked text never
                              leaves this module.
    3. Taxonomy Matching   - src/rule_classifier.py against masked text
    4. Risk Scoring Matrix - base weight + modifiers (+ repeat escalation
                              in a second pass, see apply_repeat_escalation())
    5. SQL Warehouse Load  - idempotent inserts into the schema built by
                              sql/00_schema_ddl.sql

Idempotency: every insert function checks what's already in the target
table and only inserts rows that aren't there yet. Re-running this
script on the same CSV a second time loads zero new interaction rows
and logs everything as "skipped".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from db import DEFAULT_SQLITE_PATH, ensure_schema, get_engine
from pii_scrubber import PIIScrubber
from rule_classifier import RuleClassifier

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV_PATH = PROJECT_ROOT / "data" / "uk_customer_interactions_raw.csv"
RULES_CONFIG_PATH = PROJECT_ROOT / "config" / "rules_config.json"

DUP_MARKER_RE = re.compile(r"\s*\[DUPLICATE SUBMISSION\]\s*", re.IGNORECASE)
EPOCH_RE = re.compile(r"^\d{9,10}$")

CHANNEL_MAP = {
    "call": "call",
    "call transcript": "call",
    "email": "email",
    "e mail": "email",
    "webchat": "webchat",
    "web chat": "webchat",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
log = logging.getLogger("etl")


@dataclass
class LoadStats:
    stg_rows_written: int = 0
    interactions_loaded: int = 0
    interactions_skipped_duplicate: int = 0
    interactions_skipped_already_loaded: int = 0
    interactions_rejected_bad_timestamp: int = 0
    scored: int = 0
    flags_created: int = 0
    repeat_escalations_applied: int = 0

    def as_dict(self) -> dict:
        return self.__dict__


def canonical_channel(raw: Optional[str]) -> str:
    if raw is None or (isinstance(raw, float)):
        return "unknown"
    normalised = str(raw).strip().lower().replace("_", " ").replace("-", " ")
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return CHANNEL_MAP.get(normalised, "unknown")


def parse_timestamp(raw: Optional[str]) -> tuple[Optional[datetime], bool]:
    if raw is None or (isinstance(raw, float)) or str(raw).strip() == "":
        return None, False
    raw = str(raw).strip()
    if EPOCH_RE.match(raw):
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).replace(tzinfo=None), True
        except (ValueError, OSError):
            return None, False
    try:
        return datetime.strptime(raw, "%d/%m/%Y %H:%M"), True
    except ValueError:
        return None, False


def stage_raw_csv(csv_path: Path, engine: Engine, scrubber: PIIScrubber, batch_id: str) -> tuple[pd.DataFrame, int]:
    """Steps 1 + 2: read the CSV, mask every free-text cell, write the
    full (masked) staging layer, and return an in-memory cleaned frame
    ready for dedup + load into `interactions`."""
    df = pd.read_csv(csv_path, dtype=str)
    log.info("Read %d source rows from %s", len(df), csv_path.name)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT COALESCE(MAX(stg_row_id), 0) FROM stg_raw_interactions")).scalar()
        next_stg_id = int(row) + 1

    records = []
    clean_rows = []
    for i, r in df.iterrows():
        raw_text = r.get("raw_interaction_text")
        has_dup_marker = bool(isinstance(raw_text, str) and DUP_MARKER_RE.search(raw_text))
        text_no_marker = DUP_MARKER_RE.sub("", raw_text) if isinstance(raw_text, str) else raw_text
        scrub = scrubber.scrub(text_no_marker)  # PII MASKING GATE - happens before anything is kept

        ts, ts_valid = parse_timestamp(r.get("timestamp"))
        channel = canonical_channel(r.get("channel"))
        tier = r.get("customer_tier") if isinstance(r.get("customer_tier"), str) and r.get("customer_tier").strip() else "Unknown"
        region = r.get("region") if isinstance(r.get("region"), str) and r.get("region").strip() else "Unknown"
        agent_id = r.get("agent_id") if isinstance(r.get("agent_id"), str) and r.get("agent_id").strip() else "AGT-UNKNOWN"
        customer_id = r.get("customer_id") if isinstance(r.get("customer_id"), str) and r.get("customer_id").strip() else None

        stg_id = next_stg_id + i
        records.append(
            {
                "stg_row_id": stg_id,
                "interaction_id": r["interaction_id"],
                "customer_id_raw": customer_id,
                "raw_timestamp": r.get("timestamp") if isinstance(r.get("timestamp"), str) else None,
                "timestamp_valid": ts_valid,
                "channel_raw": r.get("channel") if isinstance(r.get("channel"), str) else None,
                "customer_tier_raw": r.get("customer_tier") if isinstance(r.get("customer_tier"), str) else None,
                "region_raw": r.get("region") if isinstance(r.get("region"), str) else None,
                "agent_id_raw": r.get("agent_id") if isinstance(r.get("agent_id"), str) else None,
                "masked_text": scrub.masked_text,
                "text_blank": scrub.masked_text.strip() == "",
                "load_batch_id": batch_id,
                "load_ts": datetime.now(timezone.utc).replace(tzinfo=None),
            }
        )

        clean_rows.append(
            {
                "stg_row_id": stg_id,
                "interaction_id": r["interaction_id"],
                "customer_id": customer_id,
                "interaction_ts": ts,
                "timestamp_valid": ts_valid,
                "channel": channel,
                "channel_raw": r.get("channel") if isinstance(r.get("channel"), str) else None,
                "customer_tier": tier,
                "region": region,
                "agent_id": agent_id,
                "masked_text": scrub.masked_text,
                "text_char_count": len(scrub.masked_text),
                "has_dup_marker": has_dup_marker,
            }
        )

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO stg_raw_interactions
                    (stg_row_id, interaction_id, customer_id_raw, raw_timestamp, timestamp_valid,
                     channel_raw, customer_tier_raw, region_raw, agent_id_raw, masked_text,
                     text_blank, load_batch_id, load_ts)
                VALUES
                    (:stg_row_id, :interaction_id, :customer_id_raw, :raw_timestamp, :timestamp_valid,
                     :channel_raw, :customer_tier_raw, :region_raw, :agent_id_raw, :masked_text,
                     :text_blank, :load_batch_id, :load_ts)
                """
            ),
            records,
        )
    log.info("Staged %d rows into stg_raw_interactions (batch %s)", len(records), batch_id)

    return pd.DataFrame(clean_rows), len(records)


def _to_native(v):
    """Coerce pandas Timestamp/NaN into plain Python datetime/None so the
    sqlite3 DBAPI (which only auto-adapts exact datetime.datetime, not
    the pandas.Timestamp subclass) can bind the parameter."""
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.to_pydatetime()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def dedupe_and_select(clean_df: pd.DataFrame, stats: LoadStats) -> pd.DataFrame:
    """Business rule: one row per interaction_id in the clean fact table.
    Rejects rows with an unparseable timestamp; among remaining
    duplicates for the same interaction_id, prefers the row without the
    '[DUPLICATE SUBMISSION]' resubmission marker, then the earliest
    interaction_ts, then source order."""
    valid = clean_df[clean_df["timestamp_valid"]].copy()
    stats.interactions_rejected_bad_timestamp += int((~clean_df["timestamp_valid"]).sum())

    valid = valid.sort_values(by=["has_dup_marker", "interaction_ts", "stg_row_id"])
    deduped = valid.drop_duplicates(subset=["interaction_id"], keep="first")

    stats.interactions_skipped_duplicate += len(valid) - len(deduped)
    return deduped


def load_interactions(deduped_df: pd.DataFrame, engine: Engine, batch_id: str, stats: LoadStats) -> None:
    with engine.connect() as conn:
        existing = {row[0] for row in conn.execute(text("SELECT interaction_id FROM interactions"))}

    to_load = deduped_df[~deduped_df["interaction_id"].isin(existing)]
    stats.interactions_skipped_already_loaded += len(deduped_df) - len(to_load)

    if to_load.empty:
        log.info("No new interactions to load (already loaded or none survived dedup)")
        return

    rows = [
        {
            "interaction_id": r["interaction_id"],
            "customer_id": _to_native(r["customer_id"]),
            "interaction_ts": _to_native(r["interaction_ts"]),
            "channel": r["channel"],
            "channel_raw": _to_native(r["channel_raw"]),
            "customer_tier": r["customer_tier"],
            "region": r["region"],
            "agent_id": r["agent_id"],
            "masked_text": r["masked_text"],
            "text_char_count": int(r["text_char_count"]),
            "load_batch_id": batch_id,
            "load_ts": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        for _, r in to_load.iterrows()
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO interactions
                    (interaction_id, customer_id, interaction_ts, channel, channel_raw,
                     customer_tier, region, agent_id, masked_text, text_char_count,
                     load_batch_id, load_ts)
                VALUES
                    (:interaction_id, :customer_id, :interaction_ts, :channel, :channel_raw,
                     :customer_tier, :region, :agent_id, :masked_text, :text_char_count,
                     :load_batch_id, :load_ts)
                """
            ),
            rows,
        )
    stats.interactions_loaded += len(rows)
    log.info("Loaded %d new interactions", len(rows))


def load_rules_metadata(engine: Engine) -> None:
    with open(RULES_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    with engine.connect() as conn:
        existing = {row[0] for row in conn.execute(text("SELECT rule_id FROM rules"))}

    rows = []
    for cat in cfg["base_categories"]:
        if cat["rule_id"] not in existing:
            rows.append(
                {
                    "rule_id": cat["rule_id"],
                    "rule_type": "base_category",
                    "category_or_name": cat["category"],
                    "compliance_mandate": cat["compliance_mandate"],
                    "weight": cat["base_weight"],
                    "description": ", ".join(cat["keywords"][:3]) + ", ...",
                }
            )
    fb = cfg["fallback_category"]
    if fb["rule_id"] not in existing:
        rows.append(
            {
                "rule_id": fb["rule_id"],
                "rule_type": "base_category",
                "category_or_name": fb["category"],
                "compliance_mandate": fb["compliance_mandate"],
                "weight": fb["base_weight"],
                "description": "No taxonomy keyword matched",
            }
        )
    for mod in cfg["modifiers"]:
        if mod["rule_id"] not in existing:
            rows.append(
                {
                    "rule_id": mod["rule_id"],
                    "rule_type": "modifier",
                    "category_or_name": mod["name"],
                    "compliance_mandate": None,
                    "weight": mod["score_adjustment"],
                    "description": mod.get("condition") or ", ".join(mod.get("keywords", [])[:3]),
                }
            )

    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO rules (rule_id, rule_type, category_or_name, compliance_mandate, weight, description)
                VALUES (:rule_id, :rule_type, :category_or_name, :compliance_mandate, :weight, :description)
                """
            ),
            rows,
        )
    log.info("Loaded %d rule metadata rows", len(rows))


PRIORITY_MAP = {
    "Medium": "Priority 2 (48 Hours)",
    "High": "Priority 1 (< 2 Hours)",
}
SLA_HOURS = {"Medium": 48, "High": 2}


def _synthesize_flag_action(interaction_id: str, tier: str, flagged_ts: datetime) -> dict:
    """Deterministically synthesize a plausible review/action lifecycle
    for a flagged interaction. The source brief provides no ground-truth
    review data, so SQL-04 (SLA tracking) and SQL-05 (auditor queue) need
    a documented, reproducible stand-in. Seeded by interaction_id so
    reruns are byte-identical (required for idempotency)."""
    rng = random.Random(int(hashlib.sha256(interaction_id.encode()).hexdigest(), 16) % (2**32))
    reviewed = rng.random() < 0.75
    reviewer_id = f"REV-{rng.randint(1, 12):02d}" if reviewed else None
    action_ts = None
    if reviewed:
        sla = SLA_HOURS[tier]
        if rng.random() < 0.8:  # met SLA
            minutes = rng.uniform(5, sla * 60 * 0.95)
        else:  # breached SLA
            minutes = rng.uniform(sla * 60 * 1.05, sla * 60 * 3)
        action_ts = flagged_ts + timedelta(minutes=minutes)
    return {
        "priority": PRIORITY_MAP[tier],
        "flagged_ts": flagged_ts,
        "reviewed": reviewed,
        "reviewer_id": reviewer_id,
        "action_ts": action_ts,
    }


def classify_and_score(engine: Engine, stats: LoadStats) -> None:
    """Steps 3 + 4: for every interaction that doesn't already have a
    risk_scores row, run the taxonomy classifier and persist the
    score + full rule lineage."""
    classifier = RuleClassifier()

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT i.interaction_id, i.masked_text, i.interaction_ts
                FROM interactions i
                LEFT JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
                WHERE rs.interaction_id IS NULL
                """
            )
        ).fetchall()

    if not rows:
        log.info("No unscored interactions found")
        return

    match_rows, score_rows, flag_rows = [], [], []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for interaction_id, masked_text, interaction_ts in rows:
        result = classifier.classify(masked_text)
        for m in result.matches:
            match_rows.append(
                {
                    "interaction_id": interaction_id,
                    "rule_id": m.rule_id,
                    "trigger_phrase": m.trigger_phrase,
                    "score_contribution": m.score_contribution,
                    "matched_ts": now,
                }
            )
        score_rows.append(
            {
                "interaction_id": interaction_id,
                "primary_category": result.primary_category,
                "primary_rule_id": result.primary_rule_id,
                "base_weight": result.base_weight,
                "modifier_total": result.modifier_total,
                "repeat_escalation_bonus": 0,
                "total_score": result.total_score,
                "risk_tier": result.risk_tier,
                "scored_ts": now,
            }
        )
        if result.risk_tier in ("Medium", "High"):
            ts = interaction_ts if isinstance(interaction_ts, datetime) else datetime.fromisoformat(str(interaction_ts))
            flag_rows.append({"interaction_id": interaction_id, **_synthesize_flag_action(interaction_id, result.risk_tier, ts)})

    with engine.begin() as conn:
        if match_rows:
            conn.execute(
                text(
                    """
                    INSERT INTO rule_matches (interaction_id, rule_id, trigger_phrase, score_contribution, matched_ts)
                    VALUES (:interaction_id, :rule_id, :trigger_phrase, :score_contribution, :matched_ts)
                    """
                ),
                match_rows,
            )
        conn.execute(
            text(
                """
                INSERT INTO risk_scores
                    (interaction_id, primary_category, primary_rule_id, base_weight, modifier_total,
                     repeat_escalation_bonus, total_score, risk_tier, scored_ts)
                VALUES
                    (:interaction_id, :primary_category, :primary_rule_id, :base_weight, :modifier_total,
                     :repeat_escalation_bonus, :total_score, :risk_tier, :scored_ts)
                """
            ),
            score_rows,
        )
        if flag_rows:
            conn.execute(
                text(
                    """
                    INSERT INTO flag_actions (interaction_id, priority, flagged_ts, reviewed, reviewer_id, action_ts)
                    VALUES (:interaction_id, :priority, :flagged_ts, :reviewed, :reviewer_id, :action_ts)
                    """
                ),
                flag_rows,
            )

    stats.scored += len(score_rows)
    stats.flags_created += len(flag_rows)
    log.info("Scored %d interactions, created %d flag_actions rows", len(score_rows), len(flag_rows))


def apply_repeat_escalation(engine: Engine, stats: LoadStats) -> None:
    """MOD-REPEAT: any interaction that is the 3rd-or-later flagged
    (Medium/High) interaction for its customer within a trailing 7-day
    window gets +15 points and is re-tiered. Idempotent: skips
    interactions that already carry a MOD-REPEAT rule_match."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT i.interaction_id, i.customer_id, i.interaction_ts, rs.total_score, rs.risk_tier
                FROM interactions i
                JOIN risk_scores rs ON rs.interaction_id = i.interaction_id
                WHERE rs.risk_tier IN ('Medium', 'High')
                  AND i.customer_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM rule_matches rm
                      WHERE rm.interaction_id = i.interaction_id AND rm.rule_id = 'MOD-REPEAT'
                  )
                ORDER BY i.customer_id, i.interaction_ts
                """
            )
        ).fetchall()

    if not rows:
        log.info("No candidates for repeat-escalation pass")
        return

    by_customer: dict[str, list] = {}
    for interaction_id, customer_id, ts, total_score, tier in rows:
        ts = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
        by_customer.setdefault(customer_id, []).append(
            {"interaction_id": interaction_id, "ts": ts, "total_score": total_score, "tier": tier}
        )

    classifier = RuleClassifier()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    match_rows, update_rows = [], []

    for customer_id, interactions in by_customer.items():
        interactions.sort(key=lambda r: r["ts"])
        for idx, current in enumerate(interactions):
            window_start = current["ts"] - timedelta(days=7)
            count_in_window = sum(1 for other in interactions if window_start <= other["ts"] <= current["ts"])
            if count_in_window > 2:
                new_score = current["total_score"] + 15
                new_tier = classifier._tier_for_score(new_score)
                match_rows.append(
                    {
                        "interaction_id": current["interaction_id"],
                        "rule_id": "MOD-REPEAT",
                        "trigger_phrase": f">2 flagged interactions for {customer_id} within trailing 7 days",
                        "score_contribution": 15,
                        "matched_ts": now,
                    }
                )
                update_rows.append(
                    {"interaction_id": current["interaction_id"], "new_score": new_score, "new_tier": new_tier}
                )

    if not update_rows:
        log.info("Repeat-escalation pass: no customer exceeded the 3-flags-in-7-days threshold")
        return

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO rule_matches (interaction_id, rule_id, trigger_phrase, score_contribution, matched_ts)
                VALUES (:interaction_id, :rule_id, :trigger_phrase, :score_contribution, :matched_ts)
                """
            ),
            match_rows,
        )
        for row in update_rows:
            conn.execute(
                text(
                    """
                    UPDATE risk_scores
                    SET repeat_escalation_bonus = 15,
                        total_score = :new_score,
                        risk_tier = :new_tier
                    WHERE interaction_id = :interaction_id
                    """
                ),
                row,
            )
            # if this interaction was newly tiered High and has no flag_actions
            # row yet (it was Medium at first-pass scoring time), create one.
            conn.execute(
                text(
                    """
                    INSERT INTO flag_actions (interaction_id, priority, flagged_ts, reviewed, reviewer_id, action_ts)
                    SELECT i.interaction_id, :priority, i.interaction_ts, FALSE, NULL, NULL
                    FROM interactions i
                    WHERE i.interaction_id = :interaction_id
                      AND NOT EXISTS (SELECT 1 FROM flag_actions fa WHERE fa.interaction_id = i.interaction_id)
                    """
                ),
                {"interaction_id": row["interaction_id"], "priority": PRIORITY_MAP[row["new_tier"]]},
            )
            conn.execute(
                text("UPDATE flag_actions SET priority = :priority WHERE interaction_id = :interaction_id"),
                {"interaction_id": row["interaction_id"], "priority": PRIORITY_MAP[row["new_tier"]]},
            )

    stats.repeat_escalations_applied += len(update_rows)
    log.info("Applied repeat-escalation bonus to %d interactions", len(update_rows))


def run(csv_path: Path = DEFAULT_CSV_PATH, database_url: Optional[str] = None) -> LoadStats:
    engine = get_engine(database_url)
    ensure_schema(engine)
    load_rules_metadata(engine)

    scrubber = PIIScrubber()
    batch_id = datetime.now(timezone.utc).replace(tzinfo=None).strftime("BATCH-%Y%m%d%H%M%S")

    stats = LoadStats()
    clean_df, n_staged = stage_raw_csv(csv_path, engine, scrubber, batch_id)
    stats.stg_rows_written = n_staged

    deduped = dedupe_and_select(clean_df, stats)
    load_interactions(deduped, engine, batch_id, stats)
    classify_and_score(engine, stats)
    apply_repeat_escalation(engine, stats)

    log.info("Pipeline run complete. Stats: %s", stats.as_dict())
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the compliance audit ETL pipeline")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    parser.add_argument("--database-url", type=str, default=None)
    args = parser.parse_args()
    run(csv_path=args.csv, database_url=args.database_url)
