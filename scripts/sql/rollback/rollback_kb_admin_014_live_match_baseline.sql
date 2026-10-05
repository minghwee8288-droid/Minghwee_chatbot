-- ROLLBACK of kb_admin_014_live_match_baseline.sql. Run FIRST (before rollback 012).
--
-- Puts kb_admin_doc_publish back exactly as 011 wrote it, drops
-- kb_admin_match_live and the baseline helper, the baseline INSERT policy,
-- restores the staged-chunk length CHECK to 009's, and drops is_baseline.
--
-- REFUSES (and changes nothing) while any baseline batch exists: its chunks may
-- be longer than 009's CHECK allows, and without them the imported rows they
-- record could never be restored. Dropping one is a decision, not a rollback.
--
-- Changes NO row of cb_knowledge_base_updated.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if to_regclass('public.cb_kb_batches') is not null
       and exists (select 1 from information_schema.columns where table_name = 'cb_kb_batches' and column_name = 'is_baseline') then
        if exists (select 1 from public.cb_kb_batches where is_baseline) then
            raise exception 'ROLLBACK 014 REFUSED: a baseline batch exists';
        end if;
    end if;
end
$$;

drop function if exists public.kb_admin_match_live(uuid, text, vector, text, text, text, integer, double precision);

-- 011's kb_admin_doc_publish, verbatim.
create or replace function public.kb_admin_doc_publish(
    p_actor_id    uuid,
    p_actor_email text,
    p_batch_id    uuid,
    p_reason      text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason   text := btrim(coalesce(p_reason, ''));
    v_batch    public.cb_kb_batches;
    v_doc      public.cb_kb_documents;
    v_canary   text;
    v_ids      uuid[];
    v_bad      integer;
    v_inserted integer;
    v_retired  integer;
    v_prev     uuid;
    r          record;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'approver');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;

    select * into v_batch from public.cb_kb_batches where id = p_batch_id for update;
    if not found then
        raise exception 'no such batch %', p_batch_id using errcode = 'KB002';
    end if;
    select * into v_doc from public.cb_kb_documents where id = v_batch.document_id for update;
    if v_batch.status <> 'staged' then
        raise exception 'the batch is %, not staged', v_batch.status using errcode = 'KB003';
    end if;
    select array_agg(s.id) into v_ids from public.cb_kb_staged_chunks s where s.batch_id = p_batch_id;
    if v_ids is null or cardinality(v_ids) <> v_batch.chunk_count then
        raise exception 'the batch has no chunks, or its count is out of step' using errcode = 'KB002';
    end if;
    if v_batch.impact_check_run_at is null then
        raise exception 'run the impact check before publishing' using errcode = 'KB003';
    end if;
    if v_batch.impact_check_batch_hash is distinct from public.kb_admin__doc_batch_hash(p_batch_id) then
        raise exception 'the batch changed after its impact check; run the check again' using errcode = 'KB003';
    end if;

    select c.model into v_canary from public.cb_kb_canary c where c.id = 1;
    if v_canary is null then
        raise exception 'cb_kb_canary is not seeded' using errcode = 'KB005';
    end if;
    select count(*) into v_bad from public.cb_kb_staged_chunks s
     where s.batch_id = p_batch_id and (s.embedding is null or s.model_name is distinct from v_canary);
    if v_bad > 0 then
        raise exception '% chunk(s) have no vector, or a vector from another model than the canary''s', v_bad
            using errcode = 'KB005';
    end if;
    -- The checks staging ran, again: nothing may have reached the table any
    -- other way, and the routing vocabulary may have moved since.
    for r in select * from public.cb_kb_staged_chunks s where s.batch_id = p_batch_id loop
        perform public.kb_admin__doc_check_chunk(r.chunk_type, r.content, r.section_heading,
                                                 r.service, r.audience, r.nationality);
    end loop;
    if exists (select 1 from public.cb_knowledge_base_updated k where k.id = any (v_ids)) then
        raise exception 'some of these chunks are already in the live table' using errcode = 'KB003';
    end if;

    -- a. In.
    perform set_config('kb_admin.via_function', 'on', true);
    insert into public.cb_knowledge_base_updated (
        id, chunk_type, source_document, section_heading, question, answer, content, embedding,
        is_active, service_type, contact_type, nationality, namespace, metadata, rag_score_floor)
    select s.id, public.kb_admin__doc_live_type(s.chunk_type), v_doc.source_name, s.section_heading,
           null, null, s.content, s.embedding, true, s.service, s.audience, s.nationality, s.namespace,
           jsonb_build_object(
               'keywords', '[]'::jsonb, 'priority', 3,
               'figures_present', public.kb_admin__doc_floor(s.content, s.section_heading) is not null,
               'table_column', null, 'date_valid_from', null)
           || s.metadata
           || jsonb_build_object('managed_by', 'ui', 'batch_id', p_batch_id,
                                 'document_id', v_doc.id, 'ordinal', s.ordinal),
           public.kb_admin__doc_floor(s.content, s.section_heading)
      from public.cb_kb_staged_chunks s
     where s.batch_id = p_batch_id
     order by s.ordinal;
    get diagnostics v_inserted = row_count;
    perform set_config('kb_admin.via_function', 'off', true);

    -- b. The old version out (qa_pair rows are never matched).
    v_retired := public.kb_admin__doc_switch_off(v_doc.source_name, v_ids,
                                                  jsonb_build_object('retired_by_batch', p_batch_id));

    -- d. Statuses: the old published batch first (one published per document).
    update public.cb_kb_batches set status = 'superseded'
     where document_id = v_doc.id and status = 'published'
    returning id into v_prev;
    update public.cb_kb_batches
       set status = 'published', published_by = p_actor_id, published_by_email = p_actor_email,
           published_at = now(), reason = v_reason
     where id = p_batch_id;

    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
    values ('batch_published', p_actor_id, p_actor_email,
            case when v_prev is null then null else jsonb_build_object('published_batch_id', v_prev) end,
            jsonb_build_object('batch_id', p_batch_id, 'document_id', v_doc.id, 'source_document', v_doc.source_name,
                               'rows_inserted', v_inserted, 'rows_retired', v_retired,
                               'superseded_batch_id', v_prev,
                               'impact_check_run_at', v_batch.impact_check_run_at,
                               'impact_check_batch_hash', v_batch.impact_check_batch_hash,
                               'self_published', v_batch.prepared_by is not distinct from p_actor_id),
            v_reason);

    return jsonb_build_object('batch_id', p_batch_id, 'document_id', v_doc.id, 'rows_inserted', v_inserted,
                              'rows_retired', v_retired, 'superseded_batch_id', v_prev);
