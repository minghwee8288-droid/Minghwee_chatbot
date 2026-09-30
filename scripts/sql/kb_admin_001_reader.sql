-- kb_admin_001_reader: the Phase 1 read-only setup, recorded.
--
-- kb-admin Phase 1 was set up by hand and nothing in the repo described it.
-- This file is that description, written so that running it against the
-- database it was read from CHANGES NOTHING: every statement is either
-- guarded (IF NOT EXISTS / a catalog check) or re-states a grant, revoke or
-- setting that is already in force. Its job is to be the base the Phase 2
-- files (002-007) build on, and to recreate Phase 1 on a fresh test copy.
--
-- Read from production (qizcnyuzgylzoyfvymfo) on 2026-09-30, as
-- kb_admin_reader, inside a READ ONLY transaction:
--   * role kb_admin_reader: LOGIN, NOINHERIT, no BYPASSRLS, CONNECTION LIMIT 5,
--     default_transaction_read_only=on, statement_timeout=5s,
--     lock_timeout=2s, idle_in_transaction_session_timeout=15s; no memberships.
--   * column SELECT on 15 columns of cb_knowledge_base_updated (NOT embedding,
--     page_or_section, frequency, rag_score_floor).
--   * table SELECT on cb_kb_rules and cb_kb_admin_users.
--   * one permissive SELECT policy `kb_admin_reader_select` USING (true) on
--     each of those three tables.
--   * cb_kb_admin_users: user_id uuid PK -> auth.users(id) ON DELETE CASCADE,
--     role text CHECK (role = 'viewer'), active boolean default true,
--     created_at timestamptz default now(); RLS on; anon and authenticated
--     hold NO privileges on it (service_role and postgres keep theirs).
--
-- NO PASSWORD is set here. If the role does not exist (a fresh copy) it is
-- created with no password, which cannot log in; set one by hand with
--   ALTER ROLE kb_admin_reader PASSWORD '...';
-- and never commit it.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

-- --- The role -----------------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'kb_admin_reader') then
        create role kb_admin_reader login noinherit nobypassrls connection limit 5;
    end if;
end
$$;

-- Re-stating the attributes and settings. Each is a no-op when already so.
alter role kb_admin_reader login noinherit nobypassrls nocreatedb nocreaterole
    noreplication connection limit 5;
alter role kb_admin_reader set default_transaction_read_only = on;
alter role kb_admin_reader set statement_timeout = '5s';
alter role kb_admin_reader set lock_timeout = '2s';
alter role kb_admin_reader set idle_in_transaction_session_timeout = '15s';

grant usage on schema public to kb_admin_reader;

-- --- The access list ----------------------------------------------------------
create table if not exists public.cb_kb_admin_users (
    user_id    uuid        not null,
    role       text        not null,
    active     boolean     not null default true,
    created_at timestamptz not null default now(),
    constraint cb_kb_admin_users_pkey primary key (user_id),
    constraint cb_kb_admin_users_user_id_fkey
        foreign key (user_id) references auth.users (id) on delete cascade
);

-- The Phase 1 check. Added only if NO role check exists at all: once
-- kb_admin_002_roles.sql has widened it, re-running this file must not put the
-- narrow one back (it would fail on any editor/approver row anyway).
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.cb_kb_admin_users'::regclass
           and conname = 'cb_kb_admin_users_role_check'
    ) then
        alter table public.cb_kb_admin_users
            add constraint cb_kb_admin_users_role_check check (role = 'viewer');
    end if;
end
$$;

alter table public.cb_kb_admin_users enable row level security;
-- The portal's keys must never see who can administer the knowledge base.
revoke all on public.cb_kb_admin_users from anon, authenticated;

-- --- Grants -------------------------------------------------------------------
-- Column-level on the knowledge base: the 15 columns kb-admin reads, and
-- deliberately NOT embedding, page_or_section, frequency or rag_score_floor.
grant select (
    id, namespace, service_type, contact_type, nationality, chunk_type,
    question, answer, content, source_document, section_heading, metadata,
    is_active, created_at, updated_at
) on public.cb_knowledge_base_updated to kb_admin_reader;

grant select on public.cb_kb_rules to kb_admin_reader;
grant select on public.cb_kb_admin_users to kb_admin_reader;

-- --- RLS policies -------------------------------------------------------------
-- All three tables have RLS on and no other policy for this role, so without
-- these the grants above would return zero rows.
do $$
declare
    t text;
begin
    foreach t in array array['cb_knowledge_base_updated', 'cb_kb_rules', 'cb_kb_admin_users'] loop
        if not exists (
            select 1 from pg_policies
             where schemaname = 'public' and tablename = t
               and policyname = 'kb_admin_reader_select'
        ) then
            execute format(
                'create policy kb_admin_reader_select on public.%I '
                'as permissive for select to kb_admin_reader using (true)', t);
        end if;
    end loop;
end
$$;

commit;
