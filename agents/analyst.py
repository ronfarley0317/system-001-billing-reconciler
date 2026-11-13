"""Analyst agent: deterministic billing reconciliation.

Pure Python `Decimal` + `csv`. No LLM, no floats. Given the extracted contract
terms and the ERP export, it recomputes what each invoice *should* have been and
attributes every cent of variance to a specific contract clause.

Two levels, because a monthly minimum applies to a month, not to an invoice:

Invoice line (rate variance), for an invoice dated D with quantity Q:
    1. rate      = promo_rate if D <= promo_expiration_date else base_rate
    2. rate      = rate * (1 + escalator%) for each escalation date <= D
                   (compounded, rounded to cents at each step)
    3. usage     = rate * Q
    line variance = usage - billed

Billing period (calendar month of the invoice date, per customer):
    expected     = max(sum of usage, monthly_minimum_floor)   (minimum counted once)
    shortfall    = expected - sum of usage
    drift        = expected - sum of billed                   (positive = under-billed)

Every period is classified. Only UNDERBILLED periods count toward a recovery
claim; over-billing, credits, mixed signs and inconsistent rows go to a person.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field, replace
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

# Variance classes. Only UNDERBILLED may become a recovery claim.
UNDERBILLED = "UNDERBILLED"
OVERBILLED = "OVERBILLED"
ZERO_VARIANCE = "ZERO"
NEEDS_REVIEW = "REVIEW"


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
    escalations: int = 0
    row_problems: tuple[str, ...] = ()  # the row disagrees with itself (credit, qty x price != amount)

    @property
    def is_leak(self) -> bool:
        """Under-billed line. Over-billing is never a leak; it is reported separately."""
        return self.drift > ZERO


@dataclass(frozen=True)
class PeriodAudit:
    """One customer's billing month: the unit a monthly minimum applies to."""
    customer_name: str
    period: date  # first day of the month
    lines: tuple[LineAudit, ...]
    usage_total: Decimal
    billed_total: Decimal
    minimum_shortfall: Decimal
    expected_total: Decimal
    drift: Decimal
    variance_class: str
    leaks: dict[str, Decimal]
    reasons: tuple[str, ...] = ()

    @property
    def invoice_ids(self) -> tuple[str, ...]:
        return tuple(l.invoice_id for l in self.lines)

    @property
    def key(self) -> str:
        return f"period:{self.period:%Y-%m}"


@dataclass(frozen=True)
class AuditReport:
    lines: list[LineAudit]
    total_billed: Decimal
    total_expected: Decimal
    total_arrears: Decimal  # under-billing in UNDERBILLED periods only: the recovery figure
    leak_totals: dict[str, Decimal]  # UNDERBILLED periods only
    periods: list[PeriodAudit] = field(default_factory=list)
    total_overbilled: Decimal = ZERO  # positive number: how much the customer was over-charged
    missing_periods: list[date] = field(default_factory=list)  # months inside the range with no invoice

    @property
    def leaking_lines(self) -> list[LineAudit]:
        """Lines in under-billed periods (what a recovery claim would cover)."""
        ids = {i for p in self.periods if p.variance_class == UNDERBILLED for i in p.invoice_ids}
        return [line for line in self.lines if line.invoice_id in ids]

    def by_class(self, variance_class: str) -> list[PeriodAudit]:
        return [p for p in self.periods if p.variance_class == variance_class]


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
    """Rate variance for one invoice line. The monthly minimum is applied per period, not here."""
    inv_date = date.fromisoformat(row["invoice_date"].strip())
    qty = int(row["quantity"])
    billed_unit = Decimal(row["unit_price"].strip())
    billed = money(Decimal(row["amount_billed"].strip()))

    problems: list[str] = []
    if billed < ZERO or qty < 0:
        problems.append("credit or negative line: needs a person")
    elif money(billed_unit * qty) != billed:
        problems.append(f"row math does not match: {qty} x {billed_unit} != {billed}")

    pre_rate, rate, basis, n = contract_rate(terms, inv_date)

    # Waterfall: billed -> correct base/promo rate -> escalated rate.
    step_rate = money(pre_rate * qty)
    usage = money(rate * qty)

    leaks: dict[str, Decimal] = {}
    rate_gap = step_rate - billed
    if rate_gap:
        promo_expired = (
            terms.promo_rate is not None
            and terms.promo_expiration_date is not None
            and inv_date > terms.promo_expiration_date
            and billed_unit == money(terms.promo_rate)
        )
        leaks[LEAK_PROMO if promo_expired and rate_gap > ZERO else LEAK_OTHER] = rate_gap
    if usage - step_rate:
        leaks[LEAK_ESCALATOR] = usage - step_rate

    drift = usage - billed
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
        expected_amount=usage,
        drift=drift,
        rate_basis=basis,
        floor_applied=False,
        leaks=leaks,
        escalations=n,
        row_problems=tuple(problems),
    )


