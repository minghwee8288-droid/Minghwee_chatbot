-- kb_admin_010_doc_owner: the NOLOGIN role that owns the document functions (011).
--
-- kb_admin_doc_owner is to documents what kb_admin_fn_owner (004) is to Q&A
-- entries. kb_admin_fn_owner is NOT changed by this file.
--
-- ON THE LIVE TABLE (cb_knowledge_base_updated) it may:
--   * SELECT the columns the publish and the impact simulation read;
--   * INSERT a fixed list of columns, and RLS admits only a document_chunk or
--     table_unit row tagged metadata.managed_by = 'ui' whose source is neither
--     'Ming Hwee Service Notes' (the loader's Q&A) nor 'Ming Hwee KB Admin'
--     (scope A's Q&A entries);
--   * UPDATE is_active and metadata ONLY, and RLS admits only document_chunk /
--     table_unit rows not from 'Ming Hwee Service Notes', and the row must
--     come out tagged managed_by = 'ui'.
--   It can never touch a qa_pair row, never DELETE, and never change text,
--   vectors, routing, chunk_type, source_document or rag_score_floor of an
--   existing row.
--
--   WHY THE UPDATE IS NOT LIMITED TO managed_by = 'ui' ROWS (decision,
--   2026-10-01): every imported document_chunk / table_unit row is tagged
--   'loader'. Limited to 'ui', the first kb-admin upload of an imported
--   document could never switch its old chunks off, and both versions would be
--   live at once. Switching one off stamps it 'ui', so the loader leaves it
--   alone from then on (load_service_notes._ui_owned).
--
--   chunk_type 'passage' does not exist in the live table: a staged passage is
--   published as 'document_chunk' (see 009).
--
-- THE LIVE-TABLE INSERT LIST, mapped from the brief's names to the real
-- columns: id, chunk_type, source_document, section_heading, question, answer,
-- content, embedding, is_active, service_type (service), contact_type
-- (audience), nationality, namespace, metadata, rag_score_floor. Never
-- frequency, page_or_section, created_at or updated_at (they take defaults).
--
-- ON THE NEW TABLES it may read and write what the functions need: documents
-- and batches (SELECT, INSERT, UPDATE), staged chunks (SELECT, INSERT, plus
-- UPDATE and DELETE only while their batch is 'staged', by RLS). Plus INSERT on
-- cb_kb_audit, SELECT (user_id, role, active) on cb_kb_admin_users and
-- SELECT (id, model) on cb_kb_canary.
--
-- NOT GRANTED, though the brief listed it: INSERT on cb_kb_entry_versions. No
-- document function writes a Q&A version, so the grant would be unused
-- privilege. One line to add if a later function needs it.
--
-- THE SHARED HELPERS. 011 reuses 004's kb_admin__require_actor,
-- kb_admin__refuse_nric, kb_admin__check_routing and kb_admin__numbers rather
-- than copying them (one definition of the NRIC pattern and the role rule).
-- They stay owned by kb_admin_fn_owner; this file grants kb_admin_doc_owner
-- EXECUTE on those four and nothing else of 004's.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>
-- Rollback:  scripts/sql/rollback/rollback_kb_admin_010_doc_owner.sql

begin;

-- =============================================================================
-- 1. The role (the 004 pattern)
-- =============================================================================
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_doc_owner') then
        create role kb_admin_doc_owner nologin noinherit nobypassrls;
    end if;
end
$$;
alter role kb_admin_doc_owner nologin noinherit nobypassrls nocreatedb nocreaterole noreplication;

-- postgres must be able to SET ROLE to the owner to hand it the functions
-- (Postgres 16+), and INHERIT it to replace them on a re-run. See 004 for why
-- both options are checked rather than plain membership.
do $$
begin
    if not exists (
        select 1 from pg_auth_members am
          join pg_roles g on g.oid = am.roleid
          join pg_roles m on m.oid = am.member
         where g.rolname = 'kb_admin_doc_owner' and m.rolname = current_user
           and am.set_option and am.inherit_option
    ) then
        execute format('grant kb_admin_doc_owner to %I with inherit true, set true', current_user);
    end if;
end
$$;

-- USAGE only. CREATE is lent for the ownership hand-over in 011 and taken back
-- in the same transaction.
grant usage on schema public to kb_admin_doc_owner;

-- =============================================================================
-- 2. The live table
-- =============================================================================
grant select (
    id, namespace, service_type, contact_type, nationality, chunk_type, question, answer,
    content, embedding, source_document, section_heading, metadata, is_active, rag_score_floor
) on public.cb_knowledge_base_updated to kb_admin_doc_owner;

grant insert (
    id, chunk_type, source_document, section_heading, question, answer, content, embedding,
    is_active, service_type, contact_type, nationality, namespace, metadata, rag_score_floor
) on public.cb_knowledge_base_updated to kb_admin_doc_owner;

grant update (is_active, metadata) on public.cb_knowledge_base_updated to kb_admin_doc_owner;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'cb_knowledge_base_updated'
                    and policyname = 'kb_admin_doc_owner_select') then
        create policy kb_admin_doc_owner_select on public.cb_knowledge_base_updated
            as permissive for select to kb_admin_doc_owner using (true);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'cb_knowledge_base_updated'
                    and policyname = 'kb_admin_doc_owner_insert_doc') then
        create policy kb_admin_doc_owner_insert_doc on public.cb_knowledge_base_updated
            as permissive for insert to kb_admin_doc_owner
            with check (
                chunk_type in ('document_chunk', 'table_unit')
                and metadata ->> 'managed_by' = 'ui'
                and source_document is not null
                and source_document not in ('Ming Hwee Service Notes', 'Ming Hwee KB Admin'));
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'cb_knowledge_base_updated'
                    and policyname = 'kb_admin_doc_owner_update_doc') then
        create policy kb_admin_doc_owner_update_doc on public.cb_knowledge_base_updated
            as permissive for update to kb_admin_doc_owner
            using (
                chunk_type in ('document_chunk', 'table_unit')
                and source_document is distinct from 'Ming Hwee Service Notes')
            with check (
                chunk_type in ('document_chunk', 'table_unit')
                and source_document is distinct from 'Ming Hwee Service Notes'
                and metadata ->> 'managed_by' = 'ui');
    end if;
