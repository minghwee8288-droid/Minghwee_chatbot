-- kb_admin_017_watcher_sources.sql - the KB watcher reads all five client sites (2026-10-16).
--
-- The five sites the agency named (MOM, the Philippine, Indonesian and
-- Myanmar embassies, ICA) were probed with the watcher's own fetcher. Three
-- things stood between the watcher and two of them, and one thing lost work:
--
--   1. RENDER (per source). The Indonesian embassy (kemlu.go.id) is a
--      JavaScript app: the plain fetch reads 0 characters. A source can now be
--      marked "render" (cb_kb_sources.render), and kb-admin then reads it
--      through the page renderer (render-service/). An editor sets it
--      (kb_admin_source_set_render, audit 'source_options').
--   2. UNVERIFIED (per source, APPROVER only, with a reason). The Myanmar
--      embassy's https certificate is self-signed and issued for another site,
--      and its https address serves a different site; its pages are only on
--      plain http. A source may be marked allow_unverified - then its address
--      may be http://, or https:// read without a certificate check - for that
--      source's own host only, never globally. Only an approver sets it, with
--      a reason (kb_admin_source_add_unverified, kb_admin_source_set_unverified;
--      audit 'source_unverified'). The address CHECK allows http:// only on
--      such a row.
--   3. A FAILED DRAFT IS RETRIED. Until now a scheduled run that recorded a
--      changed snapshot and then failed to draft (the model, the embedding or
--      the live search) left that snapshot as the last good one, so the next
--      run saw "unchanged" and the change was never proposed. A snapshot can
--      now be marked draft_failed (kb_admin_source_record_draft, audit
--      'source_draft_failed'); kb-admin diffs against the last snapshot that
--      was NOT a failed draft and retries on the next run. The page shows
--      "Drafting failed" with the reason (last_status 'draft_failed').
--
-- Additive only: six new columns on two watcher tables (015's), two CHECKs
-- widened (the address, the status), four new functions, three new audit
-- actions. No row of any table is changed; the live knowledge-base table is
-- not touched. The bot reads none of this. kb_admin_editor then executes 31
-- functions (27 + 4).
--
-- Needs 015 and 016. Re-runnable. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/kb_admin_017_watcher_sources.sql
-- Rollback: scripts/sql/rollback/rollback_kb_admin_017_watcher_sources.sql
-- (never run without an explicit go-word).

begin;

do $$
begin
    if to_regclass('public.cb_kb_drafts') is null then
        raise exception 'FAIL: apply kb_admin_015_watcher before 017';
    end if;
    if not exists (select 1 from information_schema.columns where table_schema = 'public'
                     and table_name = 'cb_kb_sources' and column_name = 'suggested_pages') then
        raise exception 'FAIL: apply kb_admin_016_watcher_overlap before 017';
    end if;
end
$$;

-- =============================================================================
-- 1. Columns (watcher tables only)
-- =============================================================================
alter table public.cb_kb_sources add column if not exists render boolean not null default false;
alter table public.cb_kb_sources add column if not exists allow_unverified boolean not null default false;
alter table public.cb_kb_sources add column if not exists unverified_reason text;
alter table public.cb_kb_sources add column if not exists unverified_set_by_email text;
alter table public.cb_kb_sources add column if not exists unverified_set_at timestamptz;
alter table public.cb_kb_source_snapshots add column if not exists draft_failed boolean not null default false;
alter table public.cb_kb_source_snapshots add column if not exists draft_error text;

do $$
begin
    -- http:// only on a source an approver marked unverified.
    alter table public.cb_kb_sources drop constraint if exists cb_kb_sources_url_check;
    alter table public.cb_kb_sources add constraint cb_kb_sources_url_check check (
        url ~ '^https?://[a-z0-9.-]+\.[a-z]{2,}(:[0-9]{2,5})?(/[^[:space:]]*)?$'
        and length(url) <= 2000
        and (url ~ '^https://' or allow_unverified));
    alter table public.cb_kb_sources drop constraint if exists cb_kb_sources_status_check;
    alter table public.cb_kb_sources add constraint cb_kb_sources_status_check check (last_status is null or last_status in
        ('first', 'changed', 'unchanged', 'failed', 'draft_failed'));
    if not exists (select 1 from pg_constraint where conname = 'cb_kb_sources_unverified_check') then
        alter table public.cb_kb_sources add constraint cb_kb_sources_unverified_check
            check (not allow_unverified or length(btrim(coalesce(unverified_reason, ''))) between 1 and 500);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'cb_kb_source_snapshots_draft_check') then
        alter table public.cb_kb_source_snapshots add constraint cb_kb_source_snapshots_draft_check
            check (not draft_failed or fetch_ok);
    end if;
end
$$;

