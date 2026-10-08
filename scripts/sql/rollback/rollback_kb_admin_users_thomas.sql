-- ROLLBACK of kb_admin_users_thomas.sql.
--
-- Switches that one access row off (active = false). Does not delete it, and
-- touches no other row: kb-admin refuses an inactive row on the next request.
--
-- REFUSES (and changes nothing) if the row does not exist.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
declare
    uid constant uuid := '4d1e5819-b1fa-4b55-ae4d-7cb4da212547';
    n   int;
begin
    update public.cb_kb_admin_users set active = false where user_id = uid;
    get diagnostics n = row_count;
    if n <> 1 then
        raise exception 'ROLLBACK REFUSED: expected 1 cb_kb_admin_users row for %, found %', uid, n;
    end if;
    raise notice 'cb_kb_admin_users: % switched off', uid;
end
$$;

commit;
