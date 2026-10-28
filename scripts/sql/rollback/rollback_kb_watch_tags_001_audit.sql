-- Rollback of kb_watch_tags_001_audit.sql: the 13 rows back to the tags they
-- carried on 2026-10-28 (all general / all / all). Only rows that still carry
-- the tags kb_watch_tags_001 set are changed; otherwise nothing is.
-- NEVER run without an explicit go-word from the user.
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/rollback/rollback_kb_watch_tags_001_audit.sql

begin;

do $$
declare
  changed int;
begin
  create temporary table kb_watch_tags_001_back (
    id uuid primary key, cur_service text, cur_audience text, cur_nationality text
  ) on commit drop;

  insert into kb_watch_tags_001_back values
    ('669b6d2b-57ed-405f-828d-2b3de6d223c6', 'transfer',   'candidate', 'PH'),
    ('1e63cc61-d361-4f97-980c-a940bcca39cf', 'home_leave', 'candidate', 'PH'),
    ('15cedbab-13db-45d0-bc7f-a97686d892bf', 'home_leave', 'candidate', 'PH'),
    ('f0c064a6-6964-4b17-ba88-556689aabffb', 'home_leave', 'candidate', 'PH'),
    ('86900097-217c-428e-9822-a58da57d3700', 'home_leave', 'candidate', 'PH'),
    ('f9c0798c-4446-4f32-8f50-c1a63709d3b9', 'transfer',   'all',       'all'),
    ('29040867-68e9-477c-89c9-0c9207f3a6a5', 'transfer',   'all',       'all'),
    ('dc7b49a8-f889-42f9-adb1-8fb26001d172', 'transfer',   'employer',  'all'),
    ('08ce241a-50fd-49c3-9640-130918f3d830', 'general',    'employer',  'all'),
    ('e8b0e590-4cf8-472e-b9da-50bce12afea5', 'general',    'employer',  'all'),
    ('ff9e3cfd-fdb0-4a9e-85bc-aa59cf80650a', 'general',    'employer',  'all'),
    ('05e96436-df8d-4e04-8025-2ad680673fc5', 'general',    'employer',  'all'),
    ('3bfcd3c8-796f-4866-a1bc-08ffec0e61bc', 'general',    'employer',  'all');

  update cb_knowledge_base_updated k
     set service_type = 'general', contact_type = 'all', nationality = 'all'
    from kb_watch_tags_001_back t
   where k.id = t.id
     and k.service_type = t.cur_service
     and k.contact_type = t.cur_audience
     and k.nationality = t.cur_nationality;
  get diagnostics changed = row_count;
  if changed <> 13 then
    raise exception 'rollback kb_watch_tags_001: % of 13 rows carried the 001 tags - nothing changed', changed;
  end if;
  raise notice 'rollback kb_watch_tags_001: 13 of 13 rows back to general/all/all - PASS';
end $$;

commit;
