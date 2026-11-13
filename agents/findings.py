"""Findings: the gate between the Analyst's math and anything a person can approve.

Pure Python, no LLM. A billing period only becomes approvable (PENDING_APPROVAL)
when it is under-billed AND every contract term behind its expected amount has a
citation that was found in the contract with its value inside that quote. Anything
else (over-billing, credits, mixed signs, missing invoices, weak evidence) is
written as REVIEW_REQUIRED with the reasons, and never counts toward a claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from agents.analyst import (
    LEAK_ESCALATOR, LEAK_FLOOR, LEAK_OTHER, LEAK_PROMO, NEEDS_REVIEW, OVERBILLED, UNDERBILLED,
    ZERO, ZERO_VARIANCE, AuditReport, PeriodAudit,
)
from agents.scout import ScoutResult
from schemas.payloads import ExtractedContractTerms

PENDING_APPROVAL = "PENDING_APPROVAL"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
MISSING_INVOICE = "MISSING_INVOICE"

# Escalators above this are almost always a misread (5.0 read as 50.0). A person confirms.
ESCALATOR_SANITY_PCT = Decimal("25")

# Which extracted clauses justify each leak category (for the ledger citation column).
LEAK_CITATION_FIELDS = {
    LEAK_PROMO: ("promo_expiration_date",),
    LEAK_ESCALATOR: ("annual_escalator_pct", "escalator_effective_date"),
    LEAK_FLOOR: ("monthly_minimum_floor",),
    LEAK_OTHER: ("base_rate",),
}


@dataclass(frozen=True)
class Finding:
    key: str  # stable per client + contract: "period:2026-04"
    period: date
    customer_name: str
    invoice_ids: tuple[str, ...]
    variance_class: str  # UNDERBILLED | OVERBILLED | REVIEW | MISSING_INVOICE
    amount: Decimal  # signed: positive = under-billed, negative = over-billed
    status: str  # PENDING_APPROVAL | REVIEW_REQUIRED
    reasons: tuple[str, ...]
    citation: str
    description: str
    breakdown: dict[str, Decimal] = field(default_factory=dict)  # one short reason per cause -> amount

    @property
    def approvable(self) -> bool:
        return self.status == PENDING_APPROVAL


def term_problems(scout: ScoutResult) -> list[str]:
    """Problems with the extracted terms as a whole. Any of these blocks every finding."""
    t = scout.terms
    problems: list[str] = []
    if (t.promo_rate is None) != (t.promo_expiration_date is None):
        problems.append("promo rate and promo end date must both be present or both absent")
    if (t.annual_escalator_pct is None) != (t.escalator_effective_date is None):
        problems.append("escalator percent and escalator start date must both be present or both absent")
    if t.annual_escalator_pct is not None and t.annual_escalator_pct > ESCALATOR_SANITY_PCT:
        problems.append(f"escalator {t.annual_escalator_pct}% is above {ESCALATOR_SANITY_PCT}%: confirm against the contract")

    quotes: dict[str, set[str]] = {}
    for c in t.verbatim_citations:
        quotes.setdefault(c.field_name, set()).add(" ".join(c.quote.split()).lower())
    for name, found in quotes.items():
        if len(found) > 1:
            problems.append(f"{name} has {len(found)} different citations: conflicting or duplicate terms")
    return problems


def required_fields(period: PeriodAudit, terms: ExtractedContractTerms) -> set[str]:
    """Every term that decided this period's expected amount."""
    fields: set[str] = set()
    for line in period.lines:
        if line.rate_basis.startswith("PROMO"):
            fields |= {"promo_rate", "promo_expiration_date"}
        else:
            fields.add("base_rate")
            if terms.promo_expiration_date is not None:
                fields.add("promo_expiration_date")  # the date decided the promo was over
        if terms.annual_escalator_pct is not None:
            fields |= {"annual_escalator_pct", "escalator_effective_date"}
    if period.minimum_shortfall > ZERO:
        fields.add("monthly_minimum_floor")
    return fields


def evidence_problems(scout: ScoutResult, fields: set[str]) -> list[str]:
    checks: dict[str, list] = {}
    for c in scout.citation_checks:
        checks.setdefault(c.field_name, []).append(c)
    problems: list[str] = []
    for name in sorted(fields):
        found = checks.get(name)
        if getattr(scout.terms, name, None) is None:
            problems.append(f"{name} is needed for this amount but was not extracted")
        elif not found:
            problems.append(f"{name} has no contract citation")
        elif not all(c.verified for c in found):
            problems.append(f"{name}: quote not found in the contract")
        elif not all(c.value_in_quote for c in found):
            problems.append(f"{name}: extracted value is not in its own quote")
    return problems


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def reason_label(category: str, terms: ExtractedContractTerms) -> str:
    """One line per cause, identical for every month it applies to, so totals can be summed by label."""
    if category == LEAK_PROMO and terms.promo_expiration_date and terms.promo_rate is not None:
        return f"Promo ended {_day(terms.promo_expiration_date)}; still billed at the {_usd(terms.promo_rate)} promo rate"
    if category == LEAK_ESCALATOR and terms.escalator_effective_date and terms.annual_escalator_pct is not None:
        return f"{terms.annual_escalator_pct}% increase from {_day(terms.escalator_effective_date)} not applied"
    if category == LEAK_FLOOR and terms.monthly_minimum_floor is not None:
        return f"{_usd(terms.monthly_minimum_floor)} monthly minimum not billed"
    return "Billed rate differs from the contract rate"


