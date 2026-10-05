-- rollback_kb_admin_012_grants_checks: nothing to undo.
--
-- kb_admin_012_grants_checks.sql ends in ROLLBACK: every probe row it inserts
-- is rolled back, and it creates no object, grant or policy. This file exists
-- so the rollback set is complete; running it changes nothing.

select 'kb_admin_012 left nothing behind; nothing to roll back' as note;
