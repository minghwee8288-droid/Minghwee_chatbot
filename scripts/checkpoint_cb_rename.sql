-- LangGraph checkpoint tables → cb_ prefix.
--
-- The library hardcodes the unprefixed names; app/graph/checkpointer.py rewrites
-- its SQL to the cb_ ones, so the code and these tables have to move together.
-- Run this with the app STOPPED — a running app writing through the old names
-- mid-rename loses those turns.
--
-- Already applied to the live database (the tables were renamed by hand, then
-- LangGraph's setup() recreated the unprefixed ones empty on the next start,
-- which is what the DROP below clears). Kept for rebuilding an environment.

BEGIN;

-- 1. Carry the existing conversation state over under the new names.
ALTER TABLE IF EXISTS public.checkpoints           RENAME TO cb_checkpoints;
ALTER TABLE IF EXISTS public.checkpoint_blobs      RENAME TO cb_checkpoint_blobs;
ALTER TABLE IF EXISTS public.checkpoint_writes     RENAME TO cb_checkpoint_writes;
ALTER TABLE IF EXISTS public.checkpoint_migrations RENAME TO cb_checkpoint_migrations;

-- 2. Drop the empty tables LangGraph recreated under the old names while the
--    app was still pointed at them. Verify they are empty first:
--      select 'checkpoints' t, count(*) from public.checkpoints
--      union all select 'checkpoint_blobs', count(*) from public.checkpoint_blobs
--      union all select 'checkpoint_writes', count(*) from public.checkpoint_writes;
--    All three must read 0 — any rows there are conversation turns that would be
--    lost, and belong in the cb_ tables instead.
DROP TABLE IF EXISTS public.checkpoint_writes;
DROP TABLE IF EXISTS public.checkpoint_blobs;
DROP TABLE IF EXISTS public.checkpoints;
DROP TABLE IF EXISTS public.checkpoint_migrations;

COMMIT;
