-- kb_admin_019_watcher_tags.sql - the KB watcher files each proposal under the right tags (2026-10-28).
--
-- What production showed: every NEW watcher proposal landed as general / all /
-- all (kb-admin passed no tags), so the MWO Singapore pages (contract
-- verification, the OEC, Balik-Manggagawa) went live as general entries - and
-- one answered a Filipino NEW-HIRE documents question with contract-
-- verification documents. The bot filters its search on service_type,
-- contact_type and nationality, so a wrong tag answers somebody else's question.
--
--   1. PROPOSED TAGS. kb-admin now proposes a service, audience and
--      nationality for every draft (kb-admin/lib/watcher/tags.ts: the drafting
--      model's tags, checked, overridden by unambiguous evidence - MWO / OFW /
--      OEC -> PH, KBRI -> ID, Balik-Manggagawa -> home_leave, ...).
--      kb_admin_watch_propose is re-created so an EDIT stores them too (until
--      now an edit always copied its entry's tags). A tag outside the allowed
--      values falls back to the entry's own (else general / all / all): never
--      an error, so a scheduled run still proposes. Its body is 015's, patched
--      in three places (the selfcheck proves it).
--   2. CHANGE THEM BEFORE APPROVAL. kb_admin_watch_set_tags (EDITOR, with a
--      reason, only on an open proposal; audit 'watch_tags') - the same role
--      and audit pattern as kb_admin_watch_edit, the same value rule as the Q&A
--      editor (kb_admin__check_routing, 004).
--   3. AN EDIT IS PUBLISHED WITH THE PROPOSAL'S TAGS. kb_admin_watch_approve is
--      re-created (its body is 016's with one change: v_d.service /
--      v_d.audience / v_d.nationality instead of the target's). Approving is
--      approver-only already, and kb_admin_publish (004) still demands an
--      approver for any routing change - the same rule as the Q&A editor.
--
-- Additive otherwise: one new function, one new audit action, three UPDATE
-- column grants on cb_kb_drafts (015's table) to kb_admin_fn_owner. No row of
-- any table is changed; the live knowledge-base table is not touched. The bot
-- reads none of this. kb_admin_editor then executes 32 functions (31 + 1).
--
-- Needs 015, 016 and 017. Re-runnable. WRITTEN, NOT APPLIED. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/kb_admin_019_watcher_tags.sql
-- Rollback: scripts/sql/rollback/rollback_kb_admin_019_watcher_tags.sql
-- (never run without an explicit go-word).

begin;

do $$
begin
    if to_regclass('public.cb_kb_drafts') is null then
        raise exception 'FAIL: apply kb_admin_015_watcher before 019';
    end if;
    if not exists (select 1 from information_schema.columns where table_schema = 'public'
                     and table_name = 'cb_kb_sources' and column_name = 'allow_unverified') then
        raise exception 'FAIL: apply kb_admin_016 and kb_admin_017 before 019';
    end if;
end
$$;

-- =============================================================================
-- 1. The proposal's tags may be changed (watcher table only)
-- =============================================================================
grant update (service, audience, nationality) on public.cb_kb_drafts to kb_admin_fn_owner;

-- =============================================================================
-- 2. Audit: one more watcher action
-- =============================================================================
do $$
declare
    current_def text;
begin
    select pg_get_constraintdef(oid) into current_def
      from pg_constraint
     where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check';
    if current_def like '%watch_tags%' then
        raise notice 'cb_kb_audit_action_check already allows watch_tags - unchanged';
        return;
    end if;
    if current_def not like '%source_draft_failed%' then
        raise exception 'FAIL: apply kb_admin_017 before 019 (audit CHECK has no 017 actions)';
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
        'watch_tags',
        'external_insert', 'external_update', 'external_delete'
    ));
end
$$;

-- =============================================================================
-- 3. Functions
-- =============================================================================

-- 3.1 Propose: 015's, with the watcher's tags stored on an edit too, and an
-- unknown tag falling back instead of being stored.
create or replace function public.kb_admin_watch_propose(
    p_actor_id uuid, p_actor_email text, p_snapshot_id uuid, p_target_entry_id uuid,
    p_question text, p_answer text, p_section_heading text, p_service text, p_audience text,
    p_nationality text, p_source_passage text, p_old_source_text text, p_model text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_who     text := public.kb_admin__watch_actor(p_actor_id);
    v_email   text := case when p_actor_id is null then 'watcher (scheduled)' else p_actor_email end;
    v_q       text := btrim(coalesce(p_question, ''));
    v_a       text := btrim(coalesce(p_answer, ''));
    v_h       text := nullif(btrim(coalesce(p_section_heading, '')), '');
    v_passage text := btrim(coalesce(p_source_passage, ''));
    v_snap    public.cb_kb_source_snapshots;
    v_src     public.cb_kb_sources;
    v_t       record;
    v_lock    text;
    v_id      uuid;
    v_service text := coalesce(nullif(btrim(p_service), ''), 'general');
    v_aud     text := coalesce(nullif(btrim(p_audience), ''), 'all');
    v_nat     text := coalesce(nullif(btrim(p_nationality), ''), 'all');
    v_def_s   text := 'general';
    v_def_a   text := 'all';
    v_def_n   text := 'all';
    v_old_q   text;
    v_old_a   text;
    v_figure  boolean;
begin
    select * into v_snap from public.cb_kb_source_snapshots where id = p_snapshot_id;
    if v_snap.id is null or not v_snap.fetch_ok then
        raise exception 'no such snapshot' using errcode = 'KB002';
    end if;
    select * into v_src from public.cb_kb_sources where id = v_snap.source_id;
    if v_q = '' or v_a = '' or length(v_q || E'\n' || v_a) > 1200 then
        raise exception 'a question and an answer of at most 1200 characters together are required' using errcode = 'KB002';
    end if;
    perform public.kb_admin__refuse_nric(v_q, v_a, coalesce(v_h, ''), v_passage);
    -- The passage must be the page's own words (whitespace aside).
    if v_passage = '' or strpos(regexp_replace(v_snap.extracted_text, '\s+', ' ', 'g'),
                                regexp_replace(v_passage, '\s+', ' ', 'g')) = 0 then
        raise exception 'the source passage is not in the fetched page' using errcode = 'KB002';
    end if;

    if p_target_entry_id is not null then
        select k.question, k.answer, k.section_heading, k.service_type, k.contact_type, k.nationality, k.chunk_type
          into v_t from public.cb_knowledge_base_updated k where k.id = p_target_entry_id;
        if not found or v_t.chunk_type <> 'qa_pair' then
            raise exception 'the target is not a Q&A entry' using errcode = 'KB002';
        end if;
        -- 019: the tags the watcher proposed when given; otherwise the entry's own.
        v_def_s := v_t.service_type;
        v_def_a := v_t.contact_type;
        v_def_n := v_t.nationality;
        v_service := coalesce(nullif(btrim(p_service), ''), v_t.service_type);
        v_aud := coalesce(nullif(btrim(p_audience), ''), v_t.contact_type);
        v_nat := coalesce(nullif(btrim(p_nationality), ''), v_t.nationality);
        v_h := coalesce(v_h, v_t.section_heading);
        v_old_q := v_t.question;
        v_old_a := v_t.answer;
    end if;
    -- 019: a tag outside the allowed values falls back (the entry's own, else
    -- general / all / all) - never an error, so a scheduled run still proposes.
    if v_aud not in ('employer', 'candidate', 'all') then v_aud := v_def_a; end if;
    if v_nat not in ('PH', 'ID', 'MM', 'all') then v_nat := v_def_n; end if;
    if v_service is distinct from v_def_s
       and not exists (select 1 from public.cb_knowledge_base_updated k
                        where k.is_active and k.service_type = v_service) then
        v_service := v_def_s;
    end if;
    v_lock := public.kb_admin__watch_fee_lock(v_old_q, v_old_a, v_q, v_a, v_src.category);
    v_figure := v_lock is null and public.kb_admin__watch_money(v_old_q, v_old_a, v_q, v_a);

    -- An older open draft for the same entry from the same page is superseded.
    if p_target_entry_id is not null then
        update public.cb_kb_drafts
           set status = 'superseded', blocked_reason = null
         where target_entry_id = p_target_entry_id and source_id = v_snap.source_id
           and status in ('pending', 'blocked');
    end if;

    insert into public.cb_kb_drafts (
        source_id, snapshot_id, source_url, source_fetched_at, kind, target_entry_id,
        old_question, old_answer, proposed_question, proposed_answer, section_heading,
        service, audience, nationality, source_passage, old_source_text, new_source_text,
        model_name, status, blocked_reason, figure_check, created_by, created_by_email)
    values (
        v_snap.source_id, v_snap.id, v_src.url, v_snap.fetched_at,
        case when p_target_entry_id is null then 'new' else 'edit' end, p_target_entry_id,
        v_old_q, v_old_a,
        v_q, v_a, v_h, v_service, v_aud, v_nat, v_passage, left(p_old_source_text, 20000),
        left(v_passage, 20000), left(p_model, 200),
        case when v_lock is null then 'pending' else 'blocked' end, v_lock, v_figure, p_actor_id, v_email)
    returning id into v_id;

    insert into public.cb_kb_audit (entry_id, action, actor_id, actor_email, new_values, reason)
    values (p_target_entry_id, 'watch_proposed', p_actor_id, v_email,
            jsonb_build_object('draft_id', v_id, 'source_id', v_snap.source_id, 'snapshot_id', v_snap.id,
                               'question', v_q, 'answer', v_a, 'blocked_reason', v_lock,
                               'figure_check', v_figure, 'by', v_who),
            case when v_lock is null then 'drafted from a source page change'
                 else 'drafted from a source page change; needs agency confirmation' end);
    return jsonb_build_object('draft_id', v_id, 'status', case when v_lock is null then 'pending' else 'blocked' end,
                              'blocked_reason', v_lock, 'figure_check', v_figure);
end;
$$;

-- 3.2 Change a proposal's tags before approval. Editor, with a reason.
create or replace function public.kb_admin_watch_set_tags(
    p_actor_id uuid, p_actor_email text, p_draft_id uuid, p_service text, p_audience text,
    p_nationality text, p_reason text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_s      text := btrim(coalesce(p_service, ''));
    v_a      text := btrim(coalesce(p_audience, ''));
    v_n      text := btrim(coalesce(p_nationality, ''));
    v_reason text := btrim(coalesce(p_reason, ''));
    v_d      public.cb_kb_drafts;
    v_ls     text;
    v_la     text;
    v_ln     text;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    select * into v_d from public.cb_kb_drafts where id = p_draft_id for update;
    if v_d.id is null or v_d.status not in ('pending', 'blocked') then
        raise exception 'this draft is no longer open' using errcode = 'KB003';
    end if;
    if v_d.target_entry_id is not null then
        select k.service_type, k.contact_type, k.nationality into v_ls, v_la, v_ln
          from public.cb_knowledge_base_updated k where k.id = v_d.target_entry_id;
    end if;
    -- The same routing rule as the Q&A editor (004): a known audience and
    -- nationality, and a service an active entry uses - or the one the
    -- proposal or its entry already has.
    perform public.kb_admin__check_routing(v_s, v_a, v_n,
        case when v_s = v_ls then v_ls else v_d.service end);
    if v_s = v_d.service and v_a = v_d.audience and v_n = v_d.nationality then
        raise exception 'the tags are unchanged' using errcode = 'KB002';
    end if;
    update public.cb_kb_drafts
       set service = v_s, audience = v_a, nationality = v_n,
           edited_at = now(), edited_by = p_actor_id, edited_by_email = p_actor_email
     where id = p_draft_id;
    insert into public.cb_kb_audit (entry_id, action, actor_id, actor_email, old_values, new_values, reason)
    values (v_d.target_entry_id, 'watch_tags', p_actor_id, p_actor_email,
            jsonb_build_object('draft_id', v_d.id, 'service', v_d.service, 'audience', v_d.audience,
                               'nationality', v_d.nationality),
            jsonb_build_object('draft_id', v_d.id, 'service', v_s, 'audience', v_a, 'nationality', v_n),
            v_reason);
    -- An edit re-filed under other tags than the live entry's: only an approver approves it (as always).
    return jsonb_build_object('service', v_s, 'audience', v_a, 'nationality', v_n,
                              'changes_live_tags', v_d.target_entry_id is not null
                                  and (v_s, v_a, v_n) is distinct from (v_ls, v_la, v_ln));
end;
$$;

-- 3.3 Approve: 016's, publishing an edit with the PROPOSAL's tags (v_d.*).
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
            coalesce(v_d.section_heading, v_t.section_heading, ''), v_d.service, v_d.audience,
            v_d.nationality, null, v_note);
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
-- Changing an owner needs the new owner to hold CREATE on the schema (as in 015-017); granted for these lines only.
grant create on schema public to kb_admin_fn_owner;
alter function public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_watch_set_tags(uuid, text, uuid, text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_watch_approve(uuid, text, uuid, text, boolean) owner to kb_admin_fn_owner;
revoke create on schema public from kb_admin_fn_owner;

revoke all on function
    public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text),
    public.kb_admin_watch_set_tags(uuid, text, uuid, text, text, text, text),
    public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)
    from public, anon, authenticated, service_role;

grant execute on function
    public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text),
    public.kb_admin_watch_set_tags(uuid, text, uuid, text, text, text, text),
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
        'public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text)',
        'public.kb_admin_watch_set_tags(uuid, text, uuid, text, text, text, text)',
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
    if not exists (select 1 from pg_proc where oid = 'public.kb_admin_watch_set_tags(uuid, text, uuid, text, text, text, text)'::regprocedure
                     and prosrc like '%kb_admin__require_actor(p_actor_id, ''editor'')%'
                     and prosrc like '%kb_admin__check_routing(%' and prosrc like '%''watch_tags''%') then
        raise exception 'FAIL: kb_admin_watch_set_tags lacks the role check, the routing check or its audit row';
    end if;
    if not exists (select 1 from pg_proc where oid = 'public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)'::regprocedure
                     and prosrc like '%v_t.section_heading, ''''), v_d.service, v_d.audience,%'
                     and prosrc not like '%v_t.service_type, v_t.contact_type,%' and prosrc like '%errcode = ''KB009''%'
                     and prosrc like '%kb_admin__require_actor(p_actor_id, ''approver'')%') then
        raise exception 'FAIL: kb_admin_watch_approve does not publish an edit with the proposal''s tags, or lost a check';
    end if;
    if not has_column_privilege('kb_admin_fn_owner', 'public.cb_kb_drafts', 'service', 'UPDATE') then
        raise exception 'FAIL: kb_admin_fn_owner cannot change a proposal''s tags';
    end if;
    foreach r in array array['anon', 'authenticated', 'service_role', 'public', 'kb_admin_editor'] loop
        if has_any_column_privilege(r, 'public.cb_kb_drafts', 'SELECT, INSERT, UPDATE, REFERENCES') then
            raise exception 'FAIL: % holds a privilege on cb_kb_drafts', r;
        end if;
    end loop;
    select count(*) into n from pg_proc p join pg_namespace s on s.oid = p.pronamespace
     where s.nspname = 'public' and has_function_privilege('kb_admin_editor', p.oid, 'EXECUTE')
       and p.proname like 'kb\_admin\_%' and p.proname not like 'kb\_admin\_\_%';
    if n <> 32 then
        raise exception 'FAIL: kb_admin_editor executes % kb_admin_* functions, expected 32 (16 + 8 + 3 + 4 + 1)', n;
    end if;
    if not (select pg_get_constraintdef(oid) like '%watch_tags%' and pg_get_constraintdef(oid) like '%source_draft_failed%'
              from pg_constraint where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check') then
        raise exception 'FAIL: cb_kb_audit_action_check does not allow watch_tags';
    end if;
    raise notice 'PASS: 019 - proposals carry their proposed tags (an edit too), editors change them with a reason, an approved edit publishes them, 32 editor functions';
end
$$;

commit;
