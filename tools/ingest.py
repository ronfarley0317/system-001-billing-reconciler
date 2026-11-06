"""Intake: read a client's invoice export, whatever its layout, into the rows the Analyst expects.

    CSV, TSV or Excel  ->  find the header row  ->  match columns  ->  parse values  ->  canonical rows

Column matching is deterministic first (known header names). Only columns the rules
cannot place are offered to the local model, which names columns and nothing else.
Every value is parsed in Python, every derived number is Decimal, and a person
confirms the mapping the first time a file layout is seen. The answer is saved next
to the file (<name>.mapping.json) and reused while the layout stays the same.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

SPREADSHEET_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xlsm")
CENT = Decimal("0.01")

FIELDS: dict[str, str] = {
    "invoice_id": "Invoice number",
    "invoice_date": "Invoice date",
    "quantity": "Quantity (seats, units)",
    "unit_price": "Unit price",
    "amount_billed": "Amount billed",
    "customer_name": "Customer",
    "product": "Product or item",
}
REQUIRED = ("invoice_id", "invoice_date", "amount_billed")
CANONICAL_HEADERS = {"invoice_id", "invoice_date", "quantity", "unit_price", "amount_billed"}

# Exact header names (after normalising), in priority order per field.
SYNONYMS: dict[str, list[str]] = {
    "invoice_id": ["invoice id", "invoice number", "invoice no", "invoice num", "invoice", "inv id", "inv number",
                   "inv no", "inv", "num", "no", "number", "doc number", "document number", "doc no", "bill number",
                   "bill no", "transaction number", "txn number", "reference", "reference number", "ref", "ref no"],
    "invoice_date": ["invoice date", "inv date", "date", "txn date", "transaction date", "bill date", "billing date",
                     "document date", "doc date", "issue date", "date issued", "posting date", "service date",
                     "period start", "billing period"],
    "quantity": ["quantity", "qty", "seats", "seat count", "active seats", "number of seats", "units", "unit count",
                 "licenses", "licences", "license count", "licence count", "users", "user count", "count", "volume"],
    "unit_price": ["unit price", "price", "rate", "unit rate", "unit cost", "price per unit", "cost per unit",
                   "sales price", "seat price", "price per seat", "rate per seat", "each"],
    "amount_billed": ["amount billed", "billed amount", "line total", "line amount", "extended amount", "ext amount",
                      "extended price", "amount", "total", "net amount", "invoice amount", "invoice total",
                      "total amount", "subtotal", "charge", "charges", "billed"],
    "customer_name": ["customer name", "customer", "client name", "client", "account name", "account", "bill to",
                      "billed to", "company", "company name", "customer full name", "name", "customer id",
                      "account id", "client id"],
    "product": ["sku", "item", "item code", "product", "product code", "product name", "item name", "service",
                "plan", "line item", "description", "item description", "line description", "memo"],
}
# Looser fallback: a header containing one of these fragments, tried after exact names.
CONTAINS: dict[str, list[str]] = {
    "invoice_date": ["date"],
    "quantity": ["qty", "quantity", "seat", "licen", "user"],
    "unit_price": ["price", "rate", "unit cost"],
    "amount_billed": ["amount", "total"],
    "invoice_id": ["invoice", "inv "],
    "customer_name": ["customer", "client", "account"],
    "product": ["sku", "item", "product"],
}
NON_BILLING_LINE = re.compile(r"\b(tax|vat|gst|hst|pst|shipping|freight|discount|credit card fee)\b", re.IGNORECASE)


class IntakeError(ValueError):
    """An export that can't be read, with a message written for the person running the tool."""


@dataclass
class IntakeResult:
    rows: list[dict[str, str]]
    columns: dict[str, Optional[str]]
    notes: list[str] = field(default_factory=list)
    canonical: bool = False


# --------------------------------------------------------------------------- reading

