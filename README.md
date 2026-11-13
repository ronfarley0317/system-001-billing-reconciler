<p align="center">
  <img src="docs/assets/hero.png" alt="System #001: Deterministic Billing Reconciler. AI reads, code counts, a human approves." width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white&labelColor=0d1117" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Ollama-qwen3%3A14b-a78bfa?style=for-the-badge&logo=ollama&logoColor=white&labelColor=0d1117" alt="Ollama qwen3:14b">
  <img src="https://img.shields.io/badge/Pydantic-v2-E92063?style=for-the-badge&logo=pydantic&logoColor=white&labelColor=0d1117" alt="Pydantic v2">
  <img src="https://img.shields.io/badge/Supabase-ledger-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white&labelColor=0d1117" alt="Supabase">
  <img src="https://img.shields.io/badge/n8n-webhook-EA4B71?style=for-the-badge&logo=n8n&logoColor=white&labelColor=0d1117" alt="n8n">
  <img src="https://img.shields.io/badge/Telegram-approval-26A5E4?style=for-the-badge&logo=telegram&logoColor=white&labelColor=0d1117" alt="Telegram">
  <img src="https://img.shields.io/badge/License-MIT-94a3b8?style=for-the-badge&labelColor=0d1117" alt="MIT License">
</p>
<p align="center">
  <img src="https://img.shields.io/badge/LLM%20math-none-fbbf24?style=flat-square&labelColor=0d1117" alt="LLM math: none">
  <img src="https://img.shields.io/badge/money-Decimal%20only-fbbf24?style=flat-square&labelColor=0d1117" alt="money: Decimal only">
  <img src="https://img.shields.io/badge/contract%20to%20AI%20providers-never-34d399?style=flat-square&labelColor=0d1117" alt="contract to AI providers: never">
  <img src="https://img.shields.io/badge/billing-human%20approval%20required-fb7185?style=flat-square&labelColor=0d1117" alt="billing: human approval required">
</p>

<p align="center">
  <a href="#the-37250-problem">The problem</a> ·
  <a href="#see-it-run">See it run</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#the-guarantees">Guarantees</a> ·
  <a href="#what-the-approver-sees">Approver view</a> ·
  <a href="#client-ready-pdfs">Client PDFs</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="#repository-map">Repo map</a> ·
  <a href="#where-the-data-goes">Data handling</a>
</p>

---

**System #001 finds revenue that signed contracts promised and billing never collected.** A local language model reads the contract and transcribes its pricing terms, with a verbatim quote for every term. Pure Python recomputes every invoice to the cent. Every finding is written to a ledger, explained in plain English, and held there until a person taps **Approve** or **Deny**. Nothing is billed without that tap.

> [!IMPORTANT]
> **Two rules this system is built on.** The language model never does math: it reads, and code counts. The contract is never sent to an AI provider: extraction runs on your own machine through Ollama.

<br>

## The $37,250 problem

The demo account is a 36-month seat subscription. The order form has four pricing clauses. Billing honoured one of them.

| Clause | What the contract says | What billing did | Leaked |
|:--|:--|:--|--:|
| **§3.2 Promo expiry** | $200/seat through June 30, 2025, then $250 base rate | Kept billing $200/seat for 11 more months | **$31,000.00** |
| **§3.3 Escalator** | +5.0% on every per-seat fee from January 1, 2026 | Never applied | **$3,250.00** |
| **§3.4 Monthly minimum** | $12,000.00 floor, regardless of seat count | Billed $8,000.00 when seats dropped to 40 | **$3,000.00** |
| | | **11 of 12 invoices under-billed** | **$37,250.00** |

Billed: **$136,000.00**. Contract says: **$173,250.00**. Nobody noticed, because the person who signs the contract is not the person who bills it, and six months later nobody's job is to reopen it.

<sub>Synthetic benchmark dataset. Meridian Fleet Systems and Apex Logistics Group are fictional. The leaks are planted by <code>generate_demo_data.py</code> so the result is reproducible.</sub>

<br>

## See it run

One command. A real run on the demo data: Qwen3 14B reads the contract locally in about 40 seconds, all six contract quotes verify against the source, and the Analyst reconciles all twelve invoices.

```bash
python run_demo.py
```

<p align="center">
  <img src="docs/assets/terminal.png" alt="Terminal output: Scout extraction with six verified citations, then the Analyst's line-by-line reconciliation totalling $37,250 in arrears" width="100%">
