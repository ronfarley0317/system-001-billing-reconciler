-- System #001: per-reason breakdown on each finding, so the finance email can say why
-- in one line per cause instead of one paragraph per month. Run once after 003.
-- Additive: one new nullable column; record_finding gains one optional parameter;
-- decide_audit_run also returns the client name and the breakdown totals.

begin;

alter table public.detected_leaks add column if not exists leak_breakdown jsonb;

comment on column public.detected_leaks.leak_breakdown is
  'Reason -> amount (exact decimal strings), computed in Python from the Decimal audit (no LLM).';

drop function if exists public.record_finding(uuid, text, text, text, text, date, text, text, numeric, text, text, text);

create or replace function public.record_finding(
  p_run_id uuid, p_finding_key text, p_client_name text, p_invoice_number text,
  p_source_invoice_ids text, p_billing_period date, p_variance_class text, p_status text,
  p_amount numeric, p_citation text, p_description text, p_review_reasons text,
  p_leak_breakdown jsonb default null)
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
                              contract_citation, leak_description, review_reasons, leak_breakdown)
  values (r.client_id, r.contract_id, p_finding_key, p_run_id, p_client_name, p_invoice_number,
          p_source_invoice_ids, p_billing_period, p_variance_class, p_status, p_amount,
          p_citation, p_description, p_review_reasons, p_leak_breakdown)
  on conflict (client_id, contract_id, finding_key) do update set
    audit_run_id = excluded.audit_run_id, client_name = excluded.client_name,
    invoice_number = excluded.invoice_number, source_invoice_ids = excluded.source_invoice_ids,
    billing_period = excluded.billing_period, variance_class = excluded.variance_class,
    status = excluded.status, calculated_arrears = excluded.calculated_arrears,
    contract_citation = excluded.contract_citation, leak_description = excluded.leak_description,
    review_reasons = excluded.review_reasons, leak_breakdown = excluded.leak_breakdown;
  return p_status;
end $$;

-- Same checks as 003. Also returns client_name and the breakdown summed across the decided months.
create or replace function public.decide_audit_run(p_run_id uuid, p_decision text, p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path = public as $$
declare r audit_runs; a authorized_approvers; v_total numeric(12, 2); v_count integer;
        v_status text; v_actor text; v_rows jsonb; v_reasons jsonb; v_client text;
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
    returning client_name, billing_period, source_invoice_ids, calculated_arrears, leak_description, leak_breakdown
  )
  select
    coalesce((select jsonb_agg(jsonb_build_object(
                'period', to_char(billing_period, 'Mon YYYY'), 'invoices', source_invoice_ids,
                'amount', calculated_arrears::text, 'why', leak_description) order by billing_period)
              from decided), '[]'::jsonb),
    coalesce((select jsonb_agg(jsonb_build_object('reason', reason, 'amount', amount::text) order by amount desc)
              from (select e.key as reason, sum(e.value::numeric) as amount
                      from decided d, jsonb_each_text(coalesce(d.leak_breakdown, '{}'::jsonb)) e
                     group by e.key) s), '[]'::jsonb),
    (select min(client_name) from decided)
    into v_rows, v_reasons, v_client;

  update audit_runs set status = v_status, decided_at = now(), decided_by = v_actor where id = p_run_id;

  return jsonb_build_object('ok', true, 'decision', v_status, 'run_id', p_run_id,
                            'client_id', r.client_id, 'client_name', coalesce(v_client, r.client_id),
                            'contract_id', r.contract_id, 'count', v_count, 'total', v_total::text,
                            'approver', a.display_name, 'rows', v_rows, 'reasons', v_reasons);
end $$;

do $$
declare f text;
begin
  foreach f in array array[
    'record_finding(uuid, text, text, text, text, date, text, text, numeric, text, text, text, jsonb)',
    'decide_audit_run(uuid, text, bigint)']
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