def _norm(header: Any) -> str:
    text = str(header or "").lower().replace("#", " number ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _cell(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _read_grid(path: Path) -> list[list[Any]]:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        try:
            book = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:
            raise IntakeError(f"Could not open {path.name} as an Excel file ({exc}).") from exc
        sheet = max(book.worksheets, key=lambda ws: ws.max_row or 0)
        return [[_cell(v) for v in row] for row in sheet.iter_rows(values_only=True)]
    if suffix == ".xls":
        raise IntakeError(f"{path.name} is an old Excel file. Open it in Excel and use Save As > Excel Workbook (.xlsx) or CSV.")
    if suffix not in (".csv", ".tsv", ".txt"):
        raise IntakeError(f"{path.name}: invoice exports must be CSV or Excel (.xlsx).")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if suffix == ".tsv" else csv.excel
    return [[_cell(v) for v in row] for row in csv.reader(text.splitlines(), dialect)]


def _known(header: str) -> bool:
    return any(header in names for names in SYNONYMS.values())


def _find_header(grid: list[list[Any]]) -> int:
    """Exports from accounting tools often put a report title and blank lines above the table."""
    best, best_score = None, 1
    for i, row in enumerate(grid[:25]):
        score = sum(_known(_norm(v)) for v in row if v not in (None, ""))
        if score > best_score:
            best, best_score = i, score
    if best is not None:
        return best
    for i, row in enumerate(grid[:25]):
        if sum(v not in (None, "") for v in row) >= 3:
            return i
    raise IntakeError("Could not find the column headings in the invoice export. Make sure the file has one row of headings above the data.")


def _headers(row: list[Any]) -> list[str]:
    seen: dict[str, int] = {}
    names = []
    for i, value in enumerate(row):
        name = str(value).strip() if value not in (None, "") else f"Column {i + 1}"
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return names


# --------------------------------------------------------------------------- column matching

def _match_columns(headers: list[str]) -> dict[str, Optional[str]]:
    normed = {h: _norm(h) for h in headers}
    used: set[str] = set()
    columns: dict[str, Optional[str]] = {f: None for f in FIELDS}
    for f, names in SYNONYMS.items():
        for name in names:
            hit = next((h for h in headers if h not in used and normed[h] == name), None)
            if hit:
                columns[f] = hit
                used.add(hit)
                break
    for f, fragments in CONTAINS.items():
        if columns[f]:
            continue
        hit = next((h for h in headers if h not in used and any(fr in f" {normed[h]} " for fr in fragments)), None)
        if hit:
            columns[f] = hit
            used.add(hit)
    return columns


class _ColumnGuess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_id: Optional[str] = Field(None, description="Column holding the invoice number or ID.")
    invoice_date: Optional[str] = Field(None, description="Column holding the invoice or billing date.")
    quantity: Optional[str] = Field(None, description="Column holding the number of seats, units or licenses billed.")
    unit_price: Optional[str] = Field(None, description="Column holding the price per seat or unit.")
    amount_billed: Optional[str] = Field(None, description="Column holding the line or invoice amount charged.")
    customer_name: Optional[str] = Field(None, description="Column naming the customer or account.")
    product: Optional[str] = Field(None, description="Column naming the product, item, SKU or service.")


def _ask_local_model(headers: list[str], samples: list[list[Any]], missing: list[str]) -> dict[str, str]:
    """Language only: the model names columns. Its answer must be an exact existing header or it is ignored."""
    from openai import OpenAI

    client = OpenAI(base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), api_key="ollama")
    sample_text = "\n".join(" | ".join(str(v) for v in row) for row in samples[:3])
    prompt = (
        "/no_think\n\nAn invoice export has these column headings:\n"
        + "\n".join(f"- {h}" for h in headers)
        + f"\n\nFirst rows (same order, separated by |):\n{sample_text}\n\n"
        f"Which heading holds each of: {', '.join(missing)}? Answer with a heading copied exactly from the list, "
        "or null if none fits. Do not calculate anything."
    )
    completion = client.chat.completions.parse(
        model=os.getenv("OLLAMA_MODEL", "qwen3:14b"), temperature=0,
        messages=[{"role": "user", "content": prompt}], response_format=_ColumnGuess, timeout=90,
    )
    guess = completion.choices[0].message.parsed
    if guess is None:
        return {}
    return {f: v for f in missing if (v := getattr(guess, f)) in headers}


# --------------------------------------------------------------------------- values

