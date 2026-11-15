"""Fail-closed coverage: contracts that price things the engine does not model go to a person."""

import unittest
from decimal import Decimal as D

from agents.analyst import reconcile
from agents.coverage import unsupported_pricing
from agents.findings import approvable_total, build_findings
from tests.fixtures import CONTRACT_TEXT, DEMO_CSV, scout, terms


def findings(contract_text, t=None):
    t = t or terms()
    return build_findings(reconcile(t, DEMO_CSV), scout(t), contract_text)


class Scan(unittest.TestCase):
    def test_demo_contract_is_fully_supported(self):
        self.assertEqual(unsupported_pricing(CONTRACT_TEXT), [])
        self.assertEqual(approvable_total(findings(CONTRACT_TEXT)), D("37250.00"))

    def test_each_unsupported_structure_is_caught(self):
        cases = {
            "index-linked increase (CPI)": "Fees adjust annually by the change in the Consumer Price Index.",
            "tiered or volume pricing": "Seats 101 and above are billed at Tier 2 pricing.",
            "proration": "Seats added mid-month are prorated to the day.",
            "amendment or addendum": "As modified by Amendment No. 2 dated March 1, 2026.",
            "credits or rebates": "Customer earns a 2% rebate on annual spend over $500,000.",
            "usage or overage charges": "Data usage in excess of 10 GB is billed as overage.",
            "surcharges": "A fuel surcharge applies to each delivery.",
            "cap on increases": "Increases shall not exceed three percent in any year.",
            "annual true-up or annual minimum": "Any shortfall is settled in an annual true-up.",
            "non-monthly billing": "Fees are invoiced quarterly in advance.",
        }
        for label, sentence in cases.items():
            with self.subTest(label=label):
                flags = unsupported_pricing(CONTRACT_TEXT + "\n" + sentence)
                self.assertEqual([f.label for f in flags], [label])

    def test_any_hit_holds_every_finding(self):
        text = CONTRACT_TEXT + "\nFees adjust annually by the change in the Consumer Price Index.\n"
        f = findings(text)
        self.assertEqual(approvable_total(f), D("0.00"))
        self.assertIn("does not model", " ".join(f[0].reasons))

    def test_flag_names_the_line(self):
        flag = unsupported_pricing("line one\nPrices are subject to CPI adjustment.")[0]
        self.assertEqual(flag.line, 2)
        self.assertIn("line 2", str(flag))


class ModelReportedTerms(unittest.TestCase):
    def test_model_listed_pricing_term_holds_the_run(self):
        f = findings(CONTRACT_TEXT, terms(other_pricing_terms=["Seats above 100 billed at $230.00"]))
        self.assertEqual(approvable_total(f), D("0.00"))
        self.assertIn("model reported another pricing term", " ".join(f[0].reasons))

    def test_payment_terms_do_not_block(self):
        listed = ["late charge of 1.5% per month", "net thirty (30) days", "sales, use, and similar taxes"]
        f = findings(CONTRACT_TEXT, terms(other_pricing_terms=listed))
        self.assertEqual(approvable_total(f), D("37250.00"))


if __name__ == "__main__":
    unittest.main()
