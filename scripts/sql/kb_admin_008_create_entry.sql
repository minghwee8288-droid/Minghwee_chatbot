-- kb_admin_008_create_entry: a SIXTH write function - create a new Q&A entry.
--
-- kb_admin_create_entry adds one brand-new question-and-answer entry that the
-- chatbot CANNOT see, plus a draft of its text. The entry only becomes
-- searchable when that draft goes through the existing kb_admin_publish
-- (canary-checked embedding, approver needed because switching on is an on/off
-- change). Nothing in 001-007 is changed.
--
-- WHY THE NEW ROW HOLDS NO QUESTION OR ANSWER YET. kb_admin_publish (004) is
-- reused unchanged, and it decides whether a new vector is needed by comparing
-- the draft's text with the LIVE row's text. If the live row already carried
-- the text, publish would see "wording unchanged", keep the live row's vector -
-- which is NULL - and switch on an entry the bot can never retrieve
-- (cb_match_knowledge_base_updated requires embedding IS NOT NULL). So the live
-- row starts with question, answer and content NULL; the text lives in the
-- draft. Publishing then sees the text change, refuses without a vector
-- (KB005), and kb-admin embeds it with the canary-verified model, exactly as
-- for an edit.
--
-- WHY TWO VERSIONS (1 published, 2 draft). kb_admin_publish requires a
-- 'published' version whose number equals the draft's based_on_version and
-- which matches the live row (optimistic locking, kb_admin__live_matches). So
-- version 1 is the baseline of the empty, switched-off row - exactly what
-- kb_admin__ensure_baseline would record on a first edit - and version 2 is the
-- draft holding the text, switched ON. (cb_kb_entry_versions has no
-- is_draft / published_at / published_by columns: a draft is status 'draft'.)
--
-- THE NEW ROW, while it is a draft:
--   chunk_type 'qa_pair', is_active false, embedding NULL, question / answer /
--   content NULL, source_document 'Ming Hwee KB Admin' (never the loader's
--   name), section_heading / service / audience / nationality from the form,
--   namespace = the namespace most active rows of that service use (it is a
--   label only: RAG_NAMESPACE is unset, so the bot never filters on it),
--   page_or_section NULL, rag_score_floor 0.420 when the text contains a
--   number (the import's convention for figure-bearing rows), else NULL.
--   metadata carries the six keys the imported rows carry: managed_by 'ui'
--   (the loader skips such rows), keywords [], priority 3 (= no boost, the
--   same as a row with no priority), figures_present, table_column NULL,
--   date_valid_from NULL.
--   rag_score_floor is set from the text at creation and is not revisited:
--   kb_admin_fn_owner cannot update that column (scope C of the plan).
--
-- THE NEW GRANT. kb_admin_fn_owner gains INSERT on ten columns of the live
-- table - never question, answer, content or embedding - and an RLS INSERT
-- policy that admits only a switched-off, embedding-less, text-less Q&A row
-- from 'Ming Hwee KB Admin' tagged managed_by 'ui'. It also gains SELECT on
-- namespace (for the mapping above). It still cannot DELETE, and still cannot
-- UPDATE rag_score_floor, chunk_type, source_document or namespace.
-- NOTE for kb_admin_007_checks.sql: its "narrow grants" check uses
-- has_table_privilege(..., 'INSERT'), which does not see column-level grants,
-- so 007 still passes. The checks at the end of THIS file state the exact
-- insert surface instead.
--
-- Errors (same codes as 004): KB001 not an active editor/approver,
-- KB002 invalid input, KB006 NRIC/FIN in the text.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>
-- Rollback:  scripts/sql/rollback/rollback_kb_admin_008_create_entry.sql

begin;

-- =============================================================================
-- 1. Audit: allow 'entry_created'
-- =============================================================================
-- The table is append-only (rows can never change), so widening its CHECK is
-- safe: every existing row already passes the narrower list.
do $$
declare
    current_def text;
begin
    select pg_get_constraintdef(oid) into current_def
      from pg_constraint
     where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check';
    if current_def like '%entry_created%' then
        raise notice 'cb_kb_audit_action_check already allows entry_created - unchanged';
        return;
    end if;
    alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
    alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
        'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
        'entry_created',
        'external_insert', 'external_update', 'external_delete'
    ));
end
$$;

