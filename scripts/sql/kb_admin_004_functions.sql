-- kb_admin_004_functions: the ONLY way the Q&A editor writes.
--
-- kb-admin's editor login (kb_admin_editor, created in 005) has no privilege
-- on any table. It can call the five functions below and nothing else. Each is
-- SECURITY DEFINER, so it runs as kb_admin_fn_owner: a NOLOGIN role that can
-- read and update a fixed list of columns of the knowledge base (Q&A rows
-- only, enforced by RLS), write versions and audit rows, and read the access
-- list. It cannot reach leads, tickets, conversations or the pricing rules.
--
-- WHO THE CALLER IS. Every function takes p_actor_id (the Supabase Auth user
-- id kb-admin has just verified with Supabase Auth) and p_actor_email, and
-- looks p_actor_id up in cb_kb_admin_users ITSELF. A bug in kb-admin's UI
-- cannot let a viewer write. The accepted limit: whoever controls the
-- kb-admin server can name any actor - they would hold every other kb-admin
-- secret too.
--
-- REVIEW RULE (Option C). An editor publishes wording-only changes. A change
-- that adds, removes or alters a NUMBER, or changes service / audience /
-- nationality, or switches the entry on/off, needs an approver. An approver
-- may publish their own such change; it is logged as self_approved.
--
-- ERRORS carry a stable SQLSTATE so kb-admin can show a plain message:
--   KB001 not an active editor/approver     KB004 needs an approver
--   KB002 invalid input / nothing to do      KB005 embedding problem
--   KB003 conflict (stale draft, open draft) KB006 text contains an NRIC/FIN
--
-- Depends on 003 (tables). kb_admin_publish reads cb_kb_canary (created in
-- 006) at CALL time; publishing refuses until the canary row is seeded.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

-- =============================================================================
-- The owner role
-- =============================================================================
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_fn_owner') then
        create role kb_admin_fn_owner nologin noinherit nobypassrls;
    end if;
end
$$;
alter role kb_admin_fn_owner nologin noinherit nobypassrls nocreatedb nocreaterole noreplication;

-- The role applying this file (postgres) must be able to SET ROLE to the new
-- owner to hand it the functions (Postgres 16+ rule), and to INHERIT its
-- privileges to replace them on a later re-run. postgres already holds a
-- superset of these privileges. createrole_self_grant is empty here (read
-- 2026-09-30), so CREATE ROLE above gives postgres an ADMIN-only membership
-- with SET and INHERIT both false: a check for "is a member" would pass and
-- the ALTER ... OWNER below would still fail. Check the two options instead.
do $$
begin
    if not exists (
        select 1 from pg_auth_members am
          join pg_roles g on g.oid = am.roleid
          join pg_roles m on m.oid = am.member
         where g.rolname = 'kb_admin_fn_owner' and m.rolname = current_user
           and am.set_option and am.inherit_option
    ) then
        execute format('grant kb_admin_fn_owner to %I with inherit true, set true', current_user);
    end if;
end
$$;

grant usage on schema public to kb_admin_fn_owner;

-- --- Narrow grants -------------------------------------------------------------
-- Knowledge base: read what the functions compare, update only what the editor
-- edits (+ content/embedding, which follow the text, and metadata, for
-- managed_by). Never namespace, chunk_type, source_document, frequency,
-- rag_score_floor, page_or_section, created_at.
grant select (
    id, chunk_type, question, answer, content, section_heading, service_type,
    contact_type, nationality, is_active, embedding, metadata
) on public.cb_knowledge_base_updated to kb_admin_fn_owner;
grant update (
    question, answer, content, section_heading, service_type, contact_type,
    nationality, is_active, embedding, metadata
) on public.cb_knowledge_base_updated to kb_admin_fn_owner;

-- Versions: insert, read, and change only a version's state and the vector it
-- ends up published with.
grant select, insert on public.cb_kb_entry_versions to kb_admin_fn_owner;
grant update (status, content, embedding, model_name) on public.cb_kb_entry_versions to kb_admin_fn_owner;

-- Audit: insert only.
grant insert on public.cb_kb_audit to kb_admin_fn_owner;

-- Access list: read only.
grant select (user_id, role, active) on public.cb_kb_admin_users to kb_admin_fn_owner;

