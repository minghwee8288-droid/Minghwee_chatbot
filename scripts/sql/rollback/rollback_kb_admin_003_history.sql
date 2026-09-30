-- ROLLBACK of kb_admin_003_history.sql. Run after rollbacks 006, 005 and 004.
--
-- Removes cb_kb_audit, cb_kb_entry_versions (their indexes, policies and the
-- reader's grants go with them) and the append-only trigger function.
--
-- THIS DESTROYS THE EDIT HISTORY. It therefore REFUSES while either table
-- holds a row, and says how many. To go ahead anyway, export both tables
-- first, then uncomment the SET LOCAL line below - a deliberate, visible
-- edit to this file, not a flag anyone can pass by accident.
--
-- It does NOT undo edits that were published: those are already in
-- cb_knowledge_base_updated (with metadata.managed_by = 'ui') and stay there.
-- Restore a row from the version history BEFORE running this if needed.
--
-- The append-only triggers refuse UPDATE, DELETE and TRUNCATE; DROP TABLE is
-- none of those, so it is not blocked.
--
-- Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

-- set local kb_admin.rollback_discard_history = 'yes';

do $$
declare
    n_versions bigint := 0;
    n_audit    bigint := 0;
begin
    if exists (select 1 from pg_roles where rolname = 'kb_admin_fn_owner')
       or exists (select 1 from pg_proc where proname like 'kb\_admin\_%' and proname <> 'kb_admin__log_external_write') then
        raise exception 'REFUSING: 004 objects still exist - apply rollback 004 first';
    end if;
    if to_regclass('public.cb_kb_entry_versions') is not null then
        execute 'select count(*) from public.cb_kb_entry_versions' into n_versions;
    end if;
    if to_regclass('public.cb_kb_audit') is not null then
        execute 'select count(*) from public.cb_kb_audit' into n_audit;
    end if;
    raise notice 'history present: % version row(s), % audit row(s)', n_versions, n_audit;
    if (n_versions + n_audit) > 0
       and coalesce(current_setting('kb_admin.rollback_discard_history', true), '') <> 'yes' then
        raise exception 'REFUSING: the history holds % version and % audit row(s). Export them, then uncomment the SET LOCAL line in this file.', n_versions, n_audit;
    end if;
end
$$;

drop table if exists public.cb_kb_audit;           -- references versions, so first
drop table if exists public.cb_kb_entry_versions;
drop function if exists public.cb_kb_audit_refuse_change();

do $$
begin
    if to_regclass('public.cb_kb_audit') is not null
       or to_regclass('public.cb_kb_entry_versions') is not null
       or exists (select 1 from pg_proc where proname = 'cb_kb_audit_refuse_change') then
        raise exception 'ROLLBACK 003 CHECK FAILED: something from 003 is still present';
    end if;
    raise notice 'PASS: rollback 003 - cb_kb_audit, cb_kb_entry_versions and the append-only function removed';
end
$$;

commit;