-- =============================================================================
-- 2. The owner's new, narrow INSERT
-- =============================================================================
grant insert (
    namespace, service_type, contact_type, nationality, chunk_type, source_document,
    section_heading, metadata, is_active, rag_score_floor
) on public.cb_knowledge_base_updated to kb_admin_fn_owner;
grant select (namespace) on public.cb_knowledge_base_updated to kb_admin_fn_owner;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_knowledge_base_updated'
                    and policyname = 'kb_admin_fn_owner_insert_new_qa') then
        create policy kb_admin_fn_owner_insert_new_qa on public.cb_knowledge_base_updated
            as permissive for insert to kb_admin_fn_owner
            with check (
                chunk_type = 'qa_pair'
                and not is_active
                and embedding is null
                and question is null and answer is null and content is null
                and source_document = 'Ming Hwee KB Admin'
                and metadata ->> 'managed_by' = 'ui');
    end if;
end
$$;

-- =============================================================================
-- 3. kb_admin_create_entry
-- =============================================================================
-- Caller: an active editor or approver.
-- Returns {entry_id, version_id (the draft), baseline_version_id}.
create or replace function public.kb_admin_create_entry(
    p_actor_id        uuid,
    p_actor_email     text,
    p_question        text,
    p_answer          text,
    p_section_heading text,
    p_service         text,
    p_audience        text,
    p_nationality     text,
    p_reason          text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_q         text := btrim(coalesce(p_question, ''));
    v_a         text := btrim(coalesce(p_answer, ''));
    v_h         text := nullif(btrim(coalesce(p_section_heading, '')), '');
    v_service   text := btrim(coalesce(p_service, ''));
    v_reason    text := btrim(coalesce(p_reason, ''));
    v_len       int;
    v_figures   boolean;
    v_namespace text;
    v_entry_id  uuid;
    v_base      public.cb_kb_entry_versions;
    v_draft     public.cb_kb_entry_versions;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    if v_q = '' or v_a = '' then
        raise exception 'question and answer are both required' using errcode = 'KB002';
    end if;
    perform public.kb_admin__refuse_nric(v_q, v_a, coalesce(v_h, ''));

    v_len := length(v_q || E'\n' || v_a);
    if v_len > 1200 then
        raise exception 'the combined question and answer is too long (% characters, limit 1200)',
            v_len using errcode = 'KB002';
    end if;

    -- A new entry has no current service, so the service must already be in
    -- use by an active row. '' is passed as the "current" service because
    -- NULL would make kb_admin__check_routing's test evaluate to NULL and let
    -- any value through; an empty service is refused here first.
    if v_service = '' then
        raise exception 'unknown service ""' using errcode = 'KB002';
    end if;
    perform public.kb_admin__check_routing(v_service, p_audience, p_nationality, '');

    v_figures := cardinality(public.kb_admin__numbers(concat_ws(' ', v_q, v_a, v_h))) > 0;

    select k.namespace into v_namespace
      from public.cb_knowledge_base_updated k
     where k.is_active and k.service_type = v_service
     group by k.namespace
     order by count(*) desc, k.namespace
     limit 1;
    v_namespace := coalesce(v_namespace, 'services_general');

    -- The row the chatbot cannot see. The safety-net trigger (006) skips it;
    -- this function logs the creation itself, with the person.
    perform set_config('kb_admin.via_function', 'on', true);
    insert into public.cb_knowledge_base_updated (
        namespace, service_type, contact_type, nationality, chunk_type, source_document,
        section_heading, metadata, is_active, rag_score_floor)
    values (
        v_namespace, v_service, p_audience, p_nationality, 'qa_pair', 'Ming Hwee KB Admin',
        v_h,
        jsonb_build_object(
            'managed_by', 'ui',
            'keywords', '[]'::jsonb,
            'priority', 3,
            'figures_present', v_figures,
            'table_column', null,
            'date_valid_from', null),
        false,
        case when v_figures then 0.420 else null end)
    returning id into v_entry_id;
    perform set_config('kb_admin.via_function', 'off', true);

    -- Version 1: the live row as it now is (empty and switched off).
    insert into public.cb_kb_entry_versions (
        entry_id, version_number, question, answer, section_heading, service,
        audience, nationality, active, content, embedding, model_name, status,
        based_on_version, created_by, created_by_email, reason)
    values (
        v_entry_id, 1, null, null, v_h, v_service,
        p_audience, p_nationality, false, null, null, null, 'published',
        null, p_actor_id, p_actor_email,
        'new entry created in kb-admin: empty and switched off until its first publish')
    returning * into v_base;

    -- Version 2: the draft holding the text, switched on.
    insert into public.cb_kb_entry_versions (
        entry_id, version_number, question, answer, section_heading, service,
        audience, nationality, active, content, embedding, model_name, status,
        based_on_version, created_by, created_by_email, reason)
    values (
        v_entry_id, 2, v_q, v_a, v_h, v_service,
        p_audience, p_nationality, true, v_q || E'\n' || v_a, null, null, 'draft',
        1, p_actor_id, p_actor_email, v_reason)
    returning * into v_draft;

    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email,
                                    old_values, new_values, reason)
    values (v_entry_id, v_draft.id, 'entry_created', p_actor_id, p_actor_email,
            null,
            public.kb_admin__fields(v_draft) || jsonb_build_object(
                'source_document', 'Ming Hwee KB Admin', 'namespace', v_namespace,
                'rag_score_floor', case when v_figures then 0.420 else null end),
            v_reason);

    return jsonb_build_object(
        'entry_id', v_entry_id,
        'version_id', v_draft.id,
        'baseline_version_id', v_base.id);
