-- Rollback of cb_call_context_001.sql. NEVER run without an explicit go-word from
-- the user. Switch CALL_CONTEXT_ENABLED off first: with the table gone no call can
-- be claimed, so nothing is analysed, but it logs an error on every run.
begin;
drop table if exists public.cb_call_context;
commit;
