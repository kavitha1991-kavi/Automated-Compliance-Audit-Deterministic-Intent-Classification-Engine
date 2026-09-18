"""Tests for src/rule_classifier.py - Deterministic Scoring Tests (brief
Section 6.2): known test sentences must trigger exact rule IDs and
cumulative risk score math."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from rule_classifier import RuleClassifier  # noqa: E402


@pytest.fixture(scope="module")
def clf():
    return RuleClassifier()


class TestBaseCategoryWeights:
    def test_formal_complaint_base_weight(self, clf):
        r = clf.classify("This is a formal complaint about your service.")
        assert r.primary_category == "Formal Complaint"
        assert r.primary_rule_id == "CAT-COMPLAINT"
        assert r.base_weight == 50
        assert r.total_score == 50
        assert r.risk_tier == "Medium"  # 31-60 band

    def test_vulnerable_hardship_base_weight(self, clf):
        r = clf.classify("I lost my job last month and cannot afford my bills.")
        assert r.primary_category == "Vulnerable / Hardship"
        assert r.base_weight == 40
        assert r.total_score == 40
        assert r.risk_tier == "Medium"

    def test_dsar_base_weight(self, clf):
        r = clf.classify("This is a subject access request under GDPR.")
        assert r.primary_category == "DSAR / Data Privacy"
        assert r.base_weight == 35
        assert r.total_score == 35
        assert r.risk_tier == "Medium"

    def test_billing_base_weight(self, clf):
        r = clf.classify("My direct debit failed again this month.")
        assert r.primary_category == "Billing & Direct Debit"
        assert r.base_weight == 20
        assert r.total_score == 20
        assert r.risk_tier == "Low"

    def test_no_match_falls_back_to_general(self, clf):
        r = clf.classify("What time do you close on Sundays?")
        assert r.primary_category == "General Enquiry"
        assert r.total_score == 0
        assert r.risk_tier == "Low"


class TestModifiers:
    def test_regulator_threat_modifier_stacks_with_base(self, clf):
        r = clf.classify("This is a formal complaint. I will contact the FCA.")
        assert r.base_weight == 50
        assert r.modifier_total == 30
        assert r.total_score == 80
        assert r.risk_tier == "High"
        rule_ids = {m.rule_id for m in r.matches}
        assert {"CAT-COMPLAINT", "MOD-REGULATOR"} <= rule_ids

    def test_negative_sentiment_modifier(self, clf):
        r = clf.classify("This is a scam and I am disgusted with this service.")
        # "scam" and "disgusted" are both sentiment keywords but the
        # modifier should only add once (one hit per modifier, not per keyword)
        assert r.modifier_total == 10

    def test_spec_worked_example_matches_brief_exactly(self, clf):
        # Brief Section 3.3 worked example: Formal Complaint (50) +
        # Regulator Threat (mentions FCA and Ombudsman, +30 once) = 80, High
        text = (
            "This is a formal complaint regarding my overcharged invoice. If you do not "
            "resolve this today, I will escalate to the Financial Ombudsman and FCA immediately."
        )
        r = clf.classify(text)
        assert r.total_score == 80
        assert r.risk_tier == "High"

    def test_multiple_modifiers_stack(self, clf):
        r = clf.classify(
            "I lost my job and cannot afford this. This is a scam - cancel my account immediately."
        )
        # base 40 (vulnerable) + sentiment 10 (scam) + cancellation 20 = 70
        assert r.base_weight == 40
        assert r.modifier_total == 30
        assert r.total_score == 70
        assert r.risk_tier == "High"


class TestPrimaryCategorySelection:
    def test_highest_weight_category_wins_as_primary(self, clf):
        # "cannot afford" (Vulnerable, 40) + "overcharged" (Billing, 20)
        r = clf.classify("I cannot afford this because I was overcharged.")
        assert r.primary_category == "Vulnerable / Hardship"
        assert r.base_weight == 40
        # both categories should still be logged for audit lineage
        rule_ids = {m.rule_id for m in r.matches}
        assert {"CAT-VULNERABLE", "CAT-BILLING"} <= rule_ids
        # but only the primary (higher) weight contributes to total_score
        assert r.total_score == 40


class TestRiskTierBoundaries:
    @pytest.mark.parametrize(
        "score,expected_tier",
        [(0, "Low"), (30, "Low"), (31, "Medium"), (60, "Medium"), (61, "High"), (150, "High")],
    )
    def test_tier_boundaries(self, clf, score, expected_tier):
        assert clf._tier_for_score(score) == expected_tier


class TestEdgeCases:
    def test_empty_text(self, clf):
        r = clf.classify("")
        assert r.primary_category == "General Enquiry"
        assert r.total_score == 0

    def test_none_text(self, clf):
        r = clf.classify(None)
        assert r.total_score == 0

    def test_case_insensitive_matching(self, clf):
        r = clf.classify("FORMAL COMPLAINT about your FCA-regulated service.")
        assert r.primary_category == "Formal Complaint"
        assert r.modifier_total == 30

    def test_non_english_text_does_not_crash(self, clf):
        r = clf.classify("Bonjour, je suis très mécontent. 你好")
        assert r.total_score == 0  # no English taxonomy keywords present

    def test_sql_injection_text_does_not_crash(self, clf):
        r = clf.classify("'; DROP TABLE interactions; -- formal complaint")
        assert r.primary_category == "Formal Complaint"
