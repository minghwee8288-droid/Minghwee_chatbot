-- cb_complaint_cases_001: a queryable copy of each helper complaint's case fields.
--
-- NOT APPLIED. Apply only on the go-word:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/cb_complaint_cases_001.sql
-- then set COMPLAINT_CASE_TABLE=true and run docker compose up -d.
--
-- The complaint flow does not need this. cb_tickets (with the portal's
-- complaint_level, migration 0133) is the record, and these same fields are on its
-- captured_info, where the portal's drawer shows them. This table is for reports
-- and follow-up lists that want real columns (next_followup_date above all).
--
-- Additive: a new table, one row per complaint ticket. Deleting a ticket deletes
-- its row, so reset_conversation.py and reset-ui need no change. RLS on and nothing
-- for the portal's roles: the bot reaches it with the service role, which bypasses
-- RLS, and the portal does not read it.

begin;

create table if not exists public.cb_complaint_cases (
  ticket_id                uuid primary key references public.cb_tickets (id) on delete cascade,
  tenant_id                uuid,
  conversation_id          bigint,
  complaint_type           text,
  facts_in_her_words       text,
  safe_to_chat             text not null default 'unknown'
    check (safe_to_chat in ('unknown', 'yes', 'no')),
  safe_status              text not null default 'unknown'
    check (safe_status in ('unknown', 'safe', 'not_safe')),
  best_time_to_contact     text,
  consent_employer_contact text not null default 'unknown'
    check (consent_employer_contact in ('unknown', 'yes', 'no')),
  employer_contact_status  text not null default 'none'
    check (employer_contact_status in ('none', 'requested', 'done')),
  next_followup_date       date,
  level_history            jsonb not null default '[]'::jsonb,
  triage_stage             text,
  triage_pending           text,
  triage_answers           jsonb not null default '{}'::jsonb,
  transcript               text,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

create index if not exists cb_complaint_cases_followup_idx
  on public.cb_complaint_cases (next_followup_date);

alter table public.cb_complaint_cases enable row level security;

comment on table public.cb_complaint_cases is
  'Ming Hwee chatbot: queryable copy of helper complaint fields. cb_tickets is the record.';

commit;
