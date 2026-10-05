# kb_admin rollbacks

**Never run any of these without an explicit go-word from the user.**

They undo `kb_admin_014` → `001`, and must be run in that order, one at a time
(there is no 007 rollback: 007 only checks; 013 is removed by 009's rollback, which
drops its table; 012's rollback is a no-op, because 012 ends in ROLLBACK):

```
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_014_live_match_baseline.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_012_grants_checks.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_011_doc_functions.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_010_doc_owner.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_009_doc_tables.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_008_create_entry.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_006_guards.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_005_editor_login.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_004_functions.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_003_history.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_002_roles.sql
python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_admin_001_reader.sql
```

- **014** puts `kb_admin_doc_publish` back exactly as 011 wrote it and drops
  `kb_admin_match_live`, the baseline helper and policy, and `is_baseline`. It
  refuses while any baseline batch exists: those batches are the only way back
  to a document's imported rows. Changes no live row.
- **012** after 014 is rolled back expects 16 editor functions and will FAIL
  until 012's file is put back to 15 (it is a checks file; its rollback is a no-op).
- **011** drops the nine document functions. It changes no row: anything already
  published stays live. Retire a document first if it should go too.
- **010** refuses until 011 is rolled back. It removes `kb_admin_doc_owner`, its
  grants and policies; `kb_admin_fn_owner` is untouched.
- **009** refuses while any document or batch exists, or any document action is
  in `cb_kb_audit`. It drops the four document tables (the seeded probes go
  with them) and narrows the audit CHECK back to the 008 list.
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

The 012 -> 009 set was dry-run on TEST (2026-10-01) in one transaction that ended in
ROLLBACK, and 009's refusal was proved with a document present.