def parse_amount(raw: Any) -> Optional[Decimal]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, Decimal)):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(repr(raw))  # via text, so binary float error never reaches the ledger
    text = str(raw).strip()
    if text in ("", "-"):
        return None
    negative = (text.startswith("(") and text.endswith(")")) or text.startswith("-") or text.endswith("-")
    digits = re.sub(r"[^\d,.]", "", text)
    if not digits:
        raise ValueError(f"not an amount: {raw!r}")
    if "," in digits and "." in digits:
        # Whichever separator comes last is the decimal point: 1,234.56 or 1.234,56
        digits = digits.replace(".", "").replace(",", ".") if digits.rfind(",") > digits.rfind(".") else digits.replace(",", "")
    elif "," in digits:
        digits = digits.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", digits) else digits.replace(",", ".")
    try:
        value = Decimal(digits)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {raw!r}") from exc
    return -value if negative else value


NAMED_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y",
                      "%d-%b-%Y", "%d-%b-%y", "%b-%d-%Y", "%Y%m%d")
NUMERIC_DATE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$")


def detect_date_order(values: list[Any]) -> Optional[str]:
    """'MDY' or 'DMY' when the data proves it (a part above 12), else None."""
    order = None
    for v in values:
        m = NUMERIC_DATE.match(str(v).strip().split(" ")[0]) if isinstance(v, str) else None
        if not m:
            continue
        first, second = int(m[1]), int(m[2])
        found = "DMY" if first > 12 else "MDY" if second > 12 else None
        if found and order and found != order:
            raise IntakeError("The date column mixes day/month/year and month/day/year formats. Export it with one date format.")
        order = found or order
    return order


def parse_date(raw: Any, order: str) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)) and 20000 < raw < 80000:  # Excel serial day number
        return date(1899, 12, 30) + timedelta(days=int(raw))
    text = str(raw).strip()
    head = text.split("T")[0].split(" ")[0]
    if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$", head):
        y, m, d = map(int, re.split(r"[-/]", head))
        return date(y, m, d)
    if m := NUMERIC_DATE.match(head):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        return date(y, a, b) if order == "MDY" else date(y, b, a)
    for fmt in NAMED_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"not a date: {raw!r}")


# --------------------------------------------------------------------------- mapping file

def mapping_path(invoices: Path) -> Path:
    return invoices.with_name(f"{invoices.stem}.mapping.json")


def _signature(headers: list[str]) -> str:
    return hashlib.sha256("|".join(_norm(h) for h in headers).encode()).hexdigest()[:16]


def _load_mapping(path: Path, signature: str) -> Optional[dict]:
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return saved if saved.get("layout") == signature else None


# --------------------------------------------------------------------------- conversation

def _sample(rows: list[list[Any]], headers: list[str], column: Optional[str]) -> str:
    if not column:
        return ""
    i = headers.index(column)
    for row in rows:
        if i < len(row) and row[i] not in (None, ""):
            v = row[i]
            return f"{v:%Y-%m-%d}" if isinstance(v, (date, datetime)) else str(v)
    return ""


def _mapping_table(columns: dict[str, Optional[str]], headers: list[str], rows: list[list[Any]], order: Optional[str]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, header_style="bold grey70", expand=False)
    table.add_column("We need", style="bold")
    table.add_column("Your column")
    table.add_column("Example from your file", style="grey62")
    for f, label in FIELDS.items():
        col = columns.get(f)
        shown = Text(col, style="cyan") if col else Text("not in file", style="yellow" if f in REQUIRED else "grey50")
        if f == "invoice_date" and col and order:
            shown.append(f"  ({'month/day/year' if order == 'MDY' else 'day/month/year'})", style="grey50")
        table.add_row(label, shown, _sample(rows, headers, col))
    return table


def _edit_columns(console: Console, columns: dict[str, Optional[str]], headers: list[str]) -> dict[str, Optional[str]]:
    console.print("\n[bold]Your columns:[/]")
    for i, h in enumerate(headers, start=1):
        console.print(f"  [cyan]{i:>2}[/]  {h}")
    console.print("  [cyan] 0[/]  not in my file\n")
    updated = {}
    for f, label in FIELDS.items():
        current = str(headers.index(columns[f]) + 1) if columns.get(f) else "0"
        choice = Prompt.ask(f"  {label}", default=current, choices=[str(i) for i in range(len(headers) + 1)], show_choices=False)
        updated[f] = headers[int(choice) - 1] if choice != "0" else None
    return updated


