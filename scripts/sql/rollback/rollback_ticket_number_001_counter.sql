-- Rollback of ticket_number_001_counter.sql. NEVER run without an explicit
-- go-word from the user. Afterwards the bot falls back to read-max-and-add-one,
-- which reuses a deleted ticket's number (the defect 001 fixed).
begin;
drop function if exists public.cb_next_ticket_number(integer);
drop table if exists public.cb_ticket_counters;
commit;
