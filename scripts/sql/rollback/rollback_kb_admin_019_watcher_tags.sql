-- =============================================================================
-- ROLLBACK of kb_admin_019_watcher_tags.sql.
--
-- Drops kb_admin_watch_set_tags, re-creates kb_admin_watch_propose exactly as
-- 015 wrote it (an edit copies its entry's tags) and kb_admin_watch_approve
-- exactly as 016 wrote it (an edit is published with the entry's own tags),
-- revokes the three UPDATE column grants, and narrows cb_kb_audit's action
-- CHECK back to the 017 list.
--
-- REFUSES (and changes nothing) while cb_kb_audit holds a 'watch_tags' row:
-- the table is append-only and the narrower CHECK would fail on it. So once a
-- reviewer has changed a proposal's tags this rollback is refused by design;
-- switch the watcher off instead. Proposals already carrying proposed tags
-- keep them (a new entry is still created with them, as under 015).
--
-- Changes NO row of cb_knowledge_base_updated.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if exists (select 1 from public.cb_kb_audit where action = 'watch_tags') then
        raise exception 'ROLLBACK 019 REFUSED: cb_kb_audit holds watch_tags rows';
    end if;
end
$$;

drop function if exists public.kb_admin_watch_set_tags(uuid, text, uuid, text, text, text, text);

-- kb_admin_watch_propose as 015 wrote it.
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
        v_service := v_t.service_type;
        v_aud := v_t.contact_type;
        v_nat := v_t.nationality;
        v_h := coalesce(v_h, v_t.section_heading);
        v_old_q := v_t.question;
        v_old_a := v_t.answer;
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

-- kb_admin_watch_approve as 016 wrote it.
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

revoke update (service, audience, nationality) on public.cb_kb_drafts from kb_admin_fn_owner;

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

do $$
begin
    if exists (select 1 from pg_proc where proname = 'kb_admin_watch_set_tags') then
        raise exception 'FAIL: kb_admin_watch_set_tags is still there';
    end if;
    if exists (select 1 from pg_proc where oid = 'public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)'::regprocedure
                 and prosrc not like '%v_t.section_heading, ''''), v_t.service_type, v_t.contact_type,%') then
        raise exception 'FAIL: kb_admin_watch_approve still publishes the proposal''s tags';
    end if;
    raise notice 'PASS: 019 rolled back - 017/018 state';
end
$$;

commit;