def _pick_one(console: Console, title: str, counts: dict[str, int], default: Optional[str], interactive: bool) -> str:
    options = sorted(counts, key=lambda k: (-counts[k], k))
    if not interactive:
        if default:
            return default
        raise IntakeError(f"{title} Run this once in a terminal to choose, or set it in the .mapping.json file next to the export.")
    console.print(f"\n[bold]{title}[/]")
    for i, name in enumerate(options, start=1):
        console.print(f"  [cyan]{i:>2}[/]  {name} [grey50]({counts[name]} rows)[/]")
    pick = Prompt.ask("  Number", default=str(options.index(default) + 1) if default in options else None,
                      choices=[str(i) for i in range(1, len(options) + 1)], show_choices=False)
    return options[int(pick) - 1]


def _pick_many(console: Console, title: str, counts: dict[str, int], default: list[str], interactive: bool) -> list[str]:
    options = sorted(counts, key=lambda k: (-counts[k], k))
    if not interactive:
        return default
    console.print(f"\n[bold]{title}[/]")
    for i, name in enumerate(options, start=1):
        console.print(f"  [cyan]{i:>2}[/]  {name} [grey50]({counts[name]} rows)[/]")
    default_txt = ",".join(str(options.index(d) + 1) for d in default)
    while True:
        raw = Prompt.ask("  Numbers, separated by commas", default=default_txt)
        try:
            picks = sorted({int(p) for p in raw.replace(" ", "").split(",") if p})
            if picks and all(1 <= p <= len(options) for p in picks):
                return [options[p - 1] for p in picks]
        except ValueError:
            pass
        console.print("  [red]Use the numbers from the list, for example 1,3[/]")


# --------------------------------------------------------------------------- main entry

