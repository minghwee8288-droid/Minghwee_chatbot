-- internal_contacts_001: cb_internal_contacts, the owner and staff numbers the
-- bot never replies to (app/services/internal_contacts.py).        2026-10-23
--
-- Additive: one new cb_* table, nothing else touched. The bot reads it with the
-- service role (RLS on, no policies, so the portal's keys see nothing). Until
-- this is applied the bot uses INTERNAL_NUMBERS from .env alone, and a failed
-- read of the table never empties the list.
--
-- Rows are added by a separate reviewed SQL file or by hand on the go-word.
-- Numbers are stored as E.164 ('+65...'); none is written here.
--
-- NOT APPLIED. Apply only on the go-word:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/internal_contacts_001_create.sql
-- Rollback: scripts/sql/rollback/rollback_internal_contacts_001_create.sql

begin;

create table if not exists public.cb_internal_contacts (
  id          uuid primary key default gen_random_uuid(),
  phone       text not null unique check (phone ~ '^\+[0-9]{8,15}$'),
  label       text,
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

alter table public.cb_internal_contacts enable row level security;
revoke all on public.cb_internal_contacts from anon, authenticated;

do $$
begin
  if to_regclass('public.cb_internal_contacts') is null then
    raise exception 'internal_contacts_001: table was not created';
  end if;
  raise notice 'internal_contacts_001: cb_internal_contacts ready - PASS';
end $$;

commit;
