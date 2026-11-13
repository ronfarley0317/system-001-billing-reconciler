"""Financial math: exact cents, variance classes, and the monthly minimum per billing period."""

import unittest
from datetime import date
from decimal import Decimal as D

from agents.analyst import (
    LEAK_ESCALATOR, LEAK_FLOOR, LEAK_PROMO, NEEDS_REVIEW, OVERBILLED, UNDERBILLED, ZERO_VARIANCE,
    contract_rate, escalations_applied, reconcile, reconcile_rows,
)
from tests.fixtures import DEMO_CSV, row, terms


class DemoDataset(unittest.TestCase):
    """The published numbers ($37,250 on 11 of 12 invoices) must not move."""

    def setUp(self):
        self.report = reconcile(terms(), DEMO_CSV)

    def test_totals_to_the_cent(self):
        self.assertEqual(self.report.total_arrears, D("37250.00"))
        self.assertEqual(self.report.total_expected, D("173250.00"))
        self.assertEqual(self.report.total_billed, D("136000.00"))
        self.assertEqual(self.report.total_overbilled, D("0.00"))

    def test_eleven_of_twelve_underbilled(self):
        self.assertEqual(len(self.report.leaking_lines), 11)
        self.assertEqual(len(self.report.by_class(UNDERBILLED)), 11)
        self.assertEqual(len(self.report.by_class(ZERO_VARIANCE)), 1)
        self.assertEqual(self.report.missing_periods, [])

    def test_breakdown_sums_to_total(self):
        self.assertEqual(sum(self.report.leak_totals.values()), self.report.total_arrears)
        self.assertEqual(self.report.leak_totals[LEAK_PROMO], D("31000.00"))
        self.assertEqual(self.report.leak_totals[LEAK_ESCALATOR], D("3250.00"))
        self.assertEqual(self.report.leak_totals[LEAK_FLOOR], D("3000.00"))


class RatesAndDates(unittest.TestCase):
    def test_promo_boundary_is_inclusive(self):
        t = terms()
        self.assertEqual(contract_rate(t, date(2025, 6, 30))[1], D("200.00"))
        self.assertEqual(contract_rate(t, date(2025, 7, 1))[1], D("250.00"))

    def test_escalator_anniversaries(self):
        eff = date(2026, 1, 1)
        self.assertEqual(escalations_applied(eff, date(2025, 12, 31)), 0)
        self.assertEqual(escalations_applied(eff, date(2026, 1, 1)), 1)
        self.assertEqual(escalations_applied(eff, date(2026, 12, 31)), 1)
        self.assertEqual(escalations_applied(eff, date(2027, 1, 1)), 2)

    def test_compounding_rounds_to_cents_each_step(self):
        t = terms(base_rate="99.99", promo_rate=None, promo_expiration_date=None, annual_escalator_pct="3.5")
        # 99.99 x 1.035 = 103.48965 -> 103.49 ; x 1.035 = 107.11215 -> 107.11
        self.assertEqual(contract_rate(t, date(2026, 1, 1))[1], D("103.49"))
        self.assertEqual(contract_rate(t, date(2027, 1, 1))[1], D("107.11"))

    def test_half_cent_rounds_up(self):
        t = terms(base_rate="10.10", promo_rate=None, promo_expiration_date=None, annual_escalator_pct="5.0",
                  monthly_minimum_floor=None)
        # 10.10 x 1.05 = 10.605 -> 10.61 (ROUND_HALF_UP, not banker's rounding)
        self.assertEqual(contract_rate(t, date(2026, 6, 1))[1], D("10.61"))


