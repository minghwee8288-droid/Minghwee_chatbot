-- replaced_threads_001: cb_replaced_threads, one row per LangGraph thread the bot has
-- stopped using on a conversation, so the 14-day pruning rule can find it.  2026-11-04
--
-- Additive: one new cb_* table, nothing else touched. Nothing deletes checkpoints yet:
-- this only records (CLAUDE.md section 9.50).
--
-- Not a column on wp_chat_conversations, which is the portal's. Deliberately no foreign
-- key to it either: 3 of the 6 replaced threads already on production belong to
-- conversations that no longer exist, and a portal delete must not touch this or be
-- blocked by it.
--
-- The bot writes it with the service role (RLS on, no policies, so the portal's keys see
-- nothing).
--
-- NOT APPLIED. Apply only on the go-word:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/replaced_threads_001_create.sql
-- Rollback: scripts/sql/rollback/rollback_replaced_threads_001_create.sql
-- Then RECORD_REPLACED_THREADS=true in .env, then scripts/backfill_replaced_threads.py.

begin;

create table if not exists public.cb_replaced_threads (
  thread_id        text primary key,
  tenant_id        uuid,
  conversation_id  bigint not null,
  replaced_at      timestamptz not null default now(),
  reason           text not null
);

create index if not exists cb_replaced_threads_replaced_at_idx
  on public.cb_replaced_threads (replaced_at);

alter table public.cb_replaced_threads enable row level security;
revoke all on public.cb_replaced_threads from anon, authenticated;

do $$
begin
  if to_regclass('public.cb_replaced_threads') is null then
    raise exception 'replaced_threads_001: table was not created';
  end if;
  raise notice 'replaced_threads_001: cb_replaced_threads ready - PASS';
end $$;

commit;
