# Pre-client hardening (branch `fix/pre-client-hardening`, 2026-11-12)

The five fixes needed before System #001 touches a real client's contract. Scope was kept
to these on purpose. Nothing here is deployed: the migration, the n8n workflow and the
approver list are applied by hand (see "Deploy" below).

## What was wrong

| # | Problem | Where | Effect |
|:-:|:--|:--|:--|
| 1 | Any nonzero variance counted as a leak, and the total summed every row | `agents/analyst.py` | Over-billing was netted against under-billing, so the claim was understated, and over-billing was never shown on its own |
| 2 | The monthly minimum was applied to each invoice | `agents/analyst.py` | A month split into two invoices was charged the minimum twice: a false finding |
| 3 | Citation failures only produced a warning | `run_demo.py`, `tools/dossier.py` | A finding built on a quote not in the contract could still be approved |
| 4 | `invoice_number` was the only unique key | `sql/001`, `tools/db_client.py` | Two clients with invoice "1001" would overwrite each other |
| 5 | Approval matched every pending row by the client name parsed from the card text | `n8n/telegram_approval_webhook.json` | One tap could approve rows from older runs; anyone in the chat could tap |
| 5b | The finance email listed every APPROVED row for that client, ever | n8n "Get Approved Leaks" | Finance could be asked to invoice months that were already invoiced |

The published demo numbers do not change: one invoice per month, no over-billing, all six
citations verified. $37,250 on 11 of 12 invoices is pinned by `tests/test_reconciliation.py`.

## What changed

- **`agents/analyst.py`**: line-level rate variance plus a `PeriodAudit` per customer per
  calendar month. The minimum is applied once to the month's total usage. Each month is
  `UNDERBILLED`, `OVERBILLED`, `ZERO` or `REVIEW` (credit lines, a row whose quantity x price
  does not equal its amount, or under- and over-billing in the same month). `total_arrears`
  is under-billing only; `total_overbilled` is separate. Months missing inside the range
  are listed. Single-invoice months keep the minimum on the line so every display is unchanged.
- **`agents/findings.py`** (new): the evidence gate. A month is `PENDING_APPROVAL` only if it
  is `UNDERBILLED` and every term behind its amount has a citation that was found in the
  contract with the value inside it. Also blocks: a promo rate without an end date (or the
  reverse), an escalator without a start date (or the reverse), an escalator over 25%, and a
  field cited with two different quotes. Everything else is `REVIEW_REQUIRED` with reasons.
- **`sql/003_audit_runs_and_scoped_approvals.sql`** (new): `audit_runs`, `authorized_approvers`,
  `finding_events` (append-only); findings keyed by `(client_id, contract_id, finding_key)`;
  approved and denied rows cannot be changed or deleted; a database check that only positive
  under-billing can be approvable; and four functions (`start_audit_run`, `record_finding`,
  `open_audit_run`, `decide_audit_run`). Existing rows are kept and backfilled.
- **`tools/db_client.py`**: calls those functions only. If any write fails, the run is never
  opened, so no card can be sent for it.
- **`tools/notifier.py`, `tools/dossier.py`**: buttons carry `approve:<run id>`; the card shows
  only the approvable amount and how many items are held for review. The full report (it
  quotes the contract) is attached only for the synthetic demo; client reports stay in `reports/`.
- **`run_demo.py`**: findings, a REVIEW REQUIRED panel, the run-based ledger, and a check that
  the ledger total equals the audit total before any card is sent.
- **`n8n/telegram_approval_webhook.json`**: parse the button, call `decide_audit_run` over the
  Supabase REST API, answer the tap with the result, and email finance only the rows that call
  returned. The card text is no longer read at all.

## How a decision is protected

`decide_audit_run(run_id, decision, telegram_user_id)` runs in one transaction and refuses when:
the approver is not in `authorized_approvers`; the run is not `AWAITING_DECISION` (already
decided, superseded by a newer run, or never opened); or the count or total of its pending
findings differs from what was on the card. A second tap on the same card is refused. A new run
for the same contract supersedes older open cards.

## Assumptions to confirm per client

- Billing month = the calendar month of the invoice date. Fine for "invoiced monthly in advance";
  wrong for mid-month billing cycles.
- The minimum applies per customer across all products on the contract.
- Rounding is `ROUND_HALF_UP` to the cent at each escalation step.
- A missing month is never claimed. It is a review item.

## Tests

`python -m unittest discover tests`: 62 tests, no Ollama, Supabase or Telegram needed.
`tests/test_sql_ledger.py` starts a throwaway local Postgres and runs the real migrations; it
skips itself where Postgres is not installed (for example on Windows without it).

## Deploy (by hand, in this order)

1. Supabase SQL editor: run `sql/003_audit_runs_and_scoped_approvals.sql`.
2. Add yourself: `insert into authorized_approvers (telegram_user_id, display_name) values (<your id>, 'Ron');`
3. n8n: deactivate the old approval workflow, import the new JSON, set the credentials, the
   Supabase URL in "Decide in Supabase", and the finance email. Activate.
4. Run `python run_demo.py` and approve once to confirm the round trip.

## Deliberately not done yet

A full multi-stage audit lifecycle, CI, linting and type checks, replay nonces beyond the
single-use run, proration, CPI-linked escalators, tiered or usage pricing, per-product minimums,
contract start and end dates, and a client-facing interface. None of these block a Leak Check
run by Ron on one contract. Revisit after the first paid diagnostic.

## Still open before a paid engagement

- The service role key lives in `.env` and in n8n. Anyone with either can write the ledger.
- Telegram chats are not end-to-end encrypted. The card carries amounts and section numbers.
- Supabase backups and a retention rule for client findings (the 30-day client-data rule) are
  not set up.
