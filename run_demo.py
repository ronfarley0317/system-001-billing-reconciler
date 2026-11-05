"""System #001 - Deterministic Billing & Contract Reconciler (local demo).

    Scout   (Qwen via Ollama)  -> transcribes contract language into a typed schema
    Verify  (pure Python)      -> every citation must appear verbatim in the contract
    Analyst (Decimal + csv)    -> recomputes every invoice and attributes the drift
    Ledger  (Supabase)         -> upserts every leaking invoice into `detected_leaks`
    HITL    (Telegram)         -> approval card for the total arrears (only if the ledger write succeeds)
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agents.analyst import (  # noqa: E402
    LEAK_ESCALATOR, LEAK_FLOOR, LEAK_OTHER, LEAK_PROMO, AuditReport, LineAudit, reconcile,
)
from agents.scout import ScoutResult, extract_terms  # noqa: E402
from tools.db_client import log_leak  # noqa: E402
from tools.dossier import invoice_description  # noqa: E402
from tools.notifier import send_hitl_dossier  # noqa: E402

CONTRACT = ROOT / "demo_data" / "apex_contract_2025.md"
ERP_CSV = ROOT / "demo_data" / "erp_invoices_export.csv"


def contract_id(path: Path) -> str:
    match = re.search(r"Order Form No\.:\**\s*([A-Z0-9-]+)", path.read_text(encoding="utf-8")) if path.exists() else None
    return match.group(1) if match else path.stem

# Camera-ready: never narrower than the layout needs (also when piped/recorded).
console = Console(width=max(Console().width, 132))


def usd(value: Decimal | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
def render_header() -> None:
    title = Text("SYSTEM #001", style="bold white on dark_red")
    title.append("  DETERMINISTIC BILLING & CONTRACT RECONCILER  ", style="bold white on grey15")
    console.print()
    console.print(Align.center(title))
    console.print(
        Align.center(
            Text(
                "LLM extracts language  ·  Decimal does the math  ·  Humans approve the money",
                style="italic grey62",
            )
        )
    )
    console.print()


# --------------------------------------------------------------------------- #
# Panel 1 - Scout extraction
# --------------------------------------------------------------------------- #
def render_extraction(result: ScoutResult) -> None:
    t = result.terms
    checks = {c.field_name: c for c in result.citation_checks}

    table = Table(box=box.SIMPLE_HEAVY, expand=True, show_edge=False, pad_edge=False)
    table.add_column("Term", style="bold cyan", no_wrap=True)
    table.add_column("Extracted", style="bold white", justify="right", no_wrap=True)
    table.add_column("§", style="grey62", justify="center", no_wrap=True)
    table.add_column("Verbatim citation", style="grey82", ratio=1)
    table.add_column("Verified", justify="center", no_wrap=True)

    rows = [
        ("Base rate / seat / mo", "base_rate", usd(t.base_rate)),
        ("Promo rate / seat / mo", "promo_rate", usd(t.promo_rate)),
        ("Promo expires", "promo_expiration_date", str(t.promo_expiration_date or "n/a")),
        ("Annual escalator", "annual_escalator_pct",
         f"{t.annual_escalator_pct}%" if t.annual_escalator_pct is not None else "n/a"),
        ("Escalator effective", "escalator_effective_date", str(t.escalator_effective_date or "n/a")),
        ("Monthly minimum floor", "monthly_minimum_floor", usd(t.monthly_minimum_floor)),
    ]
    for label, key, value in rows:
        c = checks.get(key)
        if c is None:
            table.add_row(label, value, "", Text("no citation returned", style="yellow"),
                          Text("⚠", style="bold yellow"))
            continue
        badge = Text("✔ match", style="bold green") if c.verified else Text("✘ NOT FOUND", style="bold red")
        table.add_row(label, value, c.section, f"“{c.quote}”", badge)

    verified = sum(c.verified for c in result.citation_checks)
    footer = Text.assemble(
        ("model ", "grey50"), (result.model, "bold magenta"),
        ("   ·   structured output → ", "grey50"), ("ExtractedContractTerms", "bold"),
        ("   ·   ", "grey50"), (f"{result.elapsed_s:.1f}s", "bold"),
        ("   ·   citations verified against source: ", "grey50"),
        (f"{verified}/{len(result.citation_checks)}",
         "bold green" if result.all_citations_verified else "bold red"),
    )

    console.print(
        Panel(
            Group(table, Text(""), footer),
            title="[bold]① SCOUT[/]  [grey62]Qwen contract extraction: language only, no arithmetic[/]",
            subtitle=f"[grey50]{CONTRACT.relative_to(ROOT)}[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


# --------------------------------------------------------------------------- #
# Panel 2 - Analyst line-by-line table
# --------------------------------------------------------------------------- #
LEAK_TAGS = {
    "Expired promo rate still billed": ("PROMO", "orange1"),
    "Annual escalator not applied": ("ESC", "magenta"),
    "Monthly minimum not enforced": ("FLOOR", "red"),
    "Other rate variance": ("VAR", "yellow"),
}


def render_audit(report: AuditReport) -> None:
    table = Table(
        box=box.SIMPLE_HEAVY,
        header_style="bold white on grey23",
        border_style="grey42",
        expand=True,
        show_edge=False,
        collapse_padding=True,
        row_styles=["", "on grey7"],
    )
    table.add_column("Invoice", style="grey70", no_wrap=True)
    table.add_column("Month", no_wrap=True)
    table.add_column("Seats", justify="right", no_wrap=True)
    table.add_column("Billed\n/seat", justify="right", no_wrap=True)
    table.add_column("Contract\n/seat", justify="right", style="bold", no_wrap=True)
    table.add_column("Basis", style="grey62", no_wrap=True)
    table.add_column("Billed", justify="right", no_wrap=True)
    table.add_column("Should be", justify="right", style="bold", no_wrap=True)
    table.add_column("Drift", justify="right", no_wrap=True)
    table.add_column("Leak", no_wrap=True)

    for line in report.lines:
        if line.is_leak:
            drift = Text(f"+{usd(line.drift)}", style="bold red")
            tags = Text(" ").join(
                Text(f" {LEAK_TAGS[k][0]} ", style=f"bold black on {LEAK_TAGS[k][1]}") for k in line.leaks
            )
        else:
            drift = Text(usd(line.drift), style="green")
            tags = Text("✔ clean", style="green")

        rate_style = "bold red" if line.billed_unit_price != line.contract_unit_rate else "green"
        should_be = Text(usd(line.expected_amount) + ("*" if line.floor_applied else " "),
                         style="bold yellow" if line.floor_applied else "bold")
        table.add_row(
            line.invoice_id,
            f"{line.invoice_date:%b %Y}",
            Text(str(line.quantity), style="bold yellow" if line.floor_applied else ""),
            Text(usd(line.billed_unit_price), style=rate_style),
            usd(line.contract_unit_rate),
            line.rate_basis.replace("BASE +", "BASE+").replace(".0% x", "%×"),
            usd(line.billed_amount),
            should_be,
            drift,
            tags,
        )

    table.add_section()
    table.add_row(
        Text("TOTAL", style="bold"), f"{len(report.lines)} inv", "", "", "", "",
        Text(usd(report.total_billed), style="bold"),
        Text(usd(report.total_expected) + " ", style="bold"),
        Text(f"+{usd(report.total_arrears)}", style="bold white on red"),
        Text(f"{len(report.leaking_lines)} leaking", style="bold red"),
    )

    legend = Text.assemble(
        *[part for k, (tag, color) in LEAK_TAGS.items() if k in report.leak_totals
          for part in ((f" {tag} ", f"bold black on {color}"), (f" {k}   ", "grey62"))],
        ("* ", "bold yellow"), ("monthly minimum floor applied", "grey62"),
    )

    console.print(
        Panel(
            Group(table, Text(""), legend),
            title="[bold]② ANALYST[/]  [grey62]Line-by-line reconciliation: pure Decimal, ROUND_HALF_UP to the cent[/]",
            subtitle=f"[grey50]{ERP_CSV.relative_to(ROOT)}[/]",
            border_style="yellow",
            padding=(1, 1),
        )
    )


# --------------------------------------------------------------------------- #
# Panel 3 - Simulated Telegram HITL approval card
# --------------------------------------------------------------------------- #
def render_hitl(report: AuditReport, scout: ScoutResult) -> None:
    leaking = report.leaking_lines
    if not leaking:
        console.print(Panel("[bold green]✔ No billing drift found. Nothing to approve.[/]",
                            border_style="green"))
        return
    first, last = leaking[0].invoice_date, leaking[-1].invoice_date

    breakdown = Table.grid(padding=(0, 2))
    breakdown.add_column(style="white")
    breakdown.add_column(justify="right", style="bold white")
    for name, amount in sorted(report.leak_totals.items(), key=lambda kv: -kv[1]):
        tag, color = LEAK_TAGS[name]
        breakdown.add_row(Text.assemble((f" {tag:^5} ", f"bold black on {color}"), f"  {name}"), usd(amount))

    headline = Text(f"{usd(report.total_arrears)}", style="bold white on red", justify="center")

    buttons = Text.assemble(
        ("  ✅ APPROVE CLAIM  ", "bold black on green"), "     ",
        ("  ❌ DENY  ", "bold white on grey30"),
        justify="center",
    )

    integrity = "✔ all contract citations verified" if scout.all_citations_verified else "⚠ unverified citations: review contract"

    body = Group(
        Text.assemble(("🤖 Billing Reconciler Bot", "bold"), ("  ·  ", "grey50"),
                      (f"{datetime.now():%Y-%m-%d %H:%M}", "grey62")),
        Text(""),
        Text("🚨 REVENUE LEAKAGE DETECTED: HUMAN APPROVAL REQUIRED", style="bold red"),
        Text(""),
        Text.assemble(("Customer   ", "grey62"), (leaking[0].customer_name, "bold")),
        Text.assemble(("Contract   ", "grey62"), (contract_id(CONTRACT), "bold")),
        Text.assemble(("Period     ", "grey62"), (f"{first:%b %Y} → {last:%b %Y}", "bold"),
                      ("   ·   ", "grey50"), (f"{len(leaking)} of {len(report.lines)} invoices under-billed", "bold")),
        Text(""),
        Text("TOTAL HISTORICAL ARREARS", style="bold grey70", justify="center"),
        headline,
        Text(""),
        breakdown,
        Text(""),
        Text.assemble(("Integrity  ", "grey62"),
                      (integrity, "green" if scout.all_citations_verified else "bold yellow")),
        Text.assemble(("Action     ", "grey62"),
                      ("No invoice will be issued until a human approves.", "italic")),
        Text(""),
        buttons,
    )

    console.print(
        Align.center(
            Panel(
                body,
                title="[bold white on red] ⚠  TELEGRAM · HITL APPROVAL CARD [/]",
                subtitle="[grey50]awaiting response from @finance-controller[/]",
                border_style="bold red",
                box=box.DOUBLE,
                padding=(1, 3),
                width=84,
            )
        )
    )
    console.print()


# --------------------------------------------------------------------------- #
# Step 4 - Supabase state ledger
# --------------------------------------------------------------------------- #
# Which extracted clauses justify each leak category.
LEAK_CITATION_FIELDS = {
    LEAK_PROMO: ("promo_expiration_date",),
    LEAK_ESCALATOR: ("annual_escalator_pct", "escalator_effective_date"),
    LEAK_FLOOR: ("monthly_minimum_floor",),
    LEAK_OTHER: ("base_rate",),
}


def citation_for(line: LineAudit, scout: ScoutResult) -> str:
    """Concatenate every contract citation supporting this invoice's leaks (deduped, in order)."""
    by_field = {c.field_name: c for c in scout.terms.verbatim_citations}
    parts: list[str] = []
    for leak in line.leaks:
        for field in LEAK_CITATION_FIELDS.get(leak, ()):
            c = by_field.get(field)
            if c is None:
                continue
            part = f"§{c.section}: {c.quote}"
            if part not in parts:
                parts.append(part)
    return " | ".join(parts) or "No supporting citation extracted"


