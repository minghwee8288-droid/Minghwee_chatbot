-- cb_tickets.priority: urgent/normal  ->  high/medium/low
--
-- STATUS: COMPLETE. All three phases applied to qizcnyuzgylzoyfvymfo.
-- cb_tkt_priority_check now reads CHECK (priority IN ('high','medium','low'))
-- and no row holds an old value. Kept as the record of the change; there is
-- nothing left to run.
--
-- The sequence mattered, and is worth keeping in mind for the next one of
-- these. Phases 1 and 2 ran first, widening the constraint to accept both
-- vocabularies and rewriting the existing rows; the bot's priority_for() was
-- changed to write 'high'/'medium'; only once a restarted bot had been seen
-- creating CB-2026-0004 as 'medium' did phase 3 narrow the constraint.
--
-- Narrowing it any earlier would have failed silently. create_ticket() in
-- app/services/ticket.py wraps its insert in `except Exception` and returns
-- None, and its caller guards with `if ticket:` — so a rejected insert does
-- NOT raise anywhere the client can see. The handover still happens, the
-- client is told someone will follow up, and no ticket is ever created. The
-- only trace is a line in the log.
--
-- The definition, before any of this:
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint
--    WHERE conrelid = 'cb_tickets'::regclass AND conname = 'cb_tkt_priority_check';


-- ---------------------------------------------------------------------------
-- Phase 1 (APPLIED) — widen the constraint to accept old and new values at
-- once. Safe while the old chatbot was still running: nothing it wrote was
-- rejected. Left commented: re-running it would re-admit the old values.
-- ---------------------------------------------------------------------------

-- ALTER TABLE cb_tickets DROP CONSTRAINT IF EXISTS cb_tkt_priority_check;
--
-- ALTER TABLE cb_tickets
--   ADD CONSTRAINT cb_tkt_priority_check
--   CHECK (priority IN ('high', 'medium', 'low', 'urgent', 'normal'));


-- ---------------------------------------------------------------------------
-- Phase 2 (APPLIED) — migrate the existing rows. 3 rows moved normal→medium,
-- 0 urgent→high. Left runnable: it is idempotent and now matches nothing.
-- 'urgent' is the chatbot's abuse-report flag, so it becomes High; everything
-- else it has ever written is 'normal', which becomes Medium. Nothing becomes
-- Low: no existing ticket was ever triaged down, and inventing that would put
-- a judgement into the data that nobody made.
-- ---------------------------------------------------------------------------

UPDATE cb_tickets SET priority = 'high'   WHERE priority = 'urgent';
UPDATE cb_tickets SET priority = 'medium' WHERE priority = 'normal';

-- Expect zero rows:
--   SELECT priority, count(*) FROM cb_tickets
--    WHERE priority IN ('urgent','normal') GROUP BY priority;


-- ---------------------------------------------------------------------------
-- Phase 3 (APPLIED) — drops the old values. Ran only after a restarted bot had
-- been seen creating CB-2026-0004 as 'medium'. This is the constraint the
-- column carries today; re-running it is a no-op.
-- ---------------------------------------------------------------------------

ALTER TABLE cb_tickets DROP CONSTRAINT IF EXISTS cb_tkt_priority_check;

ALTER TABLE cb_tickets
  ADD CONSTRAINT cb_tkt_priority_check
  CHECK (priority IN ('high', 'medium', 'low'));

-- LEGACY_PRIORITY_LEVEL has since been removed from
-- ticket-system/utils/constants.js; priorityValues() and priorityLevel()
-- remain as the seams a future re-vocabularying would use.
