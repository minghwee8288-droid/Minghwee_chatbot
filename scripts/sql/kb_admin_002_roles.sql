-- kb_admin_002_roles: allow 'editor' and 'approver' beside 'viewer'.
--
--   viewer   - reads (Phase 1, unchanged)
--   editor   - saves drafts; publishes WORDING-ONLY changes
--   approver - everything an editor can do, plus publishing changes that touch
--              a number, the routing (service / audience / nationality) or
--              on/off; may approve their own change (logged self_approved)
--
-- This WIDENS the check only. No existing row is changed: every row today is
-- 'viewer' (1 row, read 2026-09-30), which both the old and the new check
-- accept. Granting someone editor/approver is a separate, reviewed UPDATE of
-- cb_kb_admin_users - kb-admin still cannot change the access list.
--
-- Idempotent: if the constraint already has the wide definition, nothing is
-- dropped or added.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
declare
    current_def text;
begin
    select pg_get_constraintdef(oid) into current_def
      from pg_constraint
     where conrelid = 'public.cb_kb_admin_users'::regclass
       and conname = 'cb_kb_admin_users_role_check';

    if current_def is not null
       and current_def like '%viewer%' and current_def like '%editor%'
       and current_def like '%approver%' then
        raise notice 'cb_kb_admin_users_role_check already allows viewer/editor/approver - unchanged';
        return;
    end if;

    if current_def is not null then
        alter table public.cb_kb_admin_users drop constraint cb_kb_admin_users_role_check;
    end if;

    alter table public.cb_kb_admin_users
        add constraint cb_kb_admin_users_role_check
        check (role in ('viewer', 'editor', 'approver'));
    raise notice 'cb_kb_admin_users_role_check widened to viewer/editor/approver';
end
$$;

commit;