grant update (render, allow_unverified, unverified_reason, unverified_set_by_email, unverified_set_at)
    on public.cb_kb_sources to kb_admin_fn_owner;
grant update (draft_failed, draft_error) on public.cb_kb_source_snapshots to kb_admin_fn_owner;

-- =============================================================================
-- 2. Audit: three more watcher actions
-- =============================================================================
do $$
declare
    current_def text;
begin
    select pg_get_constraintdef(oid) into current_def
      from pg_constraint
     where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check';
    if current_def like '%source_draft_failed%' then
        raise notice 'cb_kb_audit_action_check already allows the 017 actions - unchanged';
        return;
    end if;
    if current_def not like '%source_redrafted%' then
        raise exception 'FAIL: apply kb_admin_016 before 017 (audit CHECK has no 016 actions)';
    end if;
    alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
    alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
        'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
        'entry_created',
        'doc_uploaded', 'batch_prepared', 'batch_published', 'batch_restored',
        'batch_discarded', 'doc_retired', 'chunk_edited',
        'source_added', 'source_removed', 'source_fetched', 'source_fetch_failed',
        'watch_proposed', 'watch_edited', 'watch_rejected', 'watch_approved', 'watch_published',
        'source_redrafted', 'source_suggested',
        'source_options', 'source_unverified', 'source_draft_failed',
        'external_insert', 'external_update', 'external_delete'
    ));
end
$$;

-- =============================================================================
-- 3. Functions
-- =============================================================================

-- 3.1 Read this page through the page renderer, or not. Editor.
create or replace function public.kb_admin_source_set_render(
    p_actor_id uuid, p_actor_email text, p_source_id uuid, p_render boolean)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_src public.cb_kb_sources;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if p_render is null then
        raise exception 'say whether the page is rendered' using errcode = 'KB002';
    end if;
    select * into v_src from public.cb_kb_sources where id = p_source_id for update;
    if v_src.id is null or not v_src.active then
        raise exception 'no such source is being watched' using errcode = 'KB003';
    end if;
    if p_render and v_src.url !~ '^https://' then
        raise exception 'the page renderer reads https:// pages only' using errcode = 'KB002';
    end if;
    if v_src.render is distinct from p_render then
        update public.cb_kb_sources set render = p_render where id = p_source_id;
        insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
        values ('source_options', p_actor_id, p_actor_email,
                jsonb_build_object('source_id', p_source_id, 'render', v_src.render),
                jsonb_build_object('source_id', p_source_id, 'render', p_render),
                case when p_render then 'read through the page renderer' else 'read with a plain fetch' end);
    end if;
    return p_render;
end;
$$;

-- 3.2 Allow (or stop allowing) an unverified connection to this source's own
-- host: APPROVER only, with a reason. An http:// source cannot be switched
-- back (it could not be read at all) - stop watching it instead.
create or replace function public.kb_admin_source_set_unverified(
    p_actor_id uuid, p_actor_email text, p_source_id uuid, p_allow boolean, p_reason text)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason text := btrim(coalesce(p_reason, ''));
    v_src    public.cb_kb_sources;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'approver');
    if p_allow is null then
        raise exception 'say whether the connection is allowed' using errcode = 'KB002';
    end if;
    if v_reason = '' or length(v_reason) > 500 then
        raise exception 'a reason of 1-500 characters is required' using errcode = 'KB002';
    end if;
    select * into v_src from public.cb_kb_sources where id = p_source_id for update;
    if v_src.id is null or not v_src.active then
        raise exception 'no such source is being watched' using errcode = 'KB003';
    end if;
    if not p_allow and v_src.url !~ '^https://' then
        raise exception 'an http:// page can only be read unverified: stop watching it instead' using errcode = 'KB002';
    end if;
    update public.cb_kb_sources
       set allow_unverified = p_allow,
           unverified_reason = case when p_allow then v_reason else null end,
           unverified_set_by_email = case when p_allow then p_actor_email else null end,
           unverified_set_at = case when p_allow then now() else null end
     where id = p_source_id;
    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
    values ('source_unverified', p_actor_id, p_actor_email,
            jsonb_build_object('source_id', p_source_id, 'allow_unverified', v_src.allow_unverified),
            jsonb_build_object('source_id', p_source_id, 'allow_unverified', p_allow, 'url', v_src.url),
            v_reason);
    return p_allow;
end;
$$;