</p>

<br>

## Architecture

Six stages, each owned by one file. Everything inside the dashed box runs on the local machine.

<p align="center">
  <img src="docs/assets/architecture.png" alt="Architecture: contract and ERP CSV, local Qwen extraction, Python Decimal reconciliation, Supabase ledger, Telegram approval card, n8n webhook" width="100%">
</p>

| # | Stage | What happens | Owned by |
|:-:|:--|:--|:--|
| 01 | **Input** | The signed order form and a flat ERP export. What was promised versus what was billed. | `demo_data/` · `generate_demo_data.py` |
| 02 | **Language** | Qwen3 14B, local through Ollama, fills a strict Pydantic schema: six pricing terms, each with a verbatim quote. Temperature 0. No arithmetic. | `agents/scout.py` · `schemas/payloads.py` |
| 03 | **Math** | Every invoice recomputed in `Decimal`, `ROUND_HALF_UP` to the cent. The monthly minimum is applied once per billing month, not per invoice. Every month is classified: under-billed, over-billed, zero, or review. | `agents/analyst.py` |
| 03b | **Gate** | A month becomes approvable only if it is under-billed and every term behind its amount has a quote found in the contract with its value inside that quote. Everything else is `REVIEW_REQUIRED`. | `agents/findings.py` |
| 04 | **State** | One audit run per execution. One row per finding in `detected_leaks`, keyed by client + contract + month, with the clause and a plain-English description. | `tools/db_client.py` · `sql/003_...` |
| 05 | **Human** | An approval card that explains itself, with the full line-by-line audit attached. | `tools/dossier.py` · `tools/notifier.py` |
| 06 | **Wiring** | n8n catches the button tap and calls one database function that approves or denies that run only, for an authorized approver. On approval it emails finance the rows that function returned. It routes. It never computes. | `n8n/telegram_approval_webhook.json` |

### The approval loop

```mermaid
sequenceDiagram
    autonumber
    participant P as run_demo.py
    participant DB as Supabase (detected_leaks)
    participant T as Telegram
    actor A as Approver
    participant N as n8n
    participant F as Finance (Gmail)
    P->>DB: Start an audit run, record 11 findings, open the run
    Note over P,DB: If any write fails, the run is never opened and no one is notified
    P->>T: Approval card for that run (the button carries the run id)
    A->>T: Taps Approve Claim (or Deny)
    T->>N: Webhook with the button and the approver
    N->>DB: decide_audit_run(run id, decision, approver)
    Note over N,DB: Checks the approver, that the run is still open, and that the total is unchanged
    N->>F: The rows that call approved, total, reasons, approver, timestamp
    N->>T: Confirmation in the chat
```

### The math, in full

```text
For an invoice dated D with quantity Q:

  1. rate      = promo_rate if D <= promo_expiration_date else base_rate
  2. rate      = rate x (1 + escalator%) for each escalation date <= D
                 (compounded, rounded to the cent at each step)
  3. usage     = rate x Q
  4. expected  = max(usage, monthly_minimum_floor)
     drift     = expected - billed            (positive = under-billed)
```

Drift is then split into reasons in clause order: promo, escalator, floor. If the reasons do not add up to the total drift **to the cent**, the Analyst raises `ArithmeticError` and the run stops. It refuses to report a number it cannot explain.

<br>

## The guarantees

These are enforced in code, not left to convention. They are also written into [`CLAUDE.md`](CLAUDE.md), so a coding agent working in this repo cannot drift from them.

<table>
<tr>
<td width="50%" valign="top">

**The model only reads**<br>
<sub>The Scout's system prompt forbids calculating, inferring, rounding or combining numbers. Money and percentages cross the model boundary as strings and are parsed into <code>Decimal</code> on the Python side. A float never touches a dollar.</sub>

</td>
<td width="50%" valign="top">

**Every term must be quoted**<br>
<sub>Each extracted term comes with a quote from the contract. Two checks run in plain Python. The quote must exist in the source text (ignoring only case, whitespace and markdown emphasis), so a made-up clause shows up as <code>NOT FOUND</code>. And the extracted value must appear inside its own quote, so a real clause cited with a misread number (5.0% read as 50.0) shows up as <code>VALUE ≠ QUOTE</code>. Either failure puts a review warning on the approval card.</sub>

</td>
</tr>
<tr>
<td valign="top">