def write_ledger(report: AuditReport, scout: ScoutResult) -> bool:
    table = Table(box=box.SIMPLE, expand=True, show_edge=False, header_style="bold grey70")
    table.add_column("Invoice", style="grey70", no_wrap=True)
    table.add_column("Arrears", justify="right", style="bold", no_wrap=True)
    table.add_column("Contract citation(s)", style="grey62", ratio=1, overflow="ellipsis", no_wrap=True)
    table.add_column("Ledger", justify="center", no_wrap=True)

    with console.status("[green]Ledger: upserting leaks to Supabase…[/]", spinner="dots"):
        try:
            for line in report.leaking_lines:
                citation = citation_for(line, scout)
                log_leak(line.customer_name, line.invoice_id, line.drift, citation,
                         invoice_description(line, scout.terms))
                table.add_row(line.invoice_id, usd(line.drift), citation, Text("✔ upserted", style="bold green"))
        except Exception as exc:
            console.print(Panel(table, title="[bold]③ LEDGER[/]  [grey62]partial write before failure[/]",
                                border_style="red", padding=(1, 1)))
            console.print(f"[bold red]Supabase error:[/] {exc}")
            return False

    footer = Text.assemble(
        (f"{len(report.leaking_lines)} rows", "bold green"),
        (" upserted into ", "grey50"), ("detected_leaks", "bold"),
        (" (on_conflict=invoice_number)   ·   status defaults to ", "grey50"),
        ("PENDING_APPROVAL", "bold yellow"),
    )
    console.print(Panel(Group(table, Text(""), footer),
                        title="[bold]③ LEDGER[/]  [grey62]Supabase state write: arrears stored as exact NUMERIC(10, 2)[/]",
                        border_style="green", padding=(1, 1)))
    return True


