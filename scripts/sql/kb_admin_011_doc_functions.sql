-- kb_admin_011_doc_functions: the document functions (Phase 3 scope B).
--
-- Nine SECURITY DEFINER functions, owned by kb_admin_doc_owner (010), with a
-- pinned search_path. kb_admin_editor may EXECUTE them; nobody else may. Every
-- one takes p_actor_id and p_actor_email first and checks the actor's role in
-- cb_kb_admin_users itself (004's kb_admin__require_actor), so a bug in
-- kb-admin's UI cannot let a viewer write.
--
--   1. kb_admin_doc_upload        editor   store a file, open a staged batch
--   2. kb_admin_doc_stage         editor   add chunks (text + vector) to it
--   3. kb_admin_doc_edit_chunk    editor   change one staged chunk
--   4. kb_admin_doc_mark_checked  editor   record that the impact check ran
--   5. kb_admin_doc_publish       APPROVER make the batch live, retire the old
--   6. kb_admin_doc_restore       APPROVER switch an earlier batch back on
--   7. kb_admin_doc_discard       editor   drop a staged batch
--   8. kb_admin_doc_retire        APPROVER switch a whole document off
--   9. kb_admin_match_with_batch  editor   READ ONLY: what search would return
--                                          if this batch were published
--
-- (4) is not in the original brief: publish requires a recorded impact check,
-- and nothing else could record one without making (9) write. Agreed
-- 2026-10-01. Staging or editing a chunk clears the record, and publish
-- recomputes the hash and refuses if it no longer matches.
--
-- WHAT THE CHATBOT SEES. Only publish, restore and retire change the live
-- table, and only these columns of these rows (the RLS in 010 enforces it):
--   * publish INSERTs the batch's chunks as document_chunk (a staged passage)
--     or table_unit, is_active true, managed_by 'ui', source_document = the
--     document's source_name, each with the SAME id as its staged chunk, so a
--     restore can find them again by id;
--   * publish, restore and retire switch OFF (is_active false, stamped
--     managed_by 'ui') every other active document_chunk / table_unit row of
--     that source_document. A qa_pair row is never touched, whatever its
--     source, and 'Ming Hwee Service Notes' rows are refused outright.
-- Nothing is ever deleted from the live table: an old version stays, switched
-- off, and restore switches it back on with the vector it already has.
--
-- THE VECTORS. Each staged chunk carries its vector and the model that made
-- it. Staging refuses a model that is not cb_kb_canary's (the Phase 2 rule),
-- and publish and restore check again, so a canary re-seeded with a new model
-- forces a re-embed rather than mixing two vector spaces in one search.
--
-- Errors (the 004 codes): KB001 not an active editor/approver, or not the
-- batch's author; KB002 invalid input; KB003 conflict (wrong state, stale
-- impact check); KB004 needs an approver; KB005 embedding problem; KB006
-- NRIC/FIN in the text.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>
-- Rollback:  scripts/sql/rollback/rollback_kb_admin_011_doc_functions.sql

begin;

-- =============================================================================
-- Internal helpers (owner only; kb_admin_editor cannot call them)
-- =============================================================================

-- A source name kb-admin may never upload under. The loader's Q&A and scope
-- A's Q&A have their own names, and the bot drops anything from an internal
-- document (rag._INTERNAL_SOURCES, mirrored here: same alternatives, matched
-- after separators are normalised to spaces, as rag._normalize_source does),
-- plus anything calling itself internal.
create or replace function public.kb_admin__doc_check_source(p_source text)
returns void
language plpgsql immutable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v text := btrim(regexp_replace(coalesce(p_source, ''), '[-_[:space:]]+', ' ', 'g'));
begin
    if v = '' then
        raise exception 'a source name is required' using errcode = 'KB002';
    end if;
    if length(p_source) > 500 then
        raise exception 'the source name is too long (limit 500)' using errcode = 'KB002';
    end if;
    if lower(v) in ('ming hwee service notes', 'ming hwee kb admin') then
        raise exception 'the source name "%" is reserved', p_source using errcode = 'KB002';
    end if;
    if v ~* '(MHOS\M|for Vendor|Blueprint|Hiring Pipelines Brief|version control|\minternal\M)' then
        raise exception 'the source name "%" is an internal document, which the chatbot never uses', p_source
            using errcode = 'KB002';
    end if;
end;
$$;

-- The namespace a chunk is filed under: the one given, if active rows already
-- use it; otherwise the one most active rows of that service use (the 008
-- rule). It is a label only - RAG_NAMESPACE is unset, so the bot never
-- filters on it - but an unknown one would still be a stray label.
create or replace function public.kb_admin__doc_namespace(p_service text, p_namespace text)
returns text
language plpgsql stable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_ns text := nullif(btrim(coalesce(p_namespace, '')), '');
begin
    if v_ns is not null then
        if not exists (select 1 from public.cb_knowledge_base_updated k where k.is_active and k.namespace = v_ns) then
            raise exception 'unknown namespace "%"', v_ns using errcode = 'KB002';
        end if;
        return v_ns;
    end if;
    select k.namespace into v_ns
      from public.cb_knowledge_base_updated k
     where k.is_active and k.service_type = p_service
     group by k.namespace order by count(*) desc, k.namespace limit 1;
    return coalesce(v_ns, 'services_general');
end;
$$;

-- One chunk's text and routing, checked. Raises; returns nothing.
create or replace function public.kb_admin__doc_check_chunk(
    p_chunk_type text, p_content text, p_heading text,
    p_service text, p_audience text, p_nationality text)
