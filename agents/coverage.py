"""Coverage check: does this contract price things in ways the engine does not model?

The engine understands six terms: base rate, promo rate and end date, a fixed annual
escalator and its start date, and a monthly minimum. A contract that also uses index-
linked increases, tiers, proration, amendments, credits, usage overages, surcharges,
caps, true-ups or non-monthly billing would still yield those six terms, and the math
would run on an incomplete model. So before any finding can be approved, the contract
text is scanned in plain code for that language. Any hit sends the whole run to a person.

This is fail-closed on purpose: a false alarm costs a review; a miss costs a wrong claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (label, pattern). Patterns are matched case-insensitively against the raw contract text.
UNSUPPORTED_PRICING = [
    ("index-linked increase (CPI)", r"\bCPI\b|consumer price index|price index|\bindexed\b"),
    ("tiered or volume pricing", r"\btier(?:s|ed)?\b|volume discount|volume[- ]based|\bper[- ]tier\b"),
    ("proration", r"\bprorat\w*|pro[- ]rata|partial (?:month|billing period)"),
    ("amendment or addendum", r"\bamendment\b|\bamended\b|\baddendum\b|\bchange order\b"),
    ("credits or rebates", r"\brebates?\b|service credits?|\bcredits? (?:against|toward|towards)\b"),
    ("usage or overage charges", r"\boverages?\b|usage[- ]based|\bin excess of\b"),
    ("surcharges", r"\bsurcharges?\b|\bfuel (?:charge|adjustment)\b"),
    ("cap on increases", r"\bnot to exceed\b|\bshall not exceed\b|\bcapped\b"),
    ("annual true-up or annual minimum", r"\btrue[- ]?up\b|annual minimum|minimum annual"),
    ("non-monthly billing", r"\b(?:invoiced|billed|payable)\s+(?:quarterly|annually|semi-annually|yearly)\b"),
]

_COMPILED = [(label, re.compile(pattern, re.IGNORECASE)) for label, pattern in UNSUPPORTED_PRICING]


@dataclass(frozen=True)
class CoverageFlag:
    label: str
    phrase: str
    line: int

    def __str__(self) -> str:
        return f"contract uses {self.label} (\"{self.phrase}\", line {self.line}), which this engine does not model"


def unsupported_pricing(contract_text: str) -> list[CoverageFlag]:
    """Every place the contract prices something outside the six supported terms."""
    flags: list[CoverageFlag] = []
    seen: set[str] = set()
    for number, line in enumerate(contract_text.splitlines(), start=1):
        for label, pattern in _COMPILED:
            match = pattern.search(line)
            if match and label not in seen:
                seen.add(label)
                flags.append(CoverageFlag(label=label, phrase=match.group(0), line=number))
    return flags
