-- =============================================================================
-- ROLLBACK of kb_admin_017_watcher_sources.sql.
--
-- Drops the four 017 functions (kb_admin_source_set_render,
-- kb_admin_source_set_unverified, kb_admin_source_add_unverified,
-- kb_admin_source_record_draft), drops the six 017 columns, puts 015's address
-- and status CHECKs back, and narrows cb_kb_audit's action CHECK back to the
-- 016 list.
--
-- REFUSES (and changes nothing) while cb_kb_audit holds a 017 action
-- ('source_options', 'source_unverified', 'source_draft_failed'), or a source
-- is rendered, unverified or http://, or a snapshot is marked draft_failed:
-- the audit table is append-only and the narrower CHECKs would fail. So once
-- 017 has been used this rollback is refused by design; switch the watcher
-- off instead.
--
-- Changes NO row of cb_knowledge_base_updated.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if exists (select 1 from public.cb_kb_audit where action in ('source_options', 'source_unverified', 'source_draft_failed')) then
        raise exception 'ROLLBACK 017 REFUSED: cb_kb_audit holds 017 actions';
    end if;
    if exists (select 1 from information_schema.columns where table_schema = 'public'
                and table_name = 'cb_kb_sources' and column_name = 'allow_unverified') then
        if exists (select 1 from public.cb_kb_sources where render or allow_unverified or url !~ '^https://') then
            raise exception 'ROLLBACK 017 REFUSED: a source is rendered, unverified or http://';
        end if;
        if exists (select 1 from public.cb_kb_source_snapshots where draft_failed) then
            raise exception 'ROLLBACK 017 REFUSED: a snapshot is marked as a failed draft';
        end if;
    end if;
end
$$;

drop function if exists public.kb_admin_source_set_render(uuid, text, uuid, boolean);
drop function if exists public.kb_admin_source_set_unverified(uuid, text, uuid, boolean, text);
drop function if exists public.kb_admin_source_add_unverified(uuid, text, text, text, text, text);
drop function if exists public.kb_admin_source_record_draft(uuid, text, uuid, boolean, text);

alter table public.cb_kb_sources drop constraint if exists cb_kb_sources_unverified_check;
alter table public.cb_kb_source_snapshots drop constraint if exists cb_kb_source_snapshots_draft_check;
alter table public.cb_kb_sources drop constraint if exists cb_kb_sources_url_check;
alter table public.cb_kb_sources add constraint cb_kb_sources_url_check check (
    url ~ '^https://[a-z0-9.-]+\.[a-z]{2,}(:[0-9]{2,5})?(/[^[:space:]]*)?$' and length(url) <= 2000);
alter table public.cb_kb_sources drop constraint if exists cb_kb_sources_status_check;
alter table public.cb_kb_sources add constraint cb_kb_sources_status_check check (last_status is null or last_status in
    ('first', 'changed', 'unchanged', 'failed'));

alter table public.cb_kb_sources drop column if exists render;
alter table public.cb_kb_sources drop column if exists allow_unverified;
alter table public.cb_kb_sources drop column if exists unverified_reason;
alter table public.cb_kb_sources drop column if exists unverified_set_by_email;
alter table public.cb_kb_sources drop column if exists unverified_set_at;
alter table public.cb_kb_source_snapshots drop column if exists draft_failed;
alter table public.cb_kb_source_snapshots drop column if exists draft_error;

alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
    'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
    'entry_created',
    'doc_uploaded', 'batch_prepared', 'batch_published', 'batch_restored',
    'batch_discarded', 'doc_retired', 'chunk_edited',
    'source_added', 'source_removed', 'source_fetched', 'source_fetch_failed',
    'watch_proposed', 'watch_edited', 'watch_rejected', 'watch_approved', 'watch_published',
    'source_redrafted', 'source_suggested',
    'external_insert', 'external_update', 'external_delete'
));

do $$
begin
    if exists (select 1 from pg_proc where proname in ('kb_admin_source_set_render', 'kb_admin_source_set_unverified',
                                                       'kb_admin_source_add_unverified', 'kb_admin_source_record_draft')) then
        raise exception 'FAIL: a 017 function is still there';
    end if;
    raise notice 'PASS: 017 rolled back - 016 state';
end
$$;

commit;