-- --- RLS policies for the owner -----------------------------------------------
-- The knowledge-base UPDATE policy is limited to Q&A rows, so even a bug in a
-- function cannot touch a document chunk, a table unit or a style example.
do $$
begin
    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_knowledge_base_updated' and policyname = 'kb_admin_fn_owner_select') then
        create policy kb_admin_fn_owner_select on public.cb_knowledge_base_updated
            as permissive for select to kb_admin_fn_owner using (true);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_knowledge_base_updated' and policyname = 'kb_admin_fn_owner_update_qa') then
        create policy kb_admin_fn_owner_update_qa on public.cb_knowledge_base_updated
            as permissive for update to kb_admin_fn_owner
            using (chunk_type = 'qa_pair') with check (chunk_type = 'qa_pair');
    end if;

    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_entry_versions' and policyname = 'kb_admin_fn_owner_all') then
        create policy kb_admin_fn_owner_all on public.cb_kb_entry_versions
            as permissive for all to kb_admin_fn_owner using (true) with check (true);
    end if;

    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_audit' and policyname = 'kb_admin_fn_owner_insert') then
        create policy kb_admin_fn_owner_insert on public.cb_kb_audit
            as permissive for insert to kb_admin_fn_owner with check (true);
    end if;

    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_admin_users' and policyname = 'kb_admin_fn_owner_select') then
        create policy kb_admin_fn_owner_select on public.cb_kb_admin_users
            as permissive for select to kb_admin_fn_owner using (true);
    end if;
end
$$;

-- =============================================================================
-- Internal helpers (not callable by kb_admin_editor or anyone but the owner)
-- =============================================================================

-- The caller's role, or an error. p_min = 'editor' accepts editor or approver;
-- p_min = 'approver' accepts approver only.
create or replace function public.kb_admin__require_actor(p_actor_id uuid, p_min text)
returns text
language plpgsql stable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_role text;
begin
    select u.role into v_role
      from public.cb_kb_admin_users u
     where u.user_id = p_actor_id and u.active;
    if v_role is null or v_role not in ('editor', 'approver') then
        raise exception 'not an active editor or approver' using errcode = 'KB001';
    end if;
    if p_min = 'approver' and v_role <> 'approver' then
        raise exception 'this action needs an approver' using errcode = 'KB004';
    end if;
    return v_role;
end;
$$;

-- NRIC / FIN: S, T, F, G or M, seven digits, a letter - the same pattern the
-- bot redacts (app/utils.py NRIC_PATTERN), case-insensitive, as a whole word.
-- Refused outright: knowledge-base text reaches every client who asks.
create or replace function public.kb_admin__refuse_nric(variadic p_texts text[])
returns void
language plpgsql immutable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    t text;
begin
    foreach t in array p_texts loop
        if t ~* '\m[STFGM][0-9]{7}[A-Z]\M' then
            raise exception 'the text contains what looks like an NRIC/FIN number'
                using errcode = 'KB006';
        end if;
    end loop;
end;
$$;

-- Every number in a text, as a sorted list, for the Option C comparison.
-- Pattern:  \$?\d[\d,]*(?:\.\d+)?%?
--   an optional $, a digit, then digits or commas, an optional decimal part,
--   an optional %.  "$1,588" "450" "2.5%" "24" "S$650" (-> "$650").
-- Commas are removed ("$1,588" = "$1588") so reformatting is not a change;
-- the $ and % are kept, so "300" -> "$300" IS a change (it became a price).
-- Deliberately coarse: it also catches times, counts and dates. A false
-- positive costs an approver's click; a false negative is an unreviewed price.
create or replace function public.kb_admin__numbers(p_text text)
returns text[]
language sql immutable
set search_path = pg_catalog, public, pg_temp
as $$
    select coalesce(array_agg(n order by n), '{}'::text[])
      from (
          select replace(m.parts[1], ',', '') as n
            from regexp_matches(coalesce(p_text, ''), '(\$?\d[\d,]*(?:\.\d+)?%?)', 'g') as m(parts)
      ) nums;
$$;