def main() -> int:
    if not CONTRACT.exists() or not ERP_CSV.exists():
        console.print("[yellow]Demo data missing. Generating…[/]")
        import generate_demo_data

        generate_demo_data.main()

    render_header()

    with console.status("[cyan]Scout: Qwen is reading the contract…[/]", spinner="dots"):
        try:
            scout = extract_terms(CONTRACT)
        except Exception as exc:  # surface Ollama/connection issues clearly
            console.print(Panel(f"[bold red]Scout failed:[/] {exc}\n\nIs Ollama running? "
                                "Try: [bold]ollama serve[/] and [bold]ollama pull qwen3:14b[/]",
                                border_style="red"))
            return 1
    render_extraction(scout)

    with console.status("[yellow]Analyst: reconciling ERP export…[/]", spinner="dots"):
        report = reconcile(scout.terms, ERP_CSV)
    render_audit(report)

    if not report.leaking_lines:
        render_hitl(report, scout)
        return 0

    # Ledger first: no approval request goes out for leaks that were never recorded.
    if not write_ledger(report, scout):
        console.print(Panel(
            "[bold red]✘ Supabase ledger write failed. Execution aborted.[/]\n"
            "[grey70]Telegram HITL notification was NOT sent.[/]",
            border_style="red",
        ))
        return 1

    console.print(Rule(style="grey23"))
    render_hitl(report, scout)

    if "--no-telegram" not in sys.argv:
        with console.status("[red]Dispatching HITL card to Telegram…[/]", spinner="dots"):
            send_hitl_dossier(report, scout, contract_id(CONTRACT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
