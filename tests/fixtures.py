"""Synthetic fixtures shared by the tests. No Ollama, Supabase, or Telegram needed."""

from __future__ import annotations

from pathlib import Path

from agents.scout import ScoutResult, verify_citations
from schemas.payloads import ExtractedContractTerms, VerbatimCitation

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_TEXT = (ROOT / "demo_data" / "apex_contract_2025.md").read_text()
DEMO_CSV = ROOT / "demo_data" / "erp_invoices_export.csv"

# Real sentences from demo_data/apex_contract_2025.md.
QUOTES = {
    "base_rate": ("3.1", "Customer shall pay a subscription fee of $250.00 per Active Seat per month"),
    "promo_rate": ("3.2", "Customer shall pay a reduced rate of $200.00 per Active Seat per month"),
    "promo_expiration_date": ("3.2", "The Promotional Rate shall expire automatically on June 30, 2025"),
    "annual_escalator_pct": ("3.3", "all per-seat Fees shall increase by five percent (5.0%) over the rate in effect"),
    "escalator_effective_date": ("3.3", "Commencing January 1, 2026, and on each anniversary of that date during the Term"),
    "monthly_minimum_floor": ("3.4", "Customer shall pay a minimum monthly fee of $12,000.00"),
}

DEMO_VALUES = dict(
    base_rate="250.00", promo_rate="200.00", promo_expiration_date="2025-06-30",
    annual_escalator_pct="5.0", escalator_effective_date="2026-01-01", monthly_minimum_floor="12000.00",
)


def terms(drop_citations: tuple[str, ...] = (), extra_citations: list[VerbatimCitation] | None = None,
          **overrides) -> ExtractedContractTerms:
    values = {**DEMO_VALUES, **overrides}
    citations = [
        VerbatimCitation(field_name=name, section=sec, quote=quote)
        for name, (sec, quote) in QUOTES.items()
        if name not in drop_citations and values.get(name) is not None
    ]
    return ExtractedContractTerms(**values, verbatim_citations=citations + (extra_citations or []))


def scout(t: ExtractedContractTerms | None = None) -> ScoutResult:
    t = t or terms()
    return ScoutResult(terms=t, citation_checks=verify_citations(t, CONTRACT_TEXT), model="fixture", elapsed_s=0.0)


def row(invoice_id: str, invoice_date: str, quantity: int, unit_price: str, amount: str | None = None,
        customer: str = "Apex Logistics Group, LLC") -> dict[str, str]:
    from decimal import Decimal

    if amount is None:
        amount = f"{Decimal(unit_price) * quantity:.2f}"
    return {"invoice_id": invoice_id, "invoice_date": invoice_date, "customer_name": customer,
            "quantity": str(quantity), "unit_price": unit_price, "amount_billed": amount}
