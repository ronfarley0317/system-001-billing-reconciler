-- System #001: the ledger every detected leak is written to before anyone is notified.
-- Run once in the Supabase SQL editor on a fresh project, then run 002.
-- Matches what tools/db_client.py writes and what the n8n workflow reads and updates.
create table if not exists public.detected_leaks (
  id                 bigint generated always as identity primary key,
  client_name        text           not null,
  invoice_number     text           not null unique,  -- upsert key: reruns never duplicate a row
  calculated_arrears numeric(10, 2) not null,         -- written as an exact string from Python Decimal
  contract_citation  text,
  status             text           not null default 'PENDING_APPROVAL',  -- PENDING_APPROVAL | APPROVED | DENIED
  created_at         timestamptz    not null default now()
);

-- No public policies: only the service role (Python) and your n8n credential can read or write.
alter table public.detected_leaks enable row level security;