end
$$;

-- =============================================================================
-- 3. The new tables (009)
-- =============================================================================
grant select, insert, update on public.cb_kb_documents to kb_admin_doc_owner;
grant select, insert, update on public.cb_kb_batches   to kb_admin_doc_owner;
grant select, insert, update, delete on public.cb_kb_staged_chunks to kb_admin_doc_owner;

do $$
begin
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_documents' and policyname = 'kb_admin_doc_owner_all') then
        create policy kb_admin_doc_owner_all on public.cb_kb_documents
            as permissive for all to kb_admin_doc_owner using (true) with check (true);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_batches' and policyname = 'kb_admin_doc_owner_all') then
        create policy kb_admin_doc_owner_all on public.cb_kb_batches
            as permissive for all to kb_admin_doc_owner using (true) with check (true);
    end if;
    -- Staged chunks: read and add freely; change or delete only while the
    -- batch is still 'staged', so a published batch's chunks (the record of
    -- what went live, and what a restore switches back on) are frozen.
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_staged_chunks' and policyname = 'kb_admin_doc_owner_select') then
        create policy kb_admin_doc_owner_select on public.cb_kb_staged_chunks
            as permissive for select to kb_admin_doc_owner using (true);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_staged_chunks' and policyname = 'kb_admin_doc_owner_insert') then
        create policy kb_admin_doc_owner_insert on public.cb_kb_staged_chunks
            as permissive for insert to kb_admin_doc_owner
            with check (exists (select 1 from public.cb_kb_batches b where b.id = batch_id and b.status = 'staged'));
    end if;
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_staged_chunks' and policyname = 'kb_admin_doc_owner_update') then
        create policy kb_admin_doc_owner_update on public.cb_kb_staged_chunks
            as permissive for update to kb_admin_doc_owner
            using (exists (select 1 from public.cb_kb_batches b where b.id = batch_id and b.status = 'staged'))
            with check (exists (select 1 from public.cb_kb_batches b where b.id = batch_id and b.status = 'staged'));
    end if;
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_staged_chunks' and policyname = 'kb_admin_doc_owner_delete') then
        create policy kb_admin_doc_owner_delete on public.cb_kb_staged_chunks
            as permissive for delete to kb_admin_doc_owner
            using (exists (select 1 from public.cb_kb_batches b where b.id = batch_id and b.status = 'staged'));
    end if;
