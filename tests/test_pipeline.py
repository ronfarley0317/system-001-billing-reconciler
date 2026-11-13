"""run_demo.main end to end, with the Scout, the ledger and Telegram mocked."""

import io
import json
import sys
import unittest
from decimal import Decimal as D
from unittest import mock

from rich.console import Console

import run_demo
from tools.db_client import OpenedRun, client_slug, record_audit
from tools.notifier import callback_data, send_hitl_dossier
from tests.fixtures import scout, terms


def opened(total="37250.00", count=11, review=0):
    return OpenedRun(run_id="11111111-2222-3333-4444-555555555555", approvable_total=D(total),
                     approvable_count=count, review_count=review)


class Pipeline(unittest.TestCase):
    def run_main(self, ledger, s=None, argv=("run_demo.py",)):
        s = s or scout()
        with mock.patch.object(sys, "argv", list(argv)), \
             mock.patch.object(run_demo, "console", Console(file=io.StringIO(), width=140)), \
             mock.patch.object(run_demo, "extract_terms", return_value=s), \
             mock.patch.object(run_demo, "record_audit", side_effect=ledger) as rec, \
             mock.patch.object(run_demo, "send_hitl_dossier", return_value=True) as send:
            code = run_demo.main()
        return code, rec, send

    def test_no_card_when_ledger_write_fails(self):
        code, rec, send = self.run_main(RuntimeError("supabase down"))
        self.assertEqual(code, 1)
        rec.assert_called_once()
        send.assert_not_called()

    def test_card_covers_exactly_the_open_run(self):
        def ledger(client_id, contract, findings):
            return opened(), {f.key: f.status for f in findings}
        code, rec, send = self.run_main(ledger)
        self.assertEqual(code, 0)
        client_id, contract, findings = rec.call_args.args
        self.assertTrue(client_id.startswith("demo-"))
        self.assertEqual(contract, "MS-OF-2025-0117")
        self.assertEqual(len(findings), 11)
        view = send.call_args.args[0]
        self.assertEqual(view.total_arrears, D("37250.00"))
        self.assertTrue(send.call_args.kwargs["attach_report"])  # synthetic demo only

    def test_previously_approved_months_are_left_off_the_card(self):
        def ledger(client_id, contract, findings):
            stored = {f.key: f.status for f in findings}
            stored["period:2025-07"] = "KEPT_APPROVED"
            return opened(total="34250.00", count=10), stored
        code, _, send = self.run_main(ledger)
        self.assertEqual(code, 0)
        self.assertEqual(send.call_args.args[0].total_arrears, D("34250.00"))

    def test_mismatch_between_ledger_and_audit_sends_nothing(self):
        def ledger(client_id, contract, findings):
            return opened(total="1.00"), {f.key: f.status for f in findings}
        code, _, send = self.run_main(ledger)
        self.assertEqual(code, 1)
        send.assert_not_called()

    def test_weak_evidence_sends_no_card(self):
        def ledger(client_id, contract, findings):
            self.assertTrue(all(f.status == "REVIEW_REQUIRED" for f in findings))
            return opened(total="0.00", count=0, review=len(findings)), {f.key: f.status for f in findings}
        code, _, send = self.run_main(ledger, s=scout(terms(annual_escalator_pct="50.0")))
        self.assertEqual(code, 0)
        send.assert_not_called()


class LedgerCalls(unittest.TestCase):
    def test_record_audit_uses_rpc_in_order_and_sends_strings(self):
        calls = []

        class FakeRpc:
            def __init__(self, name, params): self.name, self.params = name, params
            def execute(self):
                calls.append((self.name, self.params))
                data = {"start_audit_run": "run-1", "record_finding": "PENDING_APPROVAL",
                        "open_audit_run": {"run_id": "run-1", "approvable_total": "37250.00",
                                           "approvable_count": 11, "review_count": 0}}[self.name]
                return mock.Mock(data=data)

        sb = mock.Mock(rpc=lambda name, params: FakeRpc(name, params))
        from agents.analyst import reconcile
        from agents.findings import build_findings
        from tests.fixtures import DEMO_CSV
        findings = build_findings(reconcile(terms(), DEMO_CSV), scout())
        run, stored = record_audit("apex", "C1", findings, sb=sb)
        self.assertEqual([c[0] for c in calls], ["start_audit_run"] + ["record_finding"] * 11 + ["open_audit_run"])
        first = calls[1][1]
        self.assertIsInstance(first["p_amount"], str)
        json.dumps(first)  # everything sent is JSON-safe; no Decimal or float
        self.assertEqual(first["p_leak_breakdown"],
                         {"Promo ended Jun 30, 2025; still billed at the $200.00 promo rate": "3000.00"})
        self.assertEqual(run.approvable_total, D("37250.00"))

    def test_failed_write_never_opens_the_run(self):
        names = []

        class FakeRpc:
            def __init__(self, name, params): self.name = name
            def execute(self):
                names.append(self.name)
                if self.name == "record_finding" and names.count("record_finding") == 3:
                    raise RuntimeError("network")
                return mock.Mock(data="run-1" if self.name == "start_audit_run" else "PENDING_APPROVAL")

        sb = mock.Mock(rpc=lambda name, params: FakeRpc(name, params))
        from agents.analyst import reconcile
        from agents.findings import build_findings
        from tests.fixtures import DEMO_CSV
        with self.assertRaises(RuntimeError):
            record_audit("apex", "C1", build_findings(reconcile(terms(), DEMO_CSV), scout()), sb=sb)
        self.assertNotIn("open_audit_run", names)

    def test_client_slug(self):
        self.assertEqual(client_slug("Apex Logistics Group, LLC"), "apex-logistics-group-llc")
        with self.assertRaises(ValueError):
            client_slug("!!!")


class TelegramCard(unittest.TestCase):
    def test_buttons_carry_the_run_id(self):
        run_id = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(callback_data("approve", run_id), f"approve:{run_id}")
        self.assertLessEqual(len(callback_data("approve", run_id).encode()), 64)
        with self.assertRaises(ValueError):
            callback_data("approve_all", run_id)

    def test_client_report_is_not_attached(self):
        from agents.analyst import reconcile
        from agents.findings import approvable_view, build_findings
        from tests.fixtures import DEMO_CSV
        report = reconcile(terms(), DEMO_CSV)
        view = approvable_view(report, build_findings(report, scout()))
        posted = []
        fake = mock.Mock(status_code=200)
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             mock.patch("tools.notifier.httpx.post", side_effect=lambda url, **kw: posted.append((url, kw)) or fake), \
             mock.patch("tools.notifier._save_report"):
            self.assertTrue(send_hitl_dossier(view, scout(), "C1", opened(), attach_report=False))
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0][0].endswith("/sendMessage"))
        body = posted[0][1]["json"]
        self.assertIn("$37,250.00", body["text"])
        self.assertEqual(body["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         "approve:11111111-2222-3333-4444-555555555555")

    def test_nothing_approvable_sends_nothing(self):
        with mock.patch("tools.notifier.httpx.post") as post:
            self.assertFalse(send_hitl_dossier(None, None, "C1", opened(total="0.00", count=0), attach_report=False))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
