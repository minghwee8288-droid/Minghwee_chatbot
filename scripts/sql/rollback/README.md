# kb_admin rollbacks

**Never run any of these without an explicit go-word from the user.**

They undo `kb_admin_008` → `001`, and must be run in that order, one at a time
(there is no 007 rollback: 007 only checks):

```
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_008_create_entry.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_006_guards.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_005_editor_login.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_004_functions.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_003_history.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_002_roles.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_001_reader.sql
```

- **008** refuses while any entry created in kb-admin (`source_document = 'Ming Hwee KB Admin'`) or any
  `entry_created` audit row exists - neither can be removed by a rollback.
- **004** refuses until 005 and 006 are rolled back.
- **003** destroys the edit history. It refuses while any history exists, until the history
  is exported and one line in the file is uncommented. Edits already published stay in
  the live table; this rollback does not undo them.
- **002** refuses while any `editor` or `approver` access row exists.
- **001** changes nothing: Phase 1 predates these files. It only verifies, read-only, that
  Phase 1 is intact and nothing from Phase 2 is left.
- `007` only checks, so it has no rollback.

All six were dry-run on TEST (2026-09-30) in one transaction that ended in ROLLBACK.
