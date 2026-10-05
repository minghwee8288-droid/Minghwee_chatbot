-- ROLLBACK of kb_admin_009_doc_tables.sql. Run AFTER rollback 010, BEFORE rollback 008.
--
-- Drops cb_kb_staged_chunks, cb_kb_batches, cb_kb_documents and
-- cb_kb_probe_questions (with 013's seeded probes - they are only a seed, and
-- re-running 009 + 013 puts them back), and narrows cb_kb_audit's action
-- CHECK back to the 008 list.
--
-- REFUSES (and changes nothing) while:
--   * any document or batch exists (an uploaded file and its history would
--     be lost);
--   * cb_kb_audit holds any document action (the table is append-only, and
--     narrowing the CHECK would fail on such a row);
--   * kb_admin_doc_owner still exists (run rollback 010 first).
--
-- Changes NO row of cb_knowledge_base_updated.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'kb_admin_doc_owner') then
        raise exception 'ROLLBACK 009 REFUSED: kb_admin_doc_owner still exists; run rollbacks 011 and 010 first';
    end if;
    -- Nested, not "and": a reference to a missing table fails at parse time.
    if to_regclass('public.cb_kb_batches') is not null then
        if exists (select 1 from public.cb_kb_batches) then
            raise exception 'ROLLBACK 009 REFUSED: batches exist';
        end if;
    end if;
    if to_regclass('public.cb_kb_documents') is not null then
        if exists (select 1 from public.cb_kb_documents) then
            raise exception 'ROLLBACK 009 REFUSED: documents exist';
        end if;
    end if;
    if exists (select 1 from public.cb_kb_audit
                where action in ('doc_uploaded', 'batch_prepared', 'batch_published', 'batch_restored',
                                 'batch_discarded', 'doc_retired', 'chunk_edited')) then
        raise exception 'ROLLBACK 009 REFUSED: cb_kb_audit holds document actions';
    end if;
end
$$;

drop table if exists public.cb_kb_staged_chunks;
drop table if exists public.cb_kb_batches;
drop table if exists public.cb_kb_documents;
drop table if exists public.cb_kb_probe_questions;

alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
    'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
    'entry_created',
    'external_insert', 'external_update', 'external_delete'
));

do $$
begin
    if to_regclass('public.cb_kb_documents') is not null or to_regclass('public.cb_kb_batches') is not null
       or to_regclass('public.cb_kb_staged_chunks') is not null or to_regclass('public.cb_kb_probe_questions') is not null then
        raise exception 'ROLLBACK 009 CHECK FAILED: a document table is still present';
    end if;
    if (select pg_get_constraintdef(oid) like '%doc_uploaded%' from pg_constraint
         where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check')
       or not (select pg_get_constraintdef(oid) like '%entry_created%' from pg_constraint
                where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check') then
        raise exception 'ROLLBACK 009 CHECK FAILED: the audit CHECK is not back to the 008 list';
    end if;
    raise notice 'ROLLBACK 009: done - the four document tables dropped, audit CHECK back to 008';
end
$$;

commit;
