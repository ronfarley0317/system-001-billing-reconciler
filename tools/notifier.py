import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from agents.analyst import AuditReport
from agents.scout import ScoutResult
from tools.db_client import OpenedRun
from tools.dossier import card_html, report_html

load_dotenv()

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _save_report(report: AuditReport, scout: ScoutResult, contract_id: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{contract_id}-{datetime.now():%Y%m%d-%H%M%S}.html"
    path.write_text(report_html(report, scout, contract_id), encoding="utf-8")
    return path


def _send_report(token: str, chat_id: str, report: AuditReport, scout: ScoutResult, contract_id: str) -> bool:
    path = _save_report(report, scout, contract_id)
    try:
        with path.open("rb") as fh:
            response = httpx.post(
                _api(token, "sendDocument"),
                data={"chat_id": chat_id, "caption": f"Line-by-line audit: {contract_id}"},
                files={"document": (path.name, fh, "text/html")},
                timeout=20,
            )
    except httpx.HTTPError as exc:
        print(f"\n[!] Failed to send audit report: {exc}")
        return False
    if response.status_code != 200:
        print(f"\n[!] Failed to send audit report: {response.text}")
    return response.status_code == 200


def callback_data(decision: str, run_id: str) -> str:
    """Button payload. The n8n workflow reads the run id from here, never from the card text.

    Telegram allows 64 bytes: "approve:" + a 36-character UUID is 44.
    """
    if decision not in ("approve", "deny"):
        raise ValueError(decision)
    data = f"{decision}:{run_id}"
    if len(data.encode()) > 64:
        raise ValueError("callback data over Telegram's 64-byte limit")
    return data


def send_hitl_dossier(report: AuditReport, scout: ScoutResult, contract_id: str,
                      run: OpenedRun, attach_report: bool, review_count: int = 0) -> bool:
    """Send the approval card for one audit run. Returns True if the card was accepted.

    `report` is the approvable view (see agents.findings.approvable_view). The full
    line-by-line report quotes the contract, so it is only attached when `attach_report`
    is true (the synthetic demo). For a client, it stays in reports/ on this machine.
    """
    if run.approvable_count == 0:
        print("[!] Nothing approvable in this run. No card sent.")
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[!] Telegram credentials missing in .env")
        return False

    if attach_report:
        # The report goes first so the card, with its buttons, is the newest message in the chat.
        attached = _send_report(token, chat_id, report, scout, contract_id)
    else:
        _save_report(report, scout, contract_id)
        attached = False

    payload = {
        "chat_id": chat_id,
        "text": card_html(report, scout, contract_id, report_attached=attached, run=run, review_count=review_count),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve Claim", "callback_data": callback_data("approve", run.run_id)},
                    {"text": "❌ Deny", "callback_data": callback_data("deny", run.run_id)},
                ]
            ]
        },
    }

    try:
        response = httpx.post(_api(token, "sendMessage"), json=payload, timeout=10)
    except httpx.HTTPError as exc:
        print(f"\n[!] Failed to reach Telegram: {exc}")
        return False

    if response.status_code == 200:
        print("\n[+] Telegram HITL Approval Card sent successfully.")
        return True
    print(f"\n[!] Failed to send Telegram card: {response.text}")
    return False
