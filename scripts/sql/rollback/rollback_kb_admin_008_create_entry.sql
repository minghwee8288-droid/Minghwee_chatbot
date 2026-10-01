-- ROLLBACK of kb_admin_008_create_entry.sql. Run BEFORE the 006 -> 001 rollbacks.
--
-- Removes: kb_admin_create_entry, the owner's INSERT grant and INSERT policy on
-- cb_knowledge_base_updated, its SELECT on namespace, and 'entry_created' from
-- the audit action check.
--
-- REFUSES (and changes nothing) while any entry created by it exists - a row
-- with source_document = 'Ming Hwee KB Admin' - or any 'entry_created' audit
-- row exists. Those rows cannot be removed by a rollback: the audit table is
-- append-only and versions hold a foreign key to the entry. Narrowing the
-- audit CHECK with such a row present would also fail.
--
-- Changes NO row of any table.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if exists (select 1 from public.cb_knowledge_base_updated
                where source_document = 'Ming Hwee KB Admin') then
        raise exception 'ROLLBACK 008 REFUSED: entries created in kb-admin exist (source_document = ''Ming Hwee KB Admin'')';
    end if;
    if exists (select 1 from public.cb_kb_audit where action = 'entry_created') then
        raise exception 'ROLLBACK 008 REFUSED: cb_kb_audit holds entry_created rows';
    end if;
end
$$;

drop function if exists public.kb_admin_create_entry(uuid, text, text, text, text, text, text, text, text);

drop policy if exists kb_admin_fn_owner_insert_new_qa on public.cb_knowledge_base_updated;

revoke insert (
    namespace, service_type, contact_type, nationality, chunk_type, source_document,
    section_heading, metadata, is_active, rag_score_floor
) on public.cb_knowledge_base_updated from kb_admin_fn_owner;
revoke select (namespace) on public.cb_knowledge_base_updated from kb_admin_fn_owner;

alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
    'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
    'external_insert', 'external_update', 'external_delete'
));

do $$
begin
    if exists (select 1 from pg_proc where proname = 'kb_admin_create_entry')
       or exists (select 1 from pg_policies where policyname = 'kb_admin_fn_owner_insert_new_qa')
       or has_any_column_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', 'INSERT')
       or has_column_privilege('kb_admin_fn_owner', 'public.cb_knowledge_base_updated', 'namespace', 'SELECT')
       or (select pg_get_constraintdef(oid) like '%entry_created%' from pg_constraint
            where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check') then
        raise exception 'ROLLBACK 008 CHECK FAILED: something from 008 is still present';
    end if;
    raise notice 'ROLLBACK 008: done - kb_admin_create_entry and its grants removed';
end
$$;

commit;