-- Routing values the editor may choose. Audience is limited to the three the
-- bot routes on. A service must already be in use by an active row (the
-- bot's retrieval vocabulary) or be the entry's own current service: a label
-- the bot has never seen silently narrows the search to 'general'.
create or replace function public.kb_admin__check_routing(
    p_service text, p_audience text, p_nationality text, p_current_service text)
returns void
language plpgsql stable
set search_path = pg_catalog, public, pg_temp
as $$
begin
    if p_audience is null or p_audience not in ('employer', 'candidate', 'all') then
        raise exception 'audience must be employer, candidate or all' using errcode = 'KB002';
    end if;
    if p_nationality is null or p_nationality not in ('PH', 'ID', 'MM', 'all') then
        raise exception 'nationality must be PH, ID, MM or all' using errcode = 'KB002';
    end if;
    if p_service is null
       or not (p_service = p_current_service
               or exists (select 1 from public.cb_knowledge_base_updated k
                           where k.is_active and k.service_type = p_service)) then
        raise exception 'unknown service "%"', p_service using errcode = 'KB002';
    end if;
end;
$$;

-- The editable fields of a version, for audit old_values / new_values.
-- Never the embedding.
create or replace function public.kb_admin__fields(p_v public.cb_kb_entry_versions)
returns jsonb
language sql immutable
set search_path = pg_catalog, public, pg_temp
as $$
    select jsonb_build_object(
        'version_number', p_v.version_number,
        'question', p_v.question, 'answer', p_v.answer,
        'section_heading', p_v.section_heading,
        'service', p_v.service, 'audience', p_v.audience,
        'nationality', p_v.nationality, 'active', p_v.active,
        'model_name', p_v.model_name);
$$;

-- The next version number for an entry.
create or replace function public.kb_admin__next_number(p_entry_id uuid)
returns integer
language sql stable
set search_path = pg_catalog, public, pg_temp
as $$
    select coalesce(max(v.version_number), 0) + 1
      from public.cb_kb_entry_versions v where v.entry_id = p_entry_id;
$$;

-- The entry's live version. LAZY BACKFILL: an entry with no history yet gets
-- the live row copied in as version 1, status 'published', created_by NULL.
-- model_name is 'unrecorded (pre-editor)': which model made a pre-editor
-- vector was never recorded, and 003's cb_kb_entry_versions_model_check
-- requires a stored vector to name one (NULL fails it, so every first edit
-- would). The label can never equal the canary's model, so a restore to the
-- baseline always re-embeds (kb_admin_publish checks).
-- Callers hold the live row's lock (FOR UPDATE) first, so two first edits of
-- the same entry cannot both create a baseline.
create or replace function public.kb_admin__ensure_baseline(p_entry_id uuid)
returns public.cb_kb_entry_versions
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v public.cb_kb_entry_versions;
begin
    select * into v from public.cb_kb_entry_versions
     where entry_id = p_entry_id and status = 'published';
    if found then
        return v;
    end if;
    if exists (select 1 from public.cb_kb_entry_versions where entry_id = p_entry_id) then
        raise exception 'entry % has history but no published version', p_entry_id
            using errcode = 'KB003';
    end if;

    insert into public.cb_kb_entry_versions (
        entry_id, version_number, question, answer, section_heading, service,
        audience, nationality, active, content, embedding, model_name, status,
        based_on_version, created_by, created_by_email, reason)
    select k.id, 1, k.question, k.answer, k.section_heading, k.service_type,
           k.contact_type, k.nationality, k.is_active, k.content, k.embedding,
           'unrecorded (pre-editor)',
           'published', null, null, null,
           'baseline: the live entry as it was before its first edit in kb-admin'
      from public.cb_knowledge_base_updated k
     where k.id = p_entry_id
    returning * into v;
    return v;
end;
$$;

-- Does the live row still hold exactly what version p_v says? False means the
-- loader, hand-run SQL or anything else changed it outside the editor.
create or replace function public.kb_admin__live_matches(p_entry_id uuid, p_v public.cb_kb_entry_versions)
returns boolean
language sql stable
set search_path = pg_catalog, public, pg_temp
as $$
    select exists (
        select 1 from public.cb_knowledge_base_updated k
         where k.id = p_entry_id
           and k.question        is not distinct from p_v.question
           and k.answer          is not distinct from p_v.answer
           and k.section_heading is not distinct from p_v.section_heading
           and k.service_type    is not distinct from p_v.service
           and k.contact_type    is not distinct from p_v.audience
           and k.nationality     is not distinct from p_v.nationality
           and k.is_active       is not distinct from p_v.active
           and k.content         is not distinct from p_v.content
           and k.embedding       is not distinct from p_v.embedding);
$$;

-- Discard an open draft and log it. Shared by kb_admin_discard_draft and by a
-- newer draft replacing the actor's own older one.
create or replace function public.kb_admin__discard(
    p_v public.cb_kb_entry_versions, p_actor_id uuid, p_actor_email text, p_reason text)
returns void
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $$
begin
    update public.cb_kb_entry_versions set status = 'discarded' where id = p_v.id;
    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email,
                                    old_values, new_values, reason)
    values (p_v.entry_id, p_v.id, 'discarded', p_actor_id, p_actor_email,
            public.kb_admin__fields(p_v), null, p_reason);
end;
$$;

