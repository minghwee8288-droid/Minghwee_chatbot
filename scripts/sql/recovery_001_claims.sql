-- recovery_001: cb_recovery_claims, one row per message the startup scan has
-- taken responsibility for answering (app/services/recovery.py).    2026-11-04
--
-- Additive: one new cb_* table, nothing else touched. The primary key is what
-- makes a claim atomic: two scans cannot both win the same message.
-- wp_chat_messages cannot do this job, because the portal writes the same rows.
--
-- Deliberately no foreign key to wp_chat_messages or wp_chat_conversations: those
-- are the portal's tables, and a reset deleting a message must not touch this or
-- be blocked by it. A Whapi id is never reused, so a leftover row does no harm.
--
-- The bot reads and writes it with the service role (RLS on, no policies, so the
-- portal's keys see nothing).
--
-- NOT APPLIED. Apply only on the go-word:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/recovery_001_claims.sql
-- Rollback: scripts/sql/rollback/rollback_recovery_001_claims.sql

begin;

create table if not exists public.cb_recovery_claims (
  whapi_message_id  text primary key,
  conversation_id   bigint not null,
  claimed_at        timestamptz not null default now()
);

alter table public.cb_recovery_claims enable row level security;
revoke all on public.cb_recovery_claims from anon, authenticated;

do $$
begin
  if to_regclass('public.cb_recovery_claims') is null then
    raise exception 'recovery_001: table was not created';
  end if;
  raise notice 'recovery_001: cb_recovery_claims ready - PASS';
end $$;

commit;