returns void
language plpgsql stable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_limit int;
begin
    if p_chunk_type is null or p_chunk_type not in ('passage', 'table_unit') then
        raise exception 'chunk_type must be passage or table_unit' using errcode = 'KB002';
    end if;
    if length(btrim(coalesce(p_content, ''))) = 0 then
        raise exception 'a chunk has no text' using errcode = 'KB002';
    end if;
    v_limit := case when p_chunk_type = 'passage' then 1200 else 3000 end;
    if char_length(p_content) > v_limit then
        raise exception 'a % is too long (% characters, limit %)', p_chunk_type, char_length(p_content), v_limit
            using errcode = 'KB002';
    end if;
    perform public.kb_admin__refuse_nric(p_content, coalesce(p_heading, ''));
    -- '' as the "current" service: NULL would let any value through (see 008).
    if btrim(coalesce(p_service, '')) = '' then
        raise exception 'unknown service ""' using errcode = 'KB002';
    end if;
    perform public.kb_admin__check_routing(p_service, p_audience, p_nationality, '');
end;
$$;

-- A float8[] from the caller, as a 1536-dimension vector, or KB005.
create or replace function public.kb_admin__doc_vector(p_embedding float8[])
returns vector
language plpgsql immutable
set search_path = pg_catalog, public, pg_temp
as $$
begin
    if p_embedding is null then
        raise exception 'a chunk has no embedding' using errcode = 'KB005';
    end if;
    if cardinality(p_embedding) <> 1536 or array_ndims(p_embedding) <> 1 then
        raise exception 'embedding has % values, not 1536', cardinality(p_embedding) using errcode = 'KB005';
    end if;
    if array_position(p_embedding, null) is not null then
        raise exception 'embedding contains an empty value' using errcode = 'KB005';
    end if;
    return p_embedding::vector(1536);
end;
$$;

-- The model a new vector must come from: cb_kb_canary's, or KB005.
create or replace function public.kb_admin__doc_require_model(p_model_name text)
returns text
language plpgsql stable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_canary text;
begin
    select c.model into v_canary from public.cb_kb_canary c where c.id = 1;
    if v_canary is null then
        raise exception 'cb_kb_canary is not seeded; refusing to store a new vector' using errcode = 'KB005';
    end if;
    if p_model_name is distinct from v_canary then
        raise exception 'embedding model "%" is not the canary''s model "%"', p_model_name, v_canary
            using errcode = 'KB005';
    end if;
    return v_canary;
end;
$$;

create or replace function public.kb_admin__doc_content_hash(p_content text)
returns text
language sql immutable
set search_path = pg_catalog, public, pg_temp
as $$
    select encode(sha256(convert_to(p_content, 'UTF8')), 'hex');
$$;

-- The fingerprint of everything in a batch that would change what goes live:
-- each chunk's position, type, heading, text, routing, metadata, model and
-- vector. The impact check records it; publish recomputes and compares.
create or replace function public.kb_admin__doc_batch_hash(p_batch_id uuid)
returns text
language sql stable
set search_path = pg_catalog, public, pg_temp
as $$
    select encode(sha256(convert_to(coalesce(string_agg(
               format('%s|%L|%L|%L|%L|%L|%L|%L|%L|%L|%L',
                      s.ordinal, s.chunk_type, s.section_heading, s.content_hash, s.service,
                      s.audience, s.nationality, s.namespace, s.metadata::text, s.model_name,
                      md5(coalesce(s.embedding::text, ''))),
               E'\n' order by s.ordinal), ''), 'UTF8')), 'hex')
      from public.cb_kb_staged_chunks s where s.batch_id = p_batch_id;
$$;

-- The live chunk_type for a staged one: the live table has no 'passage'.
create or replace function public.kb_admin__doc_live_type(p_chunk_type text)
returns text
language sql immutable
set search_path = pg_catalog, public, pg_temp
as $$
    select case p_chunk_type when 'passage' then 'document_chunk' else p_chunk_type end;
$$;

-- The per-row floor publish would set: the import's convention (0.420 when the
-- text carries a figure), the same rule 008 uses.
create or replace function public.kb_admin__doc_floor(p_content text, p_heading text)
returns numeric
language sql immutable
set search_path = pg_catalog, public, pg_temp
as $$
    select case when cardinality(public.kb_admin__numbers(concat_ws(' ', p_heading, p_content))) > 0
                then 0.420 else null end::numeric(4,3);
$$;

