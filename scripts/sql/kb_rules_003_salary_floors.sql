-- Salary floors for Indonesia and Myanmar (2026-10-15), from the agency's
-- approved fees and process document, decision C: a basic salary of S$550
-- (ID) and S$500 (MM); the Philippines stays S$650. The budget question never
-- offers a band below the floor for the nationality in hand
-- (info_collector._effective_options). kb_rules.DEFAULTS carries the same
-- three values, so the DRIFT check in selfcheck_kb_prep.py stays green.
--
-- Idempotent: ON CONFLICT DO NOTHING. Rollback:
-- scripts/sql/rollback/rollback_kb_rules_003_salary_floors.sql
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

insert into public.cb_kb_rules (rule_type, service_type, nationality, value, locked, updated_by)
values
    ('salary_floor', 'all', 'ID', '550', false, 'fees_doc_2026-10-15'),
    ('salary_floor', 'all', 'MM', '500', false, 'fees_doc_2026-10-15')
on conflict (rule_type, service_type, nationality) do nothing;

do $$
begin
    if (select count(*) from public.cb_kb_rules
        where rule_type = 'salary_floor' and nationality in ('PH', 'ID', 'MM')) <> 3 then
        raise exception 'kb_rules_003: expected three salary floors (PH, ID, MM)';
    end if;
end $$;

commit;