end;
$$;

-- =============================================================================
-- 4. Ownership and EXECUTE (the 004 pattern)
-- =============================================================================
grant create on schema public to kb_admin_fn_owner;
alter function public.kb_admin_create_entry(uuid, text, text, text, text, text, text, text, text)
    owner to kb_admin_fn_owner;
revoke create on schema public from kb_admin_fn_owner;
revoke all on function public.kb_admin_create_entry(uuid, text, text, text, text, text, text, text, text)
    from public, anon, authenticated, service_role;
grant execute on function public.kb_admin_create_entry(uuid, text, text, text, text, text, text, text, text)
    to kb_admin_editor;

-- =============================================================================
-- 5. Checks (abort the whole file if any fails)
-- =============================================================================
do $$
declare
    f constant text := 'public.kb_admin_create_entry(uuid, text, text, text, text, text, text, text, text)';
    r text;
    c text;
begin
    if not exists (select 1 from pg_proc p where p.oid = f::regprocedure and p.prosecdef
                     and pg_get_userbyid(p.proowner) = 'kb_admin_fn_owner'
                     and exists (select 1 from unnest(p.proconfig) x where x like 'search_path=%pg_temp%')) then
        raise exception 'FAIL: kb_admin_create_entry is not SECURITY DEFINER / owned by kb_admin_fn_owner / pinned';
    end if;
    if not has_function_privilege('kb_admin_editor', f, 'EXECUTE') then
        raise exception 'FAIL: kb_admin_editor cannot EXECUTE kb_admin_create_entry';
    end if;
    foreach r in array array['anon', 'authenticated', 'service_role', 'public'] loop
        if has_function_privilege(r, f, 'EXECUTE') then
            raise exception 'FAIL: % can EXECUTE kb_admin_create_entry', r;
        end if;
    end loop;
    -- The insert surface is exactly these ten columns, and never table-wide.
    if has_table_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', 'INSERT')
       or has_table_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', 'DELETE') then
        raise exception 'FAIL: kb_admin_fn_owner holds table-wide INSERT or DELETE on the live table';
    end if;
    foreach c in array array['question', 'answer', 'content', 'embedding', 'id', 'frequency',
                             'page_or_section', 'created_at', 'updated_at'] loop
        if has_column_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', c, 'INSERT') then
            raise exception 'FAIL: kb_admin_fn_owner may INSERT column %', c;
        end if;
    end loop;
    foreach c in array array['rag_score_floor', 'chunk_type', 'source_document', 'namespace'] loop
        if has_column_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', c, 'UPDATE') then
            raise exception 'FAIL: kb_admin_fn_owner may UPDATE column %', c;
        end if;
    end loop;
    if not exists (select 1 from pg_policies where tablename = 'cb_knowledge_base_updated'
                     and policyname = 'kb_admin_fn_owner_insert_new_qa' and cmd = 'INSERT') then
        raise exception 'FAIL: the INSERT policy for kb_admin_fn_owner is missing';
    end if;
    if exists (select 1 from pg_policies where tablename = 'cb_knowledge_base_updated' and cmd in ('INSERT', 'ALL')
                 and not (roles = array['kb_admin_fn_owner']::name[])) then
        raise exception 'FAIL: an INSERT policy on the live table names another role';
    end if;
    if not (select pg_get_constraintdef(oid) like '%entry_created%' from pg_constraint
             where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check') then
        raise exception 'FAIL: cb_kb_audit_action_check does not allow entry_created';
    end if;
    if has_schema_privilege('kb_admin_fn_owner', 'public', 'CREATE') then
        raise exception 'FAIL: kb_admin_fn_owner kept CREATE on schema public';
    end if;
    raise notice 'PASS: 008 - kb_admin_create_entry in place, insert surface narrow, audit allows entry_created';
end
$$;

commit;
