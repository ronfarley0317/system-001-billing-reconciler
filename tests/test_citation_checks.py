"""Both citation checks, run against the synthetic Apex contract. No Ollama needed.

    python -m unittest discover tests
"""

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from agents.scout import value_in_quote, verify_citations
from schemas.payloads import ExtractedContractTerms, VerbatimCitation

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = (ROOT / "demo_data" / "apex_contract_2025.md").read_text(encoding="utf-8")

PROMO_END = ("The Promotional Rate shall expire automatically on June 30, 2025, and the Base Rate "
             "shall apply to all billing periods thereafter without further notice")
ESCALATOR = ("all per-seat Fees shall increase by five percent (5.0%) over the rate in effect "
             "immediately prior to such date")
MINIMUM = 'Customer shall pay a minimum monthly fee of $12,000.00 (the "Monthly Minimum")'


def terms(escalator: str = "5.0", citations: list[VerbatimCitation] | None = None) -> ExtractedContractTerms:
    return ExtractedContractTerms(
        base_rate="250.00", promo_rate="200.00", promo_expiration_date="2025-06-30",
        annual_escalator_pct=escalator, escalator_effective_date="2026-01-01",
        monthly_minimum_floor="12000.00",
        verbatim_citations=citations or [
            VerbatimCitation(field_name="promo_expiration_date", section="3.2", quote=PROMO_END),
            VerbatimCitation(field_name="annual_escalator_pct", section="3.3", quote=ESCALATOR),
            VerbatimCitation(field_name="monthly_minimum_floor", section="3.4", quote=MINIMUM),
        ],
    )


class QuoteMustExist(unittest.TestCase):
    def test_real_quotes_pass(self):
        checks = verify_citations(terms(), CONTRACT)
        self.assertTrue(all(c.verified for c in checks))

    def test_invented_quote_is_not_found(self):
        fake = VerbatimCitation(field_name="annual_escalator_pct", section="3.3",
                                quote="Fees shall increase by three percent (3.0%) each year")
        (check,) = verify_citations(terms(citations=[fake]), CONTRACT)
        self.assertFalse(check.verified)
        self.assertFalse(check.ok)


class ValueMustBeInItsQuote(unittest.TestCase):
    def test_correct_values_pass(self):
        checks = verify_citations(terms(), CONTRACT)
        self.assertTrue(all(c.ok for c in checks))

    def test_misread_number_with_real_quote_is_caught(self):
        checks = {c.field_name: c for c in verify_citations(terms(escalator="50.0"), CONTRACT)}
        escalator = checks["annual_escalator_pct"]
        self.assertTrue(escalator.verified)          # the quote is real...
        self.assertFalse(escalator.value_in_quote)   # ...but 50.0 is not in it
        self.assertTrue(checks["monthly_minimum_floor"].ok)

    def test_number_formats(self):
        self.assertTrue(value_in_quote(Decimal("12000.00"), "a fee of $12,000.00 per month"))
        self.assertTrue(value_in_quote(Decimal("12000.00"), "a fee of $12,000 per month"))
        self.assertTrue(value_in_quote(Decimal("5.0"), "increase by 5% each year"))
        self.assertFalse(value_in_quote(Decimal("1200.00"), "a fee of $12,000.00 per month"))

    def test_date_formats(self):
        d = date(2025, 6, 30)
        self.assertTrue(value_in_quote(d, "expires on June 30, 2025, and"))
        self.assertTrue(value_in_quote(d, "expires on 06/30/2025"))
        self.assertFalse(value_in_quote(d, "expires on June 3, 2025"))

    def test_words_only_value_is_flagged_not_guessed(self):
        self.assertFalse(value_in_quote(Decimal("5.0"), "increase by five percent each year"))

    def test_missing_value_is_flagged(self):
        self.assertFalse(value_in_quote(None, MINIMUM))


if __name__ == "__main__":
    unittest.main()