-- 3.3 Register a page that can only be read unverified (http://, or https://
-- with a broken certificate): APPROVER only, with a reason.
create or replace function public.kb_admin_source_add_unverified(
    p_actor_id uuid, p_actor_email text, p_url text, p_label text, p_category text, p_reason text)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_url    text := btrim(coalesce(p_url, ''));
    v_label  text := btrim(coalesce(p_label, ''));
    v_reason text := btrim(coalesce(p_reason, ''));
    v_host   text;
    v_id     uuid;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'approver');
    if v_url !~ '^https?://[a-z0-9.-]+\.[a-z]{2,}(:[0-9]{2,5})?(/[^[:space:]]*)?$' or length(v_url) > 2000 then
        raise exception 'the address must be a full http:// or https:// web address' using errcode = 'KB002';
    end if;
    v_host := substring(v_url from '^https?://([^/:]+)');
    if v_host ~ '^[0-9.]+$' or v_host in ('localhost') or v_host ~ '\.(local|internal|localhost)$' then
        raise exception 'the address must be a public web page' using errcode = 'KB002';
    end if;
    if v_label = '' or length(v_label) > 200 then
        raise exception 'a label of 1-200 characters is required' using errcode = 'KB002';
    end if;
    if p_category not in ('government_rule', 'process', 'embassy', 'other') then
        raise exception 'unknown category' using errcode = 'KB002';
    end if;
    if v_reason = '' or length(v_reason) > 500 then
        raise exception 'a reason of 1-500 characters is required' using errcode = 'KB002';
    end if;

    select id into v_id from public.cb_kb_sources where url = v_url for update;
    if v_id is not null then
        if (select active from public.cb_kb_sources where id = v_id) then
            raise exception 'this address is already being watched' using errcode = 'KB003';
        end if;
        update public.cb_kb_sources
           set active = true, label = v_label, category = p_category,
               removed_by = null, removed_by_email = null, removed_at = null,
               added_by = p_actor_id, added_by_email = p_actor_email, added_at = now(),
               allow_unverified = true, unverified_reason = v_reason,
               unverified_set_by_email = p_actor_email, unverified_set_at = now()
         where id = v_id;
    else
        insert into public.cb_kb_sources (url, label, category, added_by, added_by_email,
                                          allow_unverified, unverified_reason, unverified_set_by_email, unverified_set_at)
        values (v_url, v_label, p_category, p_actor_id, p_actor_email, true, v_reason, p_actor_email, now())
        returning id into v_id;
    end if;

    insert into public.cb_kb_audit (action, actor_id, actor_email, new_values, reason)
    values ('source_added', p_actor_id, p_actor_email,
            jsonb_build_object('source_id', v_id, 'url', v_url, 'label', v_label, 'category', p_category,
                               'allow_unverified', true),
            'source page registered for watching');
    insert into public.cb_kb_audit (action, actor_id, actor_email, new_values, reason)
    values ('source_unverified', p_actor_id, p_actor_email,
            jsonb_build_object('source_id', v_id, 'allow_unverified', true, 'url', v_url),
            v_reason);
    return v_id;
end;
$$;

