-- kb_admin_016_watcher_overlap.sql - the KB watcher drafts better (2026-10-15).
--
-- What production showed: the MOM levy INDEX page drafted four "new entry"
-- proposals that only pointed elsewhere ("The MOM page provides information
-- on...") and none was checked against the entries that already answer it.
-- The drafting rules live in kb-admin (lib/watcher/*); this file gives them
-- somewhere to put what they find, and makes the database enforce the one
-- rule that must not depend on the screen:
--
--   1. OVERLAP. Each proposal records the similar live entries kb-admin found
--      with the bot's own search (cb_kb_drafts.similar_entries: id, question,
--      similarity, the figures that differ) and whether it CONFLICTS with one
--      (conflict_check: a figure differs, or a near-duplicate exists). A
--      conflicting proposal can be approved only with the approver's tick -
--      kb_admin_watch_approve refuses without it (KB009) - so an approval can
--      never silently create a second, conflicting answer.
--   2. SUGGESTED PAGES. A page that is mainly an index drafts nothing; its
--      useful sub-pages are recorded on the source (cb_kb_sources.
--      suggested_pages) for a one-click "Watch this page" (the existing
--      kb_admin_source_add).
--   3. RE-DRAFT. kb_admin_source_redraft (APPROVER only, audit-logged as
--      'source_redrafted') supersedes a page's open proposals and hands back
--      its latest snapshot, so proposals can be regenerated after the drafting
--      rules improve without waiting for the page to change.
--
-- Additive only: three new functions, two new audit actions, five new columns
-- on two watcher tables (015's), and kb_admin_watch_approve re-created with
-- the KB009 check (its body is 015's, patched in three places). No row of any
-- table is changed; the live knowledge-base table is not touched. The bot
-- reads none of this. kb_admin_editor then executes 27 functions (24 + 3).
--
-- Needs 015. Re-runnable. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/kb_admin_016_watcher_overlap.sql
-- Rollback: scripts/sql/rollback/rollback_kb_admin_016_watcher_overlap.sql
-- (never run without an explicit go-word).

begin;

do $$
begin
    if to_regclass('public.cb_kb_drafts') is null then
        raise exception 'FAIL: apply kb_admin_015_watcher before 016';
    end if;
end
$$;

-- =============================================================================
-- 1. Columns (watcher tables only)
-- =============================================================================
alter table public.cb_kb_sources add column if not exists suggested_pages jsonb not null default '[]'::jsonb;
alter table public.cb_kb_sources add column if not exists suggested_at timestamptz;
alter table public.cb_kb_drafts add column if not exists similar_entries jsonb not null default '[]'::jsonb;
alter table public.cb_kb_drafts add column if not exists conflict_check boolean not null default false;
alter table public.cb_kb_drafts add column if not exists conflict_note text;
alter table public.cb_kb_drafts add column if not exists conflict_confirmed boolean not null default false;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'cb_kb_sources_suggested_check') then
        alter table public.cb_kb_sources add constraint cb_kb_sources_suggested_check
            check (jsonb_typeof(suggested_pages) = 'array' and jsonb_array_length(suggested_pages) <= 20);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'cb_kb_drafts_similar_check') then
        alter table public.cb_kb_drafts add constraint cb_kb_drafts_similar_check
            check (jsonb_typeof(similar_entries) = 'array' and jsonb_array_length(similar_entries) <= 10);
    end if;
end
$$;

grant update (suggested_pages, suggested_at) on public.cb_kb_sources to kb_admin_fn_owner;
grant update (similar_entries, conflict_check, conflict_note, conflict_confirmed) on public.cb_kb_drafts to kb_admin_fn_owner;

-- =============================================================================
-- 2. Audit: two more watcher actions
-- =============================================================================
do $$
declare
    current_def text;
begin
    select pg_get_constraintdef(oid) into current_def
      from pg_constraint
     where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check';
    if current_def like '%source_redrafted%' then
        raise notice 'cb_kb_audit_action_check already allows the 016 actions - unchanged';
        return;
    end if;
    if current_def not like '%watch_published%' then
        raise exception 'FAIL: apply kb_admin_015 before 016 (audit CHECK has no watcher actions)';
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
        'external_insert', 'external_update', 'external_delete'
    ));
end
$$;

-- =============================================================================
-- 3. Functions
-- =============================================================================

-- 3.1 Propose, with what the overlap check found. The proposal itself goes
-- through 015's kb_admin_watch_propose unchanged (passage check, fee lock,
-- figure check, superseding, audit); this records the similar entries and the
-- conflict on it in the same transaction.
create or replace function public.kb_admin_watch_propose_checked(
    p_actor_id uuid, p_actor_email text, p_snapshot_id uuid, p_target_entry_id uuid,
    p_question text, p_answer text, p_section_heading text, p_service text, p_audience text,
    p_nationality text, p_source_passage text, p_old_source_text text, p_model text,
    p_similar jsonb, p_conflict boolean, p_conflict_note text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_similar jsonb := coalesce(p_similar, '[]'::jsonb);
    v_item    jsonb;
    v         jsonb;
begin
    if jsonb_typeof(v_similar) <> 'array' or jsonb_array_length(v_similar) > 10 then
        raise exception 'similar entries must be a list of at most 10' using errcode = 'KB002';
    end if;
    for v_item in select * from jsonb_array_elements(v_similar) loop
        if jsonb_typeof(v_item) <> 'object'
           or coalesce(v_item ->> 'id', '') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           or jsonb_typeof(v_item -> 'similarity') is distinct from 'number'
           or length(coalesce(v_item ->> 'question', '')) > 1200 then
            raise exception 'a similar entry is malformed' using errcode = 'KB002';
        end if;
    end loop;
    v := public.kb_admin_watch_propose(p_actor_id, p_actor_email, p_snapshot_id, p_target_entry_id,
        p_question, p_answer, p_section_heading, p_service, p_audience, p_nationality,
        p_source_passage, p_old_source_text, p_model);
    update public.cb_kb_drafts
       set similar_entries = v_similar,
           conflict_check = coalesce(p_conflict, false),
           conflict_note = left(nullif(btrim(coalesce(p_conflict_note, '')), ''), 1000)
     where id = (v ->> 'draft_id')::uuid;
    return v || jsonb_build_object('conflict_check', coalesce(p_conflict, false),
                                   'similar', jsonb_array_length(v_similar));
end;
$$;

-- 3.2 Record a page's suggested sub-pages (an index page drafts nothing).
-- Same host as the page, https, at most 20; audited only when they change.
create or replace function public.kb_admin_source_set_suggestions(
    p_actor_id uuid, p_actor_email text, p_source_id uuid, p_pages jsonb)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_who   text := public.kb_admin__watch_actor(p_actor_id);
    v_email text := case when p_actor_id is null then 'watcher (scheduled)' else p_actor_email end;
    v_pages jsonb := coalesce(p_pages, '[]'::jsonb);
    v_src   public.cb_kb_sources;
    v_host  text;
    v_item  jsonb;
begin
    select * into v_src from public.cb_kb_sources where id = p_source_id for update;
    if v_src.id is null or not v_src.active then
        raise exception 'no such source is being watched' using errcode = 'KB003';
    end if;
    if jsonb_typeof(v_pages) <> 'array' or jsonb_array_length(v_pages) > 20 then
        raise exception 'suggested pages must be a list of at most 20' using errcode = 'KB002';
    end if;
    v_host := split_part(split_part(v_src.url, '://', 2), '/', 1);
    for v_item in select * from jsonb_array_elements(v_pages) loop
        if jsonb_typeof(v_item) <> 'object'
           or coalesce(v_item ->> 'url', '') !~ '^https://[a-z0-9.-]+\.[a-z]{2,}(:[0-9]{2,5})?(/[^[:space:]]*)?$'
           or length(v_item ->> 'url') > 2000
           or split_part(split_part(v_item ->> 'url', '://', 2), '/', 1) <> v_host
           or length(btrim(coalesce(v_item ->> 'label', ''))) not between 1 and 200 then
            raise exception 'a suggested page is malformed or on another site' using errcode = 'KB002';
        end if;
    end loop;
    if v_src.suggested_pages is distinct from v_pages then
        update public.cb_kb_sources set suggested_pages = v_pages, suggested_at = now() where id = p_source_id;
        insert into public.cb_kb_audit (action, actor_id, actor_email, new_values, reason)
        values ('source_suggested', p_actor_id, v_email,
                jsonb_build_object('source_id', p_source_id, 'pages', v_pages, 'by', v_who),
                case when jsonb_array_length(v_pages) = 0 then 'no sub-pages suggested'
                     else 'the page is mainly an index; sub-pages suggested to watch' end);
    end if;
    return jsonb_array_length(v_pages);
end;
$$;

-- 3.3 Re-draft from the latest snapshot: APPROVER only. Supersedes the page's
-- open proposals and returns the snapshot to draft from (and the one before
-- it, to diff against). kb-admin then drafts and proposes as after a fetch.
create or replace function public.kb_admin_source_redraft(
    p_actor_id uuid, p_actor_email text, p_source_id uuid, p_reason text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason text := btrim(coalesce(p_reason, ''));
    v_src    public.cb_kb_sources;
    v_last   uuid;
    v_prev   uuid;
    v_n      integer;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'approver');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    select * into v_src from public.cb_kb_sources where id = p_source_id for update;
    if v_src.id is null or not v_src.active then
        raise exception 'no such source is being watched' using errcode = 'KB003';
    end if;
    select id into v_last from public.cb_kb_source_snapshots
     where source_id = p_source_id and fetch_ok order by fetched_at desc, id desc limit 1;
    if v_last is null then
        raise exception 'the page has never been read: use Fetch now first' using errcode = 'KB003';
    end if;
    select id into v_prev from public.cb_kb_source_snapshots
     where source_id = p_source_id and fetch_ok and id <> v_last
       and fetched_at <= (select fetched_at from public.cb_kb_source_snapshots where id = v_last)
     order by fetched_at desc, id desc limit 1;
    update public.cb_kb_drafts
       set status = 'superseded', blocked_reason = null
     where source_id = p_source_id and status in ('pending', 'blocked');
    get diagnostics v_n = row_count;
    insert into public.cb_kb_audit (action, actor_id, actor_email, new_values, reason)
    values ('source_redrafted', p_actor_id, p_actor_email,
            jsonb_build_object('source_id', p_source_id, 'snapshot_id', v_last,
                               'previous_snapshot_id', v_prev, 'superseded', v_n),
            v_reason);
    return jsonb_build_object('snapshot_id', v_last, 'previous_snapshot_id', v_prev, 'superseded', v_n);
end;
$$;

-- 3.4 Approve: 015's function with the KB009 conflict check (tick required).
create or replace function public.kb_admin_watch_approve(
    p_actor_id uuid, p_actor_email text, p_draft_id uuid, p_reason text, p_confirm_figure boolean)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason  text := btrim(coalesce(p_reason, ''));
    v_d       public.cb_kb_drafts;
    v_t       record;
    v_lock    text;
    v_version uuid;
    v_entry   uuid;
    v_created jsonb;
    v_note    text;
    v_cat     text;
    v_figure  boolean;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'approver');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    select * into v_d from public.cb_kb_drafts where id = p_draft_id for update;
    if v_d.id is null or v_d.status not in ('pending', 'blocked') then
        raise exception 'this draft is no longer open' using errcode = 'KB003';
    end if;

    select s.category into v_cat from public.cb_kb_sources s where s.id = v_d.source_id;
    if v_d.target_entry_id is not null then
        select k.question, k.answer, k.section_heading, k.service_type, k.contact_type, k.nationality
          into v_t from public.cb_knowledge_base_updated k where k.id = v_d.target_entry_id;
        if not found then
            raise exception 'the entry no longer exists' using errcode = 'KB003';
        end if;
        v_lock := coalesce(v_d.blocked_reason,
                           public.kb_admin__watch_fee_lock(v_t.question, v_t.answer, v_d.proposed_question, v_d.proposed_answer, v_cat));
        v_figure := public.kb_admin__watch_money(v_t.question, v_t.answer, v_d.proposed_question, v_d.proposed_answer);
    else
        v_lock := coalesce(v_d.blocked_reason,
                           public.kb_admin__watch_fee_lock(null, null, v_d.proposed_question, v_d.proposed_answer, v_cat));
        v_figure := public.kb_admin__watch_money(null, null, v_d.proposed_question, v_d.proposed_answer);
    end if;
    if v_lock is not null then
        raise exception 'needs agency confirmation: %', v_lock using errcode = 'KB007';
    end if;
    if v_figure and not coalesce(p_confirm_figure, false) then
        raise exception 'this changes a figure: confirm you checked it against the source' using errcode = 'KB008';
    end if;
    -- 016: the proposal states a figure that differs from a similar live entry
    -- (or a near-duplicate exists): the approver must tick that they checked.
    if v_d.conflict_check and not coalesce(p_confirm_figure, false) then
        raise exception 'this disagrees with an existing entry: confirm you checked both against the source' using errcode = 'KB009';
    end if;

    v_note := left(v_reason || ' [from ' || v_d.source_url || ', fetched '
                   || to_char(v_d.source_fetched_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC]', 1000);
    if v_d.target_entry_id is not null then
        if v_t.question is distinct from v_d.old_question or v_t.answer is distinct from v_d.old_answer then
            raise exception 'the entry has changed since this draft was made' using errcode = 'KB003';
        end if;
        v_version := public.kb_admin_save_draft(
            p_actor_id, p_actor_email, v_d.target_entry_id, v_d.proposed_question, v_d.proposed_answer,
            coalesce(v_d.section_heading, v_t.section_heading, ''), v_t.service_type, v_t.contact_type,
            v_t.nationality, null, v_note);
        v_entry := v_d.target_entry_id;
    else
        v_created := public.kb_admin_create_entry(
            p_actor_id, p_actor_email, v_d.proposed_question, v_d.proposed_answer,
            coalesce(v_d.section_heading, ''), v_d.service, v_d.audience, v_d.nationality, v_note);
        v_version := (v_created ->> 'version_id')::uuid;
        v_entry := (v_created ->> 'entry_id')::uuid;
    end if;

    update public.cb_kb_drafts
       set status = 'approved', approved_by = p_actor_id, approved_by_email = p_actor_email,
           approved_at = now(), reviewed_at = now(), reviewed_by = p_actor_id,
           reviewed_by_email = p_actor_email, review_reason = v_reason,
           entry_id_after = v_entry, version_id = v_version,
           figure_check = v_figure, figure_confirmed = v_figure,
           conflict_confirmed = v_d.conflict_check
     where id = p_draft_id;
    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email, new_values, reason)
    values (v_entry, v_version, 'watch_approved', p_actor_id, p_actor_email,
            jsonb_build_object('draft_id', v_d.id, 'source_url', v_d.source_url,
                               'source_fetched_at', v_d.source_fetched_at, 'kind', v_d.kind,
                               'source_category', v_cat, 'figure_confirmed', v_figure,
                               'conflict_confirmed', v_d.conflict_check),
            v_reason);
    return jsonb_build_object('entry_id', v_entry, 'version_id', v_version);
end;
$$;

-- =============================================================================
-- 4. Ownership and EXECUTE (the 004 / 008 / 015 pattern)
-- =============================================================================
grant create on schema public to kb_admin_fn_owner;
alter function public.kb_admin_watch_propose_checked(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb, boolean, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_source_set_suggestions(uuid, text, uuid, jsonb) owner to kb_admin_fn_owner;
alter function public.kb_admin_source_redraft(uuid, text, uuid, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_watch_approve(uuid, text, uuid, text, boolean) owner to kb_admin_fn_owner;
revoke create on schema public from kb_admin_fn_owner;

revoke all on function
    public.kb_admin_watch_propose_checked(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb, boolean, text),
    public.kb_admin_source_set_suggestions(uuid, text, uuid, jsonb),
    public.kb_admin_source_redraft(uuid, text, uuid, text),
    public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)
    from public, anon, authenticated, service_role;

grant execute on function
    public.kb_admin_watch_propose_checked(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb, boolean, text),
    public.kb_admin_source_set_suggestions(uuid, text, uuid, jsonb),
    public.kb_admin_source_redraft(uuid, text, uuid, text),
    public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)
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
        'public.kb_admin_watch_propose_checked(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb, boolean, text)',
        'public.kb_admin_source_set_suggestions(uuid, text, uuid, jsonb)',
        'public.kb_admin_source_redraft(uuid, text, uuid, text)',
        'public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)'] loop
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
    if not exists (select 1 from pg_proc where oid = 'public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)'::regprocedure
                     and prosrc like '%errcode = ''KB009''%' and prosrc like '%errcode = ''KB008''%'
                     and prosrc like '%errcode = ''KB007''%' and prosrc like '%kb_admin__require_actor(p_actor_id, ''approver'')%') then
        raise exception 'FAIL: kb_admin_watch_approve lacks the KB007/KB008/KB009 checks or the approver rule';
    end if;
    if not exists (select 1 from pg_proc where oid = 'public.kb_admin_source_redraft(uuid, text, uuid, text)'::regprocedure
                     and prosrc like '%kb_admin__require_actor(p_actor_id, ''approver'')%') then
        raise exception 'FAIL: kb_admin_source_redraft is not approver-only';
    end if;
    foreach r in array array['anon', 'authenticated', 'service_role', 'public', 'kb_admin_editor'] loop
        if has_any_column_privilege(r, 'public.cb_kb_drafts', 'SELECT, INSERT, UPDATE, REFERENCES')
           or has_any_column_privilege(r, 'public.cb_kb_sources', 'SELECT, INSERT, UPDATE, REFERENCES') then
            raise exception 'FAIL: % holds a privilege on a watcher table', r;
        end if;
    end loop;
    if has_table_privilege('kb_admin_fn_owner', 'public.cb_kb_rules', 'SELECT, INSERT, UPDATE, DELETE') then
        raise exception 'FAIL: kb_admin_fn_owner holds a privilege on cb_kb_rules';
    end if;
    select count(*) into n from pg_proc p join pg_namespace s on s.oid = p.pronamespace
     where s.nspname = 'public' and has_function_privilege('kb_admin_editor', p.oid, 'EXECUTE')
       and p.proname like 'kb\_admin\_%' and p.proname not like 'kb\_admin\_\_%';
    if n <> 27 then
        raise exception 'FAIL: kb_admin_editor executes % kb_admin_* functions, expected 27 (16 + 8 + 3)', n;
    end if;
    if not (select pg_get_constraintdef(oid) like '%source_redrafted%' and pg_get_constraintdef(oid) like '%watch_published%'
              from pg_constraint where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check') then
        raise exception 'FAIL: cb_kb_audit_action_check does not allow the 016 actions';
    end if;
    raise notice 'PASS: 016 - overlap and suggested pages recorded, re-draft approver-only, approve needs the tick on a conflict (KB009), 27 editor functions';
end
$$;

commit;
