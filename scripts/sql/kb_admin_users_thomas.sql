-- kb-admin access: give the Ming Hwee account with this Supabase Auth id the
-- approver role (Thomas, Ming Hwee; requested 2026-10-08).
--
-- Inserts ONE row into cb_kb_admin_users. Touches no other row.
--
-- The email address is deliberately not written here (this repo is public). The
-- account is found by its auth id, and the file REFUSES, changing nothing, unless:
--   * exactly one auth.users row has that id, and its email is @minghwee.com;
--   * that account has no cb_kb_admin_users row yet (a second run refuses rather
--     than silently re-activating or re-roling a row someone may have changed).
-- The id was matched against the email read-only before this file was written.
--
-- Rollback: rollback/rollback_kb_admin_users_thomas.sql (sets active = false).
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
declare
    uid constant uuid := '4d1e5819-b1fa-4b55-ae4d-7cb4da212547';
begin
    if (select count(*) from auth.users
         where id = uid and lower(email) like '%@minghwee.com') <> 1 then
        raise exception 'REFUSED: no Ming Hwee auth account with id %', uid;
    end if;
    if exists (select 1 from public.cb_kb_admin_users where user_id = uid) then
        raise exception 'REFUSED: % already has a cb_kb_admin_users row', uid;
    end if;

    insert into public.cb_kb_admin_users (user_id, role, active)
    values (uid, 'approver', true);

    if (select role from public.cb_kb_admin_users where user_id = uid and active) <> 'approver' then
        raise exception 'REFUSED: row did not read back as an active approver';
    end if;
    raise notice 'cb_kb_admin_users: % added as approver', uid;
end
$$;

commit;
