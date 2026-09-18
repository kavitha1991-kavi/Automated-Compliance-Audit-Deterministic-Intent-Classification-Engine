"""Tests for src/pii_scrubber.py - PII Scrubbing Tests (brief Section 6.1):
all 5 UK PII regex patterns must mask with 100% precision and zero
unmasked leakage."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from pii_scrubber import PIIScrubber  # noqa: E402


@pytest.fixture(scope="module")
def scrubber():
    return PIIScrubber()


class TestSpecWorkedExamples:
    """The exact 5 example rows from the brief's Section 3.1 table."""

    def test_ni_number(self, scrubber):
        r = scrubber.scrub("My NI number is QQ123456C.")
        assert r.masked_text == "My NI number is [REDACTED_NI_NUMBER]."
        assert r.entities_found == ["NI_NUMBER"]

    def test_postcode(self, scrubber):
        r = scrubber.scrub("I live at SW1A 1AA in London.")
        assert r.masked_text == "I live at [REDACTED_POSTCODE] in London."

    def test_phone(self, scrubber):
        r = scrubber.scrub("Call me on 07911 123456.")
        assert r.masked_text == "Call me on [REDACTED_PHONE]."

    def test_email(self, scrubber):
        r = scrubber.scrub("Contact user@domain.co.uk.")
        assert r.masked_text == "Contact [REDACTED_EMAIL]."

    def test_card(self, scrubber):
        r = scrubber.scrub("Paid with card 4532 0150 1234 5678.")
        assert r.masked_text == "Paid with card [REDACTED_CARD]."


class TestKnownGapsFixedDuringBuild:
    """Regression tests for two real bugs found and fixed while building
    this project - see README Design Decisions and the description
    fields in config/pii_patterns.json."""

    def test_ni_number_strict_hmrc_letters_still_masked(self, scrubber):
        # The brief's own literal regex excludes the letter Q from the
        # NINO prefix, which would have missed its own worked example
        # (QQ123456C). Covered by test_ni_number above; this test checks
        # a second Q-prefixed case isn't a fluke.
        r = scrubber.scrub("NINO QQ654321A on file.")
        assert "[REDACTED_NI_NUMBER]" in r.masked_text
        assert "QQ654321A" not in r.masked_text

    def test_19_digit_card_number_masked(self, scrubber):
        # SQL-11 (PII assurance check) caught this: a card-shaped token
        # longer than 16 digits was NOT masked by the brief's literal
        # {13,16} pattern, because a trailing \b anchor can never be
        # satisfied by a quantifier capped below the token's true length.
        r = scrubber.scrub("I was double billed on card 4616638723951745187. Refund please.")
        assert "4616638723951745187" not in r.masked_text
        assert "[REDACTED_CARD]" in r.masked_text

    def test_16_digit_card_number_still_masked(self, scrubber):
        # Make sure widening the upper bound didn't break the common case.
        r = scrubber.scrub("Card 4532015012345678 was charged twice.")
        assert "4532015012345678" not in r.masked_text


class TestMultiplePIIInOneMessage:
    def test_all_entities_in_one_message(self, scrubber):
        text = (
            "Hi, I'm at SW1A 1AA, my NI is QQ123456C, email me at "
            "test@example.com or call 07911 123456. Card 4532015012345678."
        )
        r = scrubber.scrub(text)
        for leaked in ["SW1A 1AA", "QQ123456C", "test@example.com", "07911 123456", "4532015012345678"]:
            assert leaked not in r.masked_text
        assert set(r.entities_found) == {"NI_NUMBER", "POSTCODE", "PHONE", "EMAIL", "CARD_NUMBER"}

    def test_repeated_entity_masks_every_occurrence(self, scrubber):
        r = scrubber.scrub("Email me at a@b.com or my backup c@d.com.")
        assert "a@b.com" not in r.masked_text
        assert "c@d.com" not in r.masked_text
        assert r.masked_text.count("[REDACTED_EMAIL]") == 2


class TestBoundaryAndEdgeCases:
    """Boundary & Edge Case Tests (brief Section 6.3): empty text,
    non-English strings, SQL injection characters, huge transcripts."""

    def test_empty_string(self, scrubber):
        r = scrubber.scrub("")
        assert r.masked_text == ""
        assert r.entities_found == []

    def test_none_input(self, scrubber):
        r = scrubber.scrub(None)
        assert r.masked_text == ""

    def test_whitespace_only(self, scrubber):
        r = scrubber.scrub("   \n\t  ")
        assert r.masked_text == ""

    def test_no_pii_present(self, scrubber):
        r = scrubber.scrub("Can you send me a copy of my latest VAT invoice?")
        assert r.masked_text == "Can you send me a copy of my latest VAT invoice?"
        assert r.entities_found == []

    def test_non_english_text_does_not_crash(self, scrubber):
        r = scrubber.scrub("Bonjour, je voudrais annuler mon abonnement s'il vous plaît. 你好世界")
        assert isinstance(r.masked_text, str)

    def test_sql_injection_characters_pass_through_safely(self, scrubber):
        text = "'; DROP TABLE interactions; --  and my email is hacker@evil.com"
        r = scrubber.scrub(text)
        assert "DROP TABLE" in r.masked_text  # scrubber only touches PII, not SQL syntax
        assert "hacker@evil.com" not in r.masked_text

    def test_multi_kilobyte_transcript_does_not_crash(self, scrubber):
        long_text = ("This is a long call transcript. " * 2000) + " My email is big@transcript.com."
        r = scrubber.scrub(long_text)
        assert "big@transcript.com" not in r.masked_text
        assert len(r.masked_text) > 10_000

    def test_numeric_but_not_card_shaped_text_untouched(self, scrubber):
        # A short run of digits below the card-number floor should
        # survive (e.g. a 4-digit extension number).
        r = scrubber.scrub("Please call extension 4521.")
        assert "4521" in r.masked_text


class TestContainsUnmaskedPII:
    def test_flags_text_with_pii(self, scrubber):
        assert scrubber.contains_unmasked_pii("Email me at a@b.com") is True

    def test_clears_masked_text(self, scrubber):
        r = scrubber.scrub("Email me at a@b.com")
        assert scrubber.contains_unmasked_pii(r.masked_text) is False

    def test_clears_plain_text(self, scrubber):
        assert scrubber.contains_unmasked_pii("Hello, how are you?") is False

    def test_handles_none(self, scrubber):
        assert scrubber.contains_unmasked_pii(None) is False