-- =============================================================================
-- 1. kb_admin_save_draft
-- =============================================================================
-- Saves a draft of one Q&A entry. Changes NOTHING the chatbot reads.
-- Caller: an active editor or approver.
--   * The entry must exist and be a Q&A pair (chunk_type 'qa_pair').
--   * Question and answer are required; a reason is required.
--   * Text containing an NRIC/FIN is refused (even as a draft).
--   * Question + newline + answer may not exceed 1200 characters (the bot
--     cuts retrieved text at rag_max_chunk_chars = 1200) - unless it is no
--     longer than the live content, so an over-limit entry can still be
--     edited and shortened, never grown.
--   * Routing values are checked (see kb_admin__check_routing).
--   * First edit of an entry: the live row is saved as version 1 first.
--   * One open draft per entry. The caller's own open draft is discarded and
--     replaced; someone else's open draft blocks the save (KB003).
--   * p_active NULL keeps the entry's current on/off state.
-- Returns the new draft's version id.
create or replace function public.kb_admin_save_draft(
    p_actor_id        uuid,
    p_actor_email     text,
    p_entry_id        uuid,
    p_question        text,
    p_answer          text,
    p_section_heading text,
    p_service         text,
    p_audience        text,
    p_nationality     text,
    p_active          boolean,
    p_reason          text)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_q      text := btrim(coalesce(p_question, ''));
    v_a      text := btrim(coalesce(p_answer, ''));
    v_h      text := nullif(btrim(coalesce(p_section_heading, '')), '');
    v_reason text := btrim(coalesce(p_reason, ''));
    v_live   record;
    v_cur    public.cb_kb_entry_versions;
    v_open   public.cb_kb_entry_versions;
    v_new    public.cb_kb_entry_versions;
    v_new_len int;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    if v_q = '' or v_a = '' then
        raise exception 'question and answer are both required' using errcode = 'KB002';
    end if;
    perform public.kb_admin__refuse_nric(v_q, v_a, coalesce(v_h, ''));

    -- Lock order in every function: the live row first, then versions.
    select k.id, k.chunk_type, k.service_type, k.is_active, k.content into v_live
      from public.cb_knowledge_base_updated k where k.id = p_entry_id for update;
    if not found then
        raise exception 'no such entry %', p_entry_id using errcode = 'KB002';
    end if;
    if v_live.chunk_type <> 'qa_pair' then
        raise exception 'only Q&A entries can be edited (this is %)', v_live.chunk_type
            using errcode = 'KB002';
    end if;
    -- Length: refused only when it is over 1200 AND grows past the live text.
    v_new_len := length(v_q || E'\n' || v_a);
    if v_new_len > 1200 and v_new_len > coalesce(length(v_live.content), 0) then
        raise exception 'the combined question and answer is too long (% characters, limit 1200)',
            v_new_len using errcode = 'KB002';
    end if;
    perform public.kb_admin__check_routing(p_service, p_audience, p_nationality, v_live.service_type);

    v_cur := public.kb_admin__ensure_baseline(p_entry_id);

    select * into v_open from public.cb_kb_entry_versions
     where entry_id = p_entry_id and status = 'draft' for update;
    if found then
        if v_open.created_by is distinct from p_actor_id then
            raise exception 'a draft of this entry is already open (by %)',
                coalesce(v_open.created_by_email, 'another user') using errcode = 'KB003';
        end if;
        perform public.kb_admin__discard(v_open, p_actor_id, p_actor_email, 'replaced by a newer draft');
    end if;

    insert into public.cb_kb_entry_versions (
        entry_id, version_number, question, answer, section_heading, service,
        audience, nationality, active, content, embedding, model_name, status,
        based_on_version, created_by, created_by_email, reason)
    values (
        p_entry_id, public.kb_admin__next_number(p_entry_id), v_q, v_a, v_h, p_service,
        p_audience, p_nationality, coalesce(p_active, v_live.is_active),
        v_q || E'\n' || v_a, null, null, 'draft',
        v_cur.version_number, p_actor_id, p_actor_email, v_reason)
    returning * into v_new;

    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email,
                                    old_values, new_values, reason)
    values (p_entry_id, v_new.id, 'draft_saved', p_actor_id, p_actor_email,
            public.kb_admin__fields(v_cur), public.kb_admin__fields(v_new), v_reason);

    return v_new.id;
end;
$$;

-- =============================================================================
-- 2. kb_admin_discard_draft
-- =============================================================================
-- Marks an open draft as discarded. Changes nothing the chatbot reads.
-- Caller: the editor who created the draft, or any active approver.
create or replace function public.kb_admin_discard_draft(
    p_actor_id    uuid,
    p_actor_email text,
    p_version_id  uuid,
    p_reason      text)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_role   text;
    v_reason text := btrim(coalesce(p_reason, ''));
    v_draft  public.cb_kb_entry_versions;
begin
    v_role := public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;

    select * into v_draft from public.cb_kb_entry_versions
     where id = p_version_id for update;
    if not found or v_draft.status <> 'draft' then
        raise exception 'no open draft %', p_version_id using errcode = 'KB002';
    end if;
    if v_draft.created_by is distinct from p_actor_id and v_role <> 'approver' then
        raise exception 'only the draft''s author or an approver can discard it'
            using errcode = 'KB001';
    end if;

    perform public.kb_admin__discard(v_draft, p_actor_id, p_actor_email, v_reason);
end;
$$;

