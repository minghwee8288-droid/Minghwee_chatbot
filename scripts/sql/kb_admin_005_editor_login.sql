-- After applying, change the password immediately and set it in Vercel KB_ADMIN_DB_PASSWORD_EDITOR.
--
--   ALTER ROLE kb_admin_editor PASSWORD '<the real one>' VALID UNTIL 'infinity';
--
-- (run by hand, never committed, never in an env file under git).
--
-- kb_admin_005_editor_login: the login kb-admin uses for WRITES.
--
-- It can do exactly two things: use schema public, and EXECUTE the five
-- kb_admin_* functions from 004. It holds no privilege on any table, so it
-- cannot read or write the knowledge base, the history, the access list or
-- anything else directly. Reads stay on kb_admin_reader.
--
-- THE PLACEHOLDER CANNOT LOG IN. The role is created with the placeholder
-- password 'CHANGE_ME_IMMEDIATELY' and VALID UNTIL 2000-01-01, i.e. already
-- expired, so the placeholder is useless to anyone who reads this file. The
-- ALTER above sets the real password AND lifts the expiry in one statement.
--
-- Re-running this file NEVER touches the password or the expiry of an
-- existing role, so it cannot undo the real password.
--
-- Settings mirror kb_admin_reader's, except it is not read-only, and the
-- statement timeout is a little longer (a publish runs several statements).
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_editor') then
        create role kb_admin_editor login noinherit nobypassrls connection limit 5
            password 'CHANGE_ME_IMMEDIATELY' valid until '2000-01-01';
    end if;
end
$$;

alter role kb_admin_editor login noinherit nobypassrls nocreatedb nocreaterole
    noreplication connection limit 5;
alter role kb_admin_editor set statement_timeout = '10s';
alter role kb_admin_editor set lock_timeout = '3s';
alter role kb_admin_editor set idle_in_transaction_session_timeout = '15s';

grant usage on schema public to kb_admin_editor;

grant execute on function
    public.kb_admin_save_draft(uuid, text, uuid, text, text, text, text, text, text, boolean, text),
    public.kb_admin_discard_draft(uuid, text, uuid, text),
    public.kb_admin_publish(uuid, text, uuid, vector, text),
    public.kb_admin_restore(uuid, text, uuid, uuid, text),
    public.kb_admin_toggle(uuid, text, uuid, boolean, text)
to kb_admin_editor;

-- Belt and braces: the role is new, so it holds nothing else - but make sure
-- no table privilege on the tables it must never touch directly.
revoke all on public.cb_knowledge_base_updated, public.cb_kb_entry_versions,
              public.cb_kb_audit, public.cb_kb_admin_users, public.cb_kb_rules
    from kb_admin_editor;

commit;
