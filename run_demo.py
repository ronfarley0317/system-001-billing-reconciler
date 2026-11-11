"""System #001 - Deterministic Billing & Contract Reconciler (local demo).

    Scout   (Qwen via Ollama)  -> transcribes contract language into a typed schema
    Verify  (pure Python)      -> every citation must appear verbatim in the contract
    Analyst (Decimal + csv)    -> recomputes every invoice and attributes the drift
    Ledger  (Supabase)         -> upserts every leaking invoice into `detected_leaks`
    HITL    (Telegram)         -> approval card for the total arrears (only if the ledger write succeeds)
"""

from __future__ import annotations

import argparse
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
    LEAK_ESCALATOR, LEAK_FLOOR, LEAK_OTHER, LEAK_PROMO, AuditReport, LineAudit, reconcile_rows,
)
from agents.scout import ScoutResult, extract_terms  # noqa: E402
from tools.db_client import log_leak  # noqa: E402
from tools.dossier import invoice_description  # noqa: E402
from tools.notifier import send_hitl_dossier  # noqa: E402
from tools.documents import CONTRACT_SUFFIXES, DocumentError, read_document  # noqa: E402
from tools.ingest import SPREADSHEET_SUFFIXES, IntakeError, IntakeResult, load_invoices, mapping_path  # noqa: E402

DEMO_CONTRACT = ROOT / "demo_data" / "apex_contract_2025.md"
DEMO_ERP_CSV = ROOT / "demo_data" / "erp_invoices_export.csv"
CLIENTS_DIR = ROOT / "clients"

CONTRACT = DEMO_CONTRACT
ERP_CSV = DEMO_ERP_CSV


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile one contract against one invoice export. With no inputs, runs the demo data.",
    )
    parser.add_argument("--client", help=f"folder name under {CLIENTS_DIR.name}/ holding the contract and the invoice export")
    parser.add_argument("--contract", type=Path, help="path to the contract (PDF, Word .docx, text or Markdown)")
    parser.add_argument("--invoices", type=Path, help="path to the invoice export (CSV or Excel .xlsx)")
    parser.add_argument("--no-telegram", action="store_true", help="skip the Telegram approval card")
    args = parser.parse_args()
    if args.client and (args.contract or args.invoices):
        parser.error("use --client on its own, or --contract with --invoices")
    if bool(args.contract) != bool(args.invoices):
        parser.error("--contract and --invoices must be given together")
    return args


class InputError(ValueError):
    """Missing or ambiguous input files, explained for the person running the tool."""


def _pick_file(folder: Path, suffixes: tuple[str, ...], hint: str, kind: str) -> Path:
    files = sorted(p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in suffixes and not p.name.startswith((".", "~$")))
    named = [p for p in files if hint in p.stem.lower()]
    if len(named) == 1 or len(files) == 1:
        return (named or files)[0]
    if not files:
        raise InputError(f"No {kind} found in {display_path(folder)}/. Add one ({', '.join(suffixes)}).")
    raise InputError(f"{display_path(folder)}/ has {len(files)} files that could be the {kind}: "
                     + ", ".join(p.name for p in files) + f". Put '{hint}' in the name of the one to use.")


def resolve_inputs(args: argparse.Namespace) -> tuple[Path, Path] | None:
    if args.client:
        folder = CLIENTS_DIR / args.client
        if not folder.is_dir():
            raise InputError(f"There is no folder {display_path(folder)}/. Create it and put the signed contract "
                             "and the invoice export inside, then run this again.")
        return (_pick_file(folder, CONTRACT_SUFFIXES, "contract", "contract"),
                _pick_file(folder, SPREADSHEET_SUFFIXES, "invoice", "invoice export"))
    if args.contract:
        return args.contract.resolve(), args.invoices.resolve()
    return None


CONTRACT_NUMBER = re.compile(r"(?:Order Form|Contract|Agreement)\s+(?:No\.?|Number|#)\s*:?\**\s*([A-Z0-9][A-Z0-9-]+)", re.IGNORECASE)


def contract_id(path: Path) -> str:
    try:
        match = CONTRACT_NUMBER.search(read_document(path))
    except (OSError, DocumentError):
        match = None
    return match.group(1) if match else path.stem


CUSTOMER_LINE = re.compile(r"^\W*(?:Customer|Client)\W*:\**\s*(.+?)\s*(?:\(\s*\"?(?:Customer|Client)\"?\s*\))?\s*$",
                           re.IGNORECASE | re.MULTILINE)


def fill_customer(intake: IntakeResult, contract_text: str, fallback: str) -> None:
    """The ledger needs a client name on every row; exports without a customer column get the contract's."""
    if all(r["customer_name"] for r in intake.rows):
        return
    match = CUSTOMER_LINE.search(contract_text)
    name = match.group(1).strip(" *") if match else fallback
    for r in intake.rows:
        r["customer_name"] = r["customer_name"] or name
    intake.notes.append(f"The export has no customer column, so rows are labelled '{name}'"
                        + (" from the contract." if match else "."))


