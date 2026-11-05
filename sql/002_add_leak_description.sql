-- System #001: plain-English explanation for every leaking invoice.
-- Run once in the Supabase SQL editor before the next run_demo.py.
-- Nullable and additive: existing rows and the n8n approval workflow are unaffected.
alter table public.detected_leaks
  add column if not exists leak_description text;

comment on column public.detected_leaks.leak_description is
  'What went wrong on this invoice, generated in Python from the Decimal audit (no LLM).';
