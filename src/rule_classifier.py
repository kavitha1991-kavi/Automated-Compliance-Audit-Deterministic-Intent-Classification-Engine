"""
Rule-Based Intent Classifier & Deterministic Risk Scoring Engine
==================================================================
Replaces an ML classifier with hierarchical JSON taxonomy matching
(config/rules_config.json). Every classification decision is a keyword
match against a named rule_id, so any score can be traced back to the
exact rule(s) and trigger phrase(s) that produced it (full audit
lineage - see rule_matches table in the schema).

Scoring formula (per the brief, Section 3.3):
    Risk Score = Base Category Weight
               + SUM(High-Risk Modifiers)
               + Severity Multiplier (Repeat Escalation - applied in a
                 separate post-load pass, see apply_repeat_escalation.py,
                 because it depends on a customer's other interactions)

Design decision - primary category selection: text can match keywords
from more than one base category (e.g. both "cannot afford" and
"overcharged"). Rather than summing multiple base weights uncapped, the
category with the HIGHEST base weight is taken as the interaction's
primary_category and contributes the base weight. Every matching
category (not just the primary one) is still written to rule_matches,
so no signal is lost for audit or for the rule-effectiveness report
(SQL-06) - only the scoring formula avoids double-counting overlapping
base categories. This is documented in the README.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rules_config.json"


@dataclass
class RuleMatch:
    rule_id: str
    rule_type: str  # "base_category" | "modifier"
    name: str
    trigger_phrase: str
    score_contribution: int


@dataclass
class ClassificationResult:
    primary_category: str
    primary_rule_id: str
    base_weight: int
    modifier_total: int
    total_score: int  # excludes repeat-escalation, applied later
    risk_tier: str
    matches: List[RuleMatch] = field(default_factory=list)


class RuleClassifier:
    def __init__(self, config_path: Path | str = DEFAULT_CONFIG_PATH):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self._base_categories = [
            {
                **cat,
                "_compiled": [self._compile_phrase(k) for k in cat["keywords"]],
            }
            for cat in self.config["base_categories"]
        ]
        self._modifiers = [
            {
                **mod,
                "_compiled": [self._compile_phrase(k) for k in mod.get("keywords", [])],
            }
            for mod in self.config["modifiers"]
            if mod["rule_id"] != "MOD-REPEAT"  # repeat escalation has no text pattern
        ]
        self._fallback = self.config["fallback_category"]
        self._tiers = self.config["risk_tiers"]

    @staticmethod
    def _compile_phrase(phrase: str) -> re.Pattern:
        # Whole-phrase, case-insensitive, word-boundary-safe substring match.
        return re.compile(re.escape(phrase), re.IGNORECASE)

    def _tier_for_score(self, score: int) -> str:
        for tier in self._tiers:
            lo = tier["min_score"]
            hi = tier["max_score"]
            if score >= lo and (hi is None or score <= hi):
                return tier["tier"]
        return self._tiers[-1]["tier"]

    def classify(self, masked_text: Optional[str]) -> ClassificationResult:
        text = masked_text or ""
        matches: List[RuleMatch] = []

        # --- base category matching ---
        matched_categories = []
        for cat in self._base_categories:
            for kw, pattern in zip(cat["keywords"], cat["_compiled"]):
                m = pattern.search(text)
                if m:
                    matched_categories.append(cat)
                    matches.append(
                        RuleMatch(
                            rule_id=cat["rule_id"],
                            rule_type="base_category",
                            name=cat["category"],
                            trigger_phrase=kw,
                            score_contribution=cat["base_weight"],
                        )
                    )
                    break  # one match per category is enough to log the category as matched

        if matched_categories:
            primary = max(matched_categories, key=lambda c: c["base_weight"])
            primary_category = primary["category"]
            primary_rule_id = primary["rule_id"]
            base_weight = primary["base_weight"]
        else:
            primary_category = self._fallback["category"]
            primary_rule_id = self._fallback["rule_id"]
            base_weight = self._fallback["base_weight"]

        # --- modifiers ---
        modifier_total = 0
        for mod in self._modifiers:
            for kw, pattern in zip(mod["keywords"], mod["_compiled"]):
                m = pattern.search(text)
                if m:
                    modifier_total += mod["score_adjustment"]
                    matches.append(
                        RuleMatch(
                            rule_id=mod["rule_id"],
                            rule_type="modifier",
                            name=mod["name"],
                            trigger_phrase=kw,
                            score_contribution=mod["score_adjustment"],
                        )
                    )
                    break  # one hit per modifier is enough; don't double count e.g. "fca" appearing twice

        total_score = base_weight + modifier_total
        risk_tier = self._tier_for_score(total_score)

        return ClassificationResult(
            primary_category=primary_category,
            primary_rule_id=primary_rule_id,
            base_weight=base_weight,
            modifier_total=modifier_total,
            total_score=total_score,
            risk_tier=risk_tier,
            matches=matches,
        )


if __name__ == "__main__":
    clf = RuleClassifier()
    samples = [
        "This is a formal complaint regarding my overcharged invoice. If you do not resolve this today, "
        "I will escalate to the Financial Ombudsman and FCA immediately.",
        "I lost my job last month and I simply cannot afford my direct debit payment.",
        "Can you send me a copy of my latest VAT invoice for last month?",
        "I am issuing a formal right to be forgotten request under GDPR.",
    ]
    for s in samples:
        r = clf.classify(s)
        print(f"{s[:60]!r:65s} -> {r.primary_category} score={r.total_score} tier={r.risk_tier}")
        for m in r.matches:
            print(f"    {m.rule_id:15s} {m.rule_type:14s} '{m.trigger_phrase}' (+{m.score_contribution})")
