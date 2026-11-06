"""Analyst agent: deterministic billing reconciliation.

Pure Python `Decimal` + `csv`. No LLM, no floats. Given the extracted contract
terms and the ERP export, it recomputes what each invoice *should* have been and
attributes every cent of drift to a specific contract clause.

Expected amount for an invoice dated D with quantity Q:
    1. rate      = promo_rate if D <= promo_expiration_date else base_rate
    2. rate      = rate * (1 + escalator%) for each escalation date <= D
                   (compounded, rounded to cents at each step)
    3. usage     = rate * Q
    4. expected  = max(usage, monthly_minimum_floor)
    drift        = expected - billed           (positive = under-billed / arrears)
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from schemas.payloads import ExtractedContractTerms

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# Leak categories, attributed in clause order so components sum exactly to drift.
LEAK_PROMO = "Expired promo rate still billed"
LEAK_ESCALATOR = "Annual escalator not applied"
LEAK_FLOOR = "Monthly minimum not enforced"
LEAK_OTHER = "Other rate variance"


@dataclass(frozen=True)
class LineAudit:
    invoice_id: str
    customer_name: str
    invoice_date: date
    quantity: int
    billed_unit_price: Decimal
    billed_amount: Decimal
    contract_unit_rate: Decimal
    usage_amount: Decimal
    expected_amount: Decimal
    drift: Decimal
    rate_basis: str  # "PROMO" | "BASE" | "BASE +5.0% x1" ...
    floor_applied: bool
    leaks: dict[str, Decimal] = field(default_factory=dict)

    @property
    def is_leak(self) -> bool:
        return self.drift != ZERO


@dataclass(frozen=True)
class AuditReport:
    lines: list[LineAudit]
    total_billed: Decimal
    total_expected: Decimal
    total_arrears: Decimal
    leak_totals: dict[str, Decimal]

    @property
    def leaking_lines(self) -> list[LineAudit]:
        return [line for line in self.lines if line.is_leak]


def escalations_applied(effective: date | None, on: date) -> int:
    """Number of escalation anniversaries (effective date + k years) on or before `on`."""
    if effective is None or on < effective:
        return 0
    years = on.year - effective.year
    if (on.month, on.day) < (effective.month, effective.day):
        years -= 1
    return years + 1


def contract_rate(terms: ExtractedContractTerms, on: date) -> tuple[Decimal, Decimal, str, int]:
    """Return (pre_escalator_rate, escalated_rate, basis_label, escalation_count)."""
    in_promo = (
        terms.promo_rate is not None
        and terms.promo_expiration_date is not None
        and on <= terms.promo_expiration_date
    )
    rate = money(terms.promo_rate if in_promo else terms.base_rate)
    basis = "PROMO" if in_promo else "BASE"

    n = escalations_applied(terms.escalator_effective_date, on) if terms.annual_escalator_pct else 0
    escalated = rate
    if n:
        factor = Decimal(1) + (terms.annual_escalator_pct / HUNDRED)
        for _ in range(n):
            escalated = money(escalated * factor)
        basis += f" +{terms.annual_escalator_pct}% x{n}"
    return rate, escalated, basis, n


def audit_row(terms: ExtractedContractTerms, row: dict[str, str]) -> LineAudit:
    inv_date = date.fromisoformat(row["invoice_date"].strip())
    qty = int(row["quantity"])
    billed_unit = Decimal(row["unit_price"].strip())
    billed = money(Decimal(row["amount_billed"].strip()))

    pre_rate, rate, basis, _ = contract_rate(terms, inv_date)

    # Waterfall: billed -> correct base/promo rate -> escalated rate -> floor.
    step_rate = money(pre_rate * qty)
    usage = money(rate * qty)
    floor = terms.monthly_minimum_floor
    expected = max(usage, money(floor)) if floor is not None else usage
    floor_applied = expected > usage

    leaks: dict[str, Decimal] = {}
    rate_gap = step_rate - billed
    if rate_gap:
        promo_expired = (
            terms.promo_rate is not None
            and terms.promo_expiration_date is not None
            and inv_date > terms.promo_expiration_date
            and billed_unit == money(terms.promo_rate)
        )
        leaks[LEAK_PROMO if promo_expired else LEAK_OTHER] = rate_gap
    if usage - step_rate:
        leaks[LEAK_ESCALATOR] = usage - step_rate
    if expected - usage:
        leaks[LEAK_FLOOR] = expected - usage

    drift = expected - billed
    if sum(leaks.values(), ZERO) != drift:
        raise ArithmeticError(f"{row['invoice_id']}: leak attribution does not reconcile to drift")

    return LineAudit(
        invoice_id=row["invoice_id"],
        customer_name=row.get("customer_name", "").strip(),
        invoice_date=inv_date,
        quantity=qty,
        billed_unit_price=billed_unit,
        billed_amount=billed,
        contract_unit_rate=rate,
        usage_amount=usage,
        expected_amount=expected,
        drift=drift,
        rate_basis=basis,
        floor_applied=floor_applied,
        leaks=leaks,
    )


def reconcile(terms: ExtractedContractTerms, csv_path: Path) -> AuditReport:
    with csv_path.open(newline="", encoding="utf-8") as fh:
        return reconcile_rows(terms, list(csv.DictReader(fh)))


def reconcile_rows(terms: ExtractedContractTerms, rows: list[dict[str, str]]) -> AuditReport:
    """Rows in the canonical layout (see tools/ingest.py): ISO dates, plain decimal strings."""
    lines = sorted((audit_row(terms, r) for r in rows), key=lambda l: l.invoice_date)

    leak_totals: dict[str, Decimal] = {}
    for line in lines:
        for name, amount in line.leaks.items():
            leak_totals[name] = leak_totals.get(name, ZERO) + amount

    return AuditReport(
        lines=lines,
        total_billed=sum((l.billed_amount for l in lines), ZERO),
        total_expected=sum((l.expected_amount for l in lines), ZERO),
        total_arrears=sum((l.drift for l in lines), ZERO),
        leak_totals=leak_totals,
    )