def render_intake(intake: IntakeResult, invoices: Path) -> None:
    if not intake.notes:
        return
    body = Text("\n".join(f"·  {note}" for note in intake.notes), style="grey70")
    console.print(Panel(body, title=f"[bold cyan]INTAKE[/]  [grey62]{len(intake.rows)} invoices ready for the Analyst[/]",
                        subtitle=f"[grey50]{display_path(invoices)}[/]", border_style="cyan", padding=(1, 2)))

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
        if not c.verified:
            badge = Text("✘ NOT FOUND", style="bold red")
        elif not c.value_in_quote:
            badge = Text("✘ VALUE ≠ QUOTE", style="bold red")
        else:
            badge = Text("✔ match", style="bold green")
        table.add_row(label, value, c.section, f"“{c.quote}”", badge)

    verified = sum(c.verified for c in result.citation_checks)
    in_quote = sum(c.value_in_quote for c in result.citation_checks)
    footer = Text.assemble(
        ("model ", "grey50"), (result.model, "bold magenta"),
        ("   ·   structured output → ", "grey50"), ("ExtractedContractTerms", "bold"),
        ("   ·   ", "grey50"), (f"{result.elapsed_s:.1f}s", "bold"),
        ("   ·   citations verified against source: ", "grey50"),
        (f"{verified}/{len(result.citation_checks)}",
         "bold green" if result.all_citations_verified else "bold red"),
        ("   ·   values found in their quote: ", "grey50"),
        (f"{in_quote}/{len(result.citation_checks)}",
         "bold green" if result.all_values_in_quotes else "bold red"),
    )

    console.print(
        Panel(
            Group(table, Text(""), footer),
            title="[bold]① SCOUT[/]  [grey62]Qwen contract extraction: language only, no arithmetic[/]",
            subtitle=f"[grey50]{display_path(CONTRACT)}[/]",
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
            subtitle=f"[grey50]{display_path(ERP_CSV)}[/]",
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

    if not scout.all_citations_verified:
        integrity = "⚠ unverified citations: review contract"
    elif not scout.all_values_in_quotes:
        integrity = "⚠ extracted value not found in its quote: review contract"
    else:
        integrity = "✔ all contract citations and values verified"

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
                      (integrity, "green" if scout.integrity_ok else "bold yellow")),
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
    global CONTRACT, ERP_CSV
    args = parse_args()
    try:
        inputs = resolve_inputs(args)
    except InputError as exc:
        console.print(Panel(f"[bold red]{exc}[/]", border_style="red"))
        return 1

    if inputs is None:
        if not CONTRACT.exists() or not ERP_CSV.exists():
            console.print("[yellow]Demo data missing. Generating…[/]")
            import generate_demo_data

            generate_demo_data.main()
    else:
        CONTRACT, ERP_CSV = inputs
        missing = [p for p in (CONTRACT, ERP_CSV) if not p.exists()]
        if missing:
            console.print(Panel(
                "[bold red]Input file not found:[/]\n" + "\n".join(f"  {display_path(p)}" for p in missing),
                border_style="red",
            ))
            return 1

    try:
        contract_text = read_document(CONTRACT)
    except DocumentError as exc:
        console.print(Panel(f"[bold red]Couldn't read the contract.[/]\n[grey70]{exc}[/]", border_style="red"))
        return 1

    render_header()

    # Intake runs before the Scout so any questions about the export are asked up front.
    try:
        intake = load_invoices(ERP_CSV, contract_text, console)
    except IntakeError as exc:
        console.print(Panel(f"[bold red]Couldn't read the invoice export.[/]\n[grey70]{exc}[/]", border_style="red"))
        return 1
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Stopped before the invoice export was confirmed. Nothing was saved or sent.[/]")
        return 1
    fill_customer(intake, contract_text, args.client or CONTRACT.stem)
    render_intake(intake, ERP_CSV)

    with console.status("[cyan]Scout: Qwen is reading the contract…[/]", spinner="dots"):
        try:
            scout = extract_terms(CONTRACT, contract_text=contract_text)
        except Exception as exc:  # surface Ollama/connection issues clearly
            console.print(Panel(f"[bold red]Scout failed:[/] {exc}\n\nIs Ollama running? "
                                "Try: [bold]ollama serve[/] and [bold]ollama pull qwen3:14b[/]",
                                border_style="red"))
            return 1
    render_extraction(scout)

    with console.status("[yellow]Analyst: reconciling ERP export…[/]", spinner="dots"):
        report = reconcile_rows(scout.terms, intake.rows)
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

    if not args.no_telegram:
        with console.status("[red]Dispatching HITL card to Telegram…[/]", spinner="dots"):
            send_hitl_dossier(report, scout, contract_id(CONTRACT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
