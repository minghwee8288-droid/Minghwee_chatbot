-- cb_call_context_001: one row per phone call the bot has looked at
-- (app/services/call_context.py, CALL_CONTEXT_ENABLED).        2026-11-04
--
-- Additive: one new cb_* table, nothing else touched. The call itself stays in
-- call_details, which is the portal's; only the analysis lives here, so no
-- transcript is copied. The primary key is what makes "each call once" atomic.
--
-- Deliberately no foreign key to call_details or wp_chat_conversations: those are
-- the portal's, and a reset or a cleanup there must not touch this or be blocked
-- by it.
--
-- The bot reads and writes it with the service role (RLS on, no policies, so the
-- portal's keys see nothing).
--
-- NOT APPLIED. Apply only on the go-word:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/cb_call_context_001.sql
-- Rollback: scripts/sql/rollback/rollback_cb_call_context_001.sql

begin;

create table if not exists public.cb_call_context (
  call_id          text primary key,              -- call_details.call_id
  tenant_id        uuid not null,
  conversation_id  bigint,                        -- null unless matched
  matched_by       text,                          -- phone | phone+call_ticket
  match_status     text not null
    check (match_status in ('processing', 'matched', 'unmatched', 'internal',
                            'no_transcript', 'no_content', 'error', 'cleared')),
  match_reason     text,                          -- why unmatched: never a number
  speakers         jsonb,                         -- {"Speaker 1": "agent", ...}
  analysis         jsonb,                         -- no transcript; short quotes only
  called_at        timestamptz,
  claimed_at       timestamptz not null default now(),
  processed_at     timestamptz,                   -- null while it may still be retried
  mentioned_at     timestamptz,                   -- Claire has referred to this call
  attempts         integer not null default 1,
  model            text,
  error            text                           -- an exception's type, never its text
);

create index if not exists cb_call_context_conversation_idx
  on public.cb_call_context (conversation_id, called_at desc)
  where conversation_id is not null;

alter table public.cb_call_context enable row level security;
revoke all on public.cb_call_context from anon, authenticated;

do $$
begin
  if to_regclass('public.cb_call_context') is null then
    raise exception 'cb_call_context_001: table was not created';
  end if;
  raise notice 'cb_call_context_001: cb_call_context ready - PASS';
end $$;

commit;
