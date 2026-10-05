-- ROLLBACK of kb_admin_011_doc_functions.sql. Run FIRST of the 011 -> 009 set
-- (after 013 and 012, which need none).
--
-- Removes the nine document functions and their eleven helpers. kb-admin can
-- then no longer upload, publish, restore or retire documents.
--
-- Changes NO row of any table. Anything already published stays in the live
-- table exactly as it is (active or switched off); this rollback does not undo
-- a publish. To switch a published document off again, retire it BEFORE
-- running this.
--
-- Never run without an explicit go-word from the user. Apply only with:
--   python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

revoke execute on function
    public.kb_admin_doc_upload(uuid, text, text, text, text, bytea, text),
    public.kb_admin_doc_stage(uuid, text, uuid, jsonb, text),
    public.kb_admin_doc_edit_chunk(uuid, text, uuid, text, text, text, text, text, text, float8[], text),
    public.kb_admin_doc_mark_checked(uuid, text, uuid),
    public.kb_admin_doc_publish(uuid, text, uuid, text),
    public.kb_admin_doc_restore(uuid, text, uuid, text),
    public.kb_admin_doc_discard(uuid, text, uuid),
    public.kb_admin_doc_retire(uuid, text, uuid, text),
    public.kb_admin_match_with_batch(uuid, text, uuid, vector, text, text, text, integer, double precision)
from kb_admin_editor;

drop function if exists public.kb_admin_doc_upload(uuid, text, text, text, text, bytea, text);
drop function if exists public.kb_admin_doc_stage(uuid, text, uuid, jsonb, text);
drop function if exists public.kb_admin_doc_edit_chunk(uuid, text, uuid, text, text, text, text, text, text, float8[], text);
drop function if exists public.kb_admin_doc_mark_checked(uuid, text, uuid);
drop function if exists public.kb_admin_doc_publish(uuid, text, uuid, text);
drop function if exists public.kb_admin_doc_restore(uuid, text, uuid, text);
drop function if exists public.kb_admin_doc_discard(uuid, text, uuid);
drop function if exists public.kb_admin_doc_retire(uuid, text, uuid, text);
drop function if exists public.kb_admin_match_with_batch(uuid, text, uuid, vector, text, text, text, integer, double precision);

drop function if exists public.kb_admin__doc_check_source(text);
drop function if exists public.kb_admin__doc_namespace(text, text);
drop function if exists public.kb_admin__doc_check_chunk(text, text, text, text, text, text);
drop function if exists public.kb_admin__doc_vector(float8[]);
drop function if exists public.kb_admin__doc_require_model(text);
drop function if exists public.kb_admin__doc_content_hash(text);
drop function if exists public.kb_admin__doc_batch_hash(uuid);
drop function if exists public.kb_admin__doc_live_type(text);
drop function if exists public.kb_admin__doc_floor(text, text);
drop function if exists public.kb_admin__doc_switch_off(text, uuid[], jsonb);
drop function if exists public.kb_admin__doc_clear_checks(uuid);

do $$
begin
    if exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                where n.nspname = 'public'
                  and (p.proname like 'kb\_admin\_doc\_%' or p.proname like 'kb\_admin\_\_doc\_%'
                       or p.proname = 'kb_admin_match_with_batch')) then
        raise exception 'ROLLBACK 011 CHECK FAILED: a document function is still present';
    end if;
    if exists (select 1 from pg_proc where pg_get_userbyid(proowner) = 'kb_admin_doc_owner') then
        raise exception 'ROLLBACK 011 CHECK FAILED: kb_admin_doc_owner still owns a function';
    end if;
    raise notice 'ROLLBACK 011: done - the document functions are gone; no row was changed';
end
$$;

commit;
