-- Rollback of replaced_threads_001_create.sql. NEVER run without an explicit go-word
-- from the user. Set RECORD_REPLACED_THREADS=false first: with the table gone every
-- replaced thread logs an error, though the client's turn carries on regardless.
-- Dropping it loses only the record: no checkpoint is touched.
begin;
drop table if exists public.cb_replaced_threads;
commit;