end
$$;

-- =============================================================================
-- 4. Audit, access list, canary (the same as kb_admin_fn_owner)
-- =============================================================================
grant insert on public.cb_kb_audit to kb_admin_doc_owner;
grant select (user_id, role, active) on public.cb_kb_admin_users to kb_admin_doc_owner;
grant select (id, model) on public.cb_kb_canary to kb_admin_doc_owner;

do $$
begin
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_audit' and policyname = 'kb_admin_doc_owner_insert') then
        create policy kb_admin_doc_owner_insert on public.cb_kb_audit
            as permissive for insert to kb_admin_doc_owner with check (true);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_admin_users' and policyname = 'kb_admin_doc_owner_select') then
        create policy kb_admin_doc_owner_select on public.cb_kb_admin_users
            as permissive for select to kb_admin_doc_owner using (true);
    end if;
    if not exists (select 1 from pg_policies where tablename = 'cb_kb_canary' and policyname = 'kb_admin_doc_owner_select') then
        create policy kb_admin_doc_owner_select on public.cb_kb_canary
            as permissive for select to kb_admin_doc_owner using (true);
    end if;
end
$$;

-- =============================================================================
-- 5. The four shared helpers from 004
-- =============================================================================
grant execute on function
    public.kb_admin__require_actor(uuid, text),
    public.kb_admin__refuse_nric(text[]),
    public.kb_admin__check_routing(text, text, text, text),
    public.kb_admin__numbers(text)
to kb_admin_doc_owner;