-- =============================================================================
-- 3. kb_admin_publish
-- =============================================================================
-- Makes a draft live: the ONE place the editor changes what the chatbot reads.
-- Caller: an active editor for a wording-only change; an active approver when
-- the change needs approval (below). An approver publishing their own such
-- draft is logged 'approved' with self_approved = true.
--
-- Needs approval (Option C) when, comparing the live entry to the draft:
--   * the numbers in question + answer + section heading differ
--     (kb_admin__numbers: any added, removed or changed), or
--   * service, audience or nationality differs, or
--   * on/off differs.
--
-- Refuses:
--   * a draft that is not open, or an entry that is not a Q&A pair;
--   * a STALE draft: its based_on_version is no longer the live version
--     (optimistic locking), or the live row was changed outside the editor
--     since that version (loader, hand-run SQL);
--   * text containing an NRIC/FIN;
--   * question + newline + answer over 1200 characters AND longer than the
--     live content (catches a restored draft, which never passes through
--     kb_admin_save_draft);
--   * a question/answer change without a vector. The vector is p_new_embedding
--     or, for a restored draft, the vector stored with it - reused only if it
--     was computed on exactly this text by the canary's model;
--   * a vector that is not 1536-dimensional, or whose model is not the one
--     recorded in cb_kb_canary (and refuses any new vector until the canary
--     row is seeded);
--   * a draft identical to the live entry with no new vector.
--
-- Writes text and vector in ONE update, so the entry is never searchable on
-- its old wording while answering with the new. Sets metadata.managed_by =
-- 'ui', so scripts/load_service_notes.py leaves the entry alone from now on.
-- The previous live version becomes 'superseded'.
--
-- The audit reason is the draft's reason.
-- Returns {entry_id, version_id, version_number, needs_approval,
--          self_approved, reembedded, why_approval: [...]}.
create or replace function public.kb_admin_publish(
    p_actor_id      uuid,
    p_actor_email   text,
    p_version_id    uuid,
    p_new_embedding vector,
    p_model_name    text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_role            text;
    v_entry_id        uuid;
    v_live            record;
    v_draft           public.cb_kb_entry_versions;
    v_cur             public.cb_kb_entry_versions;
    v_text_changed    boolean;
    v_other_changed   boolean;
    v_why             text[] := '{}';
    v_needs_approval  boolean;
    v_self_approved   boolean := false;
    v_canary_model    text;
    v_new_content     text;
    v_emb             vector;
    v_model           text;
    v_content         text;
    v_reembedded      boolean := false;
    v_new_len         int;
begin
    v_role := public.kb_admin__require_actor(p_actor_id, 'editor');

    select entry_id into v_entry_id from public.cb_kb_entry_versions where id = p_version_id;
    if v_entry_id is null then
        raise exception 'no such draft %', p_version_id using errcode = 'KB002';
    end if;

    -- Lock order: live row, then versions.
    select k.id, k.chunk_type, k.question, k.answer, k.content, k.section_heading,
           k.service_type, k.contact_type, k.nationality, k.is_active, k.embedding
      into v_live
      from public.cb_knowledge_base_updated k where k.id = v_entry_id for update;
    if not found then
        raise exception 'no such entry %', v_entry_id using errcode = 'KB002';
    end if;
    if v_live.chunk_type <> 'qa_pair' then
        raise exception 'only Q&A entries can be edited (this is %)', v_live.chunk_type using errcode = 'KB002';
    end if;

    select * into v_draft from public.cb_kb_entry_versions where id = p_version_id for update;
    if v_draft.status <> 'draft' then
        raise exception 'version % is %, not an open draft', p_version_id, v_draft.status
            using errcode = 'KB002';
    end if;

    select * into v_cur from public.cb_kb_entry_versions
     where entry_id = v_entry_id and status = 'published' for update;
    if not found or v_draft.based_on_version is distinct from v_cur.version_number then
        raise exception 'the entry has changed since this draft was started; reopen it'
            using errcode = 'KB003';
    end if;
    if not public.kb_admin__live_matches(v_entry_id, v_cur) then
        raise exception 'the live entry was changed outside the editor since this draft was started; reopen it'
            using errcode = 'KB003';
    end if;

    perform public.kb_admin__refuse_nric(coalesce(v_draft.question, ''), coalesce(v_draft.answer, ''),
                                         coalesce(v_draft.section_heading, ''));

    -- Length: refused only when it is over 1200 AND grows past the live text.
    v_new_len := length(coalesce(v_draft.question, '') || E'\n' || coalesce(v_draft.answer, ''));
    if v_new_len > 1200 and v_new_len > coalesce(length(v_live.content), 0) then
        raise exception 'the combined question and answer is too long (% characters, limit 1200)',
            v_new_len using errcode = 'KB002';
    end if;

    -- What changed.
    v_text_changed := v_draft.question is distinct from v_live.question
                   or v_draft.answer   is distinct from v_live.answer;
    if public.kb_admin__numbers(concat_ws(' ', v_live.question, v_live.answer, v_live.section_heading))
       is distinct from
       public.kb_admin__numbers(concat_ws(' ', v_draft.question, v_draft.answer, v_draft.section_heading)) then
        v_why := array_append(v_why, 'numbers'::text);
    end if;
    if v_draft.service     is distinct from v_live.service_type then v_why := array_append(v_why, 'service'::text); end if;
    if v_draft.audience    is distinct from v_live.contact_type then v_why := array_append(v_why, 'audience'::text); end if;
    if v_draft.nationality is distinct from v_live.nationality  then v_why := array_append(v_why, 'nationality'::text); end if;
    if v_draft.active      is distinct from v_live.is_active    then v_why := array_append(v_why, 'on_off'::text); end if;
    v_other_changed := v_draft.section_heading is distinct from v_live.section_heading
                    or cardinality(v_why) > 0;

    if not v_text_changed and not v_other_changed and p_new_embedding is null then
        raise exception 'the draft is identical to the live entry; nothing to publish'
            using errcode = 'KB002';
    end if;

    -- Option C.
    v_needs_approval := cardinality(v_why) > 0;
    if v_needs_approval then
        if v_role <> 'approver' then
            raise exception 'this change needs an approver (%)', array_to_string(v_why, ', ')
                using errcode = 'KB004';
        end if;
        v_self_approved := v_draft.created_by is not distinct from p_actor_id;
    end if;

    -- The vector.
    select c.model into v_canary_model from public.cb_kb_canary c where c.id = 1;
    v_new_content := v_draft.question || E'\n' || v_draft.answer;

    if p_new_embedding is not null then
        if vector_dims(p_new_embedding) <> 1536 then
            raise exception 'embedding has % dimensions, not 1536', vector_dims(p_new_embedding)
                using errcode = 'KB005';
        end if;
        if v_canary_model is null then
            raise exception 'cb_kb_canary is not seeded; refusing to store a new vector'
                using errcode = 'KB005';
        end if;
        if p_model_name is distinct from v_canary_model then
            raise exception 'embedding model "%" is not the canary''s model "%"', p_model_name, v_canary_model
                using errcode = 'KB005';
        end if;
        v_emb := p_new_embedding;
        v_model := p_model_name;
        v_content := v_new_content;
        v_reembedded := true;
    elsif v_text_changed then
        -- A restored draft carries the vector it was published with. Reuse it
        -- only if it was computed on exactly this text by the current model.
        if v_draft.embedding is not null
           and v_draft.content is not distinct from v_new_content
           and v_canary_model is not null
           and v_draft.model_name is not distinct from v_canary_model then
            v_emb := v_draft.embedding;
            v_model := v_draft.model_name;
            v_content := v_new_content;
        else
            raise exception 'the question or answer changed; a new embedding is required'
                using errcode = 'KB005';
        end if;
    else
        -- Wording unchanged (heading / routing / on-off only): keep the live
        -- vector and the text it was computed on.
        v_emb := v_live.embedding;
        v_model := v_cur.model_name;
        v_content := v_live.content;
    end if;

    -- The write the chatbot sees. The safety-net trigger (006) skips writes
    -- made here; this function logs them itself, with the person.
    perform set_config('kb_admin.via_function', 'on', true);
    update public.cb_knowledge_base_updated k
       set question        = v_draft.question,
           answer          = v_draft.answer,
           content         = v_content,
           section_heading = v_draft.section_heading,
           service_type    = v_draft.service,
           contact_type    = v_draft.audience,
           nationality     = v_draft.nationality,
           is_active       = v_draft.active,
           embedding       = v_emb,
           metadata        = coalesce(k.metadata, '{}'::jsonb) || '{"managed_by": "ui"}'::jsonb
     where k.id = v_entry_id;
    perform set_config('kb_admin.via_function', 'off', true);

    update public.cb_kb_entry_versions set status = 'superseded' where id = v_cur.id;
    update public.cb_kb_entry_versions
       set status = 'published', content = v_content, embedding = v_emb, model_name = v_model
     where id = v_draft.id
    returning * into v_draft;

    if v_needs_approval then
        insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email,
                                        old_values, new_values, reason, self_approved)
        values (v_entry_id, v_draft.id, 'approved', p_actor_id, p_actor_email,
                public.kb_admin__fields(v_cur), public.kb_admin__fields(v_draft),
                'approved (' || array_to_string(v_why, ', ') || '): ' || v_draft.reason,
                v_self_approved);
    end if;
    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email,
                                    old_values, new_values, reason)
    values (v_entry_id, v_draft.id, 'published', p_actor_id, p_actor_email,
            public.kb_admin__fields(v_cur),
            public.kb_admin__fields(v_draft) || jsonb_build_object('reembedded', v_reembedded),
            v_draft.reason);

    return jsonb_build_object(
        'entry_id', v_entry_id,
        'version_id', v_draft.id,
        'version_number', v_draft.version_number,
        'needs_approval', v_needs_approval,
        'self_approved', v_self_approved,
        'reembedded', v_reembedded,
        'why_approval', to_jsonb(v_why));
