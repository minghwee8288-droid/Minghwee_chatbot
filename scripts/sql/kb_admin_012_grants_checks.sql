-- kb_admin_012_grants_checks: verify 009-011 took. Run AFTER 011. Leaves
-- NOTHING behind.
--
-- Unlike 007 this cannot be a READ ONLY transaction: proving that the CHECKs
-- refuse an oversized chunk, and that a staged chunk is invisible to the
-- bot's search, needs real rows. So the file opens a normal transaction,
-- inserts its probe rows, checks, and ends in ROLLBACK - nothing is ever
-- committed. It never calls a kb_admin_doc_* function (they need a real
-- person in cb_kb_admin_users); the behaviour of those is tested separately.
--
-- Each block RAISEs NOTICE 'PASS ...' or RAISEs EXCEPTION 'FAIL ...'. The
-- first FAIL stops the run with a non-zero exit from apply_sql.py.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>
-- Rollback:  scripts/sql/rollback/rollback_kb_admin_012_grants_checks.sql (a no-op)

begin;

-- -----------------------------------------------------------------------------
-- 1. The four tables: RLS on, nothing for the portal / public roles
-- -----------------------------------------------------------------------------
do $$
declare
    t text;
    r text;
begin
    foreach t in array array['cb_kb_documents', 'cb_kb_batches', 'cb_kb_staged_chunks', 'cb_kb_probe_questions'] loop
        if to_regclass('public.' || t) is null then
            raise exception 'FAIL: table % is missing', t;
        end if;
        if not (select c.relrowsecurity from pg_class c where c.oid = ('public.' || t)::regclass) then
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
    end loop;
    raise notice 'PASS: four document tables - RLS on, no access or policy for anon / authenticated / service_role / PUBLIC';
end
$$;

