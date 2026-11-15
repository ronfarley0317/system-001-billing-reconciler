-- System #001: before the first client. Run once after 004. Safe to run twice.
--
--   * Decided findings are frozen whole: no column of an APPROVED or DENIED row can change.
--   * Each approval card carries a SHA-256 fingerprint of exactly the findings it shows.
--     A decision is refused unless the findings still match that fingerprint.
--   * The run records when finance was emailed. If the email fails, an approver can
--     resend it from Telegram, and it can only be sent once.
--   * purge_client removes one client's findings, runs and history at the end of an
--     engagement, behind a typed confirmation, and keeps a receipt with no client data.

begin;

alter table public.audit_runs
  add column if not exists card_fingerprint    text,
  add column if not exists finance_notified_at timestamptz;

create table if not exists public.client_purges (
  id               bigint generated always as identity primary key,
  client_id        text        not null,
  purged_at        timestamptz not null default now(),
  runs_deleted     integer     not null,
  findings_deleted integer     not null,
  events_deleted   integer     not null
);
alter table public.client_purges enable row level security;

-- Decided findings: frozen whole. Deletes only inside purge_client.
create or replace function public.guard_finding() returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    if old.status in ('APPROVED', 'DENIED')
       and current_setting('physis.purge', true) is distinct from old.client_id then
      raise exception 'finding % was %; decided findings cannot be deleted', old.id, old.status;
    end if;
    return old;
  end if;

  if old.status in ('APPROVED', 'DENIED') then
    if (to_jsonb(new) - 'updated_at') is distinct from (to_jsonb(old) - 'updated_at') then
      raise exception 'finding % was %; decided findings are immutable', old.id, old.status;
    end if;
  elsif new.status = 'APPROVED' and old.status <> 'PENDING_APPROVAL' then
    raise exception 'finding % is %; only PENDING_APPROVAL can be approved', old.id, old.status;
  end if;

  new.updated_at := now();
  return new;
end $$;

-- finding_events: append-only, except when purge_client removes that client's history.
create or replace function public.guard_finding_events() returns trigger language plpgsql as $$
declare v_client text := current_setting('physis.purge', true);
begin
  if tg_op = 'DELETE' and v_client is not null and v_client <> '' and exists (
       select 1 from detected_leaks d where d.id = old.finding_id and d.client_id = v_client) then
    return old;
  end if;
  raise exception 'finding_events is append-only';
end $$;

drop trigger if exists finding_events_append_only on public.finding_events;
create trigger finding_events_append_only before update or delete on public.finding_events
  for each row execute function public.guard_finding_events();

-- What the card shows, as one hash. Order and formatting are fixed so the hash is stable.
create or replace function public.run_fingerprint(p_run_id uuid)
returns text language sql stable security definer set search_path = public as $$
  select encode(sha256(convert_to(coalesce(string_agg(
           finding_key || '|' || calculated_arrears::text || '|' || coalesce(source_invoice_ids, '') || '|' ||
           coalesce(contract_citation, '') || '|' || coalesce(leak_description, '') || '|' ||
           coalesce(leak_breakdown::text, ''),
           E'\n' order by finding_key), ''), 'UTF8')), 'hex')
    from detected_leaks
   where audit_run_id = p_run_id and status = 'PENDING_APPROVAL'
$$;

-- What finance needs for a decided run: the same shape decide_audit_run returns.
create or replace function public.run_summary(p_run_id uuid, p_status text)
returns jsonb language sql stable security definer set search_path = public as $$
  with d as (
    select client_name, billing_period, source_invoice_ids, calculated_arrears, leak_description, leak_breakdown
      from detected_leaks where audit_run_id = p_run_id and status = p_status
  )
  select jsonb_build_object(
    'rows', coalesce((select jsonb_agg(jsonb_build_object(
              'period', to_char(billing_period, 'Mon YYYY'), 'invoices', source_invoice_ids,
              'amount', calculated_arrears::text, 'why', leak_description) order by billing_period) from d), '[]'::jsonb),
    'reasons', coalesce((select jsonb_agg(jsonb_build_object('reason', reason, 'amount', amount::text) order by amount desc)
              from (select e.key as reason, sum(e.value::numeric) as amount
                      from d, jsonb_each_text(coalesce(d.leak_breakdown, '{}'::jsonb)) e group by e.key) s), '[]'::jsonb),
    'client_name', (select min(client_name) from d),
    'count', (select count(*) from d),
    'total', (select coalesce(sum(calculated_arrears), 0)::numeric(12, 2)::text from d))
$$;

