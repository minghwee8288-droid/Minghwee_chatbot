-- cb_tickets.created_lead_id: survive the lead row going away.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f scripts/ticket_lead_fk_set_null.sql
-- Idempotent. Safe to run with the app up: it takes a brief ACCESS EXCLUSIVE
-- lock on cb_tickets to swap the constraint, and re-validating it scans the
-- table (three rows today, so this is instant).
--
-- WHY
--
-- cb_tickets_created_lead_id_fkey was created with the default NO ACTION, so a
-- lead row cannot be deleted while a ticket points at it — and, worse, the graph
-- checkpoint keeps created_lead_id for the life of the conversation thread. A
-- lead deleted by hand in the Supabase editor therefore breaks ticket creation
-- on that conversation permanently: every insert violates the constraint, the
-- exception is swallowed in ticket_service.create(), and the handover is logged
-- with ticket_id NULL.
--
-- Live, 2026-09-01: conversation 36 (+6587533650) raised TEN ticket_raised
-- handovers between 04:29 and 04:51 UTC, every one with ticket_id NULL and no
-- ticket behind it, because L-2026-0001 had been removed from `leads`. The
-- client had answered all twenty qualification questions. Nothing reached an
-- agent.
--
-- SET NULL is the right resolution in both directions: the ticket is the work
-- item and must survive, while the lead link is a convenience. app/services/
-- ticket.py now also retries the insert without the link if it still fails, so
-- this migration and that retry are belt and braces — the migration prevents the
-- breakage, the retry recovers conversations whose checkpoints already carry a
-- dead id.

begin;

alter table public.cb_tickets
    drop constraint if exists cb_tickets_created_lead_id_fkey;

alter table public.cb_tickets
    add constraint cb_tickets_created_lead_id_fkey
    foreign key (created_lead_id)
    references public.leads (id)
    on delete set null;

commit;

-- Verify — expect: FOREIGN KEY (created_lead_id) REFERENCES leads(id) ON DELETE SET NULL
--
-- select pg_get_constraintdef(con.oid)
-- from pg_constraint con
-- join pg_class rel on rel.oid = con.conrelid
-- join pg_namespace n on n.oid = rel.relnamespace
-- where n.nspname = 'public'
--   and rel.relname = 'cb_tickets'
--   and con.conname = 'cb_tickets_created_lead_id_fkey';
--
-- Tickets whose lead has since been deleted (should be none today):
--
-- select ticket_number, conversation_id, created_lead_id
-- from public.cb_tickets t
-- where t.created_lead_id is not null
--   and not exists (select 1 from public.leads l where l.id = t.created_lead_id);
