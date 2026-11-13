"""Ledger writes. Every call goes through a database function from sql/003, so the
rules (client scoping, decided findings stay decided, one run per card) are enforced
by Postgres, not by whoever calls it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv
from supabase import Client, create_client

from agents.findings import Finding

load_dotenv()

CENT = Decimal("0.01")


def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise ValueError("Supabase credentials missing from .env")

    return create_client(url, key)


def client_slug(name: str) -> str:
    """Stable client id from a folder or company name: 'Apex Logistics Group, LLC' -> 'apex-logistics-group-llc'."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot build a client id from {name!r}")
    return slug


@dataclass(frozen=True)
class OpenedRun:
    run_id: str
    approvable_total: Decimal
    approvable_count: int
    review_count: int


def _money(value: Decimal) -> str:
    # Decimal isn't JSON-serializable, and float would lose precision.
    # Send the exact cent value as a string; Postgres NUMERIC parses it losslessly.
    return str(value.quantize(CENT))


def start_audit_run(client_id: str, contract_id: str, sb: Client | None = None) -> str:
    sb = sb or get_supabase_client()
    return sb.rpc("start_audit_run", {"p_client_id": client_id, "p_contract_id": contract_id}).execute().data


def record_finding(run_id: str, finding: Finding, sb: Client | None = None) -> str:
    """Returns the stored status, or KEPT_APPROVED / KEPT_DENIED if a person already decided it."""
    sb = sb or get_supabase_client()
    params = {
        "p_run_id": run_id,
        "p_finding_key": finding.key,
        "p_client_name": finding.customer_name,
        "p_invoice_number": ", ".join(finding.invoice_ids) or finding.key,
        "p_source_invoice_ids": ", ".join(finding.invoice_ids),
        "p_billing_period": finding.period.isoformat(),
        "p_variance_class": finding.variance_class,
        "p_status": finding.status,
        "p_amount": _money(finding.amount),
        "p_citation": finding.citation,
        "p_description": finding.description,
        "p_review_reasons": "; ".join(finding.reasons),
        "p_leak_breakdown": {label: _money(amount) for label, amount in finding.breakdown.items()},
    }
    return sb.rpc("record_finding", params).execute().data


def open_audit_run(run_id: str, sb: Client | None = None) -> OpenedRun:
    sb = sb or get_supabase_client()
    data = sb.rpc("open_audit_run", {"p_run_id": run_id}).execute().data
    return OpenedRun(
        run_id=str(data["run_id"]),
        approvable_total=Decimal(str(data["approvable_total"])),
        approvable_count=int(data["approvable_count"]),
        review_count=int(data["review_count"]),
    )


def record_audit(client_id: str, contract_id: str, findings: list[Finding],
                 sb: Client | None = None) -> tuple[OpenedRun, dict[str, str]]:
    """Start a run, record every finding, then open it for a decision.

    If any write fails, the exception propagates and the run is never opened, so no
    approval card can be sent for it. The next run for this contract supersedes it.
    """
    sb = sb or get_supabase_client()
    run_id = start_audit_run(client_id, contract_id, sb)
    stored = {f.key: record_finding(run_id, f, sb) for f in findings}
    return open_audit_run(run_id, sb), stored