-- Same as 003, plus the fingerprint of what the card will show.
create or replace function public.open_audit_run(p_run_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare r audit_runs; v_total numeric(12, 2); v_count integer; v_review integer; v_print text;
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
  v_print := run_fingerprint(p_run_id);

  update audit_runs set
    status = case when v_count > 0 then 'AWAITING_DECISION' else 'NO_APPROVABLE' end,
    approvable_total = v_total, approvable_count = v_count, review_count = v_review,
    card_fingerprint = v_print, opened_at = now()
   where id = p_run_id;

  return jsonb_build_object('run_id', p_run_id, 'approvable_total', v_total::text,
                            'approvable_count', v_count, 'review_count', v_review, 'fingerprint', v_print);
end $$;

-- Same checks as 004, and the findings must still match the card's fingerprint.
create or replace function public.decide_audit_run(p_run_id uuid, p_decision text, p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path = public as $$
declare r audit_runs; a authorized_approvers; v_total numeric(12, 2); v_count integer;
        v_status text; v_actor text;
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
  if v_count <> r.approvable_count or v_total <> r.approvable_total
     or run_fingerprint(p_run_id) is distinct from r.card_fingerprint then
    return jsonb_build_object('ok', false, 'reason',
      'The findings changed after this card was sent. Nothing changed. Rerun the audit.');
  end if;

  v_status := case p_decision when 'APPROVE' then 'APPROVED' else 'DENIED' end;
  v_actor  := a.display_name || ' (telegram ' || a.telegram_user_id || ')';
  perform set_config('physis.actor', v_actor, true);

  update detected_leaks set status = v_status, decided_at = now(), decided_by = v_actor
   where audit_run_id = p_run_id and status = 'PENDING_APPROVAL';
  update audit_runs set status = v_status, decided_at = now(), decided_by = v_actor where id = p_run_id;

  return run_summary(p_run_id, v_status) || jsonb_build_object(
    'ok', true, 'decision', v_status, 'run_id', p_run_id, 'client_id', r.client_id,
    'contract_id', r.contract_id, 'approver', a.display_name, 'fingerprint', r.card_fingerprint)
    || case when (run_summary(p_run_id, v_status) ->> 'client_name') is null
            then jsonb_build_object('client_name', r.client_id) else '{}'::jsonb end;
end $$;

-- After the finance email is sent. Returns false if it was already recorded.
create or replace function public.mark_finance_notified(p_run_id uuid)
returns boolean language plpgsql security definer set search_path = public as $$
begin
  update audit_runs set finance_notified_at = now()
   where id = p_run_id and status = 'APPROVED' and finance_notified_at is null;
  return found;
end $$;

-- For the Resend button after a failed email: an approved run that finance never got.
create or replace function public.finance_payload(p_run_id uuid, p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path = public as $$
declare r audit_runs;
begin
  if not exists (select 1 from authorized_approvers where telegram_user_id = p_telegram_user_id and active) then
    return jsonb_build_object('ok', false, 'reason', 'You are not an authorized approver.');
  end if;
  select * into r from audit_runs where id = p_run_id;
  if not found or r.status <> 'APPROVED' then
    return jsonb_build_object('ok', false, 'reason', 'Only an approved audit run can be sent to finance.');
  end if;
  if r.finance_notified_at is not null then
    return jsonb_build_object('ok', false, 'reason', 'Finance was already emailed for this run. Nothing sent.');
  end if;
  return run_summary(p_run_id, 'APPROVED') || jsonb_build_object(
    'ok', true, 'decision', 'APPROVED', 'run_id', p_run_id, 'client_id', r.client_id,
    'contract_id', r.contract_id, 'approver', split_part(r.decided_by, ' (telegram', 1),
    'fingerprint', r.card_fingerprint);
end $$;

-- End of engagement. Type the confirmation exactly: 'PURGE <client_id>'.
create or replace function public.purge_client(p_client_id text, p_confirm text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_events integer; v_findings integer; v_runs integer;
begin
  if coalesce(p_client_id, '') = '' or p_confirm is distinct from 'PURGE ' || p_client_id then
    raise exception 'confirmation must be exactly: PURGE %', p_client_id;
  end if;
  perform set_config('physis.purge', p_client_id, true);

  delete from finding_events e using detected_leaks d
   where d.id = e.finding_id and d.client_id = p_client_id;
  get diagnostics v_events = row_count;
  delete from detected_leaks where client_id = p_client_id;
  get diagnostics v_findings = row_count;
  delete from audit_runs where client_id = p_client_id;
  get diagnostics v_runs = row_count;

  insert into client_purges (client_id, runs_deleted, findings_deleted, events_deleted)
  values (p_client_id, v_runs, v_findings, v_events);
  perform set_config('physis.purge', '', true);

  return jsonb_build_object('client_id', p_client_id, 'runs_deleted', v_runs,
                            'findings_deleted', v_findings, 'events_deleted', v_events);
end $$;

do $$
declare f text;
begin
  foreach f in array array['run_fingerprint(uuid)', 'run_summary(uuid, text)', 'open_audit_run(uuid)',
    'decide_audit_run(uuid, text, bigint)', 'mark_finance_notified(uuid)', 'finance_payload(uuid, bigint)',
    'purge_client(text, text)']
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
