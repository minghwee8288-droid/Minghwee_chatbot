-- ROLLBACK of kb_admin_015_watcher.sql (the knowledge-base watcher).
--
-- Drops the eight watcher functions and the three helpers, the three tables
-- (cb_kb_drafts, cb_kb_source_snapshots, cb_kb_sources), the extra column
-- grant 015 gave kb_admin_fn_owner (SELECT source_document on the live table),
-- and narrows cb_kb_audit's action CHECK back to the 009 list.
--
-- REFUSES (and changes nothing) while:
--   * any source, snapshot or draft exists (that history would be lost);
--   * cb_kb_audit holds any watcher action (the table is append-only, and
--     narrowing the CHECK would fail on such a row).
-- So on a database where the watcher has been used, this rollback is refused
-- by design; switch the watcher off instead (KB_SOURCES_ENABLED / WATCHER_ENABLED
-- unset in kb-admin's environment).
--
-- Changes NO row of cb_knowledge_base_updated. An entry a watcher draft
-- became is an ordinary kb-admin entry and is restored or switched off in
-- kb-admin like any other.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    -- Nested, not "and": a reference to a missing table fails at parse time.
    if to_regclass('public.cb_kb_sources') is not null then
        if exists (select 1 from public.cb_kb_sources) then
            raise exception 'ROLLBACK 015 REFUSED: sources exist';
        end if;
    end if;
    if to_regclass('public.cb_kb_source_snapshots') is not null then
        if exists (select 1 from public.cb_kb_source_snapshots) then
            raise exception 'ROLLBACK 015 REFUSED: snapshots exist';
        end if;
    end if;
    if to_regclass('public.cb_kb_drafts') is not null then
        if exists (select 1 from public.cb_kb_drafts) then
            raise exception 'ROLLBACK 015 REFUSED: drafts exist';
        end if;
    end if;
    if exists (select 1 from public.cb_kb_audit
                where action in ('source_added', 'source_removed', 'source_fetched', 'source_fetch_failed',
                                 'watch_proposed', 'watch_edited', 'watch_rejected', 'watch_approved',
                                 'watch_published')) then
        raise exception 'ROLLBACK 015 REFUSED: cb_kb_audit holds watcher actions';
    end if;
end
$$;

drop function if exists public.kb_admin_watch_mark_published(uuid, text, uuid);
drop function if exists public.kb_admin_watch_approve(uuid, text, uuid, text, boolean);
drop function if exists public.kb_admin_watch_reject(uuid, text, uuid, text);
drop function if exists public.kb_admin_watch_edit(uuid, text, uuid, text, text, text, text);
drop function if exists public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text);
drop function if exists public.kb_admin_source_record_fetch(uuid, text, uuid, boolean, integer, text, text, text, text);
drop function if exists public.kb_admin_source_remove(uuid, text, uuid, text);
drop function if exists public.kb_admin_source_add(uuid, text, text, text, text);
drop function if exists public.kb_admin__watch_fee_lock(text, text, text, text, text);
drop function if exists public.kb_admin__watch_money(text, text, text, text);
drop function if exists public.kb_admin__watch_actor(uuid);

drop table if exists public.cb_kb_drafts;
drop table if exists public.cb_kb_source_snapshots;
drop table if exists public.cb_kb_sources;

revoke select (source_document) on public.cb_knowledge_base_updated from kb_admin_fn_owner;

alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
    'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
    'entry_created',
    'doc_uploaded', 'batch_prepared', 'batch_published', 'batch_restored',
    'batch_discarded', 'doc_retired', 'chunk_edited',
    'external_insert', 'external_update', 'external_delete'
));

do $$
begin
    if to_regclass('public.cb_kb_sources') is not null or to_regclass('public.cb_kb_drafts') is not null
       or to_regclass('public.cb_kb_source_snapshots') is not null then
        raise exception 'FAIL: a watcher table survived the rollback';
    end if;
    if exists (select 1 from pg_proc where proname like 'kb\_admin\_watch\_%' or proname like 'kb\_admin\_source\_%') then
        raise exception 'FAIL: a watcher function survived the rollback';
    end if;
    raise notice 'PASS: rollback 015 - watcher tables and functions gone, audit CHECK back to the 009 list';
end
$$;

commit;
