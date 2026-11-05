"""Render client uploads (invoice CSVs and contracts) into viewer-friendly HTML + PDF.

    python tools/render_pdfs.py                  # render anything new or changed, then exit
    python tools/render_pdfs.py --watch          # keep running and render files as they land
    python tools/render_pdfs.py --install-agent  # start the watcher automatically at login (macOS / Windows)
    python tools/render_pdfs.py --uninstall-agent

On Windows use .venv\\Scripts\\python instead of python. PDFs are printed with Chrome, or with
Microsoft Edge, which every Windows PC has.

Watched folders default to client_inbox/ and demo_data/ (override with --dir, repeatable).
Output goes to a rendered/ folder inside each watched folder. A file is re-rendered only when
its contents change, and every CSV in a folder is re-rendered when a contract next to it changes.

Rendering is local (headless Chrome); nothing leaves the machine. Presentation only: values are
shown exactly as uploaded, never corrected.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRS = (ROOT / "client_inbox", ROOT / "demo_data")
OUT_DIRNAME = "rendered"
STATE_FILE = ".render_state.json"
CONTRACT_EXTS = {".md", ".txt"}
INPUT_EXTS = CONTRACT_EXTS | {".csv"}
POLL_SECONDS = 3
SETTLE_SECONDS = 3
MAX_INVOICE_PAGES = 60

AGENT_LABEL = "com.physis.system001.render-pdfs"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
AGENT_LOG = ROOT / "logs" / "render_pdfs.log"
AGENT_PID = ROOT / "logs" / "render_pdfs.pid"
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
WIN_STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
WIN_LAUNCHER = WIN_STARTUP / "System001 Render PDFs.cmd"

MAC_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
WIN_BROWSERS = (
    ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
    ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
    ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
    ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
    ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
)
PATH_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "msedge", "chrome")

REQUIRED_COLUMNS = {"invoice_id", "invoice_date", "customer_name", "quantity", "unit_price", "amount_billed"}
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%m/%d/%y")
CURRENCY_SYMBOLS = {"USD": "$", "CAD": "CA$", "AUD": "A$", "EUR": "€", "GBP": "£"}

console = Console()

BASE_CSS = """
  @page { size: Letter; margin: 0 }
  * { box-sizing: border-box }
  html { -webkit-print-color-adjust: exact; print-color-adjust: exact }
  body { margin: 0; background: #e9ecf1; color: #111827;
         font: 13px/1.55 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif }
  .page { width: 8.5in; min-height: 11in; margin: 24px auto; padding: 0.7in 0.75in 0.9in;
          background: #fff; box-shadow: 0 2px 12px rgba(0,0,0,.08); position: relative }
  @media print { body { background: #fff }
                 .page { margin: 0; box-shadow: none; page-break-after: always; height: 11in; overflow: hidden }
                 .page:last-child { page-break-after: auto } .page.flow { height: auto; overflow: visible } }
  @media screen and (max-width: 8.6in) { .page { width: auto; min-height: 0; margin: 12px; padding: 24px 16px 56px } }
  .muted { color: #6b7280 }
  .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap }
  table { border-collapse: collapse; width: 100% }
  th { font-size: 10px; text-transform: uppercase; letter-spacing: .07em; color: #6b7280;
       text-align: left; padding: 8px 8px; border-bottom: 1.5px solid #111827 }
  td { padding: 8px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top }
  code { font: 11px ui-monospace, Menlo, monospace }
  .brand { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px;
           border-bottom: 3px solid #0f2a4a; padding-bottom: 14px; margin-bottom: 24px }
  .brand .name { font-size: 15px; font-weight: 700; color: #0f2a4a; letter-spacing: .01em }
  .brand .doc { font-size: 22px; font-weight: 700; letter-spacing: .12em; color: #0f2a4a }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 10px;
          font-weight: 700; letter-spacing: .06em }
  .pill.paid { background: #d1fae5; color: #065f46 }
  .pill.open { background: #fef3c7; color: #92400e }
  .warn { background: #fef3c7; color: #92400e; border-radius: 6px; padding: 8px 12px; margin-bottom: 16px; font-size: 12px }
  footer.foot { position: absolute; left: 0.75in; right: 0.75in; bottom: 0.45in;
                font-size: 10px; color: #9ca3af; display: flex; justify-content: space-between; gap: 16px }
  @media screen and (max-width: 8.6in) { footer.foot { left: 16px; right: 16px; bottom: 16px } }
"""

INVOICE_CSS = """
  h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .09em; color: #6b7280; margin: 26px 0 10px }
  .tiles { display: grid; grid-template-columns: .7fr 1.2fr 1.5fr 1fr; gap: 10px }
  @media screen and (max-width: 8.6in) { .tiles { grid-template-columns: repeat(2, 1fr) } }
  .tile { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 12px; background: #f9fafb }
  .tile .k { font-size: 10px; text-transform: uppercase; letter-spacing: .07em; color: #6b7280 }
  .tile .v { font-size: 19px; font-weight: 700; font-variant-numeric: tabular-nums; color: #0f2a4a }
  .tile .v.sm { font-size: 14px; padding-top: 4px; white-space: nowrap }
  .note { font-size: 11px; margin: 4px 0 0 }
  tbody tr:nth-child(even) td { background: #fafbfc }
  table.ledger td { padding: 6px 8px }
  tr.total td { font-weight: 700; border-top: 1.5px solid #111827; border-bottom: none; background: #fff }
  .meta { display: flex; justify-content: space-between; gap: 24px; margin-bottom: 30px }
  .meta .k { font-size: 10px; text-transform: uppercase; letter-spacing: .07em; color: #6b7280; margin-bottom: 4px }
  .meta dl { display: grid; grid-template-columns: auto auto; gap: 3px 16px; margin: 0 }
  .meta dt { color: #6b7280 } .meta dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums }
  table.sum { width: 46%; margin: 18px 0 0 auto }
  table.sum td { border-bottom: 1px solid #e5e7eb }
  table.sum tr.grand td { font-size: 15px; font-weight: 700; color: #0f2a4a; border-top: 1.5px solid #111827; border-bottom: none }
  .scroll { overflow-x: auto }
  table.raw { font-size: 11px } table.raw td, table.raw th { padding: 5px 6px; white-space: nowrap }
"""

CONTRACT_CSS = """
  .page { font: 12.5px/1.65 "Iowan Old Style", "Palatino", Georgia, serif }
  h1 { font: 700 20px/1.2 -apple-system, "Helvetica Neue", Arial, sans-serif; letter-spacing: .14em;
       text-align: center; color: #0f2a4a; margin: 0 0 22px }
  h2 { font: 700 12px/1.3 -apple-system, "Helvetica Neue", Arial, sans-serif; text-transform: uppercase;
       letter-spacing: .1em; color: #0f2a4a; margin: 22px 0 6px; padding-bottom: 4px; border-bottom: 1px solid #d1d5db }
  h3, h4 { font: 600 12.5px/1.3 -apple-system, "Helvetica Neue", Arial, sans-serif; margin: 14px 0 2px }
  p { margin: 0 0 8px; text-align: justify }
  hr { border: 0; border-top: 1px solid #d1d5db; margin: 18px 0 }
  code { background: #f3f4f6; padding: 1px 4px; border-radius: 3px }
  dl.kv { display: grid; grid-template-columns: 170px 1fr; gap: 4px 14px; margin: 0;
          background: #f7f9fc; border: 1px solid #e5e7eb; border-radius: 6px; padding: 14px 18px }
  dl.kv dt { font: 600 10.5px/1.9 -apple-system, "Helvetica Neue", Arial, sans-serif; text-transform: uppercase;
             letter-spacing: .06em; color: #6b7280 }
  dl.kv dd { margin: 0 } dl.kv dt strong { font-weight: inherit }
  table.md { margin-top: 10px; table-layout: fixed }
  table.md th { font-size: 11px; color: #0f2a4a }
  table.md td { border-bottom: none; padding: 5px 8px }
"""


def document(title: str, css: str, pages: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{BASE_CSS}{css}</style></head><body>\n{pages}\n</body></html>")


def footer(left: str) -> str:
    return (f'<footer class="foot"><span>{html.escape(left)}</span>'
            f"<span>Rendered {date.today():%b %d, %Y}</span></footer>")


def money(value: Decimal, currency: str = "") -> str:
    symbol = CURRENCY_SYMBOLS.get(currency.upper())
    if symbol:
        return f"-{symbol}{-value:,.2f}" if value < 0 else f"{symbol}{value:,.2f}"
    return f"{value:,.2f} {currency}".strip()


def nice_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


# --------------------------------------------------------------------------- contracts

KV_LINE = re.compile(r"^\*\*(.+?):\*\*\s*(.+)$")
# Payment terms phrased as "net thirty (30) days" or "Net 30" -> 30.
NET_TERMS = re.compile(r"\bnet\s+(?:[a-z-]+\s+)?\(?(\d{1,3})\)?", re.IGNORECASE)


@dataclass(frozen=True)
class Contract:
    path: Path
    text: str
    title: str
    order_no: str | None
    provider: str | None
    customer: str | None
    account: str | None
    net_days: int | None

    def covers(self, customer_id: str, customer_name: str) -> bool:
        if self.account and customer_id and self.account.lower() == customer_id.lower():
            return True
        return bool(self.customer and self.customer.lower() == customer_name.lower())


def _strip_role(value: str) -> str:
    return re.sub(r'\s*\("[^"]+"\)\s*$', "", value).strip()


def read_contract(path: Path) -> Contract:
    text = path.read_text(encoding="utf-8", errors="replace")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if m := KV_LINE.match(line.strip()):
            fields.setdefault(m[1].strip().lower(), _strip_role(m[2]))
    h1 = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), None)
    net = NET_TERMS.search(text)
    return Contract(
        path=path,
        text=text,
        title=h1 or path.stem.replace("_", " ").title(),
        order_no=fields.get("order form no.") or fields.get("contract no.") or fields.get("agreement no."),
        provider=fields.get("provider") or fields.get("vendor") or fields.get("supplier"),
        customer=fields.get("customer") or fields.get("client"),
        account=fields.get("customer account") or fields.get("customer id") or fields.get("account"),
        net_days=int(net[1]) if net else None,
    )


def _inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", t)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", t)


def markdown_to_html(md: str) -> str:
    """Contract-grade Markdown subset: headings, rules, key/value header, lists, tables, paragraphs."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", md.strip()):
        lines = [l.rstrip() for l in block.splitlines() if l.strip()]
        while lines and lines[0].lstrip().startswith("#"):
            head = lines.pop(0).strip()
            level = min(len(head) - len(head.lstrip("#")), 4)
            out.append(f"<h{level}>{_inline(head.lstrip('#').strip())}</h{level}>")
        if not lines:
            continue
        if all(l.strip() in {"---", "***", "___"} for l in lines):
            out.append("<hr>")
        elif all(KV_LINE.match(l.strip()) for l in lines):
            rows = "".join(f"<dt>{_inline(m[1])}</dt><dd>{_inline(m[2])}</dd>"
                           for m in (KV_LINE.match(l.strip()) for l in lines))
            out.append(f'<dl class="kv">{rows}</dl>')
        elif lines[0].lstrip().startswith("|"):
            rows = [l for l in lines if not re.match(r"^\s*\|[\s\-|:]+\|\s*$", l)]
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            head = "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
            body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
            out.append(f'<table class="md"><tr>{head}</tr>{body}</table>')
        elif all(re.match(r"^\s*[-*+]\s+", l) for l in lines):
            items = "".join(f"<li>{_inline(re.sub(r'^\s*[-*+]\s+', '', l))}</li>" for l in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{_inline(' '.join(l.strip() for l in lines))}</p>")
    return "\n".join(out)


def contract_document(c: Contract) -> tuple[str, str]:
    body = markdown_to_html(c.text) if c.path.suffix.lower() == ".md" else "".join(
        f"<p>{html.escape(p.strip())}</p>" for p in re.split(r"\n\s*\n", c.text) if p.strip())
    left = " · ".join(filter(None, [c.order_no, c.customer])) or c.path.name
    page = f"""<div class="page flow">
  <div class="brand"><div class="name">{html.escape(c.provider or c.title)}</div>
    <div class="muted" style="text-align:right;font-size:11px">Contract<br>Confidential</div></div>
  {body}
  {footer(left)}
</div>"""
    title = f"{c.customer} Contract" if c.customer else c.title
    return document(title, CONTRACT_CSS, page), "contract"


# --------------------------------------------------------------------------- invoices

@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    day: date
    customer_id: str
    customer_name: str
    sku: str
    description: str
    qty: Decimal
    unit: Decimal
    amount: Decimal
    currency: str
    status: str


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _date(raw: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognised date {raw!r}")


def _dec(raw: str) -> Decimal:
    cleaned = re.sub(r"[\s,$€£]", "", raw)
    if cleaned.startswith("(") and cleaned.endswith(")"):  # accounting-style negatives
        cleaned = "-" + cleaned[1:-1]
    return Decimal(cleaned)


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if any(isinstance(v, str) and v.strip() for v in r.values())]
        return list(reader.fieldnames or []), rows


def to_invoices(headers: list[str], rows: list[dict]) -> list[Invoice]:
    key = {_norm(h): h for h in headers}
    missing = REQUIRED_COLUMNS - key.keys()
    if missing:
        raise ValueError(f"missing invoice columns: {', '.join(sorted(missing))}")

    def get(row: dict, name: str) -> str:
        value = row.get(key[name]) if name in key else ""
        return value.strip() if isinstance(value, str) else ""

    invoices = []
    for n, r in enumerate(rows, start=2):
        try:
            invoices.append(Invoice(
                invoice_id=get(r, "invoice_id"), day=_date(get(r, "invoice_date")),
                customer_id=get(r, "customer_id"), customer_name=get(r, "customer_name"),
                sku=get(r, "sku"), description=get(r, "description"),
                qty=_dec(get(r, "quantity")), unit=_dec(get(r, "unit_price")), amount=_dec(get(r, "amount_billed")),
                currency=get(r, "currency").upper(), status=get(r, "status"),
            ))
        except (ValueError, ArithmeticError) as exc:
            raise ValueError(f"row {n}: {exc}") from None
    if not invoices:
        raise ValueError("no invoice rows")
    return sorted(invoices, key=lambda i: (i.day, i.invoice_id))


def _pill(status: str) -> str:
    if not status:
        return ""
    cls = "paid" if status.upper() == "PAID" else "open"
    return f'<span class="pill {cls}">{html.escape(status.upper())}</span>'


def _short(value: Decimal) -> str:
    return f"{value / 1000:,.0f}k" if abs(value) >= 1000 else f"{value:,.0f}"


def _bar_chart(invoices: list[Invoice]) -> str:
    months: dict[date, Decimal] = {}
    for i in invoices:
        m = i.day.replace(day=1)
        months[m] = months.get(m, Decimal(0)) + i.amount
    series = sorted(months.items())
    peak = max(v for _, v in series)
    if peak <= 0:
        return ""
    w, h, pad_b, pad_t = Decimal(700), Decimal(135), Decimal(22), Decimal(18)
    slot = w / len(series)
    bw = slot * Decimal("0.62")
    label_every = max(1, math.ceil(len(series) / 12))
    bars = []
    for n, (month, value) in enumerate(series):
        bh = max(value, Decimal(0)) / peak * (h - pad_b - pad_t)
        x, y = n * slot + (slot - bw) / 2, h - pad_b - bh
        cx = x + bw / 2
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" fill="#1f4e8c"/>')
        if len(series) <= 18:
            bars.append(f'<text x="{cx:.1f}" y="{y - 5:.1f}" text-anchor="middle" font-size="9" fill="#374151">{_short(value)}</text>')
        if n % label_every == 0:
            bars.append(f'<text x="{cx:.1f}" y="{h - 6}" text-anchor="middle" font-size="9" fill="#6b7280">{month:%b %y}</text>')
    return (f'<h2>Amount billed per month</h2><svg viewBox="0 0 {w} {h}" width="100%" role="img" '
            f'aria-label="Amount billed per month"><line x1="0" y1="{h - pad_b}" x2="{w}" y2="{h - pad_b}" '
            f'stroke="#d1d5db"/>{"".join(bars)}</svg>')


def ledger_page(invoices: list[Invoice], source: str, contract: Contract | None, invoice_pages: bool) -> str:
    e = html.escape
    first = invoices[0]
    currencies = {i.currency for i in invoices}
    cur = first.currency if len(currencies) == 1 else ""
    total = sum((i.amount for i in invoices), Decimal(0))
    total_txt = money(total, cur) if len(currencies) == 1 else "Mixed currencies"
    with_status = [i for i in invoices if i.status]
    paid = sum(i.status.upper() == "PAID" for i in with_status)
    status_txt = f"{paid} of {len(invoices)} paid" if with_status else "—"
    units = sorted({i.unit for i in invoices})
    rows = "".join(
        f"<tr><td>{e(i.invoice_id)}</td><td>{i.day:%b %d, %Y}</td><td>{e(i.sku)}</td>"
        f"<td class='num'>{i.qty:,}</td><td class='num'>{money(i.unit, i.currency)}</td>"
        f"<td class='num'>{money(i.amount, i.currency)}</td><td>{_pill(i.status)}</td></tr>"
        for i in invoices
    )
    sub = " · ".join(filter(None, [first.customer_name, first.customer_id]))
    skipped = "" if invoice_pages else (
        f'<p class="muted note">Individual invoice pages skipped above {MAX_INVOICE_PAGES} invoices.</p>')
    return f"""<div class="page flow">
  <div class="brand"><div><div class="name">Invoice Ledger</div><div class="muted">{e(sub)}</div></div>
    <div class="doc">LEDGER</div></div>
  <div class="tiles">
    <div class="tile"><div class="k">Invoices</div><div class="v">{len(invoices)}</div></div>
    <div class="tile"><div class="k">Total billed</div><div class="v">{e(total_txt)}</div></div>
    <div class="tile"><div class="k">Period</div><div class="v sm">{invoices[0].day:%b %Y} – {invoices[-1].day:%b %Y}</div></div>
    <div class="tile"><div class="k">Status</div><div class="v sm">{status_txt}</div></div>
  </div>
  {_bar_chart(invoices)}
  <p class="muted note">Unit prices as uploaded: {e(", ".join(money(u, cur) for u in units[:6]))}{" …" if len(units) > 6 else ""}.</p>
  {skipped}
  <h2>All invoices</h2>
  <table class="ledger"><thead><tr><th>Invoice</th><th>Date</th><th>SKU</th><th class="num">Qty</th>
    <th class="num">Unit price</th><th class="num">Amount</th><th>Status</th></tr></thead>
  <tbody>{rows}</tbody>
  <tr class="total"><td colspan="5">Total</td><td class="num">{e(total_txt)}</td><td></td></tr></table>
  {footer(" · ".join(filter(None, [f"Source: {source}", contract and contract.order_no])))}
</div>"""


def invoice_page(i: Invoice, contract: Contract | None) -> str:
    e = html.escape
    terms = ""
    if contract and contract.net_days:
        due = i.day + timedelta(days=contract.net_days)
        terms = (f"<dt>Due date</dt><dd>{nice_date(due)}</dd>"
                 f"<dt>Terms</dt><dd>Net {contract.net_days}</dd>")
    provider = contract.provider if contract and contract.provider else "Invoice record"
    order = f'<div class="muted">Order Form {e(contract.order_no)}</div>' if contract and contract.order_no else ""
    return f"""<div class="page">
  <div class="brand"><div><div class="name">{e(provider)}</div>{order}</div>
    <div style="text-align:right"><div class="doc">INVOICE</div><div style="margin-top:6px">{_pill(i.status)}</div></div></div>
  <div class="meta">
    <div><div class="k">Bill to</div><b>{e(i.customer_name)}</b>
      {f'<br><span class="muted">Account {e(i.customer_id)}</span>' if i.customer_id else ''}</div>
    <dl><dt>Invoice no.</dt><dd>{e(i.invoice_id)}</dd>
        <dt>Invoice date</dt><dd>{nice_date(i.day)}</dd>{terms}
        {f'<dt>Currency</dt><dd>{e(i.currency)}</dd>' if i.currency else ''}</dl>
  </div>
  <table><tr><th>SKU</th><th>Description</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr>
    <tr><td><code>{e(i.sku)}</code></td><td>{e(i.description)}</td><td class="num">{i.qty:,}</td>
        <td class="num">{money(i.unit, i.currency)}</td><td class="num">{money(i.amount, i.currency)}</td></tr></table>
  <table class="sum"><tr><td>Subtotal</td><td class="num">{money(i.amount, i.currency)}</td></tr>
    <tr class="grand"><td>Total{f' ({e(i.currency)})' if i.currency else ''}</td><td class="num">{money(i.amount, i.currency)}</td></tr></table>
  {footer(" · ".join(filter(None, [i.invoice_id, contract and contract.order_no and f"Order Form {contract.order_no}"])))}
</div>"""


def plain_table_document(path: Path, headers: list[str], rows: list[dict], reason: str) -> str:
    e = html.escape
    numeric = re.compile(r"^\(?-?[$€£]?[\d,]+(\.\d+)?\)?$")
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(
            f"<td class='{'num' if numeric.match(v := str(r.get(h) or '').strip()) else ''}'>{e(v)}</td>"
            for h in headers) + "</tr>"
        for r in rows
    )
    page = f"""<div class="page flow">
  <div class="brand"><div><div class="name">{e(path.stem.replace("_", " ").title())}</div>
    <div class="muted">{len(rows)} rows · {len(headers)} columns</div></div><div class="doc">DATA</div></div>
  <div class="warn">Shown as a plain table because it doesn't match the invoice layout ({e(reason)}).</div>
  <div class="scroll"><table class="raw"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>
  {footer(f"Source: {path.name}")}
</div>"""
    return document(path.stem, INVOICE_CSS, page)


def invoices_document(path: Path, contracts: list[Contract]) -> tuple[str, str]:
    headers, rows = read_csv(path)
    try:
        invoices = to_invoices(headers, rows)
    except ValueError as exc:
        return plain_table_document(path, headers, rows, str(exc)), f"plain table: {exc}"

    groups: dict[str, list[Invoice]] = {}
    for i in invoices:
        groups.setdefault(i.customer_id or i.customer_name, []).append(i)
    invoice_pages = len(invoices) <= MAX_INVOICE_PAGES

    pages = []
    for group in groups.values():
        contract = next((c for c in contracts if c.covers(group[0].customer_id, group[0].customer_name)), None)
        pages.append(ledger_page(group, path.name, contract, invoice_pages))
        if invoice_pages:
            pages.extend(invoice_page(i, contract) for i in group)

    title = f"{invoices[0].customer_name} Invoices" if len(groups) == 1 else path.stem
    summary = f"{len(groups)} ledger(s)" + (f" + {len(invoices)} invoices" if invoice_pages else "")
    return document(title, INVOICE_CSS, "\n".join(pages)), summary


# --------------------------------------------------------------------------- rendering + watching

def find_chrome() -> str | None:
    candidates: list[Path] = [Path(c) for c in MAC_BROWSERS] if IS_MAC else []
    if IS_WINDOWS:
        candidates += [Path(os.environ[var]) / rel for var, rel in WIN_BROWSERS if os.environ.get(var)]
    for c in candidates:
        if c.exists():
            return str(c)
    return next((found for name in PATH_BROWSERS if (found := shutil.which(name))), None)


def to_pdf(chrome: str, src: Path, dest: Path) -> None:
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
         f"--print-to-pdf={dest}", src.as_uri()],
        check=True, capture_output=True, timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
    )


def _log(msg: str) -> None:
    console.print(f"[dim]{datetime.now():%H:%M:%S}[/] {msg}")


def _log_to_file(path: Path) -> None:
    global console
    path.parent.mkdir(parents=True, exist_ok=True)
    console = Console(file=path.open("a", encoding="utf-8", buffering=1), width=120, no_color=True)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_input(path: Path) -> bool:
    return (path.is_file() and path.suffix.lower() in INPUT_EXTS
            and not path.name.startswith((".", "~$")) and not path.stem.lower().startswith("readme"))


def scan(folder: Path, chrome: str | None, *, settle: bool, force: bool = False) -> int:
    if not folder.is_dir():
        return 0
    out = folder / OUT_DIRNAME
    state_path = out / STATE_FILE
    try:
        state: dict[str, str] = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}

    now = time.time()
    inputs = sorted(p for p in folder.iterdir() if _is_input(p))
    if settle:  # wait out in-progress uploads / sync clients still writing
        inputs = [p for p in inputs if now - p.stat().st_mtime >= SETTLE_SECONDS]
    digests = {p.name: _digest(p) for p in inputs}
    changed = {p for p in inputs if force or state.get(p.name) != digests[p.name]}
    if not changed:
        return 0

    contracts = [read_contract(p) for p in inputs if p.suffix.lower() in CONTRACT_EXTS]
    if any(p.suffix.lower() in CONTRACT_EXTS for p in changed):
        changed |= {p for p in inputs if p.suffix.lower() == ".csv"}

    stems = [p.stem for p in inputs]
    out.mkdir(exist_ok=True)
    rendered = 0
    for p in sorted(changed):
        name = p.stem if stems.count(p.stem) == 1 else f"{p.stem}-{p.suffix[1:].lower()}"
        try:
            if p.suffix.lower() == ".csv":
                page, summary = invoices_document(p, contracts)
            else:
                page, summary = contract_document(next(c for c in contracts if c.path == p))
            html_path = out / f"{name}.html"
            html_path.write_text(page, encoding="utf-8")
            if chrome:
                to_pdf(chrome, html_path, out / f"{name}.pdf")
                _log(f"[green]✔[/] {p.relative_to(folder.parent)} → {OUT_DIRNAME}/{name}.pdf [dim]({summary})[/]")
            else:
                _log(f"[yellow]✔[/] {p.relative_to(folder.parent)} → {OUT_DIRNAME}/{name}.html "
                     f"[dim](no Chrome found: print it to PDF from a browser)[/]")
            rendered += 1
        except Exception as exc:  # one bad upload must never stop the watcher
            _log(f"[red]✘[/] {p.relative_to(folder.parent)}: {exc}")
        state[p.name] = digests[p.name]

    state_path.write_text(json.dumps(state, indent=2))
    return rendered


def watch(folders: list[Path], chrome: str | None) -> None:
    _log(f"Watching {', '.join(str(f) for f in folders)} [dim](every {POLL_SECONDS}s, Ctrl+C to stop)[/]")
    if not chrome:
        _log("[yellow]No Chrome or Edge found: HTML only. Open a file in a browser and print it to PDF.[/]")
    while True:
        for f in folders:
            try:
                scan(f, chrome, settle=True)
            except Exception as exc:
                _log(f"[red]✘[/] scanning {f}: {exc}")
        time.sleep(POLL_SECONDS)


def _watch_args(python: str, folders: list[Path], log: bool) -> list[str]:
    args = [python, str(Path(__file__).resolve()), "--watch"]
    if log:
        args += ["--log", str(AGENT_LOG)]
    for f in folders:
        args += ["--dir", str(f.resolve())]
    return args


def _install_mac(folders: list[Path]) -> None:
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with AGENT_PLIST.open("wb") as fh:
        plistlib.dump({
            "Label": AGENT_LABEL,
            "ProgramArguments": _watch_args(sys.executable, folders, log=False),
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(AGENT_LOG),
            "StandardErrorPath": str(AGENT_LOG),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        }, fh)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(AGENT_PLIST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_PLIST)], check=True)


def _windows_launcher(folders: list[Path]) -> str:
    """Startup-folder .cmd: needs no admin rights, and pythonw keeps the watcher windowless."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else Path(sys.executable))
    args = " ".join(f'"{a}"' for a in _watch_args(exe, folders, log=True))
    return ("@echo off\r\n"
            "rem System #001 PDF watcher. Remove with: .venv\\Scripts\\python tools\\render_pdfs.py --uninstall-agent\r\n"
            f'cd /d "{ROOT}"\r\n'
            f'start "" {args}\r\n')


def _stop_windows_watcher() -> None:
    try:
        pid = int(AGENT_PID.read_text().strip())
    except (OSError, ValueError):
        return
    # Only kill the PID if it is still a Python process: Windows recycles PIDs.
    found = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True).stdout.lower()
    if "python" in found:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    AGENT_PID.unlink(missing_ok=True)


def _install_windows(folders: list[Path]) -> None:
    if not WIN_STARTUP.is_dir():
        raise SystemExit(f"Windows Startup folder not found: {WIN_STARTUP}")
    _stop_windows_watcher()
    WIN_LAUNCHER.write_text(_windows_launcher(folders), encoding="utf-8")
    subprocess.run(["cmd", "/c", str(WIN_LAUNCHER)], check=True, creationflags=subprocess.CREATE_NO_WINDOW)


def install_agent(folders: list[Path]) -> None:
    AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    if IS_MAC:
        _install_mac(folders)
    elif IS_WINDOWS:
        _install_windows(folders)
    else:
        raise SystemExit("Start-at-login is set up for macOS and Windows only. Run --watch instead.")
    _log(f"[green]✔[/] Background watcher installed and running. Log: {AGENT_LOG}")


def uninstall_agent() -> None:
    if IS_MAC:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(AGENT_PLIST)], capture_output=True)
        AGENT_PLIST.unlink(missing_ok=True)
    elif IS_WINDOWS:
        _stop_windows_watcher()
        WIN_LAUNCHER.unlink(missing_ok=True)
    _log("[green]✔[/] Background watcher stopped and removed.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Render invoice CSVs and contracts to PDF.")
    ap.add_argument("--dir", action="append", type=Path, help="folder to process (repeatable)")
    ap.add_argument("--watch", action="store_true", help="keep running and render new or changed files")
    ap.add_argument("--force", action="store_true", help="re-render everything, even unchanged files")
    ap.add_argument("--install-agent", action="store_true", help="run the watcher automatically at login")
    ap.add_argument("--uninstall-agent", action="store_true", help="stop and remove the login watcher")
    ap.add_argument("--log", type=Path, help="write output to this file instead of the terminal")
    args = ap.parse_args()
    if args.log:
        _log_to_file(args.log)
    folders = [d.resolve() for d in args.dir] if args.dir else list(DEFAULT_DIRS)
    for f in folders:
        f.mkdir(parents=True, exist_ok=True)

    if args.uninstall_agent:
        uninstall_agent()
        return 0
    if args.install_agent:
        install_agent(folders)
        return 0

    chrome = find_chrome()
    if args.watch:
        if args.log:
            AGENT_PID.parent.mkdir(parents=True, exist_ok=True)
            AGENT_PID.write_text(str(os.getpid()))
        try:
            watch(folders, chrome)
        except KeyboardInterrupt:
            _log("Stopped.")
        return 0

    total = sum(scan(f, chrome, settle=False, force=args.force) for f in folders)
    _log(f"{total} file(s) rendered." if total else "Nothing new to render.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
