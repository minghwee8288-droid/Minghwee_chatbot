-- kb_admin_014_live_match_baseline: an exact "now" for the impact check, and a
-- way back to a document's imported rows.
--
-- 1. kb_admin_match_live - READ ONLY. The bot's own live search
--    (cb_match_knowledge_base_updated: is_active, an embedding, no
--    style_example, the same three routing filters with their catch-alls, the
--    threshold raised to a row's own rag_score_floor), callable by
--    kb_admin_editor. Neither kb-admin login may read the embedding column, so
--    without this the impact check had no exact "top 5 now" for a document
--    that is being replaced. It returns the row's metadata too, because the
--    bot reranks by metadata.priority after the search.
--
-- 2. The BASELINE batch. A document imported before kb-admin existed has live
--    rows that belong to no batch, so once a publish switched them off nothing
--    could switch them back on (kb_admin_doc_restore restores a superseded
--    BATCH). kb_admin_doc_publish now first records those rows as a batch of
--    their own - status 'superseded', is_baseline true, one staged chunk per
--    live row with the SAME id, text, heading, routing, metadata and vector -
--    and the unchanged kb_admin_doc_restore can then switch them back on.
--    Rows already in some batch are never recorded again (a staged chunk's id
--    is its primary key), so a second publish adds no second baseline.
--
--    What that needs, and nothing more:
--      * is_baseline on cb_kb_batches and cb_kb_staged_chunks (default false);
--      * the staged-chunk length CHECK exempts a baseline chunk - imported rows
--        run to 2,076 characters (passage) and 5,512 (table), the very defect
--        the chunker fixes, and the baseline must hold them exactly;
--      * one more INSERT policy for kb_admin_doc_owner on staged chunks: a
--        baseline chunk into a baseline batch that is 'superseded' (010's
--        policy admits only a 'staged' batch, and a document may have just one
--        staged batch - the one being published);
--      * kb_admin_reader may read is_baseline (never file_bytes or embedding).
--    kb_admin_doc_publish is replaced with ONE added step (the baseline call,
--    after the canary and chunk checks, before anything is inserted) and its
--    audit entry and result gain baseline_batch_id. Nothing else in it changed.
--
-- The model recorded on a baseline chunk is the canary's: the imported rows
-- are the vectors the bot searches today, in the canary's space (proved for
-- the import's Q&A rows by scripts/check_embedding_parity.mjs).
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>
-- Rollback:  scripts/sql/rollback/rollback_kb_admin_014_live_match_baseline.sql

begin;

do $$
begin
    if to_regprocedure('public.kb_admin_doc_publish(uuid, text, uuid, text)') is null
       or to_regprocedure('public.kb_admin_match_with_batch(uuid, text, uuid, vector, text, text, text, integer, double precision)') is null then
        raise exception '014 needs 011 applied first';
    end if;
end
$$;

-- =============================================================================
-- Tables
-- =============================================================================
alter table public.cb_kb_batches add column if not exists is_baseline boolean not null default false;
alter table public.cb_kb_staged_chunks add column if not exists is_baseline boolean not null default false;

alter table public.cb_kb_staged_chunks drop constraint cb_kb_staged_chunks_length_check;
alter table public.cb_kb_staged_chunks add constraint cb_kb_staged_chunks_length_check
    check (is_baseline or char_length(content) <= case when chunk_type = 'passage' then 1200 else 3000 end);

grant select (is_baseline) on public.cb_kb_batches to kb_admin_reader;
grant select (is_baseline) on public.cb_kb_staged_chunks to kb_admin_reader;

do $$
begin
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_staged_chunks'
                    and policyname = 'kb_admin_doc_owner_insert_baseline') then
        create policy kb_admin_doc_owner_insert_baseline on public.cb_kb_staged_chunks
            as permissive for insert to kb_admin_doc_owner
            with check (is_baseline and exists (select 1 from public.cb_kb_batches b
                                                 where b.id = batch_id and b.is_baseline and b.status = 'superseded'));
    end if;
end
$$;

-- =============================================================================
-- 1. kb_admin_match_live
-- =============================================================================
-- What the bot's search returns today: cb_match_knowledge_base_updated's pool,
-- filters, threshold and order, read as kb_admin_doc_owner (which may read the
-- embedding column). Read-only (STABLE). Caller: an editor or approver.
create or replace function public.kb_admin_match_live(
    p_actor_id        uuid,
    p_actor_email     text,
    p_query_embedding vector,
    p_service         text,
    p_audience        text,
    p_nationality     text,
    p_match_count     integer default 5,
    p_match_threshold double precision default 0.35)
