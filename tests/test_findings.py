"""The evidence gate: nothing becomes approvable unless every term behind it checks out."""

import unittest
from decimal import Decimal as D

from agents.analyst import reconcile, reconcile_rows
from agents.findings import MISSING_INVOICE, PENDING_APPROVAL, REVIEW_REQUIRED, approvable_total, build_findings
from schemas.payloads import VerbatimCitation
from tests.fixtures import DEMO_CSV, row, scout, terms


def findings_for(t, rows=None):
    s = scout(t)
    report = reconcile_rows(t, rows) if rows is not None else reconcile(t, DEMO_CSV)
    return build_findings(report, s)


class Gate(unittest.TestCase):
    def test_clean_demo_is_fully_approvable(self):
        f = findings_for(terms())
        self.assertEqual(len(f), 11)
        self.assertTrue(all(x.status == PENDING_APPROVAL for x in f))
        self.assertEqual(approvable_total(f), D("37250.00"))

    def test_invented_quote_blocks_every_finding(self):
        fake = VerbatimCitation(field_name="promo_expiration_date", section="3.2",
                                quote="The Promotional Rate shall expire on June 30, 2025 unless renewed")
        f = findings_for(terms(drop_citations=("promo_expiration_date",), extra_citations=[fake]))
        self.assertTrue(all(x.status == REVIEW_REQUIRED for x in f))
        self.assertEqual(approvable_total(f), D("0.00"))
        self.assertTrue(any("quote not found" in r for r in f[0].reasons))

    def test_misread_number_with_real_quote_is_blocked(self):
        f = findings_for(terms(annual_escalator_pct="50.0"))
        self.assertEqual(approvable_total(f), D("0.00"))
        reasons = " ".join(f[-1].reasons)
        self.assertIn("not in its own quote", reasons)
        self.assertIn("above 25", reasons)

    def test_missing_minimum_citation_blocks_only_minimum_months(self):
        f = findings_for(terms(drop_citations=("monthly_minimum_floor",)))
        blocked = [x for x in f if x.status == REVIEW_REQUIRED]
        self.assertEqual([x.key for x in blocked], ["period:2026-04", "period:2026-05"])
        self.assertEqual(approvable_total(f), D("29250.00"))

    def test_conflicting_citations_block_everything(self):
        other = VerbatimCitation(field_name="base_rate", section="3.4",
                                 quote="Customer shall pay a minimum monthly fee of $12,000.00")
        f = findings_for(terms(extra_citations=[other]))
        self.assertEqual(approvable_total(f), D("0.00"))
        self.assertIn("different citations", " ".join(f[0].reasons))

    def test_promo_rate_without_end_date_is_ambiguous(self):
        f = findings_for(terms(promo_expiration_date=None))
        self.assertEqual(approvable_total(f), D("0.00"))
        self.assertIn("promo rate and promo end date", " ".join(f[0].reasons))

    def test_overbilling_is_review_never_approvable(self):
        f = findings_for(terms(), [row("A", "2025-08-01", 60, "300.00")])
        self.assertEqual(f[0].status, REVIEW_REQUIRED)
        self.assertEqual(f[0].amount, D("-3000.00"))
        self.assertEqual(approvable_total(f), D("0.00"))

    def test_zero_variance_writes_nothing(self):
        self.assertEqual(findings_for(terms(), [row("A", "2025-08-01", 60, "250.00")]), [])

    def test_missing_month_is_a_review_item_with_no_amount(self):
        f = findings_for(terms(), [row("A", "2025-08-01", 60, "250.00"), row("B", "2025-10-01", 60, "250.00")])
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0].variance_class, f[0].status, f[0].amount), (MISSING_INVOICE, REVIEW_REQUIRED, D("0.00")))

    def test_split_invoice_finding_lists_both_invoices(self):
        f = findings_for(terms(), [row("A", "2026-04-01", 20, "262.50"), row("B", "2026-04-15", 20, "262.50")])
        self.assertEqual(f[0].invoice_ids, ("A", "B"))
        self.assertEqual(f[0].amount, D("1500.00"))
        self.assertEqual(f[0].status, PENDING_APPROVAL)
        self.assertIn("applies once for the month", f[0].description)


if __name__ == "__main__":
    unittest.main()
