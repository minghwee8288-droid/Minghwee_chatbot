-- "ROLLBACK" of kb_admin_001_reader.sql - on production this CHANGES NOTHING.
--
-- 001 records the Phase 1 setup that already existed on production before
-- Phase 2; applied there it is a no-op by design. Undoing it would mean
-- removing kb-admin's read access (kb_admin_reader, cb_kb_admin_users, the
-- grants and policies), which is Phase 1 and is NOT part of this rollback.
--
-- So this file only VERIFIES, inside a READ ONLY transaction, that after
-- rollbacks 006 -> 002:
--   * Phase 1 is intact exactly as 001 describes it, and
--   * nothing from Phase 2 is left anywhere.
-- Any FAIL raises and exits non-zero.
--
-- (007 is read-only checks; it created nothing, so it has no rollback.)
--
-- Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin transaction read only;

do $$
declare
    r record;
    cols text;
    def text;
begin
    -- ---- Phase 1 intact ------------------------------------------------------
    select * into r from pg_roles where rolname = 'kb_admin_reader';
    if r is null or not r.rolcanlogin or r.rolinherit or r.rolbypassrls or r.rolconnlimit <> 5 then
        raise exception 'FAIL: kb_admin_reader missing or its attributes changed';
    end if;
    if not exists (select 1 from pg_db_role_setting s join pg_roles ro on ro.oid = s.setrole
                    where ro.rolname = 'kb_admin_reader'
                      and 'default_transaction_read_only=on' = any (s.setconfig)) then
        raise exception 'FAIL: kb_admin_reader is no longer read-only by default';
    end if;

    select string_agg(column_name, ',' order by column_name) into cols
      from information_schema.column_privileges
     where grantee = 'kb_admin_reader' and table_schema = 'public'
       and table_name = 'cb_knowledge_base_updated' and privilege_type = 'SELECT';
    if cols is distinct from 'answer,chunk_type,contact_type,content,created_at,id,is_active,metadata,namespace,nationality,question,section_heading,service_type,source_document,updated_at' then
        raise exception 'FAIL: kb_admin_reader columns on cb_knowledge_base_updated are %', cols;
    end if;
    if not has_table_privilege('kb_admin_reader', 'public.cb_kb_rules', 'SELECT')
       or not has_table_privilege('kb_admin_reader', 'public.cb_kb_admin_users', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader lost SELECT on cb_kb_rules or cb_kb_admin_users';
    end if;
    if (select count(*) from pg_policies where schemaname = 'public' and policyname = 'kb_admin_reader_select'
          and tablename in ('cb_knowledge_base_updated', 'cb_kb_rules', 'cb_kb_admin_users')) <> 3 then
        raise exception 'FAIL: a Phase 1 kb_admin_reader_select policy is missing';
    end if;
    if not (select relrowsecurity from pg_class where oid = 'public.cb_kb_admin_users'::regclass) then
        raise exception 'FAIL: RLS is off on cb_kb_admin_users';
    end if;
    if has_table_privilege('anon', 'public.cb_kb_admin_users', 'SELECT')
       or has_table_privilege('authenticated', 'public.cb_kb_admin_users', 'SELECT') then
        raise exception 'FAIL: the portal keys can read cb_kb_admin_users';
    end if;
    select pg_get_constraintdef(oid) into def from pg_constraint
     where conrelid = 'public.cb_kb_admin_users'::regclass and conname = 'cb_kb_admin_users_role_check';
    if def is null or def like '%editor%' or def like '%approver%' then
        raise exception 'FAIL: role check is % (want viewer only)', def;
    end if;
    raise notice 'PASS: Phase 1 intact (reader role + settings, 15 columns, 2 tables, 3 policies, RLS, viewer-only check)';

    -- ---- Nothing from Phase 2 left --------------------------------------------
    if exists (select 1 from pg_roles where rolname in ('kb_admin_editor', 'kb_admin_fn_owner')) then
        raise exception 'FAIL: a Phase 2 role remains';
    end if;
    if exists (select 1 from pg_proc p join pg_namespace s on s.oid = p.pronamespace
                where s.nspname = 'public' and (p.proname like 'kb\_admin\_%' or p.proname = 'cb_kb_audit_refuse_change')) then
        raise exception 'FAIL: a Phase 2 function remains';
    end if;
    if to_regclass('public.cb_kb_audit') is not null or to_regclass('public.cb_kb_entry_versions') is not null
       or to_regclass('public.cb_kb_canary') is not null then
        raise exception 'FAIL: a Phase 2 table remains';
    end if;
    if exists (select 1 from pg_trigger where tgrelid = 'public.cb_knowledge_base_updated'::regclass
                and tgname = 'cb_kb_updated_audit_external')
       or exists (select 1 from pg_constraint where conname = 'cb_knowledge_base_updated_embedding_dims_check') then
        raise exception 'FAIL: the 006 trigger or dimension check remains';
    end if;
    if exists (select 1 from pg_policies where schemaname = 'public' and policyname like 'kb\_admin\_fn\_owner%') then
        raise exception 'FAIL: a kb_admin_fn_owner policy remains';
    end if;
    raise notice 'PASS: nothing from Phase 2 (002-006) remains';
end
$$;

commit;