end;
$$;

alter function public.kb_admin_doc_publish(uuid, text, uuid, text) owner to kb_admin_doc_owner;
revoke all on function public.kb_admin_doc_publish(uuid, text, uuid, text) from public, anon, authenticated, service_role;
grant execute on function public.kb_admin_doc_publish(uuid, text, uuid, text) to kb_admin_editor;

drop function if exists public.kb_admin__doc_record_baseline(uuid, text, uuid, text, text);
drop policy if exists kb_admin_doc_owner_insert_baseline on public.cb_kb_staged_chunks;

alter table public.cb_kb_staged_chunks drop constraint cb_kb_staged_chunks_length_check;
alter table public.cb_kb_staged_chunks add constraint cb_kb_staged_chunks_length_check
    check (char_length(content) <= case when chunk_type = 'passage' then 1200 else 3000 end);
alter table public.cb_kb_staged_chunks drop column if exists is_baseline;
alter table public.cb_kb_batches drop column if exists is_baseline;

do $$
begin
    if to_regprocedure('public.kb_admin_match_live(uuid, text, vector, text, text, text, integer, double precision)') is not null
       or to_regprocedure('public.kb_admin__doc_record_baseline(uuid, text, uuid, text, text)') is not null
       or pg_get_functiondef('public.kb_admin_doc_publish(uuid, text, uuid, text)'::regprocedure) like '%baseline%'
       or exists (select 1 from information_schema.columns where table_name in ('cb_kb_batches', 'cb_kb_staged_chunks')
                   and column_name = 'is_baseline') then
        raise exception 'ROLLBACK 014 CHECK FAILED: something of 014 is still present';
    end if;
    if (select pg_get_userbyid(proowner) from pg_proc where oid = 'public.kb_admin_doc_publish(uuid, text, uuid, text)'::regprocedure)
         <> 'kb_admin_doc_owner'
       or not has_function_privilege('kb_admin_editor', 'public.kb_admin_doc_publish(uuid, text, uuid, text)', 'EXECUTE') then
        raise exception 'ROLLBACK 014 CHECK FAILED: kb_admin_doc_publish ownership or EXECUTE is wrong';
    end if;
    raise notice 'ROLLBACK 014: done - publish is 011''s again, kb_admin_match_live and the baseline gone';
end
$$;

commit;
