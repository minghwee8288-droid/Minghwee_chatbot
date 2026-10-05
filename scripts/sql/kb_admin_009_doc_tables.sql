-- kb_admin_009_doc_tables: the tables behind document upload (Phase 3 scope B).
--
-- Four new tables. NOTHING here touches cb_knowledge_base_updated, and the bot
-- reads none of them: a staged chunk is invisible to the chatbot until
-- kb_admin_doc_publish (011) copies it into the live table.
--
--   cb_kb_documents        one row per source document, with the uploaded file
--   cb_kb_batches          one row per prepared version of a document
--   cb_kb_staged_chunks    the chunks of a batch, with their vectors
--   cb_kb_probe_questions  the questions the impact check runs (seeded in 013)
--
-- DECISIONS (2026-10-01, agreed before writing):
--   * source_name is the EXACT source_document string the live rows carry (the
--     imported rows use file names, e.g. 'Ming_Hwee_Client_Service_Agreement_SOURCE.docx').
--     Publishing matches on it to retire the previous version. display_label is
--     the optional friendly name for the UI.
--   * Staging says 'passage' (<= 1200 chars, the bot's rag_max_chunk_chars) or
--     'table_unit' (<= 3000, rag_max_table_chars). The live table's chunk_type
--     CHECK has no 'passage': publish writes a passage as 'document_chunk',
--     which the bot already reads as prose (rag.PROSE_CHUNK_TYPES).
--   * cb_kb_staged_chunks.model_name records which model made the vector, so
--     publish can refuse a vector that is not the canary's model (the Phase 2 rule).
--   * cb_kb_probe_questions.question_text is UNIQUE so 013 can be re-run.
--   * Two partial unique indexes: at most ONE staged and ONE published batch per
--     document at a time.
--
-- ACCESS. RLS on everywhere. Everything revoked from public, anon, authenticated
-- and service_role (Supabase's default privileges grant those on new tables).
-- kb_admin_reader gets column SELECT that never includes file_bytes or
-- embedding. The function owner (kb_admin_doc_owner) is created and granted in 010.
--
-- Also widens cb_kb_audit's action CHECK with the seven document actions.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>
-- Rollback:  scripts/sql/rollback/rollback_kb_admin_009_doc_tables.sql

begin;

-- =============================================================================
-- 1. Tables
-- =============================================================================
create table if not exists public.cb_kb_documents (
    id                 uuid        not null default gen_random_uuid(),
    source_name        text        not null,
    display_label      text,
    file_name          text        not null,
    file_type          text        not null,
    file_bytes         bytea       not null,
    file_size_bytes    integer     not null,
    uploaded_by        uuid        not null,
    uploaded_by_email  text        not null,
    uploaded_at        timestamptz not null default now(),

    constraint cb_kb_documents_pkey primary key (id),
    constraint cb_kb_documents_source_name_key unique (source_name),
    constraint cb_kb_documents_source_name_check check (length(btrim(source_name)) > 0 and length(source_name) <= 500),
    constraint cb_kb_documents_file_type_check check (file_type in ('docx', 'md', 'txt')),
    constraint cb_kb_documents_file_size_check
        check (file_size_bytes > 0 and file_size_bytes <= 5242880 and file_size_bytes = octet_length(file_bytes))
);

create table if not exists public.cb_kb_batches (
    id                       uuid        not null default gen_random_uuid(),
    document_id              uuid        not null,
    status                   text        not null default 'staged',
    chunk_count              integer     not null default 0,
    prepared_by              uuid        not null,
    prepared_by_email        text        not null,
    prepared_at              timestamptz not null default now(),
    published_by             uuid,
    published_by_email       text,
    published_at             timestamptz,
    impact_check_run_at      timestamptz,
    impact_check_batch_hash  text,
    reason                   text,
    created_at               timestamptz not null default now(),

    constraint cb_kb_batches_pkey primary key (id),
    constraint cb_kb_batches_document_fkey foreign key (document_id) references public.cb_kb_documents (id),
    constraint cb_kb_batches_status_check
        check (status in ('staged', 'published', 'superseded', 'discarded', 'retired')),
    constraint cb_kb_batches_chunk_count_check check (chunk_count >= 0),
    -- The stamp is all or nothing.
    constraint cb_kb_batches_impact_check
        check ((impact_check_run_at is null) = (impact_check_batch_hash is null))
);

create index if not exists cb_kb_batches_document_idx on public.cb_kb_batches (document_id);
create unique index if not exists cb_kb_batches_one_staged
    on public.cb_kb_batches (document_id) where status = 'staged';
create unique index if not exists cb_kb_batches_one_published
    on public.cb_kb_batches (document_id) where status = 'published';

create table if not exists public.cb_kb_staged_chunks (
    id               uuid         not null default gen_random_uuid(),
    batch_id         uuid         not null,
    ordinal          integer      not null,
    chunk_type       text         not null,
    section_heading  text,
    content          text         not null,
    content_hash     text         not null,
    embedding        vector(1536),
    model_name       text,
    service          text,
    audience         text,
    nationality      text,
    namespace        text,
    metadata         jsonb        not null default '{}'::jsonb,

    constraint cb_kb_staged_chunks_pkey primary key (id),
    constraint cb_kb_staged_chunks_batch_fkey
        foreign key (batch_id) references public.cb_kb_batches (id) on delete cascade,
    constraint cb_kb_staged_chunks_ordinal_key unique (batch_id, ordinal),
    constraint cb_kb_staged_chunks_ordinal_check check (ordinal >= 0),
    constraint cb_kb_staged_chunks_chunk_type_check check (chunk_type in ('passage', 'table_unit')),
    constraint cb_kb_staged_chunks_content_check check (length(btrim(content)) > 0),
    constraint cb_kb_staged_chunks_length_check
        check (char_length(content) <= case when chunk_type = 'passage' then 1200 else 3000 end),
    constraint cb_kb_staged_chunks_model_check
        check (embedding is null or length(btrim(coalesce(model_name, ''))) > 0),
    constraint cb_kb_staged_chunks_audience_check
        check (audience is null or audience in ('employer', 'candidate', 'all')),
    constraint cb_kb_staged_chunks_nationality_check
        check (nationality is null or nationality in ('PH', 'ID', 'MM', 'all'))
);

create table if not exists public.cb_kb_probe_questions (
    id             uuid        not null default gen_random_uuid(),
    question_text  text        not null,
    service        text,
    audience       text,
    nationality    text,
    is_active      boolean     not null default true,
    created_at     timestamptz not null default now(),
    notes          text,

    constraint cb_kb_probe_questions_pkey primary key (id),
    constraint cb_kb_probe_questions_text_key unique (question_text),
    constraint cb_kb_probe_questions_text_check check (length(btrim(question_text)) > 0),
    constraint cb_kb_probe_questions_audience_check
        check (audience is null or audience in ('employer', 'candidate', 'all')),
    constraint cb_kb_probe_questions_nationality_check
        check (nationality is null or nationality in ('PH', 'ID', 'MM', 'all'))
);

comment on table public.cb_kb_documents is
    'kb-admin: uploaded source documents. source_name = the live source_document string.';
comment on table public.cb_kb_batches is
    'kb-admin: one prepared version of a document (staged / published / superseded / discarded / retired).';
comment on table public.cb_kb_staged_chunks is
    'kb-admin: chunks of a batch. The bot never reads this table.';
comment on table public.cb_kb_probe_questions is
    'kb-admin: questions the impact check runs before a batch is published.';

-- =============================================================================
-- 2. RLS and grants
-- =============================================================================
alter table public.cb_kb_documents       enable row level security;
alter table public.cb_kb_batches         enable row level security;
alter table public.cb_kb_staged_chunks   enable row level security;
alter table public.cb_kb_probe_questions enable row level security;

revoke all on public.cb_kb_documents, public.cb_kb_batches, public.cb_kb_staged_chunks,
              public.cb_kb_probe_questions
    from public, anon, authenticated, service_role;

-- kb_admin_reader: never file_bytes, never embedding.
grant select (id, source_name, display_label, file_name, file_type, file_size_bytes,
              uploaded_by, uploaded_by_email, uploaded_at)
    on public.cb_kb_documents to kb_admin_reader;
grant select (id, document_id, status, chunk_count, prepared_by, prepared_by_email, prepared_at,
              published_by, published_by_email, published_at, impact_check_run_at,
              impact_check_batch_hash, reason, created_at)
    on public.cb_kb_batches to kb_admin_reader;
grant select (id, batch_id, ordinal, chunk_type, section_heading, content, content_hash,
              model_name, service, audience, nationality, namespace, metadata)
    on public.cb_kb_staged_chunks to kb_admin_reader;
grant select (id, question_text, service, audience, nationality, is_active, created_at, notes)
    on public.cb_kb_probe_questions to kb_admin_reader;

do $$
declare
    t text;
begin
    foreach t in array array['cb_kb_documents', 'cb_kb_batches', 'cb_kb_staged_chunks', 'cb_kb_probe_questions'] loop
        if not exists (select 1 from pg_policies where schemaname = 'public'
                        and tablename = t and policyname = 'kb_admin_reader_select') then
            execute format('create policy kb_admin_reader_select on public.%I
                                as permissive for select to kb_admin_reader using (true)', t);
        end if;
    end loop;
end
$$;

-- =============================================================================
-- 3. Audit: the seven document actions
-- =============================================================================
-- cb_kb_audit is append-only, so widening its CHECK is safe: every existing row
-- already passes the narrower list (the 008 list plus these).
do $$
declare
    current_def text;
begin
    select pg_get_constraintdef(oid) into current_def
      from pg_constraint
     where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check';
    if current_def like '%chunk_edited%' then
        raise notice 'cb_kb_audit_action_check already allows the document actions - unchanged';
        return;
    end if;
    if current_def not like '%entry_created%' then
        raise exception 'FAIL: apply kb_admin_008 before 009 (audit CHECK has no entry_created)';
    end if;
    alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
    alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
        'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
        'entry_created',
        'doc_uploaded', 'batch_prepared', 'batch_published', 'batch_restored',
        'batch_discarded', 'doc_retired', 'chunk_edited',
        'external_insert', 'external_update', 'external_delete'
    ));
end
$$;

-- =============================================================================
-- 4. Checks (abort the whole file if any fails)
-- =============================================================================
do $$
declare
    t text;
    r text;
    c text;
begin
    foreach t in array array['cb_kb_documents', 'cb_kb_batches', 'cb_kb_staged_chunks', 'cb_kb_probe_questions'] loop
        if not (select c2.relrowsecurity from pg_class c2 where c2.oid = ('public.' || t)::regclass) then
            raise exception 'FAIL: RLS is off on %', t;
        end if;
        foreach r in array array['anon', 'authenticated', 'service_role', 'public'] loop
            if has_table_privilege(r, 'public.' || t, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
               or has_any_column_privilege(r, 'public.' || t, 'SELECT, INSERT, UPDATE, REFERENCES') then
                raise exception 'FAIL: % holds a privilege on %', r, t;
            end if;
        end loop;
        if exists (select 1 from pg_policies where schemaname = 'public' and tablename = t
                    and roles && array['anon', 'authenticated', 'service_role', 'public']::name[]) then
            raise exception 'FAIL: a policy on % names a portal/public role', t;
        end if;
        if has_table_privilege('kb_admin_editor', 'public.' || t, 'SELECT, INSERT, UPDATE, DELETE')
           or has_any_column_privilege('kb_admin_editor', 'public.' || t, 'SELECT, INSERT, UPDATE') then
            raise exception 'FAIL: kb_admin_editor holds a privilege on %', t;
        end if;
    end loop;

    if has_column_privilege('kb_admin_reader', 'public.cb_kb_documents', 'file_bytes', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader can SELECT cb_kb_documents.file_bytes';
    end if;
    if has_column_privilege('kb_admin_reader', 'public.cb_kb_staged_chunks', 'embedding', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader can SELECT cb_kb_staged_chunks.embedding';
    end if;
    if has_table_privilege('kb_admin_reader', 'public.cb_kb_documents', 'SELECT')
       or has_table_privilege('kb_admin_reader', 'public.cb_kb_staged_chunks', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader holds table-wide SELECT where it must be column-level';
    end if;
    foreach c in array array['source_name', 'file_name', 'file_size_bytes'] loop
        if not has_column_privilege('kb_admin_reader', 'public.cb_kb_documents', c, 'SELECT') then
            raise exception 'FAIL: kb_admin_reader cannot SELECT cb_kb_documents.%', c;
        end if;
    end loop;
    foreach c in array array['content', 'chunk_type', 'service'] loop
        if not has_column_privilege('kb_admin_reader', 'public.cb_kb_staged_chunks', c, 'SELECT') then
            raise exception 'FAIL: kb_admin_reader cannot SELECT cb_kb_staged_chunks.%', c;
        end if;
    end loop;
    if not has_table_privilege('kb_admin_reader', 'public.cb_kb_batches', 'SELECT')
       and not has_column_privilege('kb_admin_reader', 'public.cb_kb_batches', 'status', 'SELECT') then
        raise exception 'FAIL: kb_admin_reader cannot read cb_kb_batches';
    end if;
    if has_table_privilege('kb_admin_reader', 'public.cb_kb_batches', 'INSERT, UPDATE, DELETE') then
        raise exception 'FAIL: kb_admin_reader can write cb_kb_batches';
    end if;

    if not (select pg_get_constraintdef(oid) like '%chunk_edited%' and pg_get_constraintdef(oid) like '%doc_retired%'
              from pg_constraint where conrelid = 'public.cb_kb_audit'::regclass
               and conname = 'cb_kb_audit_action_check') then
        raise exception 'FAIL: cb_kb_audit_action_check does not allow the document actions';
    end if;
    raise notice 'PASS: 009 - four tables, RLS on, no portal/public access, reader sees no file_bytes or embedding, audit widened';
end
$$;

-- The CHECK constraints, exercised. Each probe runs in its own subtransaction
-- (an exception block) and is rolled back, so nothing is left behind.
do $$
declare
    v_doc   uuid := gen_random_uuid();
    v_batch uuid := gen_random_uuid();
    v_ok    boolean;
begin
    insert into public.cb_kb_documents (id, source_name, file_name, file_type, file_bytes, file_size_bytes,
                                        uploaded_by, uploaded_by_email)
    values (v_doc, '009 self-check', 'x.md', 'md', '\x41'::bytea, 1, gen_random_uuid(), 'check@invalid');
    insert into public.cb_kb_batches (id, document_id, prepared_by, prepared_by_email)
    values (v_batch, v_doc, gen_random_uuid(), 'check@invalid');

    -- passage at 1200: accepted; 1201: refused. table_unit at 3000 / 3001.
    insert into public.cb_kb_staged_chunks (batch_id, ordinal, chunk_type, content, content_hash)
    values (v_batch, 0, 'passage', repeat('a', 1200), 'h'), (v_batch, 1, 'table_unit', repeat('a', 3000), 'h');

    v_ok := false;
    begin
        insert into public.cb_kb_staged_chunks (batch_id, ordinal, chunk_type, content, content_hash)
        values (v_batch, 2, 'passage', repeat('a', 1201), 'h');
    exception when check_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: a 1201-character passage was accepted'; end if;

    v_ok := false;
    begin
        insert into public.cb_kb_staged_chunks (batch_id, ordinal, chunk_type, content, content_hash)
        values (v_batch, 2, 'table_unit', repeat('a', 3001), 'h');
    exception when check_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: a 3001-character table was accepted'; end if;

    v_ok := false;
    begin
        insert into public.cb_kb_staged_chunks (batch_id, ordinal, chunk_type, content, content_hash)
        values (v_batch, 2, 'document_chunk', 'x', 'h');
    exception when check_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: a staged chunk_type other than passage/table_unit was accepted'; end if;

    v_ok := false;
    begin
        insert into public.cb_kb_documents (source_name, file_name, file_type, file_bytes, file_size_bytes,
                                            uploaded_by, uploaded_by_email)
        values ('009 self-check 2', 'x.pdf', 'pdf', '\x41'::bytea, 1, gen_random_uuid(), 'check@invalid');
    exception when check_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: file_type pdf was accepted'; end if;

    v_ok := false;
    begin
        insert into public.cb_kb_documents (source_name, file_name, file_type, file_bytes, file_size_bytes,
                                            uploaded_by, uploaded_by_email)
        values ('009 self-check 3', 'x.md', 'md', '\x41'::bytea, 5242881, gen_random_uuid(), 'check@invalid');
    exception when check_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: a file over 5 MB (or a wrong size) was accepted'; end if;

    v_ok := false;
    begin
        insert into public.cb_kb_batches (document_id, status, prepared_by, prepared_by_email)
        values (v_doc, 'staged', gen_random_uuid(), 'check@invalid');
    exception when unique_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: a second staged batch for one document was accepted'; end if;

    v_ok := false;
    begin
        update public.cb_kb_batches set status = 'live' where id = v_batch;
    exception when check_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: batch status ''live'' was accepted'; end if;

    -- Take the probe rows back out; the outer transaction commits only the DDL.
    delete from public.cb_kb_staged_chunks where batch_id = v_batch;
    delete from public.cb_kb_batches where id = v_batch;
    delete from public.cb_kb_documents where id = v_doc;
    if exists (select 1 from public.cb_kb_documents) or exists (select 1 from public.cb_kb_batches) then
        raise exception 'FAIL: self-check rows were left behind';
    end if;
    raise notice 'PASS: 009 - CHECKs refuse a 1201-char passage, a 3001-char table, a bad type/size/status, a second staged batch';
end
$$;

commit;