class VarianceClasses(unittest.TestCase):
    def test_overbilling_is_never_a_recovery_claim(self):
        r = reconcile_rows(terms(), [row("A", "2025-08-01", 60, "300.00")])
        self.assertEqual(r.periods[0].variance_class, OVERBILLED)
        self.assertEqual(r.total_arrears, D("0.00"))
        self.assertEqual(r.total_overbilled, D("3000.00"))
        self.assertEqual(r.leaking_lines, [])
        self.assertEqual(r.leak_totals, {})

    def test_overbilled_month_does_not_reduce_underbilled_total(self):
        r = reconcile_rows(terms(), [row("A", "2025-08-01", 60, "300.00"), row("B", "2025-09-01", 60, "200.00")])
        self.assertEqual(r.total_arrears, D("3000.00"))  # old code netted this to 0.00
        self.assertEqual(r.total_overbilled, D("3000.00"))

    def test_exact_billing_is_zero_variance(self):
        r = reconcile_rows(terms(), [row("A", "2025-08-01", 60, "250.00")])
        self.assertEqual(r.periods[0].variance_class, ZERO_VARIANCE)
        self.assertEqual(r.total_arrears, D("0.00"))

    def test_credit_line_goes_to_review(self):
        r = reconcile_rows(terms(), [row("A", "2025-08-01", 60, "250.00"),
                                     row("CR", "2025-08-15", -1, "250.00", amount="-250.00")])
        self.assertEqual(r.periods[0].variance_class, NEEDS_REVIEW)
        self.assertEqual(r.total_arrears, D("0.00"))

    def test_row_that_disagrees_with_itself_goes_to_review(self):
        r = reconcile_rows(terms(), [row("A", "2025-08-01", 60, "200.00", amount="11000.00")])
        self.assertEqual(r.periods[0].variance_class, NEEDS_REVIEW)
        self.assertIn("row math", r.periods[0].reasons[0])

    def test_mixed_signs_in_one_month_go_to_review(self):
        r = reconcile_rows(terms(), [row("A", "2025-08-01", 30, "300.00"), row("B", "2025-08-15", 30, "200.00")])
        self.assertEqual(r.periods[0].variance_class, NEEDS_REVIEW)
        self.assertEqual(r.total_arrears, D("0.00"))


class MonthlyMinimum(unittest.TestCase):
    def test_minimum_counts_once_for_split_invoices(self):
        # Apr 2026, two invoices of 20 seats at the correct $262.50: $10,500 total, $12,000 minimum.
        r = reconcile_rows(terms(), [row("A", "2026-04-01", 20, "262.50"), row("B", "2026-04-15", 20, "262.50")])
        p = r.periods[0]
        self.assertEqual(len(p.lines), 2)
        self.assertEqual(p.minimum_shortfall, D("1500.00"))
        self.assertEqual(p.drift, D("1500.00"))  # per-invoice logic would claim 13,500.00
        self.assertEqual(r.total_arrears, D("1500.00"))
        self.assertTrue(all(not l.floor_applied for l in p.lines))

    def test_split_invoices_above_minimum_owe_nothing(self):
        r = reconcile_rows(terms(), [row("A", "2026-04-01", 30, "262.50"), row("B", "2026-04-20", 30, "262.50")])
        self.assertEqual(r.periods[0].variance_class, ZERO_VARIANCE)

    def test_single_invoice_month_keeps_floor_on_the_line(self):
        r = reconcile_rows(terms(), [row("A", "2026-04-01", 40, "200.00")])
        line = r.lines[0]
        self.assertTrue(line.floor_applied)
        self.assertEqual(line.expected_amount, D("12000.00"))
        self.assertEqual(line.drift, D("4000.00"))

    def test_periods_are_per_customer(self):
        r = reconcile_rows(terms(), [row("A", "2026-04-01", 20, "262.50", customer="One"),
                                     row("B", "2026-04-01", 20, "262.50", customer="Two")])
        self.assertEqual(len(r.periods), 2)
        self.assertEqual(r.total_arrears, D("13500.00"))  # each customer has its own minimum

    def test_missing_month_is_reported(self):
        r = reconcile_rows(terms(), [row("A", "2025-08-01", 60, "250.00"), row("B", "2025-10-01", 60, "250.00")])
        self.assertEqual(r.missing_periods, [date(2025, 9, 1)])
        self.assertEqual(r.total_arrears, D("0.00"))  # never guessed into a claim


if __name__ == "__main__":
    unittest.main()
