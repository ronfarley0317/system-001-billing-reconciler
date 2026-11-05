import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from agents.analyst import AuditReport
from agents.scout import ScoutResult
from tools.dossier import card_html, report_html

load_dotenv()

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _send_report(token: str, chat_id: str, report: AuditReport, scout: ScoutResult, contract_id: str) -> bool:
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{contract_id}-{datetime.now():%Y%m%d-%H%M%S}.html"
    path.write_text(report_html(report, scout, contract_id), encoding="utf-8")
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


def send_hitl_dossier(report: AuditReport, scout: ScoutResult, contract_id: str) -> bool:
    """Send the audit report, then the approval card. Returns True if the card was accepted."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[!] Telegram credentials missing in .env")
        return False

    # The report goes first so the card, with its buttons, is the newest message in the chat.
    attached = _send_report(token, chat_id, report, scout, contract_id)

    payload = {
        "chat_id": chat_id,
        "text": card_html(report, scout, contract_id, report_attached=attached),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve Claim", "callback_data": "approve_claim"},
                    {"text": "❌ Deny", "callback_data": "deny_claim"},
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