-- -----------------------------------------------------------------------------
-- 2. kb_admin_doc_owner can do what 010 says, and nothing more
-- -----------------------------------------------------------------------------
do $$
declare
    kb constant text := 'public.cb_knowledge_base_updated';
    c text;
    t record;
    ins text[] := array['id', 'chunk_type', 'source_document', 'section_heading', 'question', 'answer',
        'content', 'embedding', 'is_active', 'service_type', 'contact_type', 'nationality', 'namespace',
        'metadata', 'rag_score_floor'];
    -- table -> privileges it may hold (table-level)
    ok_tables jsonb := jsonb_build_object(
        'cb_kb_documents', 'SELECT,INSERT,UPDATE',
        'cb_kb_batches', 'SELECT,INSERT,UPDATE',
        'cb_kb_staged_chunks', 'SELECT,INSERT,UPDATE,DELETE',
        'cb_kb_audit', 'INSERT');
    p text;
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_doc_owner'
                    and not rolcanlogin and not rolbypassrls and not rolsuper) then
        raise exception 'FAIL: kb_admin_doc_owner missing, or can log in / bypass RLS';
    end if;
    if has_schema_privilege('kb_admin_doc_owner', 'public', 'CREATE') then
        raise exception 'FAIL: kb_admin_doc_owner holds CREATE on schema public';
    end if;

    -- Live table: column-level only; INSERT exactly the 15; UPDATE exactly is_active + metadata.
    foreach p in array array['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'] loop
        if has_table_privilege('kb_admin_doc_owner', kb, p) then
            raise exception 'FAIL: kb_admin_doc_owner holds table-wide % on the live table', p;
        end if;
    end loop;
    for c in select attname from pg_attribute where attrelid = kb::regclass and attnum > 0 and not attisdropped loop
        if has_column_privilege('kb_admin_doc_owner', kb, c, 'INSERT') <> (c = any (ins)) then
            raise exception 'FAIL: kb_admin_doc_owner INSERT on live column % is wrong', c;
        end if;
        if has_column_privilege('kb_admin_doc_owner', kb, c, 'UPDATE') <> (c in ('is_active', 'metadata')) then
            raise exception 'FAIL: kb_admin_doc_owner UPDATE on live column % is wrong', c;
        end if;
    end loop;
    -- Its live-table policies never admit a qa_pair and never delete.
    if exists (select 1 from pg_policies where tablename = 'cb_knowledge_base_updated'
                and 'kb_admin_doc_owner' = any (roles)
                and (cmd in ('DELETE', 'ALL') or coalesce(qual, '') like '%qa_pair%'
                     or coalesce(with_check, '') like '%qa_pair%')) then
        raise exception 'FAIL: a live-table policy for kb_admin_doc_owner deletes or admits qa_pair';
    end if;
    if (select count(*) from pg_policies where tablename = 'cb_knowledge_base_updated'
         and 'kb_admin_doc_owner' = any (roles)) <> 3 then
        raise exception 'FAIL: expected exactly 3 live-table policies for kb_admin_doc_owner (select, insert, update)';
    end if;

    -- Every other table in public: nothing, except the listed ones.
    for t in select c2.oid, c2.relname from pg_class c2 join pg_namespace n on n.oid = c2.relnamespace
              where n.nspname = 'public' and c2.relkind in ('r', 'v', 'm', 'p', 'f')
                and c2.relname not in ('cb_knowledge_base_updated', 'cb_kb_admin_users', 'cb_kb_canary') loop
        foreach p in array array['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] loop
            if has_table_privilege('kb_admin_doc_owner', t.oid, p)
               <> (coalesce(ok_tables ->> t.relname, '') like '%' || p || '%' and ok_tables ? t.relname) then
                raise exception 'FAIL: kb_admin_doc_owner % on % is wrong', p, t.relname;
            end if;
        end loop;
    end loop;
    if has_table_privilege('kb_admin_doc_owner', 'public.cb_kb_admin_users', 'SELECT, INSERT, UPDATE, DELETE')
       or has_column_privilege('kb_admin_doc_owner', 'public.cb_kb_admin_users', 'user_id', 'UPDATE')
       or not has_column_privilege('kb_admin_doc_owner', 'public.cb_kb_admin_users', 'role', 'SELECT') then
        raise exception 'FAIL: kb_admin_doc_owner on cb_kb_admin_users is not exactly SELECT (user_id, role, active)';
    end if;
    if has_column_privilege('kb_admin_doc_owner', 'public.cb_kb_canary', 'embedding', 'SELECT')
       or not has_column_privilege('kb_admin_doc_owner', 'public.cb_kb_canary', 'model', 'SELECT') then
        raise exception 'FAIL: kb_admin_doc_owner on cb_kb_canary is not exactly SELECT (id, model)';
    end if;

    -- Of kb_admin_fn_owner's functions, exactly the four helpers.
    for c in select p2.oid::regprocedure::text from pg_proc p2 join pg_namespace n on n.oid = p2.pronamespace
              where n.nspname = 'public' and pg_get_userbyid(p2.proowner) = 'kb_admin_fn_owner' loop
        if has_function_privilege('kb_admin_doc_owner', c, 'EXECUTE') <> (c in (
               'kb_admin__require_actor(uuid,text)', 'kb_admin__refuse_nric(text[])',
               'kb_admin__check_routing(text,text,text,text)', 'kb_admin__numbers(text)')) then
            raise exception 'FAIL: kb_admin_doc_owner EXECUTE on % is wrong', c;
        end if;
    end loop;
    raise notice 'PASS: kb_admin_doc_owner - NOLOGIN, 15 INSERT + 2 UPDATE live columns under 3 policies, new tables as 010 says, nothing else';
end
$$;

-- -----------------------------------------------------------------------------
-- 3. kb_admin_editor: EXECUTE on exactly 15 functions, no table, no helper
-- -----------------------------------------------------------------------------
do $$
declare
    expected text[] := array[
        'kb_admin_save_draft(uuid,text,uuid,text,text,text,text,text,text,boolean,text)',
        'kb_admin_discard_draft(uuid,text,uuid,text)',
        'kb_admin_publish(uuid,text,uuid,vector,text)',
        'kb_admin_restore(uuid,text,uuid,uuid,text)',
        'kb_admin_toggle(uuid,text,uuid,boolean,text)',
        'kb_admin_create_entry(uuid,text,text,text,text,text,text,text,text)',
        'kb_admin_doc_upload(uuid,text,text,text,text,bytea,text)',
        'kb_admin_doc_stage(uuid,text,uuid,jsonb,text)',
        'kb_admin_doc_edit_chunk(uuid,text,uuid,text,text,text,text,text,text,double precision[],text)',
        'kb_admin_doc_mark_checked(uuid,text,uuid)',
        'kb_admin_doc_publish(uuid,text,uuid,text)',
        'kb_admin_doc_restore(uuid,text,uuid,text)',
        'kb_admin_doc_discard(uuid,text,uuid)',
        'kb_admin_doc_retire(uuid,text,uuid,text)',
        'kb_admin_match_with_batch(uuid,text,uuid,vector,text,text,text,integer,double precision)'];
    got text[];
    t record;
begin
    select array_agg(p.oid::regprocedure::text order by 1) into got
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and has_function_privilege('kb_admin_editor', p.oid, 'EXECUTE')
       and p.proname like 'kb\_admin%';
    if got is distinct from (select array_agg(x order by x) from unnest(expected) x) then
        raise exception 'FAIL: kb_admin_editor executes %, expected %', got, expected;
    end if;
    for t in select c.oid, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
              where n.nspname = 'public' and c.relkind in ('r', 'v', 'm', 'p', 'f') loop
        if has_table_privilege('kb_admin_editor', t.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
           or has_any_column_privilege('kb_admin_editor', t.oid, 'SELECT, INSERT, UPDATE, REFERENCES') then
            raise exception 'FAIL: kb_admin_editor holds a privilege on public.%', t.relname;
        end if;
    end loop;
    raise notice 'PASS: kb_admin_editor executes exactly 15 functions (6 Q&A + 9 document) and holds no table privilege';
end
$$;

-- -----------------------------------------------------------------------------
-- 4. kb_admin_reader: the allowed columns, never file_bytes or embedding
-- -----------------------------------------------------------------------------
do $$
declare
    t text;
    c text;
begin
    foreach t in array array['cb_kb_documents', 'cb_kb_batches', 'cb_kb_staged_chunks', 'cb_kb_probe_questions'] loop
        if has_table_privilege('kb_admin_reader', 'public.' || t, 'INSERT, UPDATE, DELETE, TRUNCATE')
           or has_any_column_privilege('kb_admin_reader', 'public.' || t, 'INSERT, UPDATE') then
            raise exception 'FAIL: kb_admin_reader can write %', t;
        end if;
        for c in select attname from pg_attribute where attrelid = ('public.' || t)::regclass
                  and attnum > 0 and not attisdropped loop
            if has_column_privilege('kb_admin_reader', 'public.' || t, c, 'SELECT')
               <> (c not in ('file_bytes', 'embedding')) then
                raise exception 'FAIL: kb_admin_reader SELECT on %.% is %', t, c,
                    case when c in ('file_bytes', 'embedding') then 'granted' else 'missing' end;
            end if;
        end loop;
        if not exists (select 1 from pg_policies where tablename = t and policyname = 'kb_admin_reader_select') then
            raise exception 'FAIL: no reader policy on %', t;
        end if;
    end loop;
    raise notice 'PASS: kb_admin_reader reads every column of the four tables except file_bytes and embedding, and writes none';
end
$$;

-- -----------------------------------------------------------------------------
-- 5. A staged chunk is invisible to the bot's search; the CHECKs bite
-- -----------------------------------------------------------------------------
do $$
declare
    v_doc   uuid := gen_random_uuid();
    v_batch uuid := gen_random_uuid();
    v_chunk uuid := gen_random_uuid();
    v_live  record;
    v_seen  boolean;
    v_ok    boolean;
begin
    -- A live row to borrow a vector from, so the staged chunk would be a
    -- perfect (similarity 1.0) match if search could see it.
    select k.id, k.embedding into v_live from public.cb_knowledge_base_updated k
     where k.is_active and k.embedding is not null and k.chunk_type <> 'style_example'
     order by k.id limit 1;
    if v_live.id is null then
        raise exception 'FAIL: no active live row with a vector to probe with';
    end if;

    insert into public.cb_kb_documents (id, source_name, file_name, file_type, file_bytes, file_size_bytes,
                                        uploaded_by, uploaded_by_email)
    values (v_doc, '012 self-check', 'x.md', 'md', '\x41'::bytea, 1, gen_random_uuid(), 'check@invalid');
    insert into public.cb_kb_batches (id, document_id, prepared_by, prepared_by_email)
    values (v_batch, v_doc, gen_random_uuid(), 'check@invalid');
    insert into public.cb_kb_staged_chunks (id, batch_id, ordinal, chunk_type, content, content_hash,
                                            embedding, model_name, service, audience, nationality, namespace)
    values (v_chunk, v_batch, 0, 'passage', '012 probe', 'h', v_live.embedding, 'probe',
            'general', 'all', 'all', 'faq');

    select exists (select 1 from public.cb_match_knowledge_base_updated(v_live.embedding, 0.0, 50) m
                    where m.id = v_chunk) into v_seen;
    if v_seen then
        raise exception 'FAIL: the bot''s search returned a staged chunk';
    end if;
    if not exists (select 1 from public.cb_match_knowledge_base_updated(v_live.embedding, 0.0, 50) m
                    where m.id = v_live.id) then
        raise exception 'FAIL: probe broken - the live row itself was not returned';
    end if;

    v_ok := false;
    begin
        insert into public.cb_kb_staged_chunks (batch_id, ordinal, chunk_type, content, content_hash)
        values (v_batch, 1, 'passage', repeat('a', 1201), 'h');
    exception when check_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: a 1201-character passage was accepted'; end if;

    v_ok := false;
    begin
        insert into public.cb_kb_staged_chunks (batch_id, ordinal, chunk_type, content, content_hash)
        values (v_batch, 1, 'table_unit', repeat('a', 3001), 'h');
    exception when check_violation then v_ok := true;
    end;
    if not v_ok then raise exception 'FAIL: a 3001-character table was accepted'; end if;

    -- The live table still refuses chunk_type 'passage' (it is published as document_chunk).
    if (select pg_get_constraintdef(oid) from pg_constraint
         where conrelid = 'public.cb_knowledge_base_updated'::regclass
           and conname = 'cb_knowledge_base_updated_chunk_type_check') like '%passage%' then
        raise exception 'FAIL: the live chunk_type CHECK was widened to passage';
    end if;
    raise notice 'PASS: a staged chunk with a perfect-match vector is invisible to cb_match_knowledge_base_updated; 1201-char passage and 3001-char table refused';
end
$$;

-- -----------------------------------------------------------------------------
-- 6. The audit CHECK includes every action
-- -----------------------------------------------------------------------------
do $$
declare
    d text;
    a text;
begin
    select pg_get_constraintdef(oid) into d from pg_constraint
     where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check';
    foreach a in array array['draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
                             'entry_created', 'doc_uploaded', 'batch_prepared', 'batch_published',
                             'batch_restored', 'batch_discarded', 'doc_retired', 'chunk_edited',
                             'external_insert', 'external_update', 'external_delete'] loop
        if d not like '%''' || a || '''%' then
            raise exception 'FAIL: cb_kb_audit_action_check does not allow %', a;
        end if;
    end loop;
    raise notice 'PASS: cb_kb_audit_action_check allows all 17 actions';
end
$$;

-- -----------------------------------------------------------------------------
-- 7. The document functions: owned by kb_admin_doc_owner, pinned, private
-- -----------------------------------------------------------------------------
do $$
declare
    p record;
    r text;
begin
    for p in select pr.oid, pr.oid::regprocedure::text sig, pr.proname, pr.prosecdef,
                    pg_get_userbyid(pr.proowner) owner, pr.proconfig
               from pg_proc pr join pg_namespace n on n.oid = pr.pronamespace
              where n.nspname = 'public' and (pr.proname like 'kb\_admin\_doc\_%' or pr.proname like 'kb\_admin\_\_doc\_%'
                                              or pr.proname = 'kb_admin_match_with_batch') loop
        if p.owner <> 'kb_admin_doc_owner' then raise exception 'FAIL: % owned by %', p.sig, p.owner; end if;
        if not exists (select 1 from unnest(p.proconfig) x where x like 'search_path=%pg_temp%') then
            raise exception 'FAIL: % has no pinned search_path', p.sig;
        end if;
        if p.prosecdef <> (p.proname not like 'kb\_admin\_\_%') then
            raise exception 'FAIL: % SECURITY DEFINER is wrong', p.sig;
        end if;
        foreach r in array array['anon', 'authenticated', 'service_role', 'public', 'kb_admin_reader'] loop
            if has_function_privilege(r, p.oid, 'EXECUTE') then
                raise exception 'FAIL: % can EXECUTE %', r, p.sig;
            end if;
        end loop;
    end loop;
    raise notice 'PASS: document functions and helpers owned by kb_admin_doc_owner, pinned, executable by nobody but the owner and (entry points) kb_admin_editor';
end
$$;

rollback;
