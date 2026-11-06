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
* Use upserts based on `invoice_number` to prevent duplicate rows during testing. Pass Decimal values as strings to preserve `NUMERIC(10, 2)` accuracy.

## 4. Coding Style
* Write clean, typed Python (Pydantic v2).
* Keep terminal outputs highly visual and clinical using the `rich` library.
* No generic comments. Comment only on complex business logic (e.g., date-matching logic for escalators).