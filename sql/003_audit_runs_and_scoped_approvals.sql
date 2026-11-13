-- System #001: audit runs, client-scoped findings, and approvals tied to one run.
-- Run once in the Supabase SQL editor after 001 and 002. Safe to run twice.
--
-- What changes, and why:
--   * A finding is identified by (client_id, contract_id, finding_key), not invoice_number,
--     so two clients with an invoice "1001" can never overwrite each other.
--   * Every run of the reconciler is an audit_run. The approval card carries the run id,
--     and an approval can only touch findings in that run.
--   * Approved and denied findings are immutable: a rerun never resets them.
--   * Every status change is written to finding_events (append-only).
--   * Only people in authorized_approvers can approve or deny.
--
-- Existing rows are kept. They get client_id from client_name, contract_id 'legacy',
-- and finding_key 'legacy:<invoice_number>'. They are not part of any run, so the new
-- approval flow cannot touch them; deny or leave them as you see fit.

begin;

create table if not exists public.audit_runs (
  id               uuid primary key default gen_random_uuid(),
  client_id        text        not null,
  contract_id      text        not null,
  status           text        not null default 'RECORDING'
                   check (status in ('RECORDING', 'AWAITING_DECISION', 'NO_APPROVABLE', 'APPROVED', 'DENIED', 'SUPERSEDED')),
  approvable_total numeric(12, 2),
  approvable_count integer,
  review_count     integer,
  created_at       timestamptz not null default now(),
  opened_at        timestamptz,
  decided_at       timestamptz,
  decided_by       text
);

create table if not exists public.authorized_approvers (
  telegram_user_id bigint primary key,
  display_name     text        not null,
  active           boolean     not null default true,
  added_at         timestamptz not null default now()
);

alter table public.detected_leaks
  add column if not exists client_id          text,
  add column if not exists contract_id        text,
  add column if not exists finding_key        text,
  add column if not exists audit_run_id       uuid references public.audit_runs (id),
  add column if not exists billing_period     date,
  add column if not exists variance_class     text,
  add column if not exists source_invoice_ids text,
  add column if not exists review_reasons     text,
  add column if not exists updated_at         timestamptz not null default now(),
  add column if not exists decided_at         timestamptz,
  add column if not exists decided_by         text;

update public.detected_leaks set
  client_id          = coalesce(client_id, trim(both '-' from lower(regexp_replace(client_name, '[^a-zA-Z0-9]+', '-', 'g')))),
  contract_id        = coalesce(contract_id, 'legacy'),
  finding_key        = coalesce(finding_key, 'legacy:' || invoice_number),
  variance_class     = coalesce(variance_class, 'UNDERBILLED'),
  source_invoice_ids = coalesce(source_invoice_ids, invoice_number)
where client_id is null or contract_id is null or finding_key is null or variance_class is null;

alter table public.detected_leaks
  alter column client_id      set not null,
  alter column contract_id    set not null,
  alter column finding_key    set not null,
  alter column variance_class set not null;

-- invoice_number stays as a readable column; it is no longer the identity. Drop any
-- uniqueness on invoice_number alone, whatever it was named when the table was made.
do $$
declare c record;
begin
  for c in
    select con.conname
      from pg_constraint con
     where con.conrelid = 'public.detected_leaks'::regclass and con.contype = 'u'
       and con.conkey = array[(select attnum from pg_attribute
                                where attrelid = 'public.detected_leaks'::regclass and attname = 'invoice_number')]
  loop
    execute format('alter table public.detected_leaks drop constraint %I', c.conname);
  end loop;
  for c in
    select i.indexrelid::regclass::text as idx
      from pg_index i
     where i.indrelid = 'public.detected_leaks'::regclass and i.indisunique and not i.indisprimary
       and i.indkey::text = (select attnum::text from pg_attribute
                              where attrelid = 'public.detected_leaks'::regclass and attname = 'invoice_number')
  loop
    execute format('drop index %s', c.idx);
  end loop;
end $$;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'detected_leaks_finding_unique') then
    alter table public.detected_leaks
      add constraint detected_leaks_finding_unique unique (client_id, contract_id, finding_key);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'detected_leaks_status_check') then
    alter table public.detected_leaks
      add constraint detected_leaks_status_check
      check (status in ('PENDING_APPROVAL', 'REVIEW_REQUIRED', 'APPROVED', 'DENIED', 'SUPERSEDED'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'detected_leaks_approvable_is_underbilling') then
    alter table public.detected_leaks
      add constraint detected_leaks_approvable_is_underbilling
      check (status not in ('PENDING_APPROVAL', 'APPROVED') or (variance_class = 'UNDERBILLED' and calculated_arrears > 0))
      not valid;  -- enforced for every new or changed row; legacy rows are not re-checked
  end if;
end $$;

create index if not exists detected_leaks_run_status on public.detected_leaks (audit_run_id, status);

-- finding_id matches detected_leaks.id, whether that table was created with a bigint or a uuid id.
do $$
declare v_type text;
begin
  select format_type(atttypid, atttypmod) into v_type
    from pg_attribute where attrelid = 'public.detected_leaks'::regclass and attname = 'id';
  execute format($f$
    create table if not exists public.finding_events (
      id           bigint generated always as identity primary key,
      finding_id   %s          not null references public.detected_leaks (id),
      audit_run_id uuid        references public.audit_runs (id),
      from_status  text,
      to_status    text        not null,
      amount       numeric(12, 2) not null,
      actor        text        not null,
      at           timestamptz not null default now()
    )$f$, v_type);
end $$;

-- finding_events is the decision history: nobody edits or deletes it.
create or replace function public.forbid_change() returns trigger language plpgsql as $$
begin
  raise exception '% is append-only', tg_table_name;
end $$;

drop trigger if exists finding_events_append_only on public.finding_events;
create trigger finding_events_append_only before update or delete on public.finding_events
  for each row execute function public.forbid_change();

-- Status rules for detected_leaks. Decisions are final.
create or replace function public.guard_finding() returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    if old.status in ('APPROVED', 'DENIED') then
      raise exception 'finding % was %; decided findings cannot be deleted', old.id, old.status;
    end if;
    return old;
  end if;

  if old.status in ('APPROVED', 'DENIED') then
    if new.status is distinct from old.status
       or new.calculated_arrears is distinct from old.calculated_arrears
       or new.audit_run_id is distinct from old.audit_run_id
       or new.finding_key is distinct from old.finding_key then
      raise exception 'finding % was %; decided findings are immutable', old.id, old.status;
    end if;
  elsif new.status = 'APPROVED' and old.status <> 'PENDING_APPROVAL' then
    raise exception 'finding % is %; only PENDING_APPROVAL can be approved', old.id, old.status;
  end if;

  new.updated_at := now();
  return new;
end $$;

drop trigger if exists detected_leaks_guard on public.detected_leaks;
create trigger detected_leaks_guard before update or delete on public.detected_leaks
  for each row execute function public.guard_finding();

create or replace function public.log_finding_event() returns trigger language plpgsql as $$
begin
  if tg_op = 'INSERT' or new.status is distinct from old.status then
    insert into public.finding_events (finding_id, audit_run_id, from_status, to_status, amount, actor)
    values (new.id, new.audit_run_id, case when tg_op = 'UPDATE' then old.status end, new.status,
            new.calculated_arrears, coalesce(nullif(current_setting('physis.actor', true), ''), 'reconciler'));
  end if;
  return null;
end $$;

drop trigger if exists detected_leaks_history on public.detected_leaks;
create trigger detected_leaks_history after insert or update on public.detected_leaks
  for each row execute function public.log_finding_event();

-- 1. The reconciler opens a run.
create or replace function public.start_audit_run(p_client_id text, p_contract_id text)
returns uuid language plpgsql security definer set search_path = public as $$
declare v_id uuid;
begin
  if coalesce(p_client_id, '') = '' or coalesce(p_contract_id, '') = '' then
    raise exception 'client_id and contract_id are required';
  end if;
  insert into audit_runs (client_id, contract_id) values (p_client_id, p_contract_id) returning id into v_id;
  return v_id;
end $$;

-- 2. It records each finding. Decided findings are left exactly as they are.
create or replace function public.record_finding(
  p_run_id uuid, p_finding_key text, p_client_name text, p_invoice_number text,
  p_source_invoice_ids text, p_billing_period date, p_variance_class text, p_status text,
  p_amount numeric, p_citation text, p_description text, p_review_reasons text)
returns text language plpgsql security definer set search_path = public as $$
declare r audit_runs; v_existing text;
begin
  select * into r from audit_runs where id = p_run_id for update;
  if not found or r.status <> 'RECORDING' then
    raise exception 'audit run % is not open for recording', p_run_id;
  end if;
  if p_status not in ('PENDING_APPROVAL', 'REVIEW_REQUIRED') then
    raise exception 'a new finding is PENDING_APPROVAL or REVIEW_REQUIRED, not %', p_status;
  end if;

  select status into v_existing from detected_leaks
   where client_id = r.client_id and contract_id = r.contract_id and finding_key = p_finding_key
   for update;
  if v_existing in ('APPROVED', 'DENIED') then
    return 'KEPT_' || v_existing;
  end if;

  insert into detected_leaks (client_id, contract_id, finding_key, audit_run_id, client_name, invoice_number,
                              source_invoice_ids, billing_period, variance_class, status, calculated_arrears,
                              contract_citation, leak_description, review_reasons)
  values (r.client_id, r.contract_id, p_finding_key, p_run_id, p_client_name, p_invoice_number,
          p_source_invoice_ids, p_billing_period, p_variance_class, p_status, p_amount,
          p_citation, p_description, p_review_reasons)
  on conflict (client_id, contract_id, finding_key) do update set
    audit_run_id = excluded.audit_run_id, client_name = excluded.client_name,
    invoice_number = excluded.invoice_number, source_invoice_ids = excluded.source_invoice_ids,
    billing_period = excluded.billing_period, variance_class = excluded.variance_class,
    status = excluded.status, calculated_arrears = excluded.calculated_arrears,
    contract_citation = excluded.contract_citation, leak_description = excluded.leak_description,
    review_reasons = excluded.review_reasons;
  return p_status;
end $$;

-- 3. It closes recording. Older open runs for the same contract stop being approvable,
--    and open findings this run did not see again are marked SUPERSEDED.
create or replace function public.open_audit_run(p_run_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare r audit_runs; v_total numeric(12, 2); v_count integer; v_review integer;
begin
  select * into r from audit_runs where id = p_run_id for update;
  if not found or r.status <> 'RECORDING' then
    raise exception 'audit run % is not open for recording', p_run_id;
  end if;

  update audit_runs set status = 'SUPERSEDED'
   where client_id = r.client_id and contract_id = r.contract_id and id <> p_run_id
     and status in ('RECORDING', 'AWAITING_DECISION');

  update detected_leaks set status = 'SUPERSEDED'
   where client_id = r.client_id and contract_id = r.contract_id
     and status in ('PENDING_APPROVAL', 'REVIEW_REQUIRED')
     and audit_run_id is distinct from p_run_id
     and finding_key not like 'legacy:%';

  select coalesce(sum(calculated_arrears), 0), count(*) into v_total, v_count
    from detected_leaks where audit_run_id = p_run_id and status = 'PENDING_APPROVAL';
  select count(*) into v_review
    from detected_leaks where audit_run_id = p_run_id and status = 'REVIEW_REQUIRED';

  update audit_runs set
    status = case when v_count > 0 then 'AWAITING_DECISION' else 'NO_APPROVABLE' end,
    approvable_total = v_total, approvable_count = v_count, review_count = v_review, opened_at = now()
   where id = p_run_id;

  return jsonb_build_object('run_id', p_run_id, 'approvable_total', v_total::text,
                            'approvable_count', v_count, 'review_count', v_review);
end $$;

-- 4. A person decides. One run, one decision, by an authorized approver, on the exact
--    amount the card showed. Everything happens in one transaction.
create or replace function public.decide_audit_run(p_run_id uuid, p_decision text, p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path = public as $$
declare r audit_runs; a authorized_approvers; v_total numeric(12, 2); v_count integer;
        v_status text; v_actor text; v_rows jsonb;
begin
  if p_decision not in ('APPROVE', 'DENY') then
    return jsonb_build_object('ok', false, 'reason', 'Unknown decision.');
  end if;
  select * into a from authorized_approvers where telegram_user_id = p_telegram_user_id and active;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'You are not an authorized approver.');
  end if;
  select * into r from audit_runs where id = p_run_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'Unknown audit run.');
  end if;
  if r.status <> 'AWAITING_DECISION' then
    return jsonb_build_object('ok', false, 'reason',
      'This card is no longer open (' || lower(replace(r.status, '_', ' ')) || '). Nothing changed.');
  end if;

  perform 1 from detected_leaks where audit_run_id = p_run_id and status = 'PENDING_APPROVAL' for update;
  select coalesce(sum(calculated_arrears), 0), count(*) into v_total, v_count
    from detected_leaks where audit_run_id = p_run_id and status = 'PENDING_APPROVAL';
  if v_count <> r.approvable_count or v_total <> r.approvable_total then
    return jsonb_build_object('ok', false, 'reason',
      'The findings changed after this card was sent. Nothing changed. Rerun the audit.');
  end if;

  v_status := case p_decision when 'APPROVE' then 'APPROVED' else 'DENIED' end;
  v_actor  := a.display_name || ' (telegram ' || a.telegram_user_id || ')';
  perform set_config('physis.actor', v_actor, true);

  with decided as (
    update detected_leaks set status = v_status, decided_at = now(), decided_by = v_actor
     where audit_run_id = p_run_id and status = 'PENDING_APPROVAL'
    returning billing_period, source_invoice_ids, calculated_arrears, leak_description
  )
  select coalesce(jsonb_agg(jsonb_build_object(
           'period', to_char(billing_period, 'Mon YYYY'), 'invoices', source_invoice_ids,
           'amount', calculated_arrears::text, 'why', leak_description) order by billing_period), '[]'::jsonb)
    into v_rows from decided;

  update audit_runs set status = v_status, decided_at = now(), decided_by = v_actor where id = p_run_id;

  return jsonb_build_object('ok', true, 'decision', v_status, 'run_id', p_run_id,
                            'client_id', r.client_id, 'contract_id', r.contract_id,
                            'count', v_count, 'total', v_total::text, 'approver', a.display_name,
                            'rows', v_rows);
end $$;

alter table public.audit_runs           enable row level security;
alter table public.authorized_approvers enable row level security;
alter table public.finding_events       enable row level security;

-- Only the service role (Python, n8n) may call these. On Supabase the roles exist;
-- on a plain Postgres test database they may not, so check first.
do $$
declare f text;
begin
  foreach f in array array['start_audit_run(text, text)',
    'record_finding(uuid, text, text, text, text, date, text, text, numeric, text, text, text)',
    'open_audit_run(uuid)', 'decide_audit_run(uuid, text, bigint)']
  loop
    execute format('revoke all on function public.%s from public', f);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on function public.%s from anon', f);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on function public.%s from authenticated', f);
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant execute on function public.%s to service_role', f);
    end if;
  end loop;
end $$;

commit;