**The math must reconcile**<br>
<sub>Per-reason amounts must sum exactly to each invoice's drift. If they do not, the run stops with <code>ArithmeticError</code>.</sub>

</td>
<td valign="top">

**Record before you ask**<br>
<sub>Leaks are written to <code>detected_leaks</code> before any notification is sent. If the ledger write fails, the approval card is never sent. No one is asked to approve money that is not on the record.</sub>

</td>
</tr>
<tr>
<td valign="top">

**Reruns are safe**<br>
<sub>Findings are keyed by client, contract and month, so a rerun updates the same row and two clients can never collide. Approved and denied findings are locked in the database: a rerun leaves them exactly as decided, and every status change is kept in <code>finding_events</code>.</sub>

</td>
<td valign="top">

**A person makes the call**<br>
<sub>Nothing is billed until an authorized approver taps Approve. A tap only affects the audit run on that card, works once, and is refused if the card is stale or the amounts changed. Over-billing and anything uncertain go to review, never into a claim.</sub>

</td>
</tr>
</table>

<br>

## What the approver sees

The approval card leads with the total and breaks it down by reason. The full audit report is attached to the same chat, so the approver can check every line on their phone before deciding.

<table>
<tr>
<td width="50%" valign="top"><img src="docs/assets/approval_card.png" alt="Approval card: $37,250 total arrears broken into promo, escalator and floor, with Approve Claim and Deny buttons"></td>
<td width="50%" valign="top"><img src="docs/assets/audit_report.png" alt="Audit report: billed vs contract tiles, why cards with contract quotes, and a line-by-line invoice table"></td>
</tr>
<tr>
<td align="center"><sub>The card (terminal preview of the Telegram message)</sub></td>
<td align="center"><sub>The attached line-by-line audit report</sub></td>
</tr>
</table>

Every row in the ledger also carries a `leak_description`, generated in Python from the audit results, so anyone opening the table knows what happened to that invoice without opening the report:

> **Apr 2026:** billed $8,000.00 for 40 seats at $200.00/seat, but the contract requires $12,000.00 (the monthly minimum; 40 seats at $262.50 only come to $10,500.00). Under-billed by $4,000.00. Expired promo rate still billed: $2,000.00 (promo ended Jun 30, 2025; base rate is $250.00/seat). Annual escalator not applied: $500.00 (+5.0% from Jan 1, 2026 raises the rate to $262.50/seat). Monthly minimum not enforced: $1,500.00 ($12,000.00 monthly minimum).

<br>

## Client-ready PDFs

Drop a client's invoice CSV or contract into `client_inbox/` and a formatted PDF appears in `client_inbox/rendered/` a few seconds later. Rendering is local, through headless Chrome or Microsoft Edge.

<table>
<tr>
<td width="33%"><img src="docs/assets/pdf_ledger.png" alt="Invoice ledger PDF page with totals and a monthly bar chart"></td>
<td width="33%"><img src="docs/assets/pdf_invoice.png" alt="Single invoice PDF page"></td>
<td width="33%"><img src="docs/assets/pdf_contract.png" alt="Formatted contract PDF page"></td>
</tr>
<tr>
<td align="center"><sub>Ledger with a monthly chart</sub></td>
<td align="center"><sub>One page per invoice</sub></td>
<td align="center"><sub>The contract, typeset</sub></td>
</tr>
</table>

```bash
python tools/render_pdfs.py                  # render anything new or changed, then exit
python tools/render_pdfs.py --watch          # keep running, render files as they land
python tools/render_pdfs.py --install-agent  # start the watcher at login (macOS and Windows)
```

- **Invoice CSVs** become a ledger page plus one page per invoice. Column names are matched loosely, and US dates, `$1,234.00` amounts and `(500.00)` negatives are understood.
- **Contracts** (`.md`, `.txt`) become a typeset PDF. When a contract sits next to a CSV for the same customer, the invoices pick up the provider name, order number and payment terms from it.
- **Anything else** still renders, as a clean table with a note explaining why. One bad file never stops the watcher.
- Only changed files are re-rendered. Values are shown exactly as uploaded, never corrected.

<br>

## Quickstart

### 1. Prerequisites

