"""
UK GDPR Regex PII Scrubber
==========================
Deterministic, regex-only PII masking for UK-specific entities: National
Insurance numbers, card numbers, phone numbers, email addresses and
postcodes. No ML, no statistical inference - every mask is traceable to a
single, testable regular expression.

Design note on pattern order: patterns are applied in the order they appear
in config/pii_patterns.json. NI numbers, card numbers, phone numbers and
emails are matched before postcodes, because a looser postcode-shaped
pattern could otherwise partially re-match a fragment of an
already-longer entity. Always mask before any analytical processing or
persistence - this module must run first in the pipeline (Step 2), before
rule_classifier (Step 3) and before anything is written to the database
(Step 5).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pii_patterns.json"

_FLAG_MAP = {"IGNORECASE": re.IGNORECASE, "": 0}


@dataclass
class PIIPattern:
    entity_type: str
    regex: re.Pattern
    mask_token: str
    description: str = ""


@dataclass
class ScrubResult:
    masked_text: str
    entities_found: List[str] = field(default_factory=list)
    mask_count: int = 0


def load_patterns(config_path: Path | str = DEFAULT_CONFIG_PATH) -> List[PIIPattern]:
    """Load and compile PII regex patterns from the JSON config."""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    patterns: List[PIIPattern] = []
    for entry in cfg["patterns"]:
        flags = 0
        for flag_name in entry.get("flags", "").split("|"):
            flags |= _FLAG_MAP.get(flag_name.strip(), 0)
        compiled = re.compile(entry["pattern"], flags)
        patterns.append(
            PIIPattern(
                entity_type=entry["entity_type"],
                regex=compiled,
                mask_token=entry["mask_token"],
                description=entry.get("description", ""),
            )
        )
    return patterns


class PIIScrubber:
    """Stateless, reusable scrubber. Instantiate once, call `.scrub()` per record."""

    def __init__(self, config_path: Path | str = DEFAULT_CONFIG_PATH):
        self.patterns = load_patterns(config_path)

    def scrub(self, text: str | None) -> ScrubResult:
        if text is None or (isinstance(text, float)) or str(text).strip() == "":
            return ScrubResult(masked_text="", entities_found=[], mask_count=0)

        masked = str(text)
        entities_found: List[str] = []
        total_masks = 0

        for pattern in self.patterns:
            masked, n = pattern.regex.subn(pattern.mask_token, masked)
            if n > 0:
                entities_found.append(pattern.entity_type)
                total_masks += n

        return ScrubResult(masked_text=masked, entities_found=entities_found, mask_count=total_masks)

    def contains_unmasked_pii(self, text: str | None) -> bool:
        """PII assurance check: does this text still match any PII pattern?
        Used by tests and mirrors SQL-11 (which checks the same thing at the
        database layer, post-load)."""
        if text is None:
            return False
        text = str(text)
        return any(p.regex.search(text) for p in self.patterns)


def mask_text(text: str | None, scrubber: PIIScrubber | None = None) -> Tuple[str, List[str]]:
    """Convenience function for one-off calls (tests, notebooks)."""
    scrubber = scrubber or PIIScrubber()
    result = scrubber.scrub(text)
    return result.masked_text, result.entities_found


if __name__ == "__main__":
    s = PIIScrubber()
    samples = [
        "My NI number is QQ123456C.",
        "I live at SW1A 1AA in London.",
        "Call me on 07911 123456.",
        "Contact user@domain.co.uk.",
        "Paid with card 4532 0150 1234 5678.",
    ]
    for sample in samples:
        r = s.scrub(sample)
        print(f"{sample!r:60s} -> {r.masked_text!r}  entities={r.entities_found}")