end;
$$;

-- =============================================================================
-- 4. kb_admin_restore
-- =============================================================================
-- Creates a NEW DRAFT holding an earlier version's content. Nothing is
-- rewound or deleted, and nothing the chatbot reads changes until the draft is
-- published - through kb_admin_publish, under the same Option C rules (so
-- restoring an old price still needs an approver).
-- Caller: an active editor or approver.
--   * The source must be a version of this entry that was once live
--     ('published' or 'superseded') and must not be the live one.
--   * The draft carries the source's vector and model, so publishing it costs
--     no embedding when the model still matches the canary. A baseline
--     (version 1) has no recorded model and is always re-embedded.
--   * The same one-open-draft rule as kb_admin_save_draft.
-- Returns the new draft's version id.
create or replace function public.kb_admin_restore(
    p_actor_id        uuid,
    p_actor_email     text,
    p_entry_id        uuid,
    p_from_version_id uuid,
    p_reason          text)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason text := btrim(coalesce(p_reason, ''));
    v_live   record;
    v_cur    public.cb_kb_entry_versions;
    v_from   public.cb_kb_entry_versions;
    v_open   public.cb_kb_entry_versions;
    v_new    public.cb_kb_entry_versions;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;

    select k.id, k.chunk_type into v_live
      from public.cb_knowledge_base_updated k where k.id = p_entry_id for update;
    if not found then
        raise exception 'no such entry %', p_entry_id using errcode = 'KB002';
    end if;
    if v_live.chunk_type <> 'qa_pair' then
        raise exception 'only Q&A entries can be edited (this is %)', v_live.chunk_type using errcode = 'KB002';
    end if;

    v_cur := public.kb_admin__ensure_baseline(p_entry_id);

    select * into v_from from public.cb_kb_entry_versions
     where id = p_from_version_id and entry_id = p_entry_id;
    if not found or v_from.status not in ('published', 'superseded') then
        raise exception 'version % is not an earlier live version of this entry', p_from_version_id
            using errcode = 'KB002';
    end if;
    if v_from.id = v_cur.id then
        raise exception 'that version is already live' using errcode = 'KB002';
    end if;

    select * into v_open from public.cb_kb_entry_versions
     where entry_id = p_entry_id and status = 'draft' for update;
    if found then
        if v_open.created_by is distinct from p_actor_id then
            raise exception 'a draft of this entry is already open (by %)',
                coalesce(v_open.created_by_email, 'another user') using errcode = 'KB003';
        end if;
        perform public.kb_admin__discard(v_open, p_actor_id, p_actor_email, 'replaced by a restore');
    end if;

    insert into public.cb_kb_entry_versions (
        entry_id, version_number, question, answer, section_heading, service,
        audience, nationality, active, content, embedding, model_name, status,
        based_on_version, created_by, created_by_email, reason)
    values (
        p_entry_id, public.kb_admin__next_number(p_entry_id), v_from.question, v_from.answer,
        v_from.section_heading, v_from.service, v_from.audience, v_from.nationality, v_from.active,
        v_from.content, v_from.embedding, v_from.model_name, 'draft',
        v_cur.version_number, p_actor_id, p_actor_email, v_reason)
    returning * into v_new;

    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email,
                                    old_values, new_values, reason)
    values (p_entry_id, v_new.id, 'restored', p_actor_id, p_actor_email,
            public.kb_admin__fields(v_cur),
            public.kb_admin__fields(v_new) || jsonb_build_object('restored_from_version', v_from.version_number),
            v_reason);

    return v_new.id;