returns table (
    id              uuid,
    chunk_type      text,
    source_document text,
    section_heading text,
    service_type    text,
    contact_type    text,
    nationality     text,
    rag_score_floor numeric,
    metadata        jsonb,
    similarity      double precision,
    preview         text)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if p_query_embedding is null or vector_dims(p_query_embedding) <> 1536 then
        raise exception 'the question''s embedding must have 1536 dimensions' using errcode = 'KB005';
    end if;
    return query
    select k.id, k.chunk_type::text, k.source_document::text, k.section_heading, k.service_type::text,
           k.contact_type::text, k.nationality::text, k.rag_score_floor, k.metadata,
           (1 - (k.embedding <=> p_query_embedding))::double precision,
           left(coalesce(k.content, concat_ws(E'\n', k.question, k.answer)), 300)
      from public.cb_knowledge_base_updated k
     where k.is_active
       and k.embedding is not null
       and k.chunk_type <> 'style_example'
       and (p_service     is null or k.service_type in (p_service, 'general'))
       and (p_audience    is null or k.contact_type in (p_audience, 'all'))
       and (p_nationality is null or k.nationality  in (p_nationality, 'all'))
       and (1 - (k.embedding <=> p_query_embedding))
             >= greatest(p_match_threshold, coalesce(k.rag_score_floor::double precision, 0))
     order by k.embedding <=> p_query_embedding
     limit greatest(coalesce(p_match_count, 5), 1);
end;
$$;

-- =============================================================================
-- 2. The baseline
-- =============================================================================
-- Records a source's active document_chunk / table_unit rows that belong to no
-- batch as a 'superseded' baseline batch, so restore can bring them back.
-- Returns the new batch id, or NULL when there is nothing to record.
create or replace function public.kb_admin__doc_record_baseline(
    p_document_id uuid, p_source text, p_actor_id uuid, p_actor_email text, p_model_name text)
returns uuid
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_batch uuid;
    n       integer;
begin
    select count(*) into n
      from public.cb_knowledge_base_updated k
     where k.source_document = p_source and k.is_active
       and k.chunk_type in ('document_chunk', 'table_unit')
       and not exists (select 1 from public.cb_kb_staged_chunks s where s.id = k.id);
    if n = 0 then
        return null;
    end if;

    insert into public.cb_kb_batches (document_id, status, chunk_count, prepared_by, prepared_by_email,
                                      reason, is_baseline)
    values (p_document_id, 'superseded', n, p_actor_id, p_actor_email,
            'the version that was live before this document was first replaced in kb-admin', true)
    returning id into v_batch;

    insert into public.cb_kb_staged_chunks (id, batch_id, ordinal, chunk_type, section_heading, content,
                                            content_hash, embedding, model_name, service, audience,
                                            nationality, namespace, metadata, is_baseline)
    select k.id, v_batch, (row_number() over (order by k.id))::integer - 1,
           case k.chunk_type when 'document_chunk' then 'passage' else 'table_unit' end,
           k.section_heading,
           coalesce(nullif(k.content, ''), concat_ws(E'\n', k.question, k.answer)),
           public.kb_admin__doc_content_hash(coalesce(nullif(k.content, ''), concat_ws(E'\n', k.question, k.answer))),
           k.embedding, p_model_name, k.service_type, k.contact_type, k.nationality, k.namespace,
           coalesce(k.metadata, '{}'::jsonb), true
      from public.cb_knowledge_base_updated k
     where k.source_document = p_source and k.is_active
       and k.chunk_type in ('document_chunk', 'table_unit')
       and not exists (select 1 from public.cb_kb_staged_chunks s where s.id = k.id);
    return v_batch;
end;
$$;

-- kb_admin_doc_publish, as in 011, plus the baseline step (marked "014").
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
    v_baseline uuid;
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

    -- 014: live rows that belong to no batch (the imported version) are recorded
    -- as a superseded baseline batch first, so kb_admin_doc_restore can bring
    -- them back after step b switches them off.
    v_baseline := public.kb_admin__doc_record_baseline(v_doc.id, v_doc.source_name, p_actor_id, p_actor_email, v_canary);

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
                               'baseline_batch_id', v_baseline,
                               'impact_check_run_at', v_batch.impact_check_run_at,
                               'impact_check_batch_hash', v_batch.impact_check_batch_hash,
                               'self_published', v_batch.prepared_by is not distinct from p_actor_id),
            v_reason);

    return jsonb_build_object('batch_id', p_batch_id, 'document_id', v_doc.id, 'rows_inserted', v_inserted,
                              'rows_retired', v_retired, 'superseded_batch_id', v_prev,
                              'baseline_batch_id', v_baseline);
end;
$$;