-- Switch OFF every active document_chunk / table_unit row of a source, except
-- p_keep. Stamps them managed_by 'ui' so the loader leaves them alone. Never a
-- qa_pair (the WHERE, and 010's RLS). Returns how many.
create or replace function public.kb_admin__doc_switch_off(
    p_source text, p_keep uuid[], p_note jsonb)
returns integer
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $$
declare
    n integer;
begin
    perform set_config('kb_admin.via_function', 'on', true);
    update public.cb_knowledge_base_updated k
       set is_active = false,
           metadata  = coalesce(k.metadata, '{}'::jsonb) || jsonb_build_object('managed_by', 'ui') || p_note
     where k.source_document = p_source
       and k.chunk_type in ('document_chunk', 'table_unit')
       and k.is_active
       and not (k.id = any (coalesce(p_keep, '{}'::uuid[])));
    get diagnostics n = row_count;
    perform set_config('kb_admin.via_function', 'off', true);
    return n;
end;
$$;

-- An open batch of this document no longer has a valid impact check once
-- what is live for the document changes.
create or replace function public.kb_admin__doc_clear_checks(p_document_id uuid)
returns void
language sql
set search_path = pg_catalog, public, pg_temp
as $$
    update public.cb_kb_batches
       set impact_check_run_at = null, impact_check_batch_hash = null
     where document_id = p_document_id and status = 'staged' and impact_check_run_at is not null;
$$;

-- =============================================================================
-- 1. kb_admin_doc_upload
-- =============================================================================
-- Stores the file and opens a staged batch. Changes nothing the chatbot reads.
-- A source already uploaded gets its file replaced and a NEW batch (refused
-- while an earlier batch of it is still staged: publish or discard that first).
-- Returns {document_id, batch_id, replaced_file}.
create or replace function public.kb_admin_doc_upload(
    p_actor_id      uuid,
    p_actor_email   text,
    p_source_name   text,
    p_file_name     text,
    p_file_type     text,
    p_file_bytes    bytea,
    p_display_label text default null)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_source text := btrim(coalesce(p_source_name, ''));
    v_file   text := btrim(coalesce(p_file_name, ''));
    v_type   text := lower(btrim(coalesce(p_file_type, '')));
    v_label  text := nullif(btrim(coalesce(p_display_label, '')), '');
    v_text   text;
    v_doc    uuid;
    v_batch  uuid;
    v_replaced boolean := false;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    perform public.kb_admin__doc_check_source(v_source);
    if v_type not in ('docx', 'md', 'txt') then
        raise exception 'file type must be docx, md or txt' using errcode = 'KB002';
    end if;
    if v_file = '' or lower(v_file) not like '%.' || v_type then
        raise exception 'the file name must end in .%', v_type using errcode = 'KB002';
    end if;
    if p_file_bytes is null or octet_length(p_file_bytes) = 0 then
        raise exception 'the file is empty' using errcode = 'KB002';
    end if;
    if octet_length(p_file_bytes) > 5242880 then
        raise exception 'the file is % bytes; the limit is 5 MB', octet_length(p_file_bytes) using errcode = 'KB002';
    end if;
    perform public.kb_admin__refuse_nric(v_source, v_file, coalesce(v_label, ''));
    -- A text file is checked whole here; a .docx is checked chunk by chunk at
    -- staging, once it has been turned into text.
    if v_type in ('md', 'txt') then
        begin
            v_text := convert_from(p_file_bytes, 'UTF8');
        exception when others then
            raise exception 'the file is not UTF-8 text' using errcode = 'KB002';
        end;
        perform public.kb_admin__refuse_nric(v_text);
    end if;

    select d.id into v_doc from public.cb_kb_documents d where d.source_name = v_source for update;
    if found then
        if exists (select 1 from public.cb_kb_batches b where b.document_id = v_doc and b.status = 'staged') then
            raise exception 'this document already has a batch being prepared; publish or discard it first'
                using errcode = 'KB003';
        end if;
        update public.cb_kb_documents
           set file_name = v_file, file_type = v_type, file_bytes = p_file_bytes,
               file_size_bytes = octet_length(p_file_bytes), display_label = coalesce(v_label, display_label),
               uploaded_by = p_actor_id, uploaded_by_email = p_actor_email, uploaded_at = now()
         where id = v_doc;
        v_replaced := true;
    else
        insert into public.cb_kb_documents (source_name, display_label, file_name, file_type, file_bytes,
                                            file_size_bytes, uploaded_by, uploaded_by_email)
        values (v_source, v_label, v_file, v_type, p_file_bytes, octet_length(p_file_bytes),
                p_actor_id, p_actor_email)
        returning id into v_doc;
    end if;

    insert into public.cb_kb_batches (document_id, status, prepared_by, prepared_by_email)
    values (v_doc, 'staged', p_actor_id, p_actor_email)
    returning id into v_batch;

    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
    values ('doc_uploaded', p_actor_id, p_actor_email, null,
            jsonb_build_object('document_id', v_doc, 'batch_id', v_batch, 'source_name', v_source,
                               'file_name', v_file, 'file_type', v_type,
                               'file_size_bytes', octet_length(p_file_bytes), 'replaced_file', v_replaced),
            'uploaded ' || v_file);

    return jsonb_build_object('document_id', v_doc, 'batch_id', v_batch, 'replaced_file', v_replaced);
end;
$$;

-- =============================================================================
-- 2. kb_admin_doc_stage
-- =============================================================================
-- Adds chunks to a staged batch. May be called more than once (large
-- documents in pages); an ordinal already in the batch is refused.
-- p_chunks: a JSON array of
--   {ordinal, chunk_type ('passage'|'table_unit'), section_heading, content,
--    embedding (1536 numbers), service, audience, nationality, namespace, metadata}
-- p_model_name: the model that made every vector in this call (must be the
-- canary's). Caller: the editor who opened the batch.
-- Returns the number of chunks added.
create or replace function public.kb_admin_doc_stage(
    p_actor_id    uuid,
    p_actor_email text,
    p_batch_id    uuid,
    p_chunks      jsonb,
    p_model_name  text)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_batch  public.cb_kb_batches;
    v_model  text;
    e        jsonb;
    v_ord    integer;
    v_type   text;
    v_text   text;
    v_head   text;
    v_svc    text;
    v_aud    text;
    v_nat    text;
    v_ns     text;
    v_meta   jsonb;
    v_emb    float8[];
    n        integer := 0;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    select * into v_batch from public.cb_kb_batches where id = p_batch_id for update;
    if not found then
        raise exception 'no such batch %', p_batch_id using errcode = 'KB002';
    end if;
    if v_batch.status <> 'staged' then
        raise exception 'the batch is %, not staged', v_batch.status using errcode = 'KB003';
    end if;
    if v_batch.prepared_by is distinct from p_actor_id then
        raise exception 'only the person who uploaded this document can add its chunks' using errcode = 'KB001';
    end if;
    if p_chunks is null or jsonb_typeof(p_chunks) <> 'array' or jsonb_array_length(p_chunks) = 0 then
        raise exception 'no chunks were given' using errcode = 'KB002';
    end if;
    v_model := public.kb_admin__doc_require_model(p_model_name);

    for e in select x from jsonb_array_elements(p_chunks) x loop
        if jsonb_typeof(e) <> 'object' or jsonb_typeof(e -> 'ordinal') <> 'number' then
            raise exception 'each chunk needs a numeric ordinal' using errcode = 'KB002';
        end if;
        v_ord  := (e ->> 'ordinal')::numeric::integer;
        v_type := e ->> 'chunk_type';
        v_text := e ->> 'content';
        v_head := nullif(btrim(coalesce(e ->> 'section_heading', '')), '');
        v_svc  := btrim(coalesce(e ->> 'service', ''));
        v_aud  := e ->> 'audience';
        v_nat  := e ->> 'nationality';
        v_meta := coalesce(e -> 'metadata', '{}'::jsonb);
        if jsonb_typeof(v_meta) <> 'object' then
            raise exception 'chunk %: metadata must be an object', v_ord using errcode = 'KB002';
        end if;
        if jsonb_typeof(e -> 'embedding') is distinct from 'array' then
            raise exception 'chunk %: embedding must be an array of 1536 numbers', v_ord using errcode = 'KB005';
        end if;
        perform public.kb_admin__doc_check_chunk(v_type, v_text, v_head, v_svc, v_aud, v_nat);
        v_ns := public.kb_admin__doc_namespace(v_svc, e ->> 'namespace');
        begin
            select array_agg(x::float8 order by i) into v_emb
              from jsonb_array_elements_text(e -> 'embedding') with ordinality t(x, i);
        exception when others then
            raise exception 'chunk %: embedding must be numbers', v_ord using errcode = 'KB005';
        end;
        if v_ord < 0 or exists (select 1 from public.cb_kb_staged_chunks s
                                 where s.batch_id = p_batch_id and s.ordinal = v_ord) then
            raise exception 'chunk %: that position is negative or already staged', v_ord using errcode = 'KB002';
        end if;

        insert into public.cb_kb_staged_chunks (batch_id, ordinal, chunk_type, section_heading, content,
                                                content_hash, embedding, model_name, service, audience,
                                                nationality, namespace, metadata)
        values (p_batch_id, v_ord, v_type, v_head, v_text, public.kb_admin__doc_content_hash(v_text),
                public.kb_admin__doc_vector(v_emb), v_model, v_svc, v_aud, v_nat, v_ns, v_meta);
        n := n + 1;
    end loop;

    update public.cb_kb_batches
       set chunk_count = (select count(*) from public.cb_kb_staged_chunks s where s.batch_id = p_batch_id),
           impact_check_run_at = null, impact_check_batch_hash = null
     where id = p_batch_id
    returning * into v_batch;

    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
    values ('batch_prepared', p_actor_id, p_actor_email, null,
            jsonb_build_object('batch_id', p_batch_id, 'document_id', v_batch.document_id,
                               'chunks_added', n, 'chunk_count', v_batch.chunk_count, 'model_name', v_model),
            format('staged %s chunk(s)', n));
    return n;
end;
$$;

-- =============================================================================
-- 3. kb_admin_doc_edit_chunk
-- =============================================================================
-- Changes one chunk of a staged batch. A new text needs a new vector
-- (p_embedding + p_model_name); routing or heading alone keeps the old one.
-- Caller: the batch's author, or an approver. Clears the impact check.
-- Returns {chunk_id, batch_id, reembedded}.
create or replace function public.kb_admin_doc_edit_chunk(
    p_actor_id        uuid,
    p_actor_email     text,
    p_chunk_id        uuid,
    p_content         text,
    p_section_heading text,
    p_service         text,
    p_audience        text,
    p_nationality     text,
    p_namespace       text,
    p_embedding       float8[],
    p_model_name      text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_role   text;
    v_chunk  public.cb_kb_staged_chunks;
    v_batch  public.cb_kb_batches;
    v_head   text := nullif(btrim(coalesce(p_section_heading, '')), '');
    v_svc    text := btrim(coalesce(p_service, ''));
    v_ns     text;
    v_emb    vector;
    v_model  text;
    v_text_changed boolean;
begin
    v_role := public.kb_admin__require_actor(p_actor_id, 'editor');
    select * into v_chunk from public.cb_kb_staged_chunks where id = p_chunk_id;
    if not found then
        raise exception 'no such staged chunk %', p_chunk_id using errcode = 'KB002';
    end if;
    select * into v_batch from public.cb_kb_batches where id = v_chunk.batch_id for update;
    if v_batch.status <> 'staged' then
        raise exception 'the batch is %, not staged', v_batch.status using errcode = 'KB003';
    end if;
    if v_batch.prepared_by is distinct from p_actor_id and v_role <> 'approver' then
        raise exception 'only the batch''s author or an approver can edit it' using errcode = 'KB001';
    end if;
    select * into v_chunk from public.cb_kb_staged_chunks where id = p_chunk_id for update;

    perform public.kb_admin__doc_check_chunk(v_chunk.chunk_type, p_content, v_head, v_svc, p_audience, p_nationality);
    v_ns := public.kb_admin__doc_namespace(v_svc, p_namespace);
    v_text_changed := p_content is distinct from v_chunk.content;

    if p_embedding is not null then
        v_model := public.kb_admin__doc_require_model(p_model_name);
        v_emb := public.kb_admin__doc_vector(p_embedding);
    elsif v_text_changed then
        raise exception 'the text changed; a new embedding is required' using errcode = 'KB005';
    else
        v_emb := v_chunk.embedding;
        v_model := v_chunk.model_name;
    end if;

    if not v_text_changed and p_embedding is null
       and v_head is not distinct from v_chunk.section_heading and v_svc is not distinct from v_chunk.service
       and p_audience is not distinct from v_chunk.audience and p_nationality is not distinct from v_chunk.nationality
       and v_ns is not distinct from v_chunk.namespace then
        raise exception 'nothing changed' using errcode = 'KB002';
    end if;

    update public.cb_kb_staged_chunks
       set content = p_content, content_hash = public.kb_admin__doc_content_hash(p_content),
           section_heading = v_head, service = v_svc, audience = p_audience, nationality = p_nationality,
           namespace = v_ns, embedding = v_emb, model_name = v_model
     where id = p_chunk_id;
    update public.cb_kb_batches set impact_check_run_at = null, impact_check_batch_hash = null where id = v_batch.id;

    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
    values ('chunk_edited', p_actor_id, p_actor_email,
            jsonb_build_object('chunk_id', p_chunk_id, 'batch_id', v_batch.id, 'ordinal', v_chunk.ordinal,
                               'content', v_chunk.content, 'section_heading', v_chunk.section_heading,
                               'service', v_chunk.service, 'audience', v_chunk.audience,
                               'nationality', v_chunk.nationality, 'namespace', v_chunk.namespace),
            jsonb_build_object('chunk_id', p_chunk_id, 'batch_id', v_batch.id, 'ordinal', v_chunk.ordinal,
                               'content', p_content, 'section_heading', v_head, 'service', v_svc,
                               'audience', p_audience, 'nationality', p_nationality, 'namespace', v_ns,
                               'reembedded', p_embedding is not null),
            format('edited chunk %s', v_chunk.ordinal));

    return jsonb_build_object('chunk_id', p_chunk_id, 'batch_id', v_batch.id, 'reembedded', p_embedding is not null);
end;
$$;

-- =============================================================================
-- 4. kb_admin_doc_mark_checked
-- =============================================================================
-- Records that the impact check has been run on the batch as it now stands:
-- the time, and the batch fingerprint computed HERE (never taken from the
-- caller). Publish refuses unless the fingerprint still matches. Not audited:
-- it changes nothing anyone reads, and publish's audit row carries the stamp.
-- Returns {batch_id, impact_check_run_at, impact_check_batch_hash}.
create or replace function public.kb_admin_doc_mark_checked(
    p_actor_id    uuid,
    p_actor_email text,
    p_batch_id    uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_batch public.cb_kb_batches;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    select * into v_batch from public.cb_kb_batches where id = p_batch_id for update;
    if not found then
        raise exception 'no such batch %', p_batch_id using errcode = 'KB002';
    end if;
    if v_batch.status <> 'staged' then
        raise exception 'the batch is %, not staged', v_batch.status using errcode = 'KB003';
    end if;
    if v_batch.chunk_count = 0 then
        raise exception 'the batch has no chunks yet' using errcode = 'KB002';
    end if;
    update public.cb_kb_batches
       set impact_check_run_at = now(), impact_check_batch_hash = public.kb_admin__doc_batch_hash(p_batch_id)
     where id = p_batch_id
    returning * into v_batch;
    return jsonb_build_object('batch_id', p_batch_id, 'impact_check_run_at', v_batch.impact_check_run_at,
                              'impact_check_batch_hash', v_batch.impact_check_batch_hash);
end;
$$;

-- =============================================================================
-- 5. kb_admin_doc_publish
-- =============================================================================
-- Makes a staged batch live, in this one transaction:
--   a. INSERTs its chunks into the live table (see the header);
--   b. switches off every other active document_chunk / table_unit row of the
--      same source_document (the previous version, or the original import);
--   c. never touches a qa_pair row, never a 'Ming Hwee Service Notes' row;
--   d. the batch becomes 'published'; the previous published batch of this
--      document becomes 'superseded'.
-- Caller: an APPROVER. Refuses: no reason; a batch that is not staged, or
-- empty; no impact check, or a batch changed since it; a chunk without a
-- vector, or with a vector from another model than the canary's.
-- Returns {batch_id, document_id, rows_inserted, rows_retired, superseded_batch_id}.
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

-- =============================================================================
-- 6. kb_admin_doc_restore
-- =============================================================================
-- Switches an earlier ('superseded') batch back on, in one transaction: every
-- other active document_chunk / table_unit row of the source goes off, the
-- batch's own rows (still in the live table, with their vectors) come back
-- on. No re-embedding - but refused if those vectors are no longer from the
-- canary's model. Caller: an APPROVER.
-- Returns {batch_id, document_id, rows_restored, rows_retired, superseded_batch_id}.
create or replace function public.kb_admin_doc_restore(
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
    v_live     integer;
    v_restored integer;
    v_retired  integer;
    v_prev     uuid;
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
    if v_batch.status <> 'superseded' then
        raise exception 'only an earlier (superseded) version can be restored; this batch is %', v_batch.status
            using errcode = 'KB003';
    end if;

    select array_agg(s.id) into v_ids from public.cb_kb_staged_chunks s where s.batch_id = p_batch_id;
    select count(*) into v_live from public.cb_knowledge_base_updated k
     where k.id = any (coalesce(v_ids, '{}'::uuid[]))
       and k.source_document = v_doc.source_name and k.chunk_type in ('document_chunk', 'table_unit');
    if v_ids is null or v_live <> cardinality(v_ids) then
        raise exception 'this version''s rows are no longer all in the live table; it cannot be restored'
            using errcode = 'KB003';
    end if;
    select c.model into v_canary from public.cb_kb_canary c where c.id = 1;
    if v_canary is null or exists (select 1 from public.cb_kb_staged_chunks s
                                    where s.batch_id = p_batch_id and s.model_name is distinct from v_canary) then
        raise exception 'this version''s vectors are not from the canary''s current model; re-upload it instead'
            using errcode = 'KB005';
    end if;

    v_retired := public.kb_admin__doc_switch_off(v_doc.source_name, v_ids,
                                                  jsonb_build_object('retired_by_batch', p_batch_id));
    perform set_config('kb_admin.via_function', 'on', true);
    update public.cb_knowledge_base_updated k
       set is_active = true,
           -- (- binds tighter than ||, hence the brackets.)
           metadata  = (coalesce(k.metadata, '{}'::jsonb) - 'retired_by_batch')
                       || jsonb_build_object('managed_by', 'ui', 'restored_by_batch', p_batch_id)
     where k.id = any (v_ids) and not k.is_active;
    get diagnostics v_restored = row_count;
    perform set_config('kb_admin.via_function', 'off', true);

    update public.cb_kb_batches set status = 'superseded'
     where document_id = v_doc.id and status = 'published'
    returning id into v_prev;
    update public.cb_kb_batches
       set status = 'published', published_by = p_actor_id, published_by_email = p_actor_email,
           published_at = now(), reason = v_reason
     where id = p_batch_id;
    perform public.kb_admin__doc_clear_checks(v_doc.id);

    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
    values ('batch_restored', p_actor_id, p_actor_email,
            case when v_prev is null then null else jsonb_build_object('published_batch_id', v_prev) end,
            jsonb_build_object('batch_id', p_batch_id, 'document_id', v_doc.id, 'source_document', v_doc.source_name,
                               'rows_restored', v_restored, 'rows_retired', v_retired,
                               'superseded_batch_id', v_prev),
            v_reason);

    return jsonb_build_object('batch_id', p_batch_id, 'document_id', v_doc.id, 'rows_restored', v_restored,
                              'rows_retired', v_retired, 'superseded_batch_id', v_prev);
end;
$$;

-- =============================================================================
-- 7. kb_admin_doc_discard
-- =============================================================================
-- Drops a staged batch: its chunks are deleted, the batch is kept as
-- 'discarded'. Changes nothing the chatbot reads.
-- Caller: the batch's author, or an approver. Returns {batch_id, chunks_deleted}.
create or replace function public.kb_admin_doc_discard(
    p_actor_id    uuid,
    p_actor_email text,
    p_batch_id    uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_role  text;
    v_batch public.cb_kb_batches;
    n       integer;
begin
    v_role := public.kb_admin__require_actor(p_actor_id, 'editor');
    select * into v_batch from public.cb_kb_batches where id = p_batch_id for update;
    if not found then
        raise exception 'no such batch %', p_batch_id using errcode = 'KB002';
    end if;
    if v_batch.status <> 'staged' then
        raise exception 'the batch is %, not staged', v_batch.status using errcode = 'KB003';
    end if;
    if v_batch.prepared_by is distinct from p_actor_id and v_role <> 'approver' then
        raise exception 'only the batch''s author or an approver can discard it' using errcode = 'KB001';
    end if;
    delete from public.cb_kb_staged_chunks where batch_id = p_batch_id;
    get diagnostics n = row_count;
    update public.cb_kb_batches
       set status = 'discarded', chunk_count = 0, impact_check_run_at = null, impact_check_batch_hash = null
     where id = p_batch_id;

    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
    values ('batch_discarded', p_actor_id, p_actor_email,
            jsonb_build_object('batch_id', p_batch_id, 'document_id', v_batch.document_id, 'chunk_count', v_batch.chunk_count),
            jsonb_build_object('batch_id', p_batch_id, 'status', 'discarded', 'chunks_deleted', n),
            'discarded a staged batch');
    return jsonb_build_object('batch_id', p_batch_id, 'chunks_deleted', n);
end;
$$;

-- =============================================================================
-- 8. kb_admin_doc_retire
-- =============================================================================
-- Switches a whole document off: every active document_chunk / table_unit
-- row of its source, imported or published here. Never a qa_pair. Records a
-- batch with status 'retired'; the published batch becomes 'superseded', so
-- kb_admin_doc_restore can bring it back. Caller: an APPROVER.
-- Returns {document_id, batch_id, rows_retired, superseded_batch_id}.
create or replace function public.kb_admin_doc_retire(
    p_actor_id    uuid,
    p_actor_email text,
    p_document_id uuid,
    p_reason      text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason  text := btrim(coalesce(p_reason, ''));
    v_doc     public.cb_kb_documents;
    v_batch   uuid;
    v_retired integer;
    v_prev    uuid;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'approver');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    select * into v_doc from public.cb_kb_documents where id = p_document_id for update;
    if not found then
        raise exception 'no such document %', p_document_id using errcode = 'KB002';
    end if;

    insert into public.cb_kb_batches (document_id, status, prepared_by, prepared_by_email,
                                      published_by, published_by_email, published_at, reason)
    values (v_doc.id, 'retired', p_actor_id, p_actor_email, p_actor_id, p_actor_email, now(), v_reason)
    returning id into v_batch;

    v_retired := public.kb_admin__doc_switch_off(v_doc.source_name, null,
                                                  jsonb_build_object('retired_by_batch', v_batch));
    if v_retired = 0 then
        raise exception 'nothing of "%" is live; there is nothing to retire', v_doc.source_name using errcode = 'KB002';
    end if;
    update public.cb_kb_batches set chunk_count = v_retired where id = v_batch;
    update public.cb_kb_batches set status = 'superseded'
     where document_id = v_doc.id and status = 'published'
    returning id into v_prev;
    perform public.kb_admin__doc_clear_checks(v_doc.id);

    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, new_values, reason)
    values ('doc_retired', p_actor_id, p_actor_email,
            case when v_prev is null then null else jsonb_build_object('published_batch_id', v_prev) end,
            jsonb_build_object('document_id', v_doc.id, 'batch_id', v_batch, 'source_document', v_doc.source_name,
                               'rows_retired', v_retired, 'superseded_batch_id', v_prev),
            v_reason);
    return jsonb_build_object('document_id', v_doc.id, 'batch_id', v_batch, 'rows_retired', v_retired,
                              'superseded_batch_id', v_prev);
end;
$$;

-- =============================================================================
-- 9. kb_admin_match_with_batch  (READ ONLY)
-- =============================================================================
-- What the bot's search would return if this staged batch were published:
-- the live table's active rows, MINUS the document_chunk / table_unit rows of
-- the batch's source that publishing would switch off, PLUS the batch's
-- chunks (as the type, floor and routing publish would give them). Then the
-- same rules as cb_match_knowledge_base_updated: embedding present, no
-- style_example, each filter lets its catch-all through (service 'general',
-- audience 'all', nationality 'all'; NULL = no filter), and a row needs
-- greatest(threshold, its own rag_score_floor). Default threshold 0.35, the
-- bot's rag_match_threshold. Writes nothing.
-- Caller: an editor or approver.
create or replace function public.kb_admin_match_with_batch(
    p_actor_id        uuid,
    p_actor_email     text,
    p_batch_id        uuid,
    p_query_embedding vector,
    p_service         text,
    p_audience        text,
    p_nationality     text,
    p_match_count     integer default 5,
    p_match_threshold double precision default 0.35)
returns table (
    id              uuid,
    from_batch      boolean,
    chunk_type      text,
    source_document text,
    section_heading text,
    service_type    text,
    contact_type    text,
    nationality     text,
    rag_score_floor numeric,
    similarity      double precision,
    preview         text)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_batch  public.cb_kb_batches;
    v_source text;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    select * into v_batch from public.cb_kb_batches b where b.id = p_batch_id;
    if not found then
        raise exception 'no such batch %', p_batch_id using errcode = 'KB002';
    end if;
    if v_batch.status <> 'staged' then
        raise exception 'the batch is %, not staged', v_batch.status using errcode = 'KB003';
    end if;
    if p_query_embedding is null or vector_dims(p_query_embedding) <> 1536 then
        raise exception 'the question''s embedding must have 1536 dimensions' using errcode = 'KB005';
    end if;
    select d.source_name into v_source from public.cb_kb_documents d where d.id = v_batch.document_id;

    return query
    with pool as (
        select k.id, false as from_batch, k.chunk_type::text as chunk_type, k.source_document::text as source_document,
               k.section_heading, k.service_type::text as service_type, k.contact_type::text as contact_type,
               k.nationality::text as nationality, k.rag_score_floor, k.embedding,
               coalesce(k.content, concat_ws(E'\n', k.question, k.answer)) as body
          from public.cb_knowledge_base_updated k
         where k.is_active
           and k.embedding is not null
           and k.chunk_type <> 'style_example'
           -- what publishing this batch would switch off
           and not (k.source_document = v_source and k.chunk_type in ('document_chunk', 'table_unit'))
        union all
        select s.id, true, public.kb_admin__doc_live_type(s.chunk_type), v_source,
               s.section_heading, s.service, s.audience, s.nationality,
               public.kb_admin__doc_floor(s.content, s.section_heading), s.embedding, s.content
          from public.cb_kb_staged_chunks s
         where s.batch_id = p_batch_id and s.embedding is not null
    ), scored as (
        select p.*, (1 - (p.embedding <=> p_query_embedding))::double precision as sim
          from pool p
         where (p_service     is null or p.service_type in (p_service, 'general'))
           and (p_audience    is null or p.contact_type in (p_audience, 'all'))
           and (p_nationality is null or p.nationality  in (p_nationality, 'all'))
    )
    select sc.id, sc.from_batch, sc.chunk_type, sc.source_document, sc.section_heading, sc.service_type,
           sc.contact_type, sc.nationality, sc.rag_score_floor, sc.sim, left(sc.body, 300)
      from scored sc
     where sc.sim >= greatest(p_match_threshold, coalesce(sc.rag_score_floor::double precision, 0))
     order by sc.sim desc
     limit greatest(coalesce(p_match_count, 5), 1);
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
        'public.kb_admin__doc_check_source(text)',
        'public.kb_admin__doc_namespace(text, text)',
        'public.kb_admin__doc_check_chunk(text, text, text, text, text, text)',
        'public.kb_admin__doc_vector(float8[])',
        'public.kb_admin__doc_require_model(text)',
        'public.kb_admin__doc_content_hash(text)',
        'public.kb_admin__doc_batch_hash(uuid)',
        'public.kb_admin__doc_live_type(text)',
        'public.kb_admin__doc_floor(text, text)',
        'public.kb_admin__doc_switch_off(text, uuid[], jsonb)',
        'public.kb_admin__doc_clear_checks(uuid)',
        'public.kb_admin_doc_upload(uuid, text, text, text, text, bytea, text)',
        'public.kb_admin_doc_stage(uuid, text, uuid, jsonb, text)',
        'public.kb_admin_doc_edit_chunk(uuid, text, uuid, text, text, text, text, text, text, float8[], text)',
        'public.kb_admin_doc_mark_checked(uuid, text, uuid)',
        'public.kb_admin_doc_publish(uuid, text, uuid, text)',
        'public.kb_admin_doc_restore(uuid, text, uuid, text)',
        'public.kb_admin_doc_discard(uuid, text, uuid)',
        'public.kb_admin_doc_retire(uuid, text, uuid, text)',
        'public.kb_admin_match_with_batch(uuid, text, uuid, vector, text, text, text, integer, double precision)'
    ]::regprocedure[] loop
        execute format('alter function %s owner to kb_admin_doc_owner', f);
        execute format('revoke all on function %s from public, anon, authenticated, service_role', f);
    end loop;
end
$$;

revoke create on schema public from kb_admin_doc_owner;

grant execute on function
    public.kb_admin_doc_upload(uuid, text, text, text, text, bytea, text),
    public.kb_admin_doc_stage(uuid, text, uuid, jsonb, text),
    public.kb_admin_doc_edit_chunk(uuid, text, uuid, text, text, text, text, text, text, float8[], text),
    public.kb_admin_doc_mark_checked(uuid, text, uuid),
    public.kb_admin_doc_publish(uuid, text, uuid, text),
    public.kb_admin_doc_restore(uuid, text, uuid, text),
    public.kb_admin_doc_discard(uuid, text, uuid),
    public.kb_admin_doc_retire(uuid, text, uuid, text),
    public.kb_admin_match_with_batch(uuid, text, uuid, vector, text, text, text, integer, double precision)
to kb_admin_editor;

-- =============================================================================
-- Checks (abort the whole file if any fails)
-- =============================================================================
do $$
declare
    p record;
    r text;
    n integer := 0;
begin
    for p in
        select pr.oid::regprocedure::text as sig, pr.proname, pr.prosecdef, pg_get_userbyid(pr.proowner) as owner,
               pr.proconfig
          from pg_proc pr join pg_namespace ns on ns.oid = pr.pronamespace
         where ns.nspname = 'public' and (pr.proname like 'kb\_admin\_doc\_%' or pr.proname like 'kb\_admin\_\_doc\_%'
                                          or pr.proname = 'kb_admin_match_with_batch')
    loop
        if p.owner <> 'kb_admin_doc_owner' then
            raise exception 'FAIL: % is owned by %', p.sig, p.owner;
        end if;
        if p.proconfig is null or not exists (select 1 from unnest(p.proconfig) c where c like 'search_path=%pg_temp%') then
            raise exception 'FAIL: % has no pinned search_path', p.sig;
        end if;
        foreach r in array array['anon', 'authenticated', 'service_role', 'public'] loop
            if has_function_privilege(r, p.sig, 'EXECUTE') then
                raise exception 'FAIL: % can EXECUTE %', r, p.sig;
            end if;
        end loop;
        if p.proname like 'kb\_admin\_\_%' then
            if p.prosecdef then raise exception 'FAIL: helper % is SECURITY DEFINER', p.sig; end if;
            if has_function_privilege('kb_admin_editor', p.sig, 'EXECUTE') then
                raise exception 'FAIL: kb_admin_editor can EXECUTE helper %', p.sig;
            end if;
        else
            if not p.prosecdef then raise exception 'FAIL: % is not SECURITY DEFINER', p.sig; end if;
            if not has_function_privilege('kb_admin_editor', p.sig, 'EXECUTE') then
                raise exception 'FAIL: kb_admin_editor cannot EXECUTE %', p.sig;
            end if;
            n := n + 1;
        end if;
    end loop;
    if n <> 9 then
        raise exception 'FAIL: expected 9 document entry functions, found %', n;
    end if;
    if (select count(*) from pg_proc pr join pg_namespace ns on ns.oid = pr.pronamespace
         where ns.nspname = 'public' and pr.prosecdef and pr.proname like 'kb\_admin\_%' and pr.proname not like 'kb\_admin\_\_%'
           and has_function_privilege('kb_admin_editor', pr.oid, 'EXECUTE')) <> 15 then
        raise exception 'FAIL: kb_admin_editor should EXECUTE exactly 15 kb_admin_* functions (6 Phase 2/3A + 9 documents)';
    end if;
    if has_schema_privilege('kb_admin_doc_owner', 'public', 'CREATE') then
        raise exception 'FAIL: kb_admin_doc_owner kept CREATE on schema public';
    end if;
    if (select provolatile from pg_proc where oid =
          'public.kb_admin_match_with_batch(uuid, text, uuid, vector, text, text, text, integer, double precision)'::regprocedure) <> 's' then
        raise exception 'FAIL: kb_admin_match_with_batch is not STABLE';
    end if;
    raise notice 'PASS: 011 - 9 document functions + 11 helpers owned by kb_admin_doc_owner, pinned; kb_admin_editor executes 15 kb_admin_* functions';
end
$$;

commit;