end;
$$;

-- =============================================================================
-- 5. kb_admin_toggle
-- =============================================================================
-- Switches one Q&A entry on or off, immediately. An 'off' entry is never
-- retrieved (the match function reads is_active rows only). Reversible by
-- toggling back.
-- Caller: an active APPROVER only.
--   * Refuses if the entry is already in the requested state, or if the live
--     row was changed outside the editor since its live version.
--   * Records a new live version (same text and vector, new on/off state), so
--     history stays in step with the live row. Any open draft becomes stale
--     and will be refused at publish.
--   * Sets metadata.managed_by = 'ui'.
-- Returns {entry_id, version_id, version_number, active}.
create or replace function public.kb_admin_toggle(
    p_actor_id    uuid,
    p_actor_email text,
    p_entry_id    uuid,
    p_active      boolean,
    p_reason      text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason text := btrim(coalesce(p_reason, ''));
    v_live   record;
    v_cur    public.cb_kb_entry_versions;
    v_new    public.cb_kb_entry_versions;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'approver');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    if p_active is null then
        raise exception 'active must be true or false' using errcode = 'KB002';
    end if;

    select k.id, k.chunk_type, k.is_active into v_live
      from public.cb_knowledge_base_updated k where k.id = p_entry_id for update;
    if not found then
        raise exception 'no such entry %', p_entry_id using errcode = 'KB002';
    end if;
    if v_live.chunk_type <> 'qa_pair' then
        raise exception 'only Q&A entries can be edited (this is %)', v_live.chunk_type using errcode = 'KB002';
    end if;
    if v_live.is_active = p_active then
        raise exception 'the entry is already %', case when p_active then 'on' else 'off' end
            using errcode = 'KB002';
    end if;

    v_cur := public.kb_admin__ensure_baseline(p_entry_id);
    if not public.kb_admin__live_matches(p_entry_id, v_cur) then
        raise exception 'the live entry was changed outside the editor; review it before switching it'
            using errcode = 'KB003';
    end if;

    update public.cb_kb_entry_versions set status = 'superseded' where id = v_cur.id;
    insert into public.cb_kb_entry_versions (
        entry_id, version_number, question, answer, section_heading, service,
        audience, nationality, active, content, embedding, model_name, status,
        based_on_version, created_by, created_by_email, reason)
    values (
        p_entry_id, public.kb_admin__next_number(p_entry_id), v_cur.question, v_cur.answer,
        v_cur.section_heading, v_cur.service, v_cur.audience, v_cur.nationality, p_active,
        v_cur.content, v_cur.embedding, v_cur.model_name, 'published',
        v_cur.version_number, p_actor_id, p_actor_email, v_reason)
    returning * into v_new;

    perform set_config('kb_admin.via_function', 'on', true);
    update public.cb_knowledge_base_updated k
       set is_active = p_active,
           metadata  = coalesce(k.metadata, '{}'::jsonb) || '{"managed_by": "ui"}'::jsonb
     where k.id = p_entry_id;
    perform set_config('kb_admin.via_function', 'off', true);

    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email,
                                    old_values, new_values, reason)
    values (p_entry_id, v_new.id, 'toggled', p_actor_id, p_actor_email,
            public.kb_admin__fields(v_cur), public.kb_admin__fields(v_new), v_reason);

    return jsonb_build_object('entry_id', p_entry_id, 'version_id', v_new.id,
                              'version_number', v_new.version_number, 'active', p_active);
