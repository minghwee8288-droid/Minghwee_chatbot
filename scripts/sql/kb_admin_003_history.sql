-- kb_admin_003_history: versions and an append-only audit log for the Q&A editor.
--
-- cb_kb_entry_versions - every state an edited entry has been in, plus drafts.
--   The chatbot NEVER reads this table: it searches cb_knowledge_base_updated
--   only (cb_match_knowledge_base_updated, active rows). A draft here is
--   therefore invisible to clients until kb_admin_publish copies it across.
--   Lazy backfill: the first edit of an entry saves the live row as version 1
--   (status 'published'); nothing is backfilled by this file.
--
-- cb_kb_audit - who changed what, when and why. Append-only: a trigger refuses
--   UPDATE, DELETE and TRUNCATE for every role. entry_id has NO foreign key on
--   purpose, so a row can record a DELETE of the entry it names.
--
-- Column names follow the editor's vocabulary (service / audience / active);
-- they map to cb_knowledge_base_updated's service_type / contact_type /
-- is_active.
--
-- ISOLATION. In this database, default privileges hand anon, authenticated and
-- service_role every privilege on a new table the moment it is created
-- (read 2026-09-30). Postgres has no DENY, so "blocked" here means: every
-- privilege REVOKED from those roles and PUBLIC, RLS ENABLED, and no policy
-- for them. service_role bypasses RLS, so for it the revoke is the block.
-- The only access left is kb_admin_reader (SELECT, below) and the function
-- owner kb_admin_fn_owner (granted in kb_admin_004_functions.sql).
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

-- --- Versions -----------------------------------------------------------------
create table if not exists public.cb_kb_entry_versions (
    id               uuid        not null default gen_random_uuid(),
    entry_id         uuid        not null,
    version_number   integer     not null,
    question         text,
    answer           text,
    section_heading  text,
    service          varchar(60) not null,
    audience         varchar(20) not null,
    nationality      varchar(5)  not null,
    active           boolean     not null,
    -- The text the embedding below was computed on (question, newline, answer
    -- for anything written by the editor). NULL for a baseline of one of the
    -- 79 FAQ rows that were imported with no content; such a version's vector
    -- is never reused by a restore (kb_admin_publish checks).
    content          text,
    embedding        vector(1536),
    model_name       text,
    status           text        not null,
    -- The live version_number this draft was started from. Publishing refuses
    -- if the live version has moved on since (optimistic locking).
    based_on_version integer,
    -- NULL only on a lazy baseline (version 1 copied from the live row).
    created_by       uuid,
    created_by_email text,
    reason           text        not null,
    created_at       timestamptz not null default now(),

    constraint cb_kb_entry_versions_pkey primary key (id),
    -- NO ACTION: an entry that has history cannot be deleted out from under
    -- it. Nothing in the repo deletes knowledge-base rows (checked
    -- 2026-09-30); retiring is is_active = false.
    constraint cb_kb_entry_versions_entry_fkey
        foreign key (entry_id) references public.cb_knowledge_base_updated (id),
    constraint cb_kb_entry_versions_number_key unique (entry_id, version_number),
    constraint cb_kb_entry_versions_number_check check (version_number > 0),
    constraint cb_kb_entry_versions_status_check
        check (status in ('draft', 'published', 'superseded', 'discarded')),
    constraint cb_kb_entry_versions_reason_check check (length(btrim(reason)) > 0),
    constraint cb_kb_entry_versions_model_check
        check (embedding is null or length(btrim(coalesce(model_name, ''))) > 0),
    -- The same vocabularies the live table enforces.
    constraint cb_kb_entry_versions_audience_check
        check (audience in ('employer', 'candidate', 'supplier', 'partner', 'all')),
    constraint cb_kb_entry_versions_nationality_check
        check (nationality in ('PH', 'ID', 'MM', 'all'))
);

-- At most one open draft and exactly-at-most one live version per entry.
create unique index if not exists cb_kb_entry_versions_one_draft
    on public.cb_kb_entry_versions (entry_id) where status = 'draft';
create unique index if not exists cb_kb_entry_versions_one_published
    on public.cb_kb_entry_versions (entry_id) where status = 'published';