def reason_breakdown(period: PeriodAudit, terms: ExtractedContractTerms) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for category, amount in period.leaks.items():
        label = reason_label(category, terms)
        out[label] = out.get(label, ZERO) + amount
    return out


def citation_text(period: PeriodAudit, scout: ScoutResult) -> str:
    by_field = {c.field_name: c for c in scout.terms.verbatim_citations}
    parts: list[str] = []
    for leak in period.leaks:
        for name in LEAK_CITATION_FIELDS.get(leak, ()):
            c = by_field.get(name)
            if c is None:
                continue
            part = f"§{c.section}: {c.quote}"
            if part not in parts:
                parts.append(part)
    return " | ".join(parts) or "No supporting citation extracted"


def _usd(value: Decimal) -> str:
    return f"${value:,.2f}" if value >= 0 else f"-${-value:,.2f}"


def describe(period: PeriodAudit, terms: ExtractedContractTerms) -> str:
    from tools.dossier import invoice_description  # display text only; no math happens there

    if len(period.lines) == 1:
        text = invoice_description(period.lines[0], terms)
    else:
        text = (f"{period.period:%b %Y}: {len(period.lines)} invoices ({', '.join(period.invoice_ids)}) billed "
                f"{_usd(period.billed_total)}; contract usage is {_usd(period.usage_total)}")
        if period.minimum_shortfall:
            text += f" and the {_usd(terms.monthly_minimum_floor)} monthly minimum applies once for the month"
        text += f", so the month should be {_usd(period.expected_total)}. Variance {_usd(period.drift)}."
    return text


def build_findings(report: AuditReport, scout: ScoutResult) -> list[Finding]:
    """One finding per billing period with a variance, plus one per missing month."""
    global_problems = term_problems(scout)
    findings: list[Finding] = []

    for period in report.periods:
        if period.variance_class == ZERO_VARIANCE:
            continue
        reasons = list(period.reasons)
        if period.variance_class == OVERBILLED:
            reasons.append("over-billed: the customer may be owed a credit; never part of a recovery claim")
        if period.variance_class in (UNDERBILLED, NEEDS_REVIEW):
            reasons += global_problems
            reasons += evidence_problems(scout, required_fields(period, scout.terms))
        status = PENDING_APPROVAL if period.variance_class == UNDERBILLED and not reasons else REVIEW_REQUIRED
        findings.append(Finding(
            key=period.key, period=period.period, customer_name=period.customer_name,
            invoice_ids=period.invoice_ids, variance_class=period.variance_class, amount=period.drift,
            status=status, reasons=tuple(reasons), citation=citation_text(period, scout),
            description=describe(period, scout.terms),
            breakdown=reason_breakdown(period, scout.terms) if period.variance_class == UNDERBILLED else {},
        ))

    customer = report.periods[0].customer_name if report.periods else ""
    floor = scout.terms.monthly_minimum_floor
    for month in report.missing_periods:
        note = f"No invoice in the export for {month:%b %Y}."
        if floor is not None:
            note += f" If the contract was active, the {_usd(floor)} monthly minimum may be owed."
        findings.append(Finding(
            key=f"period:{month:%Y-%m}", period=month, customer_name=customer, invoice_ids=(),
            variance_class=MISSING_INVOICE, amount=ZERO, status=REVIEW_REQUIRED,
            reasons=("missing invoice: check whether it exists outside this export",),
            citation="", description=note,
        ))
    return sorted(findings, key=lambda f: f.period)


def approvable_total(findings: list[Finding]) -> Decimal:
    return sum((f.amount for f in findings if f.approvable), ZERO)


def approvable_view(report: AuditReport, findings: list[Finding],
                    keys: set[str] | None = None) -> AuditReport:
    """The report narrowed to approvable periods: what the approval card is allowed to show.

    `keys` limits it further to findings the ledger actually left open (a finding a person
    already decided in an earlier run is not approvable again).
    """
    from dataclasses import replace

    allowed = {f.key for f in findings if f.approvable}
    if keys is not None:
        allowed &= keys
    periods = [p for p in report.periods if p.key in allowed and p.variance_class == UNDERBILLED]
    leak_totals: dict[str, Decimal] = {}
    for p in periods:
        for name, amount in p.leaks.items():
            leak_totals[name] = leak_totals.get(name, ZERO) + amount
    return replace(report, periods=periods, leak_totals=leak_totals,
                   total_arrears=sum((p.drift for p in periods), ZERO))