end;
$$;

-- =============================================================================
-- Ownership and EXECUTE
-- =============================================================================
-- ALTER ... OWNER TO requires the new owner to hold CREATE on the schema.
-- Held only for the hand-over, then taken back in the same transaction.
grant create on schema public to kb_admin_fn_owner;

do $$
declare
    f regprocedure;
begin
    foreach f in array array[
        'public.kb_admin__require_actor(uuid, text)',
        'public.kb_admin__refuse_nric(text[])',
        'public.kb_admin__numbers(text)',
        'public.kb_admin__check_routing(text, text, text, text)',
        'public.kb_admin__fields(public.cb_kb_entry_versions)',
        'public.kb_admin__next_number(uuid)',
        'public.kb_admin__ensure_baseline(uuid)',
        'public.kb_admin__live_matches(uuid, public.cb_kb_entry_versions)',
        'public.kb_admin__discard(public.cb_kb_entry_versions, uuid, text, text)',
        'public.kb_admin_save_draft(uuid, text, uuid, text, text, text, text, text, text, boolean, text)',
        'public.kb_admin_discard_draft(uuid, text, uuid, text)',
        'public.kb_admin_publish(uuid, text, uuid, vector, text)',
        'public.kb_admin_restore(uuid, text, uuid, uuid, text)',
        'public.kb_admin_toggle(uuid, text, uuid, boolean, text)'
    ]::regprocedure[] loop
        execute format('alter function %s owner to kb_admin_fn_owner', f);
        -- Postgres grants EXECUTE to PUBLIC on every new function, and this
        -- database's default privileges add anon, authenticated and
        -- service_role. Take all of it back; only the owner keeps it.
        execute format('revoke all on function %s from public, anon, authenticated, service_role', f);
    end loop;
end
$$;

revoke create on schema public from kb_admin_fn_owner;

-- kb_admin_editor is created in 005, which also grants this. Granted here too
-- when the role already exists, so re-running 004 alone cannot drop it.
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'kb_admin_editor') then
        grant execute on function
            public.kb_admin_save_draft(uuid, text, uuid, text, text, text, text, text, text, boolean, text),
            public.kb_admin_discard_draft(uuid, text, uuid, text),
            public.kb_admin_publish(uuid, text, uuid, vector, text),
            public.kb_admin_restore(uuid, text, uuid, uuid, text),
            public.kb_admin_toggle(uuid, text, uuid, boolean, text)
        to kb_admin_editor;
    end if;
end
$$;

commit;