-- =============================================================================
-- 6. Checks (abort the whole file if any fails)
-- =============================================================================
do $$
declare
    kb constant text := 'public.cb_knowledge_base_updated';
    c text;
    allowed_insert text[] := array['id', 'chunk_type', 'source_document', 'section_heading', 'question',
        'answer', 'content', 'embedding', 'is_active', 'service_type', 'contact_type', 'nationality',
        'namespace', 'metadata', 'rag_score_floor'];
    f text;
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_doc_owner'
                    and not rolcanlogin and not rolbypassrls and not rolsuper and not rolinherit) then
        raise exception 'FAIL: kb_admin_doc_owner missing, or can log in / bypass RLS / inherit';
    end if;
    if has_schema_privilege('kb_admin_doc_owner', 'public', 'CREATE') then
        raise exception 'FAIL: kb_admin_doc_owner holds CREATE on schema public';
    end if;

    -- Live table: never table-wide, never DELETE, never TRUNCATE.
    if has_table_privilege('kb_admin_doc_owner', kb, 'INSERT')
       or has_table_privilege('kb_admin_doc_owner', kb, 'UPDATE')
       or has_table_privilege('kb_admin_doc_owner', kb, 'DELETE')
       or has_table_privilege('kb_admin_doc_owner', kb, 'TRUNCATE') then
        raise exception 'FAIL: kb_admin_doc_owner holds table-wide INSERT/UPDATE/DELETE/TRUNCATE on the live table';
    end if;
    -- INSERT: exactly the fifteen columns.
    for c in select attname from pg_attribute where attrelid = kb::regclass and attnum > 0 and not attisdropped loop
        if has_column_privilege('kb_admin_doc_owner', kb, c, 'INSERT') <> (c = any (allowed_insert)) then
            raise exception 'FAIL: kb_admin_doc_owner INSERT on column % is %', c,
                case when c = any (allowed_insert) then 'missing' else 'granted but must not be' end;
        end if;
        if has_column_privilege('kb_admin_doc_owner', kb, c, 'UPDATE') <> (c in ('is_active', 'metadata')) then
            raise exception 'FAIL: kb_admin_doc_owner UPDATE on column % is wrong', c;
        end if;
    end loop;
    if not exists (select 1 from pg_policies where tablename = 'cb_knowledge_base_updated'
                    and policyname = 'kb_admin_doc_owner_insert_doc' and cmd = 'INSERT'
                    and with_check like '%managed_by%' and with_check like '%document_chunk%'
                    and with_check not like '%qa_pair%') then
        raise exception 'FAIL: the doc-owner INSERT policy is missing or wrong';
    end if;
    if not exists (select 1 from pg_policies where tablename = 'cb_knowledge_base_updated'
                    and policyname = 'kb_admin_doc_owner_update_doc' and cmd = 'UPDATE'
                    and qual like '%document_chunk%' and qual not like '%qa_pair%'
                    and with_check like '%managed_by%') then
        raise exception 'FAIL: the doc-owner UPDATE policy is missing or wrong';
    end if;
    -- No policy for this role reaches qa_pair rows or deletes anything.
    if exists (select 1 from pg_policies where tablename = 'cb_knowledge_base_updated'
                and 'kb_admin_doc_owner' = any (roles) and cmd in ('DELETE', 'ALL')) then
        raise exception 'FAIL: a DELETE/ALL policy on the live table names kb_admin_doc_owner';
    end if;

    -- Other tables it must not reach.
    foreach f in array array['public.cb_kb_entry_versions', 'public.cb_kb_rules', 'public.leads',
                             'public.cb_tickets', 'public.wp_chat_conversations'] loop
        if to_regclass(f) is not null
           and (has_table_privilege('kb_admin_doc_owner', f, 'SELECT, INSERT, UPDATE, DELETE')
                or has_any_column_privilege('kb_admin_doc_owner', f, 'SELECT, INSERT, UPDATE')) then
            raise exception 'FAIL: kb_admin_doc_owner holds a privilege on %', f;
        end if;
    end loop;
    if has_table_privilege('kb_admin_doc_owner', 'public.cb_kb_audit', 'UPDATE, DELETE, SELECT') then
        raise exception 'FAIL: kb_admin_doc_owner can do more than INSERT on cb_kb_audit';
    end if;
    if has_table_privilege('kb_admin_doc_owner', 'public.cb_kb_documents', 'DELETE')
       or has_table_privilege('kb_admin_doc_owner', 'public.cb_kb_batches', 'DELETE') then
        raise exception 'FAIL: kb_admin_doc_owner can DELETE documents or batches';
    end if;

    -- Exactly the four helpers of 004's functions.
    for f in select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace
              where n.nspname = 'public' and pg_get_userbyid(p.proowner) = 'kb_admin_fn_owner' loop
        if has_function_privilege('kb_admin_doc_owner', f, 'EXECUTE') <> (f in (
               'kb_admin__require_actor(uuid,text)', 'kb_admin__refuse_nric(text[])',
               'kb_admin__check_routing(text,text,text,text)', 'kb_admin__numbers(text)')) then
            raise exception 'FAIL: kb_admin_doc_owner EXECUTE on % is wrong', f;
        end if;
    end loop;

    -- kb_admin_fn_owner unchanged: still no DELETE and no table-wide INSERT.
    if has_table_privilege('kb_admin_fn_owner', kb, 'INSERT') or has_table_privilege('kb_admin_fn_owner', kb, 'DELETE')
       or has_table_privilege('kb_admin_fn_owner', 'public.cb_kb_documents', 'SELECT') then
        raise exception 'FAIL: kb_admin_fn_owner gained a privilege';
    end if;
    raise notice 'PASS: 010 - kb_admin_doc_owner is NOLOGIN, its live-table surface is exactly 15 INSERT + 2 UPDATE columns under RLS, nothing else';
end
$$;

commit;