def _month(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def audit_period(terms: ExtractedContractTerms, customer: str, period: date,
                 lines: list[LineAudit]) -> tuple[PeriodAudit, list[LineAudit]]:
    """Apply the monthly minimum once to the whole month, then classify the variance.

    Returns the period and its lines. A month with a single invoice gets the minimum
    shortfall folded into that line, so per-invoice displays stay exact; with several
    invoices the shortfall belongs to the month and no single invoice carries it.
    """
    usage_total = sum((l.usage_amount for l in lines), ZERO)
    billed_total = sum((l.billed_amount for l in lines), ZERO)
    floor = terms.monthly_minimum_floor
    expected = max(usage_total, money(floor)) if floor is not None else usage_total
    shortfall = expected - usage_total
    drift = expected - billed_total

    leaks: dict[str, Decimal] = {}
    for line in lines:
        for name, amount in line.leaks.items():
            leaks[name] = leaks.get(name, ZERO) + amount
    if shortfall:
        leaks[LEAK_FLOOR] = shortfall
    if sum(leaks.values(), ZERO) != drift:
        raise ArithmeticError(f"{customer} {period:%Y-%m}: period attribution does not reconcile to drift")

    if shortfall and len(lines) == 1:
        only = lines[0]
        lines = [replace(only, expected_amount=expected, drift=drift, floor_applied=True,
                         leaks={**only.leaks, LEAK_FLOOR: shortfall})]

    reasons = [p for l in lines for p in l.row_problems]
    components = [l.usage_amount - l.billed_amount for l in lines] + [shortfall]
    if any(c > ZERO for c in components) and any(c < ZERO for c in components):
        reasons.append("under- and over-billing in the same month: needs a person")

    if reasons:
        variance_class = NEEDS_REVIEW
    elif drift > ZERO:
        variance_class = UNDERBILLED
    elif drift < ZERO:
        variance_class = OVERBILLED
    else:
        variance_class = ZERO_VARIANCE

    period_audit = PeriodAudit(
        customer_name=customer, period=period, lines=tuple(lines), usage_total=usage_total,
        billed_total=billed_total, minimum_shortfall=shortfall, expected_total=expected, drift=drift,
        variance_class=variance_class, leaks=leaks, reasons=tuple(reasons),
    )
    return period_audit, lines


def reconcile(terms: ExtractedContractTerms, csv_path: Path) -> AuditReport:
    with csv_path.open(newline="", encoding="utf-8") as fh:
        return reconcile_rows(terms, list(csv.DictReader(fh)))


def reconcile_rows(terms: ExtractedContractTerms, rows: list[dict[str, str]]) -> AuditReport:
    """Rows in the canonical layout (see tools/ingest.py): ISO dates, plain decimal strings."""
    raw = sorted((audit_row(terms, r) for r in rows), key=lambda l: (l.invoice_date, l.invoice_id))

    groups: dict[tuple[str, date], list[LineAudit]] = {}
    for line in raw:
        groups.setdefault((line.customer_name, _month(line.invoice_date)), []).append(line)

    periods: list[PeriodAudit] = []
    lines: list[LineAudit] = []
    for (customer, period), group in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        audit, adjusted = audit_period(terms, customer, period, group)
        periods.append(audit)
        lines.extend(adjusted)
    lines.sort(key=lambda l: (l.invoice_date, l.invoice_id))

    missing: list[date] = []
    for customer in sorted({c for c, _ in groups}):
        months = sorted(p for c, p in groups if c == customer)
        cursor = months[0] if months else None
        while cursor is not None and cursor < months[-1]:
            cursor = _next_month(cursor)
            if cursor not in months and cursor not in missing:
                missing.append(cursor)

    under = [p for p in periods if p.variance_class == UNDERBILLED]
    leak_totals: dict[str, Decimal] = {}
    for p in under:
        for name, amount in p.leaks.items():
            leak_totals[name] = leak_totals.get(name, ZERO) + amount

    return AuditReport(
        lines=lines,
        total_billed=sum((l.billed_amount for l in lines), ZERO),
        total_expected=sum((p.expected_total for p in periods), ZERO),
        total_arrears=sum((p.drift for p in under), ZERO),
        leak_totals=leak_totals,
        periods=periods,
        total_overbilled=-sum((p.drift for p in periods if p.variance_class == OVERBILLED), ZERO),
        missing_periods=sorted(missing),
    )