-- =============================================================================
-- Ownership and EXECUTE (the 004 pattern)
-- =============================================================================
grant create on schema public to kb_admin_doc_owner;
do $$
declare
    f regprocedure;
begin
    foreach f in array array[
        'public.kb_admin_match_live(uuid, text, vector, text, text, text, integer, double precision)',
        'public.kb_admin__doc_record_baseline(uuid, text, uuid, text, text)',
        'public.kb_admin_doc_publish(uuid, text, uuid, text)'
    ]::regprocedure[] loop
        execute format('alter function %s owner to kb_admin_doc_owner', f);
        execute format('revoke all on function %s from public, anon, authenticated, service_role', f);
    end loop;
end
$$;
revoke create on schema public from kb_admin_doc_owner;

grant execute on function
    public.kb_admin_match_live(uuid, text, vector, text, text, text, integer, double precision),
    public.kb_admin_doc_publish(uuid, text, uuid, text)
to kb_admin_editor;

-- =============================================================================
-- Checks (abort the whole file if any fails)
-- =============================================================================
do $$
declare
    f text;
    r text;
begin
    foreach f in array array[
        'public.kb_admin_match_live(uuid, text, vector, text, text, text, integer, double precision)',
        'public.kb_admin__doc_record_baseline(uuid, text, uuid, text, text)',
        'public.kb_admin_doc_publish(uuid, text, uuid, text)'] loop
        if (select pg_get_userbyid(proowner) from pg_proc where oid = f::regprocedure) <> 'kb_admin_doc_owner' then
            raise exception 'FAIL: % is not owned by kb_admin_doc_owner', f;
        end if;
        if not exists (select 1 from pg_proc p, unnest(p.proconfig) c where p.oid = f::regprocedure and c like 'search_path=%pg_temp%') then
            raise exception 'FAIL: % has no pinned search_path', f;
        end if;
        foreach r in array array['anon', 'authenticated', 'service_role', 'public', 'kb_admin_reader'] loop
            if has_function_privilege(r, f, 'EXECUTE') then
                raise exception 'FAIL: % can EXECUTE %', r, f;
            end if;
        end loop;
    end loop;
    if (select prosecdef from pg_proc where oid = 'public.kb_admin__doc_record_baseline(uuid, text, uuid, text, text)'::regprocedure)
       or has_function_privilege('kb_admin_editor', 'public.kb_admin__doc_record_baseline(uuid, text, uuid, text, text)', 'EXECUTE') then
        raise exception 'FAIL: the baseline helper must be private and not SECURITY DEFINER';
    end if;
    if (select provolatile from pg_proc where oid =
          'public.kb_admin_match_live(uuid, text, vector, text, text, text, integer, double precision)'::regprocedure) <> 's'
       or not has_function_privilege('kb_admin_editor',
          'public.kb_admin_match_live(uuid, text, vector, text, text, text, integer, double precision)', 'EXECUTE')
       or not has_function_privilege('kb_admin_editor', 'public.kb_admin_doc_publish(uuid, text, uuid, text)', 'EXECUTE') then
        raise exception 'FAIL: kb_admin_match_live must be STABLE, and the editor must execute it and publish';
    end if;
    if (select count(*) from pg_proc pr join pg_namespace ns on ns.oid = pr.pronamespace
         where ns.nspname = 'public' and pr.prosecdef and pr.proname like 'kb\_admin\_%' and pr.proname not like 'kb\_admin\_\_%'
           and has_function_privilege('kb_admin_editor', pr.oid, 'EXECUTE')) <> 16 then
        raise exception 'FAIL: kb_admin_editor should EXECUTE exactly 16 kb_admin_* functions';
    end if;
    if has_schema_privilege('kb_admin_doc_owner', 'public', 'CREATE') then
        raise exception 'FAIL: kb_admin_doc_owner kept CREATE on schema public';
    end if;
    if has_column_privilege('kb_admin_reader', 'public.cb_kb_staged_chunks', 'embedding', 'SELECT')
       or not has_column_privilege('kb_admin_reader', 'public.cb_kb_staged_chunks', 'is_baseline', 'SELECT')
       or not has_column_privilege('kb_admin_reader', 'public.cb_kb_batches', 'is_baseline', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader column grants are wrong';
    end if;
    if pg_get_constraintdef((select oid from pg_constraint where conname = 'cb_kb_staged_chunks_length_check'))
       not like '%is_baseline%' then
        raise exception 'FAIL: the staged-chunk length CHECK does not exempt baseline chunks';
    end if;
    raise notice 'PASS: 014 - kb_admin_match_live and the baseline publish installed; kb_admin_editor executes 16 kb_admin_* functions';
end
$$;

commit;
