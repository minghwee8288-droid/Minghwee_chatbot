-- ROLLBACK of kb_admin_010_doc_owner.sql. Run AFTER rollback 011, BEFORE rollback 009.
--
-- Removes kb_admin_doc_owner: its policies on the live table and on the
-- access list, audit and canary tables, every grant to it, and the role.
-- kb_admin_fn_owner and its grants are untouched.
--
-- REFUSES (and changes nothing) while the role still owns a function - i.e.
-- until rollback 011 has run.
--
-- Changes NO row of any table. Rows the role published stay where they are.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_doc_owner') then
        raise notice 'ROLLBACK 010: kb_admin_doc_owner does not exist - nothing to do';
        return;
    end if;
    if exists (select 1 from pg_proc where pg_get_userbyid(proowner) = 'kb_admin_doc_owner') then
        raise exception 'ROLLBACK 010 REFUSED: kb_admin_doc_owner still owns functions; run rollback 011 first';
    end if;
end
$$;

drop policy if exists kb_admin_doc_owner_select      on public.cb_knowledge_base_updated;
drop policy if exists kb_admin_doc_owner_insert_doc  on public.cb_knowledge_base_updated;
drop policy if exists kb_admin_doc_owner_update_doc  on public.cb_knowledge_base_updated;
drop policy if exists kb_admin_doc_owner_insert      on public.cb_kb_audit;
drop policy if exists kb_admin_doc_owner_select      on public.cb_kb_admin_users;
drop policy if exists kb_admin_doc_owner_select      on public.cb_kb_canary;
drop policy if exists kb_admin_doc_owner_all         on public.cb_kb_documents;
drop policy if exists kb_admin_doc_owner_all         on public.cb_kb_batches;
drop policy if exists kb_admin_doc_owner_select      on public.cb_kb_staged_chunks;
drop policy if exists kb_admin_doc_owner_insert      on public.cb_kb_staged_chunks;
drop policy if exists kb_admin_doc_owner_update      on public.cb_kb_staged_chunks;
drop policy if exists kb_admin_doc_owner_delete      on public.cb_kb_staged_chunks;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'kb_admin_doc_owner') then
        revoke all on public.cb_knowledge_base_updated from kb_admin_doc_owner;
        revoke all (id, namespace, service_type, contact_type, nationality, chunk_type, question, answer,
                    content, embedding, source_document, section_heading, metadata, is_active, rag_score_floor)
            on public.cb_knowledge_base_updated from kb_admin_doc_owner;
        revoke all on public.cb_kb_documents, public.cb_kb_batches, public.cb_kb_staged_chunks,
                      public.cb_kb_audit from kb_admin_doc_owner;
        revoke all (user_id, role, active) on public.cb_kb_admin_users from kb_admin_doc_owner;
        revoke all (id, model) on public.cb_kb_canary from kb_admin_doc_owner;
        revoke execute on function
            public.kb_admin__require_actor(uuid, text),
            public.kb_admin__refuse_nric(text[]),
            public.kb_admin__check_routing(text, text, text, text),
            public.kb_admin__numbers(text)
        from kb_admin_doc_owner;
        revoke usage, create on schema public from kb_admin_doc_owner;
        -- Anything left (there should be nothing) - privileges only; it owns
        -- no object, checked above.
        drop owned by kb_admin_doc_owner;
        drop role kb_admin_doc_owner;
    end if;
end
$$;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'kb_admin_doc_owner')
       or exists (select 1 from pg_policies where policyname like 'kb\_admin\_doc\_owner%') then
        raise exception 'ROLLBACK 010 CHECK FAILED: kb_admin_doc_owner or one of its policies is still present';
    end if;
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_fn_owner')
       or not exists (select 1 from pg_policies where policyname = 'kb_admin_fn_owner_update_qa') then
        raise exception 'ROLLBACK 010 CHECK FAILED: kb_admin_fn_owner was disturbed';
    end if;
    raise notice 'ROLLBACK 010: done - kb_admin_doc_owner removed, kb_admin_fn_owner untouched';
end
$$;

commit;
