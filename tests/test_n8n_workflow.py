"""The exported n8n approval workflow: structure checks (no n8n instance needed)."""

import json
import unittest
from pathlib import Path

WORKFLOW = json.loads((Path(__file__).resolve().parent.parent / "n8n" / "telegram_approval_webhook.json").read_text())
NODES = {n["name"]: n for n in WORKFLOW["nodes"]}


def branches(name: str) -> list[list[str]]:
    return [[c["node"] for c in out] for out in WORKFLOW["connections"].get(name, {}).get("main", [])]


def parents(name: str) -> list[tuple[str, int]]:
    return [(src, i) for src, conn in WORKFLOW["connections"].items()
            for i, out in enumerate(conn.get("main", [])) for c in out if c["node"] == name]


class ApprovalWorkflow(unittest.TestCase):
    def test_card_text_is_never_used_as_identity(self):
        self.assertNotIn("callback_query.message.text", json.dumps(WORKFLOW))

    def test_no_direct_table_updates(self):
        self.assertFalse([n for n in NODES.values() if n["type"] == "n8n-nodes-base.supabase"])

    def test_decision_goes_through_the_database_function(self):
        decide = NODES["Decide in Supabase"]["parameters"]
        self.assertTrue(decide["url"].endswith("/rest/v1/rpc/decide_audit_run"))
        for field in ("p_run_id", "p_decision", "p_telegram_user_id"):
            self.assertIn(field, decide["jsonBody"])

    def test_finance_email_only_after_a_recorded_approval_or_an_authorized_resend(self):
        self.assertEqual(parents("Email Finance"), [("Finance Payload", 0)])
        self.assertEqual(sorted(parents("Finance Payload")), [("Answer Resending", 0), ("Approved?", 0)])
        self.assertEqual(parents("Approved?"), [("Answer Recorded", 0)])
        self.assertEqual(parents("Answer Recorded"), [("Decision Recorded?", 0)])
        self.assertEqual(parents("Decision Recorded?"), [("Decide in Supabase", 0)])
        self.assertEqual(parents("Decide in Supabase"), [("Resend?", 1)])
        self.assertEqual(parents("Answer Resending"), [("Payload Ready?", 0)])
        self.assertEqual(parents("Payload Ready?"), [("Get Finance Payload", 0)])
        self.assertEqual(parents("Get Finance Payload"), [("Resend?", 0)])
        self.assertEqual(parents("Resend?"), [("Valid Button?", 0)])
        self.assertTrue(NODES["Get Finance Payload"]["parameters"]["url"].endswith("/rpc/finance_payload"))

    def test_failed_email_alerts_with_a_resend_button_and_is_not_marked_sent(self):
        self.assertEqual(NODES["Email Finance"].get("onError"), "continueErrorOutput")
        self.assertEqual(branches("Email Finance"), [["Mark Finance Notified"], ["Alert Email Failed"]])
        self.assertEqual(branches("Mark Finance Notified"), [["Send Approval Confirmation"]])
        button = NODES["Alert Email Failed"]["parameters"]["inlineKeyboard"]["rows"][0]["row"]["buttons"][0]
        self.assertTrue(button["additionalFields"]["callback_data"].startswith("=resend:"))
        self.assertNotIn("Alert Email Failed", WORKFLOW["connections"])

    def test_rejections_and_bad_buttons_send_nothing_to_finance(self):
        self.assertEqual(branches("Decision Recorded?")[1], ["Answer Rejected"])
        self.assertEqual(branches("Valid Button?")[1], ["Answer Invalid Button"])
        self.assertEqual(branches("Payload Ready?")[1], ["Answer Resend Refused"])
        self.assertNotIn("Answer Rejected", WORKFLOW["connections"])
        self.assertNotIn("Answer Invalid Button", WORKFLOW["connections"])
        self.assertNotIn("Answer Resend Refused", WORKFLOW["connections"])

    def test_email_lists_only_rows_returned_by_this_decision(self):
        msg = NODES["Email Finance"]["parameters"]["message"]
        self.assertIn("$('Finance Payload').item.json.rows", msg)
        self.assertNotIn("getAll", json.dumps(WORKFLOW))

    def test_email_is_one_line_per_cause_and_uses_the_client_name(self):
        params = NODES["Email Finance"]["parameters"]
        self.assertIn(".reasons.map(", params["message"])
        self.assertNotIn("r.why", params["message"])
        self.assertIn(".client_name", params["subject"])
        self.assertNotIn("client_id", params["subject"] + params["message"])


if __name__ == "__main__":
    unittest.main()