def load_invoices(path: Path, contract_text: str = "", console: Optional[Console] = None,
                  interactive: Optional[bool] = None) -> IntakeResult:
    console = console or Console()
    interactive = sys.stdin.isatty() if interactive is None else interactive
    grid = _read_grid(path)
    header_at = _find_header(grid)
    headers = _headers(grid[header_at])
    body = [r for r in grid[header_at + 1:] if any(v not in (None, "") for v in r)]
    if not body:
        raise IntakeError(f"{path.name} has headings but no invoice rows.")

    canonical = CANONICAL_HEADERS <= {h for h in headers}
    signature = _signature(headers)
    saved = None if canonical else _load_mapping(mapping_path(path), signature)
    draft = None
    if saved and interactive and not saved.get("confirmed_by_person"):
        draft, saved = saved, None
    notes: list[str] = []
    if header_at:
        notes.append(f"Skipped {header_at} line(s) above the column headings.")

    if canonical:
        columns = {f: (f if f in headers else None) for f in FIELDS}
        columns["product"] = next((h for h in ("sku", "product", "item") if h in headers), None)
    elif saved:
        columns = {f: saved["columns"].get(f) for f in FIELDS}
        console.print(f"[grey62]Intake: using the saved column mapping in {mapping_path(path).name}[/]")
    elif draft:
        columns = {f: draft["columns"].get(f) for f in FIELDS}
    else:
        columns = _match_columns(headers)
        missing = [f for f in REQUIRED if not columns[f]]
        if not columns["quantity"] and not columns["unit_price"]:
            missing += ["quantity", "unit_price"]
        if missing:
            try:
                with console.status("[magenta]Intake: asking the local model to name the remaining columns…[/]"):
                    named = _ask_local_model(headers, body, missing)
                columns.update(named)
                if named:
                    notes.append("The local model matched " + ", ".join(f"{FIELDS[f]} to '{c}'" for f, c in named.items())
                                 + ". Check those lines.")
            except Exception:
                notes.append("The local model was not reachable, so only the built-in column names were used.")

    # Date order: data first, then a saved answer, then ask once.
    date_idx = headers.index(columns["invoice_date"]) if columns.get("invoice_date") else None
    date_values = [r[date_idx] for r in body if date_idx is not None and date_idx < len(r)]
    order = detect_date_order(date_values) or (saved or {}).get("date_order")

    if not canonical and not saved:
        while True:
            console.print(Panel(
                Group(Text("Here is how I read your invoice export. Check each line before the numbers are used.", style="grey70"),
                      _mapping_table(columns, headers, body, order)),
                title="[bold]INTAKE[/]  [grey62]" + path.name + "[/]", border_style="cyan", padding=(1, 2)))
            if not interactive:
                notes.append(f"Column mapping was not confirmed by a person (no terminal). Review {mapping_path(path).name}.")
                break
            if Confirm.ask("Does this look right?", default=True):
                break
            columns = _edit_columns(console, columns, headers)
            date_idx = headers.index(columns["invoice_date"]) if columns.get("invoice_date") else None
            date_values = [r[date_idx] for r in body if date_idx is not None and date_idx < len(r)]
            order = detect_date_order(date_values) or order

    missing = [FIELDS[f] for f in REQUIRED if not columns.get(f)]
    if not columns.get("quantity") and not columns.get("unit_price"):
        missing.append("Quantity or Unit price")
    if missing:
        raise IntakeError("The invoice export needs these columns: " + ", ".join(missing) + ". Add them to the export, or run again and pick them from your columns.")

    has_numeric_dates = any(NUMERIC_DATE.match(str(v).strip().split(" ")[0]) for v in date_values if isinstance(v, str))
    if has_numeric_dates and not order:
        if interactive and not canonical:
            order = "MDY" if Confirm.ask("Dates like 03/04/2026: is that March 4 (month first)?", default=True) else "DMY"
        else:
            order = "MDY"
            notes.append("Dates like 03/04/2026 were read month first (US format).")
    order = order or "MDY"

    def col(row: list[Any], f: str) -> Any:
        name = columns.get(f)
        if not name:
            return None
        i = headers.index(name)
        return row[i] if i < len(row) else None

    # Keep real invoice lines: drop subtotal/total rows and lines with no invoice number.
    first_data_row = header_at + 2
    numbered = [(first_data_row + i, r) for i, r in enumerate(grid[header_at + 1:]) if any(v not in (None, "") for v in r)]
    kept, dropped = [], 0
    for n, r in numbered:
        inv = col(r, "invoice_id")
        if inv in (None, "") or re.search(r"\btotal\b", str(inv), re.IGNORECASE):
            dropped += 1
            continue
        kept.append((n, r))
    if dropped:
        notes.append(f"Skipped {dropped} row(s) with no invoice number (totals, subtotals or blank lines).")

    contract_lower = contract_text.lower()

    customer = (saved or {}).get("customer")
    if columns.get("customer_name"):
        counts: dict[str, int] = {}
        for _, r in kept:
            name = str(col(r, "customer_name") or "").strip()
            counts[name] = counts.get(name, 0) + 1
        if len(counts) > 1:
            if customer not in counts:
                guess = next((c for c in counts if c and c.lower() in contract_lower), None)
                customer = _pick_one(console, "This export has more than one customer. Which one is this contract for?",
                                     counts, guess, interactive)
            kept = [(n, r) for n, r in kept if str(col(r, "customer_name") or "").strip() == customer]
            notes.append(f"Using only customer: {customer}.")

    products = (saved or {}).get("products")
    if columns.get("product"):
        counts = {}
        for _, r in kept:
            name = str(col(r, "product") or "").strip()
            counts[name] = counts.get(name, 0) + 1
        if len(counts) > 1:
            if not products or not set(products) <= set(counts):
                named = [p for p in counts if p and p.lower() in contract_lower]
                default = named or [p for p in counts if not NON_BILLING_LINE.search(p)] or list(counts)
                products = _pick_many(console, "Which of these products or line types does this contract price?",
                                      counts, default, interactive)
            kept = [(n, r) for n, r in kept if str(col(r, "product") or "").strip() in products]
            notes.append("Using only: " + ", ".join(products) + ".")

    # Parse every kept line. Bad values stop the run with the row number, never get skipped silently.
    lines = []
    for excel_row, r in kept:
        try:
            inv = str(col(r, "invoice_id")).strip()
            if isinstance(col(r, "invoice_id"), float) and float(col(r, "invoice_id")).is_integer():
                inv = str(int(col(r, "invoice_id")))
            lines.append({
                "invoice_id": inv,
                "date": parse_date(col(r, "invoice_date"), order),
                "customer": str(col(r, "customer_name") or "").strip(),
                "qty": parse_amount(col(r, "quantity")) if columns.get("quantity") else None,
                "unit": parse_amount(col(r, "unit_price")) if columns.get("unit_price") else None,
                "amount": parse_amount(col(r, "amount_billed")),
            })
        except ValueError as exc:
            raise IntakeError(f"Row {excel_row} of {path.name}: {exc}. Fix that cell and run again.") from None
        if lines[-1]["amount"] is None and (lines[-1]["qty"] is None or lines[-1]["unit"] is None):
            raise IntakeError(f"Row {excel_row} of {path.name} has no amount. Fix that cell and run again.")

    if not lines:
        raise IntakeError(f"No invoice lines are left in {path.name} after filtering.")

    # One invoice can arrive as several lines (split seats, mid-month adds). The Analyst audits whole invoices.
    merged: dict[str, dict] = {}
    split = set()
    for ln in lines:
        if ln["amount"] is None:
            ln["amount"] = ln["qty"] * ln["unit"]
        if ln["invoice_id"] in merged:
            split.add(ln["invoice_id"])
            m = merged[ln["invoice_id"]]
            m["amount"] += ln["amount"]
            m["qty"] = None if m["qty"] is None or ln["qty"] is None else m["qty"] + ln["qty"]
            m["units"].add(ln["unit"])
        else:
            merged[ln["invoice_id"]] = {**ln, "units": {ln["unit"]}}
    if split:
        notes.append(f"Combined split lines on {len(split)} invoice(s) into one line each.")

    credits = [k for k, m in merged.items() if m["amount"] < 0]
    for k in credits:
        del merged[k]
    if credits:
        notes.append(f"Set aside {len(credits)} credit note(s) with negative totals ({', '.join(credits[:5])}"
                     f"{', ...' if len(credits) > 5 else ''}). Review credits separately.")
    if not merged:
        raise IntakeError(f"No invoices are left in {path.name} after setting aside credit notes.")

    derived_unit = derived_qty = False
    rows = []
    for m in merged.values():
        qty, amount = m["qty"], m["amount"]
        unit = next(iter(m["units"])) if len(m["units"]) == 1 else None
        if qty is None:
            if not unit:
                raise IntakeError(f"Invoice {m['invoice_id']} has no quantity and no unit price, so seats can't be worked out.")
            qty = amount / unit
            derived_qty = True
        if qty != qty.to_integral_value():
            raise IntakeError(f"Invoice {m['invoice_id']} bills a fractional quantity ({qty}). This system audits whole seats or units.")
        if unit is None:
            unit = (amount / qty).quantize(CENT, rounding=ROUND_HALF_UP) if qty else Decimal("0")
            derived_unit = True
        rows.append({
            "invoice_id": m["invoice_id"],
            "invoice_date": m["date"].isoformat(),
            "customer_name": m["customer"],
            "quantity": str(int(qty)),
            "unit_price": str(unit.quantize(CENT, rounding=ROUND_HALF_UP)),
            "amount_billed": str(amount.quantize(CENT, rounding=ROUND_HALF_UP)),
        })
    if derived_unit:
        notes.append("Unit price was worked out as amount / quantity (Python Decimal) where the file didn't give one.")
    if derived_qty:
        notes.append("Quantity was worked out as amount / unit price (Python Decimal) where the file didn't give one.")

    if not canonical:
        record = {
            "layout": signature, "columns": columns, "date_order": order, "customer": customer, "products": products,
            "confirmed_by_person": interactive or bool((saved or {}).get("confirmed_by_person")),
        }
        if not saved or any(saved.get(k) != v for k, v in record.items()):
            mapping_path(path).write_text(json.dumps({**record, "saved_at": datetime.now().isoformat(timespec="seconds")}, indent=2))
            notes.append(f"Saved this mapping to {mapping_path(path).name}. Delete that file to be asked again.")

    rows.sort(key=lambda r: (r["invoice_date"], r["invoice_id"]))
    return IntakeResult(rows=rows, columns=columns, notes=notes, canonical=canonical)