-- 3.4 Record whether drafting from a snapshot worked. A failure marks the
-- snapshot so kb-admin retries it on the next run (it diffs against the last
-- snapshot that was not a failed draft); a success clears the mark. The
-- schedule calls this with a NULL actor, as it records its snapshots.
create or replace function public.kb_admin_source_record_draft(
    p_actor_id uuid, p_actor_email text, p_snapshot_id uuid, p_ok boolean, p_error text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_who   text := public.kb_admin__watch_actor(p_actor_id);
    v_email text := case when p_actor_id is null then 'watcher (scheduled)' else p_actor_email end;
    v_snap  public.cb_kb_source_snapshots;
    v_error text := left(coalesce(nullif(btrim(p_error), ''), 'drafting failed'), 1000);
begin
    if p_ok is null then
        raise exception 'say whether drafting worked' using errcode = 'KB002';
    end if;
    select * into v_snap from public.cb_kb_source_snapshots where id = p_snapshot_id for update;
    if v_snap.id is null or not v_snap.fetch_ok then
        raise exception 'no such snapshot' using errcode = 'KB003';
    end if;
    if p_ok then
        if v_snap.draft_failed then
            update public.cb_kb_source_snapshots set draft_failed = false, draft_error = null where id = p_snapshot_id;
        end if;
        update public.cb_kb_sources
           set last_status = 'changed', last_error = null
         where id = v_snap.source_id and last_status = 'draft_failed';
    else
        update public.cb_kb_source_snapshots set draft_failed = true, draft_error = v_error where id = p_snapshot_id;
        update public.cb_kb_sources
           set last_status = 'draft_failed', last_error = v_error
         where id = v_snap.source_id;
        insert into public.cb_kb_audit (action, actor_id, actor_email, new_values, reason)
        values ('source_draft_failed', p_actor_id, v_email,
                jsonb_build_object('source_id', v_snap.source_id, 'snapshot_id', p_snapshot_id, 'by', v_who),
                left(v_error, 500));
    end if;
    return jsonb_build_object('snapshot_id', p_snapshot_id, 'draft_failed', not p_ok);
end;
$$;

-- =============================================================================
-- 4. Ownership and access
-- =============================================================================
alter function public.kb_admin_source_set_render(uuid, text, uuid, boolean) owner to kb_admin_fn_owner;
alter function public.kb_admin_source_set_unverified(uuid, text, uuid, boolean, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_source_add_unverified(uuid, text, text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_source_record_draft(uuid, text, uuid, boolean, text) owner to kb_admin_fn_owner;

revoke all on function
    public.kb_admin_source_set_render(uuid, text, uuid, boolean),
    public.kb_admin_source_set_unverified(uuid, text, uuid, boolean, text),
    public.kb_admin_source_add_unverified(uuid, text, text, text, text, text),
    public.kb_admin_source_record_draft(uuid, text, uuid, boolean, text)
    from public, anon, authenticated, service_role;

grant execute on function
    public.kb_admin_source_set_render(uuid, text, uuid, boolean),
    public.kb_admin_source_set_unverified(uuid, text, uuid, boolean, text),
    public.kb_admin_source_add_unverified(uuid, text, text, text, text, text),
    public.kb_admin_source_record_draft(uuid, text, uuid, boolean, text)
    to kb_admin_editor;

-- =============================================================================
-- 5. Checks (abort the whole file if any fails)
-- =============================================================================
do $$
declare
    f text;
    r text;
    n int;
begin
    foreach f in array array[
        'public.kb_admin_source_set_render(uuid, text, uuid, boolean)',
        'public.kb_admin_source_set_unverified(uuid, text, uuid, boolean, text)',
        'public.kb_admin_source_add_unverified(uuid, text, text, text, text, text)',
        'public.kb_admin_source_record_draft(uuid, text, uuid, boolean, text)'] loop
        if not exists (select 1 from pg_proc p where p.oid = f::regprocedure and p.prosecdef
                         and pg_get_userbyid(p.proowner) = 'kb_admin_fn_owner'
                         and exists (select 1 from unnest(p.proconfig) x where x like 'search_path=%pg_temp%')) then
            raise exception 'FAIL: % is not SECURITY DEFINER / owned by kb_admin_fn_owner / pinned', f;
        end if;
        if not has_function_privilege('kb_admin_editor', f, 'EXECUTE') then
            raise exception 'FAIL: kb_admin_editor cannot EXECUTE %', f;
        end if;
        foreach r in array array['anon', 'authenticated', 'service_role', 'public'] loop
            if has_function_privilege(r, f, 'EXECUTE') then
                raise exception 'FAIL: % can EXECUTE %', r, f;
            end if;
        end loop;
    end loop;
    foreach f in array array[
        'public.kb_admin_source_set_unverified(uuid, text, uuid, boolean, text)',
        'public.kb_admin_source_add_unverified(uuid, text, text, text, text, text)'] loop
        if not exists (select 1 from pg_proc where oid = f::regprocedure
                         and prosrc like '%kb_admin__require_actor(p_actor_id, ''approver'')%') then
            raise exception 'FAIL: % is not approver-only', f;
        end if;
    end loop;
    foreach r in array array['anon', 'authenticated', 'service_role', 'public', 'kb_admin_editor'] loop
        if has_any_column_privilege(r, 'public.cb_kb_sources', 'SELECT, INSERT, UPDATE, REFERENCES')
           or has_any_column_privilege(r, 'public.cb_kb_source_snapshots', 'SELECT, INSERT, UPDATE, REFERENCES') then
            raise exception 'FAIL: % holds a privilege on a watcher table', r;
        end if;
    end loop;
    select count(*) into n from pg_proc p join pg_namespace s on s.oid = p.pronamespace
     where s.nspname = 'public' and has_function_privilege('kb_admin_editor', p.oid, 'EXECUTE')
       and p.proname like 'kb\_admin\_%' and p.proname not like 'kb\_admin\_\_%';
    if n <> 31 then
        raise exception 'FAIL: kb_admin_editor executes % kb_admin_* functions, expected 31 (16 + 8 + 3 + 4)', n;
    end if;
    if not (select pg_get_constraintdef(oid) like '%source_draft_failed%' and pg_get_constraintdef(oid) like '%source_redrafted%'
              from pg_constraint where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check') then
        raise exception 'FAIL: cb_kb_audit_action_check does not allow the 017 actions';
    end if;
    if exists (select 1 from public.cb_kb_sources where url !~ '^https://' and not allow_unverified) then
        raise exception 'FAIL: an http:// source is not marked unverified';
    end if;
    raise notice 'PASS: 017 - render and unverified per source (unverified approver-only, with a reason), failed drafts retried, 31 editor functions';
end
$$;

commit;
