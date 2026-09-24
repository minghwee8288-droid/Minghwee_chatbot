-- Ownership, so the loader and the KB Admin UI never overwrite each other.
--
-- Every row that exists today is the developer's: written by
-- scripts/load_service_notes.py or by the original bulk import, and corrected
-- only through the loader's UPDATES / TEXT_REPLACEMENTS / RETIRED. Tag them
-- all metadata.managed_by = 'loader'. The UI will write 'ui', and the loader
-- skips any row tagged 'ui' (reports it, never touches it).
--
-- MERGED into the existing metadata with ||, never replacing it, and only on
-- rows that carry no managed_by yet - so a re-run is a no-op and a row the UI
-- has already claimed is never re-tagged. No column is added or changed.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

update public.cb_knowledge_base_updated
   set metadata = coalesce(metadata, '{}'::jsonb) || '{"managed_by": "loader"}'::jsonb
 where metadata is null
    or not (metadata ? 'managed_by');

commit;
