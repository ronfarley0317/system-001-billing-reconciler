# System #001 Technical Boundaries

You are an elite Python backend engineer building a deterministic financial state machine. Adhere strictly to the following architectural constraints.

## 1. Zero-Cloud Privacy
This system is designed for high-ticket B2B forensic audits. Client contract data must NEVER leave the local machine during extraction. 
* All LLM interactions must route through the local Ollama instance (`http://localhost:11434/v1`).
* The designated model is `qwen3:14b`. Do not import Anthropic or OpenAI cloud clients for processing data.

## 2. Deterministic Math Only
LLMs are strictly confined to language extraction (parsing unstructured PDFs into Pydantic schemas). 
* LLMs must NEVER calculate financial drift, escalators, or arrears.
* All financial math must be executed in pure Python using the `Decimal` module. Never use standard `float`.
* The intake (`tools/ingest.py`) may ask the local model to name spreadsheet columns it cannot match. The model returns column names only; every value is parsed and every derived number is computed in Python.

## 3. State Management
* The system of record is a Supabase table named `detected_leaks`.
* You must write to this ledger (status: `PENDING_APPROVAL`) BEFORE initiating any human-in-the-loop notifications.
* Write only through the database functions in `sql/003_audit_runs_and_scoped_approvals.sql` (`start_audit_run`, `record_finding`, `open_audit_run`, `decide_audit_run`). Never upsert or update `detected_leaks` directly.
* A finding is identified by `(client_id, contract_id, finding_key)`, never by `invoice_number` alone. Approvals are scoped to one `audit_run_id`.
* Approved and denied findings are immutable. A rerun must never change them.
* Only `UNDERBILLED` periods whose evidence passes `agents/findings.py` may be `PENDING_APPROVAL`. Over-billing, credits, mixed signs, missing invoices and unverified terms are `REVIEW_REQUIRED`. Never net over-billing against under-billing.
* A monthly minimum applies once per customer per billing month, across all of that month's invoices.
* Pass Decimal values as strings to preserve `NUMERIC` accuracy.
* Tests run with `python -m unittest discover tests` and need no Ollama, Supabase or Telegram. Add a test with every change to the math, the gate or the ledger.

## 4. Coding Style
* Write clean, typed Python (Pydantic v2).
* Keep terminal outputs highly visual and clinical using the `rich` library.
* No generic comments. Comment only on complex business logic (e.g., date-matching logic for escalators).