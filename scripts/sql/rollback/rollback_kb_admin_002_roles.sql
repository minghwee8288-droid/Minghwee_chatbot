-- ROLLBACK of kb_admin_002_roles.sql. Run after rollbacks 006, 005, 004, 003.
--
-- Puts back the Phase 1 check on cb_kb_admin_users: role = 'viewer' only.
--
-- REFUSES while any row is 'editor' or 'approver' - the narrow check could not
-- be added over them, and deciding what those people become (viewer, or no
-- access) is a person's call, not this file's. Change or remove those rows in
-- a separate reviewed statement first, e.g.
--   update public.cb_kb_admin_users set role = 'viewer' where role <> 'viewer';
--
-- Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
declare
    n bigint;
    def text;
begin
    select count(*) into n from public.cb_kb_admin_users where role <> 'viewer';
    if n > 0 then
        raise exception 'REFUSING: % access row(s) are editor/approver - make them viewer or remove them first', n;
    end if;

    alter table public.cb_kb_admin_users drop constraint if exists cb_kb_admin_users_role_check;
    alter table public.cb_kb_admin_users
        add constraint cb_kb_admin_users_role_check check (role = 'viewer');

    select pg_get_constraintdef(oid) into def from pg_constraint
     where conrelid = 'public.cb_kb_admin_users'::regclass and conname = 'cb_kb_admin_users_role_check';
    if def is null or def like '%editor%' or def like '%approver%' or def not like '%viewer%' then
        raise exception 'ROLLBACK 002 CHECK FAILED: role check is %', def;
    end if;
    raise notice 'PASS: rollback 002 - role check restored to %', def;
end
$$;

commit;