create index if not exists cb_kb_entry_versions_created_idx
    on public.cb_kb_entry_versions (created_at desc);

comment on table public.cb_kb_entry_versions is
    'kb-admin Q&A editor: drafts and every published state of an edited entry. '
    'Not read by the chatbot. Written only by the kb_admin_* functions.';

-- --- Audit --------------------------------------------------------------------
create table if not exists public.cb_kb_audit (
    id             bigint      generated always as identity,
    entry_id       uuid,
    version_id     uuid,
    action         text        not null,
    -- The person (kb-admin actions). NULL for an out-of-band write.
    actor_id       uuid,
    actor_email    text,
    -- The database role that made the write: the login for editor actions,
    -- 'service_role' / 'postgres' / ... for out-of-band writes.
    actor_db_role  text        not null default session_user,
    old_values     jsonb,
    new_values     jsonb,
    reason         text        not null,
    self_approved  boolean     not null default false,
    created_at     timestamptz not null default now(),

    constraint cb_kb_audit_pkey primary key (id),
    constraint cb_kb_audit_version_fkey
        foreign key (version_id) references public.cb_kb_entry_versions (id),
    -- The six editor actions, plus the three the safety-net trigger on the
    -- live table writes (kb_admin_006_guards.sql).
    constraint cb_kb_audit_action_check check (action in (
        'draft_saved', 'published', 'approved', 'restored', 'toggled', 'discarded',
        'external_insert', 'external_update', 'external_delete'
    )),
    constraint cb_kb_audit_reason_check check (length(btrim(reason)) > 0),
    constraint cb_kb_audit_self_approved_check
        check (not self_approved or action = 'approved')
);

create index if not exists cb_kb_audit_entry_idx
    on public.cb_kb_audit (entry_id, created_at desc);
create index if not exists cb_kb_audit_created_idx
    on public.cb_kb_audit (created_at desc);

comment on table public.cb_kb_audit is
    'kb-admin: append-only log of every knowledge-base write, editor or '
    'out-of-band. UPDATE, DELETE and TRUNCATE are refused by trigger.';

-- --- Append-only --------------------------------------------------------------
create or replace function public.cb_kb_audit_refuse_change()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
    raise exception 'cb_kb_audit is append-only: % is not allowed', tg_op
        using errcode = 'insufficient_privilege';
end;
$$;

revoke all on function public.cb_kb_audit_refuse_change() from public, anon, authenticated, service_role;

create or replace trigger cb_kb_audit_append_only
    before update or delete on public.cb_kb_audit
    for each row execute function public.cb_kb_audit_refuse_change();

create or replace trigger cb_kb_audit_no_truncate
    before truncate on public.cb_kb_audit
    for each statement execute function public.cb_kb_audit_refuse_change();

-- --- Isolation ----------------------------------------------------------------
alter table public.cb_kb_entry_versions enable row level security;
alter table public.cb_kb_audit enable row level security;

revoke all on public.cb_kb_entry_versions from public, anon, authenticated, service_role;
revoke all on public.cb_kb_audit from public, anon, authenticated, service_role;
revoke all on sequence public.cb_kb_audit_id_seq from public, anon, authenticated, service_role;

-- --- kb-admin may read the history --------------------------------------------
-- Column-level on versions: everything except the embedding, the same line
-- Phase 1 drew on the live table.
grant select (
    id, entry_id, version_number, question, answer, section_heading, service,
    audience, nationality, active, content, model_name, status, based_on_version,
    created_by, created_by_email, reason, created_at
) on public.cb_kb_entry_versions to kb_admin_reader;
grant select on public.cb_kb_audit to kb_admin_reader;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_entry_versions' and policyname = 'kb_admin_reader_select') then
        create policy kb_admin_reader_select on public.cb_kb_entry_versions
            as permissive for select to kb_admin_reader using (true);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_audit' and policyname = 'kb_admin_reader_select') then
        create policy kb_admin_reader_select on public.cb_kb_audit
            as permissive for select to kb_admin_reader using (true);
    end if;
end
$$;

commit;
