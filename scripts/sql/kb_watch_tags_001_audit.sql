-- kb_watch_tags_001: service / audience / nationality for the KB-watcher
-- entries published since 2026-10-16 that went live mis-tagged.   2026-10-28
--
-- WHY: until now the watcher proposed every NEW entry as general/all/all
-- (kb-admin passed no tags to kb_admin_watch_propose), so an entry about one
-- procedure for one nationality was in EVERY service's search. Live,
-- conversation 3766 (28 Oct, 6:31 PM SGT): "What documents do I need to hire a
-- Filipino helper?" was answered with the MWO contract-verification documents
-- (669b6d2b, general/all/all, 0.729), ahead of the new-hire document rows.
--
-- Audit, read-only, of all 41 published watcher drafts (cb_kb_drafts) against
-- their live rows: 13 change below. Every other row keeps its tags - the edits
-- of existing entries carry that entry's own (already reviewed) tags, and the
-- remaining new MOM entries (accommodation, weekly rest day) are rules either
-- side of the desk asks about. No ICA entry has been published yet.
--
--   MWO Singapore (Philippine Migrant Workers Office) - written TO the worker:
--     669b6d2b contract verification documents      -> transfer   / candidate / PH
--     1e63cc61 verification before returning home   -> home_leave / candidate / PH
--     15cedbab no verified contract (Balik-Mangg.)  -> home_leave / candidate / PH
--     f0c064a6 first-time OFW, OEC                  -> home_leave / candidate / PH
--     86900097 OEC after changing employers         -> home_leave / candidate / PH
--   MOM rules:
--     f9c0798c transfer: new WP issued by           -> transfer   / all
--     29040867 EA: no new employer before Special Pass expiry -> transfer / all
--     dc7b49a8 authorisations to transfer via an EA -> transfer   / employer
--     08ce241a late levy payment                    -> general    / employer
--     e8b0e590 cancel WP after she has left         -> general    / employer
--     ff9e3cfd asking for her medical results       -> general    / employer
--     05e96436 child under 7, measles immunity      -> general    / employer
--     3bfcd3c8 WP application processing time      -> general    / employer
--
-- Contract verification is filed under transfer: at MWO Singapore it is done
-- when a Filipino helper starts with a new employer here. 'transfer' is also
-- what an employer's transfer searches (transfer_employer is aliased to it),
-- and the candidate audience keeps it out of an employer's answers.
--
-- NOT APPLIED. Apply only on the go-word:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/kb_watch_tags_001_audit.sql
-- Rollback: scripts/sql/rollback/rollback_kb_watch_tags_001_audit.sql
--
-- Each row changes only if it still carries the tags read on 2026-10-28;
-- otherwise the whole file stops and changes nothing. Side effects, by design:
-- the updated_at trigger stamps the 13 rows and the external-write trigger
-- (kb_admin_006) records each as external_* in cb_kb_audit. No text, embedding
-- or other column changes. Re-running it after it applied stops on the guard.

begin;

do $$
declare
  changed int;
begin
  create temporary table kb_watch_tags_001 (
    id uuid primary key,
    old_service text, old_audience text, old_nationality text,
    new_service text, new_audience text, new_nationality text
  ) on commit drop;

  insert into kb_watch_tags_001 values
    ('669b6d2b-57ed-405f-828d-2b3de6d223c6', 'general', 'all', 'all', 'transfer',   'candidate', 'PH'),
    ('1e63cc61-d361-4f97-980c-a940bcca39cf', 'general', 'all', 'all', 'home_leave', 'candidate', 'PH'),
    ('15cedbab-13db-45d0-bc7f-a97686d892bf', 'general', 'all', 'all', 'home_leave', 'candidate', 'PH'),
    ('f0c064a6-6964-4b17-ba88-556689aabffb', 'general', 'all', 'all', 'home_leave', 'candidate', 'PH'),
    ('86900097-217c-428e-9822-a58da57d3700', 'general', 'all', 'all', 'home_leave', 'candidate', 'PH'),
    ('f9c0798c-4446-4f32-8f50-c1a63709d3b9', 'general', 'all', 'all', 'transfer',   'all',       'all'),
    ('29040867-68e9-477c-89c9-0c9207f3a6a5', 'general', 'all', 'all', 'transfer',   'all',       'all'),
    ('dc7b49a8-f889-42f9-adb1-8fb26001d172', 'general', 'all', 'all', 'transfer',   'employer',  'all'),
    ('08ce241a-50fd-49c3-9640-130918f3d830', 'general', 'all', 'all', 'general',    'employer',  'all'),
    ('e8b0e590-4cf8-472e-b9da-50bce12afea5', 'general', 'all', 'all', 'general',    'employer',  'all'),
    ('ff9e3cfd-fdb0-4a9e-85bc-aa59cf80650a', 'general', 'all', 'all', 'general',    'employer',  'all'),
    ('05e96436-df8d-4e04-8025-2ad680673fc5', 'general', 'all', 'all', 'general',    'employer',  'all'),
    ('3bfcd3c8-796f-4866-a1bc-08ffec0e61bc', 'general', 'all', 'all', 'general',    'employer',  'all');

  update cb_knowledge_base_updated k
     set service_type = t.new_service, contact_type = t.new_audience, nationality = t.new_nationality
    from kb_watch_tags_001 t
   where k.id = t.id
     and k.service_type = t.old_service
     and k.contact_type = t.old_audience
     and k.nationality = t.old_nationality;
  get diagnostics changed = row_count;
  if changed <> 13 then
    raise exception 'kb_watch_tags_001: % of 13 rows still carried the tags read on 2026-10-28 - nothing changed', changed;
  end if;
  raise notice 'kb_watch_tags_001: 13 of 13 rows re-tagged - PASS';
end $$;

commit;
