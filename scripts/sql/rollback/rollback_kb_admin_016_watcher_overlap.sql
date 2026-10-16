-- ROLLBACK of kb_admin_016_watcher_overlap.sql.
--
-- Drops the three 016 functions (kb_admin_watch_propose_checked,
-- kb_admin_source_set_suggestions, kb_admin_source_redraft), re-creates
-- kb_admin_watch_approve exactly as 015 wrote it (no KB009 check), drops the
-- five 016 columns and their checks, and narrows cb_kb_audit's action CHECK
-- back to the 015 list.
--
-- REFUSES (and changes nothing) while cb_kb_audit holds a 016 action
-- ('source_redrafted', 'source_suggested'): the table is append-only and the
-- narrower CHECK would fail on such a row - and a proposal that recorded
-- similar entries or a conflict would lose them. So once 016 has been used
-- this rollback is refused by design; switch the watcher off instead.
--
-- Changes NO row of cb_knowledge_base_updated.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if exists (select 1 from public.cb_kb_audit where action in ('source_redrafted', 'source_suggested')) then
        raise exception 'ROLLBACK 016 REFUSED: cb_kb_audit holds 016 actions';
    end if;
    if exists (select 1 from information_schema.columns where table_schema = 'public'
                and table_name = 'cb_kb_drafts' and column_name = 'conflict_check') then
        if exists (select 1 from public.cb_kb_drafts where conflict_check or similar_entries <> '[]'::jsonb) then
            raise exception 'ROLLBACK 016 REFUSED: proposals carry overlap results';
        end if;
    end if;
end
$$;

drop function if exists public.kb_admin_watch_propose_checked(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb, boolean, text);
drop function if exists public.kb_admin_source_set_suggestions(uuid, text, uuid, jsonb);
drop function if exists public.kb_admin_source_redraft(uuid, text, uuid, text);

-- kb_admin_watch_approve as 015 wrote it.
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
           figure_check = v_figure, figure_confirmed = v_figure
     where id = p_draft_id;
    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email, new_values, reason)
    values (v_entry, v_version, 'watch_approved', p_actor_id, p_actor_email,
            jsonb_build_object('draft_id', v_d.id, 'source_url', v_d.source_url,
                               'source_fetched_at', v_d.source_fetched_at, 'kind', v_d.kind,
                               'source_category', v_cat, 'figure_confirmed', v_figure),
            v_reason);
    return jsonb_build_object('entry_id', v_entry, 'version_id', v_version);
end;
$$;

alter function public.kb_admin_watch_approve(uuid, text, uuid, text, boolean) owner to kb_admin_fn_owner;

alter table public.cb_kb_drafts drop constraint if exists cb_kb_drafts_similar_check;
alter table public.cb_kb_sources drop constraint if exists cb_kb_sources_suggested_check;
alter table public.cb_kb_drafts drop column if exists similar_entries;
alter table public.cb_kb_drafts drop column if exists conflict_check;
alter table public.cb_kb_drafts drop column if exists conflict_note;
alter table public.cb_kb_drafts drop column if exists conflict_confirmed;
alter table public.cb_kb_sources drop column if exists suggested_pages;
alter table public.cb_kb_sources drop column if exists suggested_at;

alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
        'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
        'entry_created',
        'doc_uploaded', 'batch_prepared', 'batch_published', 'batch_restored',
        'batch_discarded', 'doc_retired', 'chunk_edited',
        'source_added', 'source_removed', 'source_fetched', 'source_fetch_failed',
        'watch_proposed', 'watch_edited', 'watch_rejected', 'watch_approved', 'watch_published',
        'external_insert', 'external_update', 'external_delete'
));

do $$
begin
    if exists (select 1 from pg_proc where proname in ('kb_admin_watch_propose_checked', 'kb_admin_source_set_suggestions', 'kb_admin_source_redraft')) then
        raise exception 'FAIL: a 016 function is still there';
    end if;
    if exists (select 1 from pg_proc where oid = 'public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)'::regprocedure
                and prosrc like '%KB009%') then
        raise exception 'FAIL: kb_admin_watch_approve still has the 016 check';
    end if;
    raise notice 'PASS: rollback 016 - back to 015 (24 editor functions)';
end
$$;

commit;
