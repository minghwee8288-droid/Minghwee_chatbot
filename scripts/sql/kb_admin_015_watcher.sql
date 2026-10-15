-- kb_admin_015_watcher: the knowledge-base watcher (Thomas's Phase 3 item 5).
--
-- "Watch, review, publish". Staff register public source pages (a MOM FDW
-- page, an embassy page); kb-admin fetches them, on demand or on a schedule,
-- keeps a snapshot of each page's main text, and when the text has really
-- changed drafts the knowledge-base change it implies. A PERSON approves or
-- rejects every draft. Nothing reaches the chatbot without that approval, and
-- an approved draft still goes live only through the existing Q&A path:
--   approve -> kb_admin_save_draft (an edit) or kb_admin_create_entry (a new
--   entry), exactly as if the approver had typed it -> kb_admin_publish with a
--   canary-checked embedding (004, 008) -> the bot sees it.
--
-- WHAT THIS FILE ADDS (all additive; no existing row, column or function is
-- changed apart from widening cb_kb_audit's action CHECK):
--   cb_kb_sources            the registered pages
--   cb_kb_source_snapshots   every fetch: the extracted main text and its hash,
--                            or the error (a failed fetch is a row, never silent)
--   cb_kb_drafts             proposed KB changes, their review state, and the
--                            PROVENANCE of an approved one: source URL, the
--                            snapshot's fetched_at, approved_by, approved_at,
--                            the entry and the version it became
--   eight SECURITY DEFINER functions, owned by kb_admin_fn_owner, executable by
--   kb_admin_editor only.
-- The live table cb_knowledge_base_updated gets NO new column: provenance
-- lives on the draft row (cb_kb_drafts.entry_id_after), so the bot's table and
-- its search function are untouched.
--
-- THE FEE LOCK (hard rule). A draft may NEVER change an agency fee, a
-- cb_kb_rules value, or a row that came from the agency's fees document
-- (docs/Minghwee bot fees and process final structure.docx, applied
-- 2026-10-15). kb_admin__watch_fee_lock() marks such a draft 'blocked' ("needs
-- agency confirmation"), and kb_admin_watch_approve refuses it (KB007) - it
-- re-checks against the target's CURRENT text at approval, so a row edited in
-- between cannot slip through. Blocked if ANY of:
--   * the target row's question is one of the 23 the fees document set, or its
--     text carries one of the 7 fees-document corrections;
--   * it names an agency fee ("agency fee", "replacement fee", "placement
--     fee", "documentation fee", "processing fee", ...) - a government fee
--     named as such ("MOM processing fee", "work permit fee") is not one;
--   * it carries a money figure next to a helper's salary (the salary floors
--     are cb_kb_rules values);
--   * it carries a money figure ($ / S$ / SGD / RM / Rp / PHP before a digit,
--     or "<n> dollars") and the page is NOT in the "government rule" category.
-- GOVERNMENT FIGURES (decision 2026-10-15). A money figure from a "government
-- rule" page (levy, security bond, MOM fees, minimum income, insurance
-- minimums) is approvable, but the draft carries figure_check: kb-admin shows
-- "This changes a figure: check it against the source before approving", and
-- kb_admin_watch_approve refuses (KB008) unless the approver ticked that they
-- did (p_confirm_figure); the tick is recorded (figure_confirmed) and audited.
-- The two lists are generated from the loader (scripts/gen_fee_locked.py ->
-- kb-admin/lib/watcher/fee-locked.ts); kb-admin's self-check fails if the
-- arrays below and that module disagree. cb_kb_rules is untouched by
-- construction: kb_admin_fn_owner holds no privilege on it.
--
-- WHO CALLS. Every function takes p_actor_id / p_actor_email and checks the
-- person in cb_kb_admin_users itself (kb_admin__require_actor, 004). Two -
-- kb_admin_source_record_fetch and kb_admin_watch_propose - also accept a NULL
-- actor: that is the scheduled watcher (kb-admin's cron route, guarded by its
-- own secret). Those two write ONLY to the three new tables and the audit,
-- never to the knowledge base, so the worst a NULL actor can do is add a
-- snapshot or a draft that a person must still approve. Adding or removing a
-- source, editing, rejecting and approving a draft always need a person;
-- approving needs an APPROVER.
--
-- ERRORS: KB001 not an active editor/approver, KB002 invalid input, KB003
-- conflict (the draft is not open, or the entry changed since it was drafted),
-- KB004 needs an approver, KB006 NRIC/FIN in the text, KB007 needs agency
-- confirmation (the fee lock), KB008 a government figure not confirmed.
--
-- ACCESS. RLS on, everything revoked from public, anon, authenticated and
-- service_role (the 009 pattern). kb_admin_reader may SELECT the three tables;
-- kb_admin_fn_owner reads and writes them through the functions only.
--
-- Apply only on the user's go-word, AFTER 001-014:
--   python scripts/apply_sql.py --expect-ref <project ref> scripts/sql/kb_admin_015_watcher.sql
-- After 015, kb_admin_editor executes 24 functions (16 + 8): kb_admin_012's
-- "exactly 16" check describes the 001-014 state and will fail if re-run; the
-- checks at the end of THIS file state the new surface.
-- Rollback: scripts/sql/rollback/rollback_kb_admin_015_watcher.sql

begin;

-- =============================================================================
-- 1. Tables
-- =============================================================================
create table if not exists public.cb_kb_sources (
    id                uuid        not null default gen_random_uuid(),
    url               text        not null,
    label             text        not null,
    category          text        not null,
    active            boolean     not null default true,
    added_by          uuid,
    added_by_email    text,
    added_at          timestamptz not null default now(),
    removed_by        uuid,
    removed_by_email  text,
    removed_at        timestamptz,
    last_checked_at   timestamptz,
    last_status       text,
    last_error        text,
    last_snapshot_id  uuid,

    constraint cb_kb_sources_pkey primary key (id),
    constraint cb_kb_sources_url_key unique (url),
    constraint cb_kb_sources_url_check check (url ~ '^https://[a-z0-9.-]+\.[a-z]{2,}(:[0-9]{2,5})?(/[^[:space:]]*)?$'
                                              and length(url) <= 2000),
    constraint cb_kb_sources_label_check check (length(btrim(label)) between 1 and 200),
    constraint cb_kb_sources_category_check check (category in ('government_rule', 'process', 'embassy', 'other')),
    constraint cb_kb_sources_status_check check (last_status is null or last_status in
        ('first', 'changed', 'unchanged', 'failed'))
);

create table if not exists public.cb_kb_source_snapshots (
    id              uuid        not null default gen_random_uuid(),
    source_id       uuid        not null,
    fetched_at      timestamptz not null default now(),
    fetch_ok        boolean     not null,
    http_status     integer,
    content_hash    text,
    title           text,
    extracted_text  text,
    changed         boolean     not null default false,
    error           text,

    constraint cb_kb_source_snapshots_pkey primary key (id),
    constraint cb_kb_source_snapshots_source_fkey foreign key (source_id) references public.cb_kb_sources (id),
    constraint cb_kb_source_snapshots_ok_check check (
        (fetch_ok and content_hash ~ '^[0-9a-f]{64}$' and extracted_text is not null and error is null)
        or (not fetch_ok and error is not null and extracted_text is null)),
    constraint cb_kb_source_snapshots_text_check check (extracted_text is null or length(extracted_text) <= 500000)
);
create index if not exists cb_kb_source_snapshots_source_idx
    on public.cb_kb_source_snapshots (source_id, fetched_at desc);

create table if not exists public.cb_kb_drafts (
    id                    uuid        not null default gen_random_uuid(),
    source_id             uuid        not null,
    snapshot_id           uuid        not null,
    source_url            text        not null,
    source_fetched_at     timestamptz not null,
    kind                  text        not null,
    target_entry_id       uuid,
    -- The target entry as it was when the draft was made (stale check).
    old_question          text,
    old_answer            text,
    proposed_question     text        not null,
    proposed_answer       text        not null,
    section_heading       text,
    service               varchar(60) not null,
    audience              varchar(20) not null,
    nationality           varchar(5)  not null,
    source_passage        text        not null,
    old_source_text       text,
    new_source_text       text,
    model_name            text,
    status                text        not null default 'pending',
    blocked_reason        text,
    -- A government page's money figure: approvable only with the approver's tick.
    figure_check          boolean     not null default false,
    figure_confirmed      boolean     not null default false,
    created_at            timestamptz not null default now(),
    created_by            uuid,
    created_by_email      text,
    edited_at             timestamptz,
    edited_by             uuid,
    edited_by_email       text,
    reviewed_at           timestamptz,
    reviewed_by           uuid,
    reviewed_by_email     text,
    review_reason         text,
    -- Provenance once approved: who, when, and what it became.
    approved_by           uuid,
    approved_by_email     text,
    approved_at           timestamptz,
    entry_id_after        uuid,
    version_id            uuid,
    published_at          timestamptz,

    constraint cb_kb_drafts_pkey primary key (id),
    constraint cb_kb_drafts_source_fkey foreign key (source_id) references public.cb_kb_sources (id),
    constraint cb_kb_drafts_snapshot_fkey foreign key (snapshot_id) references public.cb_kb_source_snapshots (id),
    constraint cb_kb_drafts_version_fkey foreign key (version_id) references public.cb_kb_entry_versions (id),
    constraint cb_kb_drafts_kind_check check (
        (kind = 'edit' and target_entry_id is not null) or (kind = 'new' and target_entry_id is null)),
    constraint cb_kb_drafts_status_check check (status in
        ('pending', 'blocked', 'approved', 'published', 'rejected', 'superseded')),
    constraint cb_kb_drafts_blocked_check check ((status = 'blocked') = (blocked_reason is not null)
                                                 or status in ('rejected', 'superseded')),
    constraint cb_kb_drafts_text_check check (length(proposed_question || E'\n' || proposed_answer) <= 1200
                                              and length(btrim(proposed_question)) > 0
                                              and length(btrim(proposed_answer)) > 0),
    constraint cb_kb_drafts_passage_check check (length(btrim(source_passage)) > 0),
    constraint cb_kb_drafts_approved_check check (status not in ('approved', 'published')
        or (approved_by is not null and approved_at is not null and version_id is not null and entry_id_after is not null))
);
create index if not exists cb_kb_drafts_status_idx on public.cb_kb_drafts (status, created_at);
create index if not exists cb_kb_drafts_entry_idx on public.cb_kb_drafts (entry_id_after);

alter table public.cb_kb_sources          enable row level security;
alter table public.cb_kb_source_snapshots enable row level security;
alter table public.cb_kb_drafts           enable row level security;

revoke all on public.cb_kb_sources, public.cb_kb_source_snapshots, public.cb_kb_drafts
    from public, anon, authenticated, service_role;

grant select on public.cb_kb_sources, public.cb_kb_source_snapshots, public.cb_kb_drafts to kb_admin_reader;

grant select, insert on public.cb_kb_sources, public.cb_kb_source_snapshots, public.cb_kb_drafts
    to kb_admin_fn_owner;
grant update (label, category, active, removed_by, removed_by_email, removed_at, last_checked_at,
              last_status, last_error, last_snapshot_id, added_by, added_by_email, added_at)
    on public.cb_kb_sources to kb_admin_fn_owner;
grant update (proposed_question, proposed_answer, section_heading, status, blocked_reason,
              figure_check, figure_confirmed, edited_at, edited_by, edited_by_email, reviewed_at, reviewed_by, reviewed_by_email,
              review_reason, approved_by, approved_by_email, approved_at, entry_id_after,
              version_id, published_at)
    on public.cb_kb_drafts to kb_admin_fn_owner;
-- To read the target's current routing values (fn_owner already reads the rest, 004/008).
grant select (source_document) on public.cb_knowledge_base_updated to kb_admin_fn_owner;

do $$
declare
    t text;
begin
    foreach t in array array['cb_kb_sources', 'cb_kb_source_snapshots', 'cb_kb_drafts'] loop
        if not exists (select 1 from pg_policies where schemaname = 'public'
                        and tablename = t and policyname = 'kb_admin_reader_select') then
            execute format('create policy kb_admin_reader_select on public.%I
                                as permissive for select to kb_admin_reader using (true)', t);
        end if;
        if not exists (select 1 from pg_policies where schemaname = 'public'
                        and tablename = t and policyname = 'kb_admin_fn_owner_all') then
            execute format('create policy kb_admin_fn_owner_all on public.%I
                                as permissive for all to kb_admin_fn_owner using (true) with check (true)', t);
        end if;
    end loop;
end
$$;

-- =============================================================================
-- 2. Audit: the nine watcher actions
-- =============================================================================
do $$
declare
    current_def text;
begin
    select pg_get_constraintdef(oid) into current_def
      from pg_constraint
     where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check';
    if current_def like '%watch_proposed%' then
        raise notice 'cb_kb_audit_action_check already allows the watcher actions - unchanged';
        return;
    end if;
    if current_def not like '%chunk_edited%' then
        raise exception 'FAIL: apply kb_admin_009 before 015 (audit CHECK has no document actions)';
    end if;
    alter table public.cb_kb_audit drop constraint cb_kb_audit_action_check;
    alter table public.cb_kb_audit add constraint cb_kb_audit_action_check check (action in (
        'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
        'entry_created',
        'doc_uploaded', 'batch_prepared', 'batch_published', 'batch_restored',
        'batch_discarded', 'doc_retired', 'chunk_edited',
        'source_added', 'source_removed', 'source_fetched', 'source_fetch_failed',
        'watch_proposed', 'watch_edited', 'watch_rejected', 'watch_approved', 'watch_published',
        'external_insert', 'external_update', 'external_delete'
    ));
end
$$;

-- =============================================================================
-- 3. Internal helpers
-- =============================================================================

-- A NULL actor is the scheduled watcher; anyone else must be an active editor.
create or replace function public.kb_admin__watch_actor(p_actor_id uuid)
returns text
language plpgsql stable
set search_path = pg_catalog, public, pg_temp
as $$
begin
    if p_actor_id is null then
        return 'watcher';
    end if;
    return public.kb_admin__require_actor(p_actor_id, 'editor');
end;
$$;

-- Does the change carry a money figure? ($ / S$ / SGD / RM / Rp / PHP / ₱
-- before a digit, or "<n> dollars"/"<n> SGD"), in the current or the new text.
create or replace function public.kb_admin__watch_money(
    p_target_question text, p_target_answer text, p_new_question text, p_new_answer text)
returns boolean
language sql immutable
set search_path = pg_catalog, public, pg_temp
as $$
    select concat_ws(E'\n', p_target_question, p_target_answer, p_new_question, p_new_answer) ~* '(\$|₱|\m(sgd|rm|rp|php)\s?)\s?[0-9]'
        or concat_ws(E'\n', p_target_question, p_target_answer, p_new_question, p_new_answer) ~* '[0-9][0-9,.]*\s*(dollars|sgd)\M';
$$;

-- The fee lock: why a draft needs agency confirmation, or NULL.
-- p_category is the source page's category: only a 'government_rule' page may
-- change a money figure (and then only with the approver's tick, KB008), and
-- never an agency fee, a helper's salary, or a fees-document row.
-- The two arrays are generated by scripts/gen_fee_locked.py (do not hand-edit;
-- kb-admin's self-check compares them with kb-admin/lib/watcher/fee-locked.ts).
create or replace function public.kb_admin__watch_fee_lock(
    p_target_question text, p_target_answer text, p_new_question text, p_new_answer text, p_category text)
returns text
language plpgsql immutable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    locked_questions constant text[] := array[
-- FEE_LOCKED_QUESTIONS:BEGIN
        'How long does a direct hire take if the helper is already in Singapore?',
        'How long does a direct hire take if the helper is overseas?',
        'How much does a new hire from Indonesia cost?',
        'How much does a new hire from Myanmar cost?',
        'How much does a new hire from the Philippines cost?',
        'How much does a replacement cost?',
        'How much does a transfer helper from Indonesia cost?',
        'How much does a transfer helper from Myanmar cost?',
        'How much does a transfer helper from the Philippines cost?',
        'What is the minimum salary for a Myanmar helper, and how much should I budget to pay a helper from Myanmar?',
        'What is the minimum salary for an Indonesian helper, and how much should I budget to pay a helper from Indonesia?',
        'What is the replacement fee for a new hire from Indonesia?',
        'What is the replacement fee for a new hire from Myanmar?',
        'What is the replacement fee for a new hire from the Philippines?',
        'What is the replacement fee for a transfer helper from Indonesia?',
        'What is the replacement fee for a transfer helper from Myanmar?',
        'What is the replacement fee for a transfer helper from the Philippines?',
        'What third-party and processing fees apply to a new hire from Indonesia?',
        'What third-party and processing fees apply to a new hire from Myanmar?',
        'What third-party and processing fees apply to a new hire from the Philippines?',
        'What third-party and processing fees apply to a transfer helper from Indonesia?',
        'What third-party and processing fees apply to a transfer helper from Myanmar?',
        'What third-party and processing fees apply to a transfer helper from the Philippines?'
-- FEE_LOCKED_QUESTIONS:END
    ]::text[];
    locked_needles constant text[] := array[
-- FEE_LOCKED_NEEDLES:BEGIN
        '(salary: from S$500/month basic, S$550 onwards with experience)',
        '(salary: from S$550/month basic, S$600 onwards with experience)',
        '(salary: from S$650/month for a fresh helper, S$670 onwards with experience)',
        'If the helper we placed is not the right fit or leaves within 6 months, the replacement falls under her package (2 replacements within 6 months), for the replacement and documentation fee that package sets. The insurance and the third-party processing fees are charged again on the replacement.',
        'Indonesian helpers have a basic salary of S$550/month and start from S$600/month with experience; Myanmar helpers have a basic salary of S$500/month and start from S$550/month with experience',
        'Outside 6 months, or for a helper we did not place, our agent will confirm the cost.',
        'helper salary (from S$500 depending on nationality and experience)'
-- FEE_LOCKED_NEEDLES:END
    ]::text[];
    all_text text := concat_ws(E'\n', p_target_question, p_target_answer, p_new_question, p_new_answer);
    -- A government fee named as such is not an agency fee.
    fee_text text := regexp_replace(all_text,
        '\m(mom|ministry of manpower|government|work permit|application|issuance|levy)\s+(processing\s+)?fees?\M', ' ', 'gi');
    money    boolean := public.kb_admin__watch_money(p_target_question, p_target_answer, p_new_question, p_new_answer);
    n text;
begin
    if p_target_question is not null and btrim(p_target_question) = any (locked_questions) then
        return 'the entry came from the agency''s fees document';
    end if;
    foreach n in array locked_needles loop
        if strpos(coalesce(p_target_answer, ''), n) > 0 then
            return 'the entry carries a correction from the agency''s fees document';
        end if;
    end loop;
    if fee_text ~* '\m(agency|service|placement|replacement|documentation|processing|administrative|admin|recruitment)\s+fees?\M' then
        return 'the change concerns an agency fee';
    end if;
    if money and all_text ~* '\msalar(y|ies)\M' then
        return 'the change touches a helper salary figure (an agency pricing rule)';
    end if;
    if money and coalesce(p_category, '') <> 'government_rule' then
        return 'the change touches a money figure and the page is not a government rule page';
    end if;
    return null;
end;
$$;

-- =============================================================================
-- 4. The eight functions
-- =============================================================================

-- 4.1 Register a source page (or switch a removed one back on).
create or replace function public.kb_admin_source_add(
    p_actor_id uuid, p_actor_email text, p_url text, p_label text, p_category text)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_url   text := btrim(coalesce(p_url, ''));
    v_label text := btrim(coalesce(p_label, ''));
    v_host  text;
    v_id    uuid;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_url !~ '^https://[a-z0-9.-]+\.[a-z]{2,}(:[0-9]{2,5})?(/[^[:space:]]*)?$' or length(v_url) > 2000 then
        raise exception 'the address must be a full https:// web address' using errcode = 'KB002';
    end if;
    v_host := substring(v_url from '^https://([^/:]+)');
    if v_host ~ '^[0-9.]+$' or v_host in ('localhost') or v_host ~ '\.(local|internal|localhost)$' then
        raise exception 'the address must be a public web page' using errcode = 'KB002';
    end if;
    if v_label = '' or length(v_label) > 200 then
        raise exception 'a label of 1-200 characters is required' using errcode = 'KB002';
    end if;
    if p_category not in ('government_rule', 'process', 'embassy', 'other') then
        raise exception 'unknown category' using errcode = 'KB002';
    end if;

    select id into v_id from public.cb_kb_sources where url = v_url for update;
    if v_id is not null then
        if (select active from public.cb_kb_sources where id = v_id) then
            raise exception 'this address is already being watched' using errcode = 'KB003';
        end if;
        update public.cb_kb_sources
           set active = true, label = v_label, category = p_category,
               removed_by = null, removed_by_email = null, removed_at = null,
               added_by = p_actor_id, added_by_email = p_actor_email, added_at = now()
         where id = v_id;
    else
        insert into public.cb_kb_sources (url, label, category, added_by, added_by_email)
        values (v_url, v_label, p_category, p_actor_id, p_actor_email)
        returning id into v_id;
    end if;

    insert into public.cb_kb_audit (action, actor_id, actor_email, new_values, reason)
    values ('source_added', p_actor_id, p_actor_email,
            jsonb_build_object('source_id', v_id, 'url', v_url, 'label', v_label, 'category', p_category),
            'source page registered for watching');
    return v_id;
end;
$$;

-- 4.2 Stop watching a source (kept, switched off; its snapshots and drafts stay).
create or replace function public.kb_admin_source_remove(
    p_actor_id uuid, p_actor_email text, p_source_id uuid, p_reason text)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason text := btrim(coalesce(p_reason, ''));
    v_src    public.cb_kb_sources;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    select * into v_src from public.cb_kb_sources where id = p_source_id for update;
    if v_src.id is null or not v_src.active then
        raise exception 'no such source is being watched' using errcode = 'KB003';
    end if;
    update public.cb_kb_sources
       set active = false, removed_by = p_actor_id, removed_by_email = p_actor_email, removed_at = now()
     where id = p_source_id;
    insert into public.cb_kb_audit (action, actor_id, actor_email, old_values, reason)
    values ('source_removed', p_actor_id, p_actor_email,
            jsonb_build_object('source_id', v_src.id, 'url', v_src.url, 'label', v_src.label), v_reason);
end;
$$;

-- 4.3 Record one fetch: a snapshot (or the error), and whether the text changed.
-- "Changed" compares the normalised-text hash kb-admin computed (whitespace
-- collapsed, dates removed) with the last SUCCESSFUL snapshot's.
create or replace function public.kb_admin_source_record_fetch(
    p_actor_id uuid, p_actor_email text, p_source_id uuid, p_ok boolean, p_http_status integer,
    p_content_hash text, p_text text, p_title text, p_error text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_who      text := public.kb_admin__watch_actor(p_actor_id);
    v_email    text := case when p_actor_id is null then 'watcher (scheduled)' else p_actor_email end;
    v_src      public.cb_kb_sources;
    v_prev     public.cb_kb_source_snapshots;
    v_snap     uuid;
    v_changed  boolean;
    v_status   text;
begin
    select * into v_src from public.cb_kb_sources where id = p_source_id for update;
    if v_src.id is null or not v_src.active then
        raise exception 'no such source is being watched' using errcode = 'KB003';
    end if;

    if not coalesce(p_ok, false) then
        insert into public.cb_kb_source_snapshots (source_id, fetch_ok, http_status, error)
        values (p_source_id, false, p_http_status, left(coalesce(nullif(btrim(p_error), ''), 'fetch failed'), 1000))
        returning id into v_snap;
        update public.cb_kb_sources
           set last_checked_at = now(), last_status = 'failed',
               last_error = left(coalesce(nullif(btrim(p_error), ''), 'fetch failed'), 1000)
         where id = p_source_id;
        insert into public.cb_kb_audit (action, actor_id, actor_email, new_values, reason)
        values ('source_fetch_failed', p_actor_id, v_email,
                jsonb_build_object('source_id', p_source_id, 'snapshot_id', v_snap, 'http_status', p_http_status,
                                   'by', v_who),
                left(coalesce(nullif(btrim(p_error), ''), 'fetch failed'), 500));
        return jsonb_build_object('snapshot_id', v_snap, 'changed', false, 'first', false, 'ok', false);
    end if;

    if coalesce(p_content_hash, '') !~ '^[0-9a-f]{64}$' or p_text is null or length(p_text) > 500000 then
        raise exception 'invalid snapshot' using errcode = 'KB002';
    end if;
    select * into v_prev from public.cb_kb_source_snapshots
     where source_id = p_source_id and fetch_ok order by fetched_at desc, id limit 1;
    v_changed := v_prev.id is null or v_prev.content_hash <> p_content_hash;
    v_status := case when v_prev.id is null then 'first' when v_changed then 'changed' else 'unchanged' end;

    insert into public.cb_kb_source_snapshots (source_id, fetch_ok, http_status, content_hash, title,
                                               extracted_text, changed)
    values (p_source_id, true, p_http_status, p_content_hash, left(p_title, 500), p_text, v_changed)
    returning id into v_snap;
    update public.cb_kb_sources
       set last_checked_at = now(), last_status = v_status, last_error = null, last_snapshot_id = v_snap
     where id = p_source_id;
    insert into public.cb_kb_audit (action, actor_id, actor_email, new_values, reason)
    values ('source_fetched', p_actor_id, v_email,
            jsonb_build_object('source_id', p_source_id, 'snapshot_id', v_snap, 'status', v_status,
                               'content_hash', p_content_hash, 'by', v_who),
            case when p_actor_id is null then 'scheduled check' else 'fetch now' end);
    return jsonb_build_object('snapshot_id', v_snap, 'changed', v_changed, 'first', v_prev.id is null,
                              'ok', true, 'previous_snapshot_id', v_prev.id);
end;
$$;

-- 4.4 Propose a KB change from a snapshot. Never touches the knowledge base.
create or replace function public.kb_admin_watch_propose(
    p_actor_id uuid, p_actor_email text, p_snapshot_id uuid, p_target_entry_id uuid,
    p_question text, p_answer text, p_section_heading text, p_service text, p_audience text,
    p_nationality text, p_source_passage text, p_old_source_text text, p_model text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_who     text := public.kb_admin__watch_actor(p_actor_id);
    v_email   text := case when p_actor_id is null then 'watcher (scheduled)' else p_actor_email end;
    v_q       text := btrim(coalesce(p_question, ''));
    v_a       text := btrim(coalesce(p_answer, ''));
    v_h       text := nullif(btrim(coalesce(p_section_heading, '')), '');
    v_passage text := btrim(coalesce(p_source_passage, ''));
    v_snap    public.cb_kb_source_snapshots;
    v_src     public.cb_kb_sources;
    v_t       record;
    v_lock    text;
    v_id      uuid;
    v_service text := coalesce(nullif(btrim(p_service), ''), 'general');
    v_aud     text := coalesce(nullif(btrim(p_audience), ''), 'all');
    v_nat     text := coalesce(nullif(btrim(p_nationality), ''), 'all');
    v_old_q   text;
    v_old_a   text;
    v_figure  boolean;
begin
    select * into v_snap from public.cb_kb_source_snapshots where id = p_snapshot_id;
    if v_snap.id is null or not v_snap.fetch_ok then
        raise exception 'no such snapshot' using errcode = 'KB002';
    end if;
    select * into v_src from public.cb_kb_sources where id = v_snap.source_id;
    if v_q = '' or v_a = '' or length(v_q || E'\n' || v_a) > 1200 then
        raise exception 'a question and an answer of at most 1200 characters together are required' using errcode = 'KB002';
    end if;
    perform public.kb_admin__refuse_nric(v_q, v_a, coalesce(v_h, ''), v_passage);
    -- The passage must be the page's own words (whitespace aside).
    if v_passage = '' or strpos(regexp_replace(v_snap.extracted_text, '\s+', ' ', 'g'),
                                regexp_replace(v_passage, '\s+', ' ', 'g')) = 0 then
        raise exception 'the source passage is not in the fetched page' using errcode = 'KB002';
    end if;

    if p_target_entry_id is not null then
        select k.question, k.answer, k.section_heading, k.service_type, k.contact_type, k.nationality, k.chunk_type
          into v_t from public.cb_knowledge_base_updated k where k.id = p_target_entry_id;
        if not found or v_t.chunk_type <> 'qa_pair' then
            raise exception 'the target is not a Q&A entry' using errcode = 'KB002';
        end if;
        v_service := v_t.service_type;
        v_aud := v_t.contact_type;
        v_nat := v_t.nationality;
        v_h := coalesce(v_h, v_t.section_heading);
        v_old_q := v_t.question;
        v_old_a := v_t.answer;
    end if;
    v_lock := public.kb_admin__watch_fee_lock(v_old_q, v_old_a, v_q, v_a, v_src.category);
    v_figure := v_lock is null and public.kb_admin__watch_money(v_old_q, v_old_a, v_q, v_a);

    -- An older open draft for the same entry from the same page is superseded.
    if p_target_entry_id is not null then
        update public.cb_kb_drafts
           set status = 'superseded', blocked_reason = null
         where target_entry_id = p_target_entry_id and source_id = v_snap.source_id
           and status in ('pending', 'blocked');
    end if;

    insert into public.cb_kb_drafts (
        source_id, snapshot_id, source_url, source_fetched_at, kind, target_entry_id,
        old_question, old_answer, proposed_question, proposed_answer, section_heading,
        service, audience, nationality, source_passage, old_source_text, new_source_text,
        model_name, status, blocked_reason, figure_check, created_by, created_by_email)
    values (
        v_snap.source_id, v_snap.id, v_src.url, v_snap.fetched_at,
        case when p_target_entry_id is null then 'new' else 'edit' end, p_target_entry_id,
        v_old_q, v_old_a,
        v_q, v_a, v_h, v_service, v_aud, v_nat, v_passage, left(p_old_source_text, 20000),
        left(v_passage, 20000), left(p_model, 200),
        case when v_lock is null then 'pending' else 'blocked' end, v_lock, v_figure, p_actor_id, v_email)
    returning id into v_id;

    insert into public.cb_kb_audit (entry_id, action, actor_id, actor_email, new_values, reason)
    values (p_target_entry_id, 'watch_proposed', p_actor_id, v_email,
            jsonb_build_object('draft_id', v_id, 'source_id', v_snap.source_id, 'snapshot_id', v_snap.id,
                               'question', v_q, 'answer', v_a, 'blocked_reason', v_lock,
                               'figure_check', v_figure, 'by', v_who),
            case when v_lock is null then 'drafted from a source page change'
                 else 'drafted from a source page change; needs agency confirmation' end);
    return jsonb_build_object('draft_id', v_id, 'status', case when v_lock is null then 'pending' else 'blocked' end,
                              'blocked_reason', v_lock, 'figure_check', v_figure);
end;
$$;

-- 4.5 Edit a draft's proposed text before approval. Re-checks the fee lock.
create or replace function public.kb_admin_watch_edit(
    p_actor_id uuid, p_actor_email text, p_draft_id uuid, p_question text, p_answer text,
    p_section_heading text, p_reason text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_q      text := btrim(coalesce(p_question, ''));
    v_a      text := btrim(coalesce(p_answer, ''));
    v_h      text := nullif(btrim(coalesce(p_section_heading, '')), '');
    v_reason text := btrim(coalesce(p_reason, ''));
    v_d      public.cb_kb_drafts;
    v_tq     text;
    v_ta     text;
    v_lock   text;
    v_cat    text;
    v_figure boolean;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    select * into v_d from public.cb_kb_drafts where id = p_draft_id for update;
    if v_d.id is null or v_d.status not in ('pending', 'blocked') then
        raise exception 'this draft is no longer open' using errcode = 'KB003';
    end if;
    if v_q = '' or v_a = '' or length(v_q || E'\n' || v_a) > 1200 then
        raise exception 'a question and an answer of at most 1200 characters together are required' using errcode = 'KB002';
    end if;
    perform public.kb_admin__refuse_nric(v_q, v_a, coalesce(v_h, ''));
    if v_d.target_entry_id is not null then
        select k.question, k.answer into v_tq, v_ta from public.cb_knowledge_base_updated k where k.id = v_d.target_entry_id;
    end if;
    select s.category into v_cat from public.cb_kb_sources s where s.id = v_d.source_id;
    v_lock := public.kb_admin__watch_fee_lock(v_tq, v_ta, v_q, v_a, v_cat);
    v_figure := v_lock is null and public.kb_admin__watch_money(v_tq, v_ta, v_q, v_a);
    update public.cb_kb_drafts
       set proposed_question = v_q, proposed_answer = v_a, section_heading = coalesce(v_h, section_heading),
           status = case when v_lock is null then 'pending' else 'blocked' end, blocked_reason = v_lock,
           figure_check = v_figure,
           edited_at = now(), edited_by = p_actor_id, edited_by_email = p_actor_email
     where id = p_draft_id;
    insert into public.cb_kb_audit (entry_id, action, actor_id, actor_email, old_values, new_values, reason)
    values (v_d.target_entry_id, 'watch_edited', p_actor_id, p_actor_email,
            jsonb_build_object('draft_id', v_d.id, 'question', v_d.proposed_question, 'answer', v_d.proposed_answer),
            jsonb_build_object('draft_id', v_d.id, 'question', v_q, 'answer', v_a, 'blocked_reason', v_lock,
                               'figure_check', v_figure),
            v_reason);
    return jsonb_build_object('status', case when v_lock is null then 'pending' else 'blocked' end,
                              'blocked_reason', v_lock, 'figure_check', v_figure);
end;
$$;

-- 4.6 Reject a draft (pending or blocked).
create or replace function public.kb_admin_watch_reject(
    p_actor_id uuid, p_actor_email text, p_draft_id uuid, p_reason text)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason text := btrim(coalesce(p_reason, ''));
    v_d      public.cb_kb_drafts;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    select * into v_d from public.cb_kb_drafts where id = p_draft_id for update;
    if v_d.id is null or v_d.status not in ('pending', 'blocked') then
        raise exception 'this draft is no longer open' using errcode = 'KB003';
    end if;
    update public.cb_kb_drafts
       set status = 'rejected', blocked_reason = null, reviewed_at = now(), reviewed_by = p_actor_id,
           reviewed_by_email = p_actor_email, review_reason = v_reason
     where id = p_draft_id;
    insert into public.cb_kb_audit (entry_id, action, actor_id, actor_email, old_values, reason)
    values (v_d.target_entry_id, 'watch_rejected', p_actor_id, p_actor_email,
            jsonb_build_object('draft_id', v_d.id, 'status', v_d.status, 'blocked_reason', v_d.blocked_reason),
            v_reason);
end;
$$;

-- 4.7 Approve a draft: an APPROVER only, never a blocked one, and a draft that
-- changes a (government) money figure only with p_confirm_figure - the
-- approver ticked that they checked it against the source (KB008 otherwise).
-- Writes the text through the existing Q&A path (save_draft / create_entry)
-- and records the provenance; kb-admin then publishes that version with
-- kb_admin_publish (canary-checked embedding) and calls 4.8.
create or replace function public.kb_admin_watch_approve(
    p_actor_id uuid, p_actor_email text, p_draft_id uuid, p_reason text, p_confirm_figure boolean)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason  text := btrim(coalesce(p_reason, ''));
    v_d       public.cb_kb_drafts;
    v_t       record;
    v_lock    text;
    v_version uuid;
    v_entry   uuid;
    v_created jsonb;
    v_note    text;
    v_cat     text;
    v_figure  boolean;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'approver');
    if v_reason = '' then
        raise exception 'a reason is required' using errcode = 'KB002';
    end if;
    select * into v_d from public.cb_kb_drafts where id = p_draft_id for update;
    if v_d.id is null or v_d.status not in ('pending', 'blocked') then
        raise exception 'this draft is no longer open' using errcode = 'KB003';
    end if;

    select s.category into v_cat from public.cb_kb_sources s where s.id = v_d.source_id;
    if v_d.target_entry_id is not null then
        select k.question, k.answer, k.section_heading, k.service_type, k.contact_type, k.nationality
          into v_t from public.cb_knowledge_base_updated k where k.id = v_d.target_entry_id;
        if not found then
            raise exception 'the entry no longer exists' using errcode = 'KB003';
        end if;
        v_lock := coalesce(v_d.blocked_reason,
                           public.kb_admin__watch_fee_lock(v_t.question, v_t.answer, v_d.proposed_question, v_d.proposed_answer, v_cat));
        v_figure := public.kb_admin__watch_money(v_t.question, v_t.answer, v_d.proposed_question, v_d.proposed_answer);
    else
        v_lock := coalesce(v_d.blocked_reason,
                           public.kb_admin__watch_fee_lock(null, null, v_d.proposed_question, v_d.proposed_answer, v_cat));
        v_figure := public.kb_admin__watch_money(null, null, v_d.proposed_question, v_d.proposed_answer);
    end if;
    if v_lock is not null then
        raise exception 'needs agency confirmation: %', v_lock using errcode = 'KB007';
    end if;
    if v_figure and not coalesce(p_confirm_figure, false) then
        raise exception 'this changes a figure: confirm you checked it against the source' using errcode = 'KB008';
    end if;

    v_note := left(v_reason || ' [from ' || v_d.source_url || ', fetched '
                   || to_char(v_d.source_fetched_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC]', 1000);
    if v_d.target_entry_id is not null then
        if v_t.question is distinct from v_d.old_question or v_t.answer is distinct from v_d.old_answer then
            raise exception 'the entry has changed since this draft was made' using errcode = 'KB003';
        end if;
        v_version := public.kb_admin_save_draft(
            p_actor_id, p_actor_email, v_d.target_entry_id, v_d.proposed_question, v_d.proposed_answer,
            coalesce(v_d.section_heading, v_t.section_heading, ''), v_t.service_type, v_t.contact_type,
            v_t.nationality, null, v_note);
        v_entry := v_d.target_entry_id;
    else
        v_created := public.kb_admin_create_entry(
            p_actor_id, p_actor_email, v_d.proposed_question, v_d.proposed_answer,
            coalesce(v_d.section_heading, ''), v_d.service, v_d.audience, v_d.nationality, v_note);
        v_version := (v_created ->> 'version_id')::uuid;
        v_entry := (v_created ->> 'entry_id')::uuid;
    end if;

    update public.cb_kb_drafts
       set status = 'approved', approved_by = p_actor_id, approved_by_email = p_actor_email,
           approved_at = now(), reviewed_at = now(), reviewed_by = p_actor_id,
           reviewed_by_email = p_actor_email, review_reason = v_reason,
           entry_id_after = v_entry, version_id = v_version,
           figure_check = v_figure, figure_confirmed = v_figure
     where id = p_draft_id;
    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email, new_values, reason)
    values (v_entry, v_version, 'watch_approved', p_actor_id, p_actor_email,
            jsonb_build_object('draft_id', v_d.id, 'source_url', v_d.source_url,
                               'source_fetched_at', v_d.source_fetched_at, 'kind', v_d.kind,
                               'source_category', v_cat, 'figure_confirmed', v_figure),
            v_reason);
    return jsonb_build_object('entry_id', v_entry, 'version_id', v_version);
end;
$$;

-- 4.8 Mark an approved draft published, once its version is live.
create or replace function public.kb_admin_watch_mark_published(
    p_actor_id uuid, p_actor_email text, p_draft_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_d      public.cb_kb_drafts;
    v_status text;
begin
    perform public.kb_admin__require_actor(p_actor_id, 'editor');
    select * into v_d from public.cb_kb_drafts where id = p_draft_id for update;
    if v_d.id is null or v_d.status <> 'approved' then
        raise exception 'this draft is not approved and waiting to be published' using errcode = 'KB003';
    end if;
    select status into v_status from public.cb_kb_entry_versions where id = v_d.version_id;
    if v_status is distinct from 'published' then
        raise exception 'its version has not been published' using errcode = 'KB003';
    end if;
    update public.cb_kb_drafts set status = 'published', published_at = now() where id = p_draft_id;
    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email, new_values, reason)
    values (v_d.entry_id_after, v_d.version_id, 'watch_published', p_actor_id, p_actor_email,
            jsonb_build_object('draft_id', v_d.id, 'source_url', v_d.source_url,
                               'source_fetched_at', v_d.source_fetched_at,
                               'approved_by', v_d.approved_by_email, 'approved_at', v_d.approved_at),
            'watcher draft published');
end;
$$;

-- =============================================================================
-- 5. Ownership and EXECUTE (the 004 / 008 pattern)
-- =============================================================================
grant create on schema public to kb_admin_fn_owner;
alter function public.kb_admin__watch_actor(uuid) owner to kb_admin_fn_owner;
alter function public.kb_admin__watch_fee_lock(text, text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin__watch_money(text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_source_add(uuid, text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_source_remove(uuid, text, uuid, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_source_record_fetch(uuid, text, uuid, boolean, integer, text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_watch_edit(uuid, text, uuid, text, text, text, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_watch_reject(uuid, text, uuid, text) owner to kb_admin_fn_owner;
alter function public.kb_admin_watch_approve(uuid, text, uuid, text, boolean) owner to kb_admin_fn_owner;
alter function public.kb_admin_watch_mark_published(uuid, text, uuid) owner to kb_admin_fn_owner;
revoke create on schema public from kb_admin_fn_owner;

revoke all on function public.kb_admin__watch_actor(uuid) from public, anon, authenticated, service_role;
revoke all on function public.kb_admin__watch_fee_lock(text, text, text, text, text) from public, anon, authenticated, service_role;
revoke all on function public.kb_admin__watch_money(text, text, text, text) from public, anon, authenticated, service_role;
revoke all on function
    public.kb_admin_source_add(uuid, text, text, text, text),
    public.kb_admin_source_remove(uuid, text, uuid, text),
    public.kb_admin_source_record_fetch(uuid, text, uuid, boolean, integer, text, text, text, text),
    public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text),
    public.kb_admin_watch_edit(uuid, text, uuid, text, text, text, text),
    public.kb_admin_watch_reject(uuid, text, uuid, text),
    public.kb_admin_watch_approve(uuid, text, uuid, text, boolean),
    public.kb_admin_watch_mark_published(uuid, text, uuid)
    from public, anon, authenticated, service_role;

grant execute on function
    public.kb_admin_source_add(uuid, text, text, text, text),
    public.kb_admin_source_remove(uuid, text, uuid, text),
    public.kb_admin_source_record_fetch(uuid, text, uuid, boolean, integer, text, text, text, text),
    public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text),
    public.kb_admin_watch_edit(uuid, text, uuid, text, text, text, text),
    public.kb_admin_watch_reject(uuid, text, uuid, text),
    public.kb_admin_watch_approve(uuid, text, uuid, text, boolean),
    public.kb_admin_watch_mark_published(uuid, text, uuid)
    to kb_admin_editor;

-- =============================================================================
-- 6. Checks (abort the whole file if any fails)
-- =============================================================================
do $$
declare
    t text;
    r text;
    f text;
    n int;
begin
    foreach t in array array['cb_kb_sources', 'cb_kb_source_snapshots', 'cb_kb_drafts'] loop
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
        if has_table_privilege('kb_admin_editor', 'public.' || t, 'SELECT, INSERT, UPDATE, DELETE')
           or has_any_column_privilege('kb_admin_editor', 'public.' || t, 'SELECT, INSERT, UPDATE') then
            raise exception 'FAIL: kb_admin_editor holds a privilege on %', t;
        end if;
        if has_table_privilege('kb_admin_reader', 'public.' || t, 'INSERT, UPDATE, DELETE') then
            raise exception 'FAIL: kb_admin_reader can write %', t;
        end if;
        if has_table_privilege('kb_admin_fn_owner', 'public.' || t, 'DELETE') then
            raise exception 'FAIL: kb_admin_fn_owner can DELETE from %', t;
        end if;
    end loop;
    if has_table_privilege('kb_admin_fn_owner', 'public.cb_kb_rules', 'SELECT, INSERT, UPDATE, DELETE') then
        raise exception 'FAIL: kb_admin_fn_owner holds a privilege on cb_kb_rules';
    end if;

    foreach f in array array[
        'public.kb_admin_source_add(uuid, text, text, text, text)',
        'public.kb_admin_source_remove(uuid, text, uuid, text)',
        'public.kb_admin_source_record_fetch(uuid, text, uuid, boolean, integer, text, text, text, text)',
        'public.kb_admin_watch_propose(uuid, text, uuid, uuid, text, text, text, text, text, text, text, text, text)',
        'public.kb_admin_watch_edit(uuid, text, uuid, text, text, text, text)',
        'public.kb_admin_watch_reject(uuid, text, uuid, text)',
        'public.kb_admin_watch_approve(uuid, text, uuid, text, boolean)',
        'public.kb_admin_watch_mark_published(uuid, text, uuid)'] loop
        if not exists (select 1 from pg_proc p where p.oid = f::regprocedure and p.prosecdef
                         and pg_get_userbyid(p.proowner) = 'kb_admin_fn_owner'
                         and exists (select 1 from unnest(p.proconfig) x where x like 'search_path=%pg_temp%')) then
            raise exception 'FAIL: % is not SECURITY DEFINER / owned by kb_admin_fn_owner / pinned', f;
        end if;
        if not has_function_privilege('kb_admin_editor', f, 'EXECUTE') then
            raise exception 'FAIL: kb_admin_editor cannot EXECUTE %', f;
        end if;
        foreach r in array array['anon', 'authenticated', 'service_role', 'public'] loop
            if has_function_privilege(r, f, 'EXECUTE') then
                raise exception 'FAIL: % can EXECUTE %', r, f;
            end if;
        end loop;
    end loop;
    if has_function_privilege('kb_admin_editor', 'public.kb_admin__watch_fee_lock(text, text, text, text, text)', 'EXECUTE') then
        raise exception 'FAIL: kb_admin_editor can call the internal fee-lock helper';
    end if;
    if has_function_privilege('kb_admin_editor', 'public.kb_admin__watch_money(text, text, text, text)', 'EXECUTE') then
        raise exception 'FAIL: kb_admin_editor can call the internal money helper';
    end if;

    select count(*) into n from pg_proc p join pg_namespace s on s.oid = p.pronamespace
     where s.nspname = 'public' and has_function_privilege('kb_admin_editor', p.oid, 'EXECUTE')
       and p.proname like 'kb\_admin\_%' and p.proname not like 'kb\_admin\_\_%';
    if n <> 24 then
        raise exception 'FAIL: kb_admin_editor executes % kb_admin_* functions, expected 24 (16 + 8)', n;
    end if;

    if not (select pg_get_constraintdef(oid) like '%watch_published%' and pg_get_constraintdef(oid) like '%chunk_edited%'
              from pg_constraint where conrelid = 'public.cb_kb_audit'::regclass and conname = 'cb_kb_audit_action_check') then
        raise exception 'FAIL: cb_kb_audit_action_check does not allow the watcher actions';
    end if;

    -- The fee lock, exercised on fixed text.
    if public.kb_admin__watch_fee_lock(null, null, 'How long does a transfer take?', 'About 1 to 2 weeks from the interview.', 'process') is not null then
        raise exception 'FAIL: the fee lock blocks a plain timing change';
    end if;
    if public.kb_admin__watch_fee_lock(null, null, 'Cost?', 'The agency fee is S$1,428.', 'government_rule') is null
       or public.kb_admin__watch_fee_lock(null, null, 'Cost?', 'It costs 450 dollars.', 'process') is null
       or public.kb_admin__watch_fee_lock(null, null, 'Cost?', 'A replacement fee applies.', 'government_rule') is null
       or public.kb_admin__watch_fee_lock(null, null, 'Salary?', 'The minimum salary is S$700.', 'government_rule') is null
       or public.kb_admin__watch_fee_lock('How much does a replacement cost?', 'x', 'y', 'z', 'government_rule') is null then
        raise exception 'FAIL: the fee lock lets an agency fee change through';
    end if;
    if public.kb_admin__watch_fee_lock(null, null, 'Levy?', 'The monthly levy is $300; the security bond is $5,000.', 'government_rule') is not null
       or public.kb_admin__watch_fee_lock(null, null, 'MOM fee?', 'The MOM processing fee is $35.', 'government_rule') is not null
       or not public.kb_admin__watch_money(null, null, 'Levy?', 'The monthly levy is $300.') then
        raise exception 'FAIL: a government figure is not approvable with the confirmation';
    end if;
    raise notice 'PASS: 015 - three tables, RLS on, no portal/public access, 8 functions granted (24 in all), audit widened, fee lock works (government figures need the tick)';
end
$$;

commit;
