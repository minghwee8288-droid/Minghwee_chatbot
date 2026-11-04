-- Rollback of recovery_001_claims.sql. NEVER run without an explicit go-word from
-- the user. Switch STARTUP_RECOVERY_SCAN off first: with the table gone no claim
-- can succeed, so the scan sends nothing, but it logs an error per conversation.
begin;
drop table if exists public.cb_recovery_claims;
commit;