| Need | Why |
|:--|:--|
| Python 3.12 | Runtime |
| [Ollama](https://ollama.com) with `qwen3:14b` | Local contract extraction (about 9 GB) |
| A Supabase project | The `detected_leaks` ledger |
| A Telegram bot | The approval card |
| n8n (Cloud or self-hosted with a public URL) | Receives the button tap |
| Chrome or Edge | Optional, for PDF rendering |

### 2. Install

```bash
git clone https://github.com/ronfarley0317/system-001-billing-reconciler.git
cd system-001-billing-reconciler
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
ollama pull qwen3:14b
cp .env.example .env               # Windows: copy .env.example .env
```

### 3. Configure `.env`

| Variable | Purpose |
|:--|:--|
| `OLLAMA_BASE_URL` | Local model endpoint. Default `http://localhost:11434/v1` |
| `OLLAMA_MODEL` | Default `qwen3:14b` |
| `TELEGRAM_BOT_TOKEN` | From @BotFather. Message your bot once before the first run |
| `TELEGRAM_CHAT_ID` | The chat that receives the approval card |
| `SUPABASE_URL` | Your project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side key. Never commit it; `.env` is git-ignored |

### 4. Create the ledger

In the Supabase SQL editor, run [`sql/001_create_detected_leaks.sql`](sql/001_create_detected_leaks.sql), [`sql/002_add_leak_description.sql`](sql/002_add_leak_description.sql), [`sql/003_audit_runs_and_scoped_approvals.sql`](sql/003_audit_runs_and_scoped_approvals.sql), then [`sql/004_finding_breakdown_for_finance_email.sql`](sql/004_finding_breakdown_for_finance_email.sql). Each keeps existing rows.

Then add yourself as an approver (your Telegram user id, from @userinfobot):

```sql
insert into authorized_approvers (telegram_user_id, display_name) values (123456789, 'Your Name');
```

No one else can approve or deny until they are added.

### 5. Wire the approval

Import [`n8n/telegram_approval_webhook.json`](n8n/telegram_approval_webhook.json) into n8n. Replace each `REPLACE_WITH_...` credential, your Supabase project URL in the **Decide in Supabase** node, and the finance email address, then switch the workflow to **Active**. Turn off any older version of this workflow: its buttons (`approve_claim`) no longer exist.

### 6. Run

```bash
python run_demo.py                 # full run: extract, reconcile, record, send the approval card
python run_demo.py --no-telegram   # everything except the Telegram message
python generate_demo_data.py       # rebuild the demo contract and ERP export
```

### 7. Run on your own data

Make a folder for the client and drop in two files, exactly as they came out of their systems. No renaming, no reformatting.

```text
clients/acme/
├── Signed Order Form.pdf      the contract: PDF, Word (.docx), text or Markdown
└── Invoices 2025.xlsx         the invoice export: Excel or CSV, from QuickBooks, NetSuite, Xero or anything else
```

```bash
python run_demo.py --client acme
```

The first time, the intake shows how it read the export and asks you to check it:

```text
╭──────────────────────── INTAKE  Invoices 2025.xlsx ────────────────────────╮
│   We need                   Your column              Example from your file │
│   Invoice number            Num                      1001                   │
│   Invoice date              Date  (month/day/year)   6/1/2025               │
│   Quantity (seats, units)   Qty                      60                     │
│   Unit price                Sales Price              $200.00                │
│   Amount billed             Amount                   $12,000.00             │
│   Customer                  Name                     Apex Logistics Group   │
│   Product or item           Item                     Fleet Telematics Seat  │
╰─────────────────────────────────────────────────────────────────────────────╯
Does this look right? [y/n] (y):
```

Press Enter if it's right, or `n` to pick the columns yourself from a numbered list. Your answers are saved next to the export (`Invoices 2025.mapping.json`) and reused on every later run while the file layout stays the same.

**What the intake sorts out for you**

- **Any column names.** `Num`, `Invoice #`, `Qty`, `Seats`, `Rate`, `Line Total` and dozens more are recognised. Anything it doesn't recognise is named by the local model, and you confirm it. The model only names columns; it never reads or produces a number.
- **Report clutter.** Titles above the headings, blank lines, subtotal and total rows are skipped.
- **Any common date format,** including Excel dates and day-first dates. If it can't tell 03/04 apart, it asks once.
- **Any common amount format:** `$12,000.00`, `12.000,00`, `(500.00)`.
- **Several customers or products in one export.** It asks which ones this contract covers, and pre-selects the answer it finds in the contract. Tax lines are left out by default.
- **One invoice split over several lines** is combined into one. **Credit notes** are set aside and listed for you to review.
- **No unit price, or no quantity?** It is worked out from the amount, in Python `Decimal`.

The export needs four things: an invoice number, an invoice date, the amount billed, and either the quantity or the unit price. If the folder holds more than one possible contract or export, put `contract` or `invoice` in the name of the right one. You can also point at files anywhere: `python run_demo.py --contract path/to/contract.pdf --invoices path/to/export.xlsx`. The `clients/` folder is git-ignored, so client data is never committed.

> [!TIP]
> Every demo run gets a fresh client id (`demo-<timestamp>`), so there is nothing to reset between recording takes.

<br>

## Repository map

```text
system-001-billing-reconciler/
├── run_demo.py                     Conductor: read, reconcile, record, then ask a human
├── CLAUDE.md                       The rulebook: local AI only, Decimal only, ledger first
├── agents/
│   ├── scout.py                    Local Qwen extraction, quote check, and value-in-quote check
│   ├── analyst.py                  Decimal reconciliation, monthly minimum per billing month, variance classes
│   └── findings.py                 The evidence gate: what is approvable and what needs a person
├── schemas/
│   └── payloads.py                 The form the model must fill: six terms, each quoted
├── tools/
│   ├── db_client.py                Audit runs and findings, through the database functions in sql/003
│   ├── dossier.py                  Approval card, audit report and leak descriptions
│   ├── notifier.py                 Telegram delivery: report first, then the card
│   ├── ingest.py                   Reads any invoice export (CSV or Excel) into the Analyst's layout
│   ├── documents.py                Reads contracts from PDF, Word, text or Markdown
│   └── render_pdfs.py              Client uploads to PDF, with a folder watcher
├── n8n/
│   └── telegram_approval_webhook.json   Run-scoped approve / deny and the finance email
├── tests/                          python -m unittest discover tests (no Ollama, Supabase or Telegram needed)
├── sql/                            Ledger schema for a fresh Supabase project
├── demo_data/                      Synthetic contract, ERP export, rendered PDFs
├── generate_demo_data.py           Builds the demo with three planted leaks
└── docs/assets/                    Images used in this README
```

Each file has exactly one job. When something breaks, you know where to look. The approval channel lives in one file: swap `tools/notifier.py` for Slack, Teams, WhatsApp Business or email and nothing upstream changes.

<br>

## Where the data goes

| Data | Where it goes | Why |
|:--|:--|:--|
| Full contract text | **Stays on this machine.** Read by Ollama on `localhost` | Extraction only |
| Invoice export, plus the column names and sample rows the intake shows the local model | **Stays on this machine** | Reading the client's layout |
| Extracted terms and all calculations | **Stays on this machine** | Python `Decimal` |
| Each finding: client, invoice, amount, clause quote, description | Your Supabase project | The system of record |
| Approval card (amounts, months, section numbers) | The Telegram chat you configure | The human decision |
| Line-by-line audit report (includes contract quotes) | **Stays on this machine** in `reports/` for client runs. Attached to Telegram only for the synthetic demo | Review |
| Approved invoice list, total, reasons, approver | Finance, by email through n8n | The handoff |
| Rendered client PDFs | **Stays on this machine** | Headless Chrome or Edge |

The contract is never sent to an AI provider. What leaves the machine is the minimum needed to act: the findings, the amounts and the supporting clauses, sent to your own database and your own approver.

<br>

## Scope

- **Six pricing terms are extracted:** base rate, promo rate, promo expiry, annual escalator, escalator start date and monthly minimum. Other clause types need a field in `schemas/payloads.py` and a rule in `agents/analyst.py`.
- **One contract and one ERP export per run.** Pass them with `--client` or `--contract`/`--invoices`; with neither, the demo data runs.
- **Contracts can be PDF, Word, text or Markdown.** A scanned PDF with no text layer needs OCR first; the tool says so when that's the case.
- **All numbers in this README come from the synthetic benchmark dataset.**

---

<p align="center">
  <b>System #001</b> · Built by Ron<br>
  <sub>Taught in public at <b>Ron Builds Systems</b>. Installed privately for business owners by <b>Physis Advisory</b>.</sub><br><br>
  <sub><i>Tools don't pay you. Systems do.</i></sub>
  <br><sub>Released under the <a href="LICENSE">MIT License</a>.</sub>
</p>
