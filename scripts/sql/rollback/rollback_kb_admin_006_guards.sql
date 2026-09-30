-- ROLLBACK of kb_admin_006_guards.sql. Run FIRST of the rollbacks (006 -> 001).
--
-- Removes: the safety-net trigger on cb_knowledge_base_updated and its
-- function, the 1536-dimension CHECK, and the canary table.
--
-- Changes NO row of cb_knowledge_base_updated. Audit rows the trigger already
-- wrote stay in cb_kb_audit until rollback 003.
--
-- Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

drop trigger if exists cb_kb_updated_audit_external on public.cb_knowledge_base_updated;
drop function if exists public.kb_admin__log_external_write();

alter table public.cb_knowledge_base_updated
    drop constraint if exists cb_knowledge_base_updated_embedding_dims_check;

-- The canary's policies and grants go with the table.
drop table if exists public.cb_kb_canary;

do $$
begin
    if exists (select 1 from pg_trigger where tgname = 'cb_kb_updated_audit_external')
       or exists (select 1 from pg_proc where proname = 'kb_admin__log_external_write')
       or exists (select 1 from pg_constraint where conname = 'cb_knowledge_base_updated_embedding_dims_check')
       or to_regclass('public.cb_kb_canary') is not null then
        raise exception 'ROLLBACK 006 CHECK FAILED: something from 006 is still present';
    end if;
    raise notice 'PASS: rollback 006 - trigger, trigger function, dimension check and canary removed';
end
$$;

commit;
