"""Approval dossier: the Telegram card text and the line-by-line HTML report.

Every number here comes straight from the Analyst's Decimal results. Nothing is
recomputed and no LLM is involved; this module only explains the audit.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from agents.analyst import (
    LEAK_ESCALATOR, LEAK_FLOOR, LEAK_OTHER, LEAK_PROMO, AuditReport, LineAudit,
)
from agents.scout import CitationCheck, ScoutResult
from schemas.payloads import ExtractedContractTerms

LEAK_ORDER = (LEAK_PROMO, LEAK_ESCALATOR, LEAK_FLOOR, LEAK_OTHER)
LEAK_TAG = {LEAK_PROMO: "PROMO", LEAK_ESCALATOR: "ESC", LEAK_FLOOR: "FLOOR", LEAK_OTHER: "VAR"}
LEAK_FIELD = {
    LEAK_PROMO: "promo_expiration_date",
    LEAK_ESCALATOR: "annual_escalator_pct",
    LEAK_FLOOR: "monthly_minimum_floor",
    LEAK_OTHER: "base_rate",
}


@dataclass(frozen=True)
class LeakReason:
    category: str
    amount: Decimal
    invoice_count: int
    section: str
    explanation: str
    quote: str


def usd(value: Decimal) -> str:
    return f"${value:,.2f}"


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _month(line: LineAudit) -> str:
    return f"{line.invoice_date:%b %Y}"


def _citation(scout: ScoutResult, field: str) -> tuple[str, str]:
    for c in scout.terms.verbatim_citations:
        if c.field_name == field:
            return c.section.lstrip("§ ").removeprefix("Section ").strip(), c.quote
    return "", ""


def leak_reasons(report: AuditReport, scout: ScoutResult) -> list[LeakReason]:
    t = scout.terms
    reasons: list[LeakReason] = []
    for category in LEAK_ORDER:
        amount = report.leak_totals.get(category)
        if not amount:
            continue
        lines = [l for l in report.leaking_lines if category in l.leaks]
        section, quote = _citation(scout, LEAK_FIELD[category])

        if category == LEAK_PROMO:
            billed = sorted({l.billed_unit_price for l in lines})
            billed_txt = " / ".join(f"{usd(b)}/seat" for b in billed)
            explanation = (
                f"Promo ended {_day(t.promo_expiration_date)}. Still billed {billed_txt} "
                f"after expiry. Base rate is {usd(t.base_rate)}/seat."
            )
        elif category == LEAK_ESCALATOR:
            explanation = (
                f"+{t.annual_escalator_pct}% from {_day(t.escalator_effective_date)} "
                f"was never applied."
            )
        elif category == LEAK_FLOOR:
            months = ", ".join(f"{_month(l)} ({usd(l.billed_amount)})" for l in lines)
            explanation = f"{usd(t.monthly_minimum_floor)} monthly minimum. Billed below it: {months}."
        else:
            explanation = "Billed rate differs from the contract rate for a reason not covered above."

        reasons.append(LeakReason(category, amount, len(lines), section, explanation, quote))
    return reasons


def _signed(value: Decimal) -> str:
    return usd(value) if value >= 0 else f"-{usd(-value)}"


def _leak_detail(category: str, line: LineAudit, t: ExtractedContractTerms) -> str:
    if category == LEAK_PROMO:
        return f"promo ended {_day(t.promo_expiration_date)}; base rate is {usd(t.base_rate)}/seat"
    if category == LEAK_ESCALATOR:
        return (f"+{t.annual_escalator_pct}% from {_day(t.escalator_effective_date)} "
                f"raises the rate to {usd(line.contract_unit_rate)}/seat")
    if category == LEAK_FLOOR:
        return f"{usd(t.monthly_minimum_floor)} monthly minimum"
    return f"billed {usd(line.billed_unit_price)}/seat does not match the contract rate"


def invoice_description(line: LineAudit, terms: ExtractedContractTerms) -> str:
    """Plain-English account of one invoice for the `leak_description` ledger column."""
    if line.floor_applied:
        required = (f"{usd(line.expected_amount)} (the monthly minimum; {line.quantity} seats at "
                    f"{usd(line.contract_unit_rate)} only come to {usd(line.usage_amount)})")
    else:
        required = f"{usd(line.expected_amount)} ({usd(line.contract_unit_rate)}/seat)"
    direction = "Under-billed" if line.drift > 0 else "Over-billed"
    parts = [
        f"{_month(line)}: billed {usd(line.billed_amount)} for {line.quantity} seats at "
        f"{usd(line.billed_unit_price)}/seat, but the contract requires {required}. "
        f"{direction} by {usd(abs(line.drift))}."
    ]
    parts += [f"{category}: {_signed(amount)} ({_leak_detail(category, line, terms)})."
              for category, amount in line.leaks.items()]
    return " ".join(parts)


def _period(report: AuditReport) -> str:
    leaking = report.leaking_lines
    return f"{_month(leaking[0])} to {_month(leaking[-1])}"


def card_html(report: AuditReport, scout: ScoutResult, contract_id: str, report_attached: bool) -> str:
    """Telegram card body (parse_mode=HTML).

    n8n reads the plain-text lines "Client: ..." and "Historical Arrears Found: ..."
    and the "Why:" block back out of this message, so keep those labels stable.
    """
    e = html.escape
    leaking = report.leaking_lines
    verified = sum(c.verified for c in scout.citation_checks)
    in_quote = sum(c.value_in_quote for c in scout.citation_checks)

    parts = [
        "🚨 <b>REVENUE LEAK DETECTED</b>",
        "<i>Human approval required. Nothing is billed until you decide.</i>",
        "",
        f"<b>Client:</b> {e(leaking[0].customer_name)}",
        f"<b>Contract:</b> {e(contract_id)}",
        f"<b>Period:</b> {_period(report)}",
        f"<b>Invoices under-billed:</b> {len(leaking)} of {len(report.lines)}",
        "",
        f"<b>Historical Arrears Found:</b> {usd(report.total_arrears)}",
        "",
        "<b>Why:</b>",
    ]
    for r in leak_reasons(report, scout):
        sec = f"§{e(r.section)} " if r.section else ""
        parts.append(f"• <b>{e(r.category)}: {usd(r.amount)}</b> ({r.invoice_count} invoices)")
        parts.append(f"   {sec}{e(r.explanation)}")
    parts += [
        "",
        f"<b>Contract citations:</b> {verified}/{len(scout.citation_checks)} verified against source",
        f"<b>Extracted values:</b> {in_quote}/{len(scout.citation_checks)} found in their own quote",
    ]
    if not scout.integrity_ok:
        parts.append("⚠️ <b>Review the contract before approving.</b> At least one check failed.")
    if report_attached:
        parts.append("📄 Full line-by-line report attached above.")
    return "\n".join(parts)


def _citation_item(c: CitationCheck) -> str:
    e = html.escape
    mark, cls = ("✔", "ok") if c.ok else ("✘", "bad")
    if not c.verified:
        problem = ' <span class="bad">quote not found in contract</span>'
    elif not c.value_in_quote:
        problem = ' <span class="bad">value not found in this quote</span>'
    else:
        problem = ""
    section = e(c.section.lstrip("§ "))
    return (f'<li><span class="{cls}">{mark}</span> '
            f'<b>§{section}</b> <span class="muted">{e(c.field_name)}</span>{problem}'
            f"<br>{e(c.quote)}</li>")


def report_html(report: AuditReport, scout: ScoutResult, contract_id: str) -> str:
    e = html.escape
    leaking = report.leaking_lines
    reasons = leak_reasons(report, scout)
    client = leaking[0].customer_name if leaking else ""

    reason_rows = "".join(
        f"""<div class="reason">
          <div class="reason-head"><span class="tag t-{LEAK_TAG[r.category].lower()}">{LEAK_TAG[r.category]}</span>
            <b>{e(r.category)}</b><span class="amt">{usd(r.amount)}</span></div>
          <p>{('§' + e(r.section) + ' ') if r.section else ''}{e(r.explanation)} <span class="muted">({r.invoice_count} invoices)</span></p>
          {f'<blockquote>{e(r.quote)}</blockquote>' if r.quote else ''}
        </div>"""
        for r in reasons
    )

    line_rows = "".join(
        f"""<tr class="{'leak' if l.is_leak else 'clean'}">
          <td>{e(l.invoice_id)}</td><td>{_month(l)}</td><td class="n">{l.quantity}</td>
          <td class="n">{usd(l.billed_unit_price)}</td><td class="n">{usd(l.contract_unit_rate)}</td>
          <td class="n">{usd(l.billed_amount)}</td><td class="n"><b>{usd(l.expected_amount)}</b>{'*' if l.floor_applied else ''}</td>
          <td class="n drift">{('+' + usd(l.drift)) if l.is_leak else usd(l.drift)}</td>
          <td>{' '.join(f'<span class="tag t-{LEAK_TAG[k].lower()}">{LEAK_TAG[k]}</span>' for k in l.leaks) or '<span class="ok">clean</span>'}</td>
        </tr>"""
        for l in report.lines
    )

    citation_rows = "".join(_citation_item(c) for c in scout.citation_checks)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Billing Audit {e(contract_id)}</title>
<style>
  :root {{ --bg:#f7f8fa; --card:#fff; --text:#111827; --muted:#6b7280; --line:#e5e7eb;
          --red:#b91c1c; --green:#047857; --amber:#b45309; --violet:#6d28d9; --orange:#c2410c; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0b0e13; --card:#121720; --text:#e6edf3; --muted:#8b98a5; --line:#232b36;
            --red:#f87171; --green:#34d399; --amber:#fbbf24; --violet:#a78bfa; --orange:#fb923c; }} }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Arial,sans-serif; }}
  main {{ max-width:980px; margin:0 auto; padding:20px 16px 40px }}
  h1 {{ font-size:20px; margin:0 0 4px }} h2 {{ font-size:15px; margin:28px 0 10px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted) }}
  .muted {{ color:var(--muted) }}
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:16px }}
  .tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px }}
  .tile .k {{ font-size:12px; color:var(--muted) }} .tile .v {{ font-size:19px; font-weight:600; font-variant-numeric:tabular-nums }}
  .tile.hot .v {{ color:var(--red) }}
  .reason {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:10px }}
  .reason-head {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap }} .reason-head .amt {{ margin-left:auto; font-weight:600; font-variant-numeric:tabular-nums }}
  .reason p {{ margin:6px 0 0 }}
  blockquote {{ margin:8px 0 0; padding:6px 10px; border-left:3px solid var(--line); color:var(--muted); font-size:13px }}
  .tag {{ display:inline-block; font:600 11px/1 ui-monospace,Menlo,monospace; padding:3px 6px; border-radius:4px; color:#fff }}
  .t-promo {{ background:var(--orange) }} .t-esc {{ background:var(--violet) }} .t-floor {{ background:var(--red) }} .t-var {{ background:var(--amber) }}
  .scroll {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; font-variant-numeric:tabular-nums; white-space:nowrap }}
  th,td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left }} th {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted) }}
  td.n {{ text-align:right }} tr.leak .drift {{ color:var(--red); font-weight:600 }} tr.total td {{ font-weight:700; border-bottom:none }}
  .ok {{ color:var(--green) }} .bad {{ color:var(--red) }}
  ul.cites {{ list-style:none; padding:0; margin:0 }} ul.cites li {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; margin-bottom:8px; font-size:13px }}
  footer {{ margin-top:28px; font-size:12px; color:var(--muted) }}
</style></head>
<body><main>
  <h1>Billing audit: {e(client)}</h1>
  <div class="muted">Contract {e(contract_id)} · {_period(report) if leaking else ''} · Status: awaiting approval</div>

  <div class="tiles">
    <div class="tile"><div class="k">Billed</div><div class="v">{usd(report.total_billed)}</div></div>
    <div class="tile"><div class="k">Contract says</div><div class="v">{usd(report.total_expected)}</div></div>
    <div class="tile hot"><div class="k">Arrears</div><div class="v">{usd(report.total_arrears)}</div></div>
    <div class="tile"><div class="k">Invoices under-billed</div><div class="v">{len(leaking)} of {len(report.lines)}</div></div>
  </div>

  <h2>Why</h2>
  {reason_rows}

  <h2>Line by line</h2>
  <div class="scroll"><table>
    <tr><th>Invoice</th><th>Month</th><th>Seats</th><th>Billed/seat</th><th>Contract/seat</th><th>Billed</th><th>Should be</th><th>Drift</th><th>Reason</th></tr>
    {line_rows}
    <tr class="total"><td>Total</td><td></td><td></td><td></td><td></td><td class="n">{usd(report.total_billed)}</td>
      <td class="n">{usd(report.total_expected)}</td><td class="n drift" style="color:var(--red)">+{usd(report.total_arrears)}</td><td></td></tr>
  </table></div>
  <p class="muted">* Monthly minimum applied.</p>

  <h2>Contract citations</h2>
  <ul class="cites">{citation_rows}</ul>

  <footer>Generated {datetime.now():%b %d, %Y %I:%M %p} by System #001: Deterministic Billing Reconciler.
  Contract terms extracted by a local model, verified word for word against the source, and each value checked against its own quote. All amounts computed in Python Decimal.</footer>
</main></body></html>
"""
