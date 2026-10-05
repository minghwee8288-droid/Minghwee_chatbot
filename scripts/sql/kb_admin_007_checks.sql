-- kb_admin_007_checks: verify 001-006 took, and that the portal cannot reach
-- anything new. Run LAST. Changes nothing: it runs inside a READ ONLY
-- transaction and only reads the catalog (has_*_privilege, pg_constraint,
-- pg_trigger, pg_proc). It never calls a kb_admin_* function, so no data is
-- touched; "can call" is checked as the EXECUTE privilege, which is exactly
-- what "permission denied" would be about.
--
-- Each block RAISEs NOTICE 'PASS ...' or RAISEs EXCEPTION 'FAIL ...'. The
-- first FAIL stops the run with a non-zero exit from apply_sql.py.
--
-- SCOPE (wording clarified 2026-10-05; no check changed): this checks the
-- PHASE 2 functions only - the five Q&A functions of 004. It says nothing
-- about 008's kb_admin_create_entry or the 009-014 document functions, and
-- has_table_privilege cannot see column-level grants (008's ten-column INSERT
-- for kb_admin_fn_owner reads as "no INSERT" here). kb_admin_012_grants_checks
-- is the full check: all 16 editor functions and the column-level surface.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin transaction read only;

-- -----------------------------------------------------------------------------
-- 1. kb_admin_reader can read the history (not the vectors in it)
-- -----------------------------------------------------------------------------
do $$
begin
    if not has_table_privilege('kb_admin_reader', 'public.cb_kb_audit', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader cannot SELECT cb_kb_audit';
    end if;
    if not has_column_privilege('kb_admin_reader', 'public.cb_kb_entry_versions', 'question', 'SELECT')
       or not has_column_privilege('kb_admin_reader', 'public.cb_kb_entry_versions', 'status', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader cannot SELECT cb_kb_entry_versions';
    end if;
    if has_column_privilege('kb_admin_reader', 'public.cb_kb_entry_versions', 'embedding', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader can SELECT cb_kb_entry_versions.embedding';
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_entry_versions' and 'kb_admin_reader' = any(roles))
       or not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_audit' and 'kb_admin_reader' = any(roles)) then
        raise exception 'FAIL: no RLS policy lets kb_admin_reader read the history tables';
    end if;
    raise notice 'PASS: kb_admin_reader reads versions (not embedding) and audit';
end
$$;

-- -----------------------------------------------------------------------------
-- 2. kb_admin_editor can EXECUTE the five Phase 2 functions, and touch no table
--    (the other 11 it executes since 008 and 011/014 are 012's to check)
-- -----------------------------------------------------------------------------
do $$
declare
    f text;
    t record;
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_editor' and rolcanlogin) then
        raise exception 'FAIL: login role kb_admin_editor does not exist';
    end if;
    foreach f in array array[
        'public.kb_admin_save_draft(uuid, text, uuid, text, text, text, text, text, text, boolean, text)',
        'public.kb_admin_discard_draft(uuid, text, uuid, text)',
        'public.kb_admin_publish(uuid, text, uuid, vector, text)',
        'public.kb_admin_restore(uuid, text, uuid, uuid, text)',
        'public.kb_admin_toggle(uuid, text, uuid, boolean, text)'
    ] loop
        if not has_function_privilege('kb_admin_editor', f, 'EXECUTE') then
            raise exception 'FAIL: kb_admin_editor cannot EXECUTE %', f;
        end if;
    end loop;

    -- No privilege of any kind on any table or view in public.
    for t in
        select c.oid, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind in ('r', 'v', 'm', 'p', 'f')
    loop
        if has_table_privilege('kb_admin_editor', t.oid,
                               'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
           or has_any_column_privilege('kb_admin_editor', t.oid, 'SELECT, INSERT, UPDATE, REFERENCES') then
            raise exception 'FAIL: kb_admin_editor holds a privilege on public.%', t.relname;
        end if;
    end loop;

    -- The internal helpers are the owner's alone.
    for f in
        select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname like 'kb\_admin\_\_%'
    loop
        if has_function_privilege('kb_admin_editor', f, 'EXECUTE') then
            raise exception 'FAIL: kb_admin_editor can EXECUTE internal helper %', f;
        end if;
    end loop;

    raise notice 'PASS: kb_admin_editor executes the five Phase 2 functions, no table, no helper (Phase 2 only; 012 checks all 16)';
end
$$;

-- -----------------------------------------------------------------------------
-- 3-6. anon, authenticated, service_role and PUBLIC reach nothing new
-- -----------------------------------------------------------------------------
do $$
declare
    r text;
    t text;
    f text;
begin
    foreach r in array array['anon', 'authenticated', 'service_role', 'public'] loop
        foreach t in array array['public.cb_kb_entry_versions', 'public.cb_kb_audit', 'public.cb_kb_canary'] loop
            if has_table_privilege(r, t, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
               or has_any_column_privilege(r, t, 'SELECT, INSERT, UPDATE, REFERENCES') then
                raise exception 'FAIL: % holds a privilege on %', r, t;
            end if;
        end loop;

        for f in
            select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'public'
               and (p.proname like 'kb\_admin\_%' or p.proname = 'cb_kb_audit_refuse_change')
        loop
            if has_function_privilege(r, f, 'EXECUTE') then
                raise exception 'FAIL: % can EXECUTE %', r, f;
            end if;
        end loop;
    end loop;
    raise notice 'PASS: anon / authenticated / service_role / PUBLIC cannot read the new tables or run the functions';
end
$$;

do $$
declare
    t text;
begin
    foreach t in array array['cb_kb_entry_versions', 'cb_kb_audit', 'cb_kb_canary'] loop
        if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                        where n.nspname = 'public' and c.relname = t and c.relrowsecurity) then
            raise exception 'FAIL: RLS is not enabled on %', t;
        end if;
        if exists (select 1 from pg_policies where schemaname = 'public' and tablename = t
                    and (roles && array['anon', 'authenticated', 'service_role', 'public']::name[])) then
            raise exception 'FAIL: a policy on % names a portal/public role', t;
        end if;
    end loop;
    raise notice 'PASS: RLS on for all three new tables, with no policy for portal/public roles';
end
$$;

-- -----------------------------------------------------------------------------
-- 7. The functions run as the narrow owner, with a pinned search_path
-- -----------------------------------------------------------------------------
do $$
declare
    p record;
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_fn_owner'
                    and not rolcanlogin and not rolbypassrls and not rolsuper) then
        raise exception 'FAIL: kb_admin_fn_owner missing, or can log in / bypass RLS';
    end if;
    for p in
        select pr.oid::regprocedure::text as sig, pr.prosecdef, pg_get_userbyid(pr.proowner) as owner,
               pr.proconfig
          from pg_proc pr join pg_namespace n on n.oid = pr.pronamespace
         where n.nspname = 'public' and pr.proname in (
               'kb_admin_save_draft', 'kb_admin_discard_draft', 'kb_admin_publish',
               'kb_admin_restore', 'kb_admin_toggle', 'kb_admin__log_external_write')
    loop
        if not p.prosecdef then
            raise exception 'FAIL: % is not SECURITY DEFINER', p.sig;
        end if;
        if p.owner <> 'kb_admin_fn_owner' then
            raise exception 'FAIL: % is owned by %, not kb_admin_fn_owner', p.sig, p.owner;
        end if;
        if p.proconfig is null or not exists (
               select 1 from unnest(p.proconfig) c where c like 'search_path=%pg_temp%') then
            raise exception 'FAIL: % has no pinned search_path ending in pg_temp', p.sig;
        end if;
    end loop;
    if (select count(*) from pg_proc pr join pg_namespace n on n.oid = pr.pronamespace
         where n.nspname = 'public' and pr.proname in (
               'kb_admin_save_draft', 'kb_admin_discard_draft', 'kb_admin_publish',
               'kb_admin_restore', 'kb_admin_toggle')) <> 5 then
        raise exception 'FAIL: expected exactly five Phase 2 kb_admin_* entry functions';
    end if;
    if has_table_privilege('kb_admin_fn_owner', 'public.cb_kb_audit', 'UPDATE')
       or has_table_privilege('kb_admin_fn_owner', 'public.cb_kb_audit', 'DELETE')
       or has_table_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', 'INSERT')
       or has_table_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', 'DELETE')
       or has_column_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', 'rag_score_floor', 'UPDATE')
       or has_column_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', 'chunk_type', 'UPDATE') then
        raise exception 'FAIL: kb_admin_fn_owner holds more than its narrow grants';
    end if;
    raise notice 'PASS: the five Phase 2 SECURITY DEFINER functions owned by a NOLOGIN, narrowly granted owner (Phase 2 only; 012 is the full check)';
end
$$;

-- -----------------------------------------------------------------------------
-- 8. The 1536-dimension constraint exists and is validated
-- -----------------------------------------------------------------------------
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.cb_knowledge_base_updated'::regclass
           and conname = 'cb_knowledge_base_updated_embedding_dims_check'
           and convalidated
           and pg_get_constraintdef(oid) like '%vector_dims(embedding) = 1536%') then
        raise exception 'FAIL: validated 1536-dimension CHECK missing on cb_knowledge_base_updated';
    end if;
    raise notice 'PASS: 1536-dimension CHECK present and validated';
end
$$;

-- -----------------------------------------------------------------------------
-- 9. The audit log is append-only, and the safety net is armed
-- -----------------------------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_trigger where tgrelid = 'public.cb_kb_audit'::regclass
                    and tgname = 'cb_kb_audit_append_only' and tgenabled <> 'D') then
        raise exception 'FAIL: append-only trigger missing or disabled on cb_kb_audit';
    end if;
    if not exists (select 1 from pg_trigger where tgrelid = 'public.cb_kb_audit'::regclass
                    and tgname = 'cb_kb_audit_no_truncate' and tgenabled <> 'D') then
        raise exception 'FAIL: no-truncate trigger missing or disabled on cb_kb_audit';
    end if;
    if not exists (select 1 from pg_trigger where tgrelid = 'public.cb_knowledge_base_updated'::regclass
                    and tgname = 'cb_kb_updated_audit_external' and tgenabled <> 'D') then
        raise exception 'FAIL: safety-net audit trigger missing or disabled on cb_knowledge_base_updated';
    end if;
    raise notice 'PASS: cb_kb_audit is append-only and the live-table safety net is enabled';
end
$$;

-- -----------------------------------------------------------------------------
-- 10. Access list and editor login state (the last is a reminder, not a FAIL)
-- -----------------------------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.cb_kb_admin_users'::regclass
                      and conname = 'cb_kb_admin_users_role_check'
                      and pg_get_constraintdef(oid) like '%editor%'
                      and pg_get_constraintdef(oid) like '%approver%') then
        raise exception 'FAIL: cb_kb_admin_users role check does not allow editor/approver';
    end if;
    raise notice 'PASS: cb_kb_admin_users accepts viewer / editor / approver';

    if exists (select 1 from pg_roles where rolname = 'kb_admin_editor'
                and rolvaliduntil is not null and rolvaliduntil < now()) then
        raise notice 'NOTE: kb_admin_editor''s password is still the expired placeholder - it cannot log in until ALTER ROLE ... PASSWORD ... VALID UNTIL ''infinity''';
    end if;
    if not exists (select 1 from public.cb_kb_canary) then
        raise notice 'NOTE: cb_kb_canary is empty - kb_admin_publish will refuse every new vector until it is seeded';
    end if;
end
$$;

-- -----------------------------------------------------------------------------
-- 11. The 1200-character length check exists in save_draft and publish
-- -----------------------------------------------------------------------------
do $$
begin
    if not exists (
        select 1 from pg_proc where proname = 'kb_admin_save_draft'
          and pg_get_functiondef(oid) like '%1200%'
    ) then
        raise exception 'FAIL: kb_admin_save_draft does not contain a 1200-char check';
    end if;
    if not exists (
        select 1 from pg_proc where proname = 'kb_admin_publish'
          and pg_get_functiondef(oid) like '%1200%'
    ) then
        raise exception 'FAIL: kb_admin_publish does not contain a 1200-char check';
    end if;
    raise notice 'PASS: 1200-character limit present in save_draft and publish';
end
$$;

-- -----------------------------------------------------------------------------
-- 12. kb_admin_fn_owner holds NO create right on schema public. 004 and 006
--     grant it only for the ownership hand-over and revoke it straight after;
--     a leftover grant would let a SECURITY DEFINER owner create objects.
-- -----------------------------------------------------------------------------
do $$
begin
    if has_schema_privilege('kb_admin_fn_owner', 'public', 'CREATE') then
        raise exception 'FAIL: kb_admin_fn_owner can CREATE in schema public';
    end if;
    raise notice 'PASS: kb_admin_fn_owner has no CREATE on schema public';
end
$$;

commit;
