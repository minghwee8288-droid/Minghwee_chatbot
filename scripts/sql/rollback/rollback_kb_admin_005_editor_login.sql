-- ROLLBACK of kb_admin_005_editor_login.sql. Run after rollback 006.
--
-- Removes the kb_admin_editor login: its EXECUTE on the five functions, its
-- USAGE on schema public, and the role itself (with its settings and password).
--
-- Do this BEFORE (or together with) removing KB_ADMIN_DB_PASSWORD_EDITOR from
-- Vercel: once the role is gone kb-admin's editor pool cannot connect, and
-- kb-admin shows editing as unavailable. Reads are unaffected.
--
-- Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_editor') then
        raise notice 'kb_admin_editor does not exist - nothing to do';
        return;
    end if;

    -- The functions may already be gone if 004 was rolled back out of order.
    if to_regprocedure('public.kb_admin_save_draft(uuid, text, uuid, text, text, text, text, text, text, boolean, text)') is not null then
        revoke execute on function
            public.kb_admin_save_draft(uuid, text, uuid, text, text, text, text, text, text, boolean, text),
            public.kb_admin_discard_draft(uuid, text, uuid, text),
            public.kb_admin_publish(uuid, text, uuid, vector, text),
            public.kb_admin_restore(uuid, text, uuid, uuid, text),
            public.kb_admin_toggle(uuid, text, uuid, boolean, text)
        from kb_admin_editor;
    end if;

    revoke usage on schema public from kb_admin_editor;
    -- Any membership in either direction (e.g. one granted by hand for a test).
    execute (
        select coalesce(string_agg(format('revoke %I from %I', g.rolname, m.rolname), '; '), 'select 1')
          from pg_auth_members am
          join pg_roles g on g.oid = am.roleid
          join pg_roles m on m.oid = am.member
         where g.rolname = 'kb_admin_editor' or m.rolname = 'kb_admin_editor');

    drop role kb_admin_editor;
end
$$;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'kb_admin_editor') then
        raise exception 'ROLLBACK 005 CHECK FAILED: kb_admin_editor still exists';
    end if;
    raise notice 'PASS: rollback 005 - kb_admin_editor removed';
end
$$;

commit;
