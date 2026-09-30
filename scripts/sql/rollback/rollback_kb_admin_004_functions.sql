-- ROLLBACK of kb_admin_004_functions.sql. Run after rollbacks 006 and 005.
--
-- Removes: the 14 kb_admin_* functions (5 callable + 9 internal helpers), the
-- five kb_admin_fn_owner RLS policies, every grant 004 gave kb_admin_fn_owner,
-- and the kb_admin_fn_owner role.
--
-- Refuses if 005 or 006 has not been rolled back yet (kb_admin_editor or the
-- safety-net trigger function still exist), because both depend on this.
--
-- Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'kb_admin_editor') then
        raise exception 'REFUSING: kb_admin_editor still exists - apply rollback 005 first';
    end if;
    if exists (select 1 from pg_proc where proname = 'kb_admin__log_external_write') then
        raise exception 'REFUSING: the 006 trigger function still exists - apply rollback 006 first';
    end if;
end
$$;

-- --- Functions (callable first, then the helpers they use) --------------------
drop function if exists public.kb_admin_save_draft(uuid, text, uuid, text, text, text, text, text, text, boolean, text);
drop function if exists public.kb_admin_discard_draft(uuid, text, uuid, text);
drop function if exists public.kb_admin_publish(uuid, text, uuid, vector, text);
drop function if exists public.kb_admin_restore(uuid, text, uuid, uuid, text);
drop function if exists public.kb_admin_toggle(uuid, text, uuid, boolean, text);
drop function if exists public.kb_admin__discard(public.cb_kb_entry_versions, uuid, text, text);
drop function if exists public.kb_admin__live_matches(uuid, public.cb_kb_entry_versions);
drop function if exists public.kb_admin__ensure_baseline(uuid);
drop function if exists public.kb_admin__next_number(uuid);
drop function if exists public.kb_admin__fields(public.cb_kb_entry_versions);
drop function if exists public.kb_admin__check_routing(text, text, text, text);
drop function if exists public.kb_admin__numbers(text);
drop function if exists public.kb_admin__refuse_nric(text[]);
drop function if exists public.kb_admin__require_actor(uuid, text);

-- --- Policies -----------------------------------------------------------------
drop policy if exists kb_admin_fn_owner_select    on public.cb_knowledge_base_updated;
drop policy if exists kb_admin_fn_owner_update_qa on public.cb_knowledge_base_updated;
drop policy if exists kb_admin_fn_owner_select    on public.cb_kb_admin_users;
do $$
begin
    if to_regclass('public.cb_kb_entry_versions') is not null then
        drop policy if exists kb_admin_fn_owner_all on public.cb_kb_entry_versions;
    end if;
    if to_regclass('public.cb_kb_audit') is not null then
        drop policy if exists kb_admin_fn_owner_insert on public.cb_kb_audit;
    end if;
end
$$;

-- --- Grants and the role ------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_fn_owner') then
        raise notice 'kb_admin_fn_owner does not exist - nothing more to do';
        return;
    end if;

    revoke all on public.cb_knowledge_base_updated from kb_admin_fn_owner;  -- incl. the column grants
    revoke all on public.cb_kb_admin_users from kb_admin_fn_owner;
    if to_regclass('public.cb_kb_entry_versions') is not null then
        revoke all on public.cb_kb_entry_versions from kb_admin_fn_owner;
    end if;
    if to_regclass('public.cb_kb_audit') is not null then
        revoke all on public.cb_kb_audit from kb_admin_fn_owner;
    end if;
    revoke all on schema public from kb_admin_fn_owner;

    execute (
        select coalesce(string_agg(format('revoke %I from %I', g.rolname, m.rolname), '; '), 'select 1')
          from pg_auth_members am
          join pg_roles g on g.oid = am.roleid
          join pg_roles m on m.oid = am.member
         where g.rolname = 'kb_admin_fn_owner' or m.rolname = 'kb_admin_fn_owner');

    drop role kb_admin_fn_owner;
end
$$;

do $$
declare
    n int;
begin
    select count(*) into n from pg_proc p join pg_namespace s on s.oid = p.pronamespace
     where s.nspname = 'public' and p.proname like 'kb\_admin\_%';
    if n <> 0 then
        raise exception 'ROLLBACK 004 CHECK FAILED: % kb_admin_* function(s) remain', n;
    end if;
    if exists (select 1 from pg_policies where schemaname = 'public' and policyname like 'kb\_admin\_fn\_owner%') then
        raise exception 'ROLLBACK 004 CHECK FAILED: a kb_admin_fn_owner policy remains';
    end if;
    if exists (select 1 from pg_roles where rolname = 'kb_admin_fn_owner') then
        raise exception 'ROLLBACK 004 CHECK FAILED: kb_admin_fn_owner still exists';
    end if;
    raise notice 'PASS: rollback 004 - 14 functions, 5 policies, the grants and kb_admin_fn_owner removed';
end
$$;

commit;
