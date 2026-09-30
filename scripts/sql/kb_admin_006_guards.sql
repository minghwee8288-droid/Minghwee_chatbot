-- kb_admin_006_guards: three guards around the live knowledge base.
--
-- 1. A CHECK that every stored vector has 1536 dimensions.
--    Pre-check run 2026-09-30 against production, read-only:
--      SELECT count(*) FROM cb_knowledge_base_updated
--       WHERE embedding IS NOT NULL AND vector_dims(embedding) != 1536;
--      -> 0   (and 0 rows with a NULL embedding; 404 rows in all)
--    NOTE: the column is ALREADY declared vector(1536) (read from pg_attribute
--    the same day), which rejects any other length by itself. The comment in
--    scripts/cb_match_knowledge_base_updated.sql calling it a "bare vector"
--    is out of date. This CHECK is therefore a second lock, kept so the rule
--    survives if the column is ever retyped to a bare vector. One wrong-length
--    vector does not just miss: it makes EVERY search error with "different
--    vector dimensions". Added NOT VALID then VALIDATEd, so the table is not
--    held under an exclusive lock while the 404 rows are checked.
--
-- 2. cb_kb_canary: one fixed sentence, its vector and the model name, made
--    ONCE by the bot's own embedding function (app/services/rag.embed_query).
--    kb-admin embeds the same sentence with its own key and refuses to publish
--    unless its vector matches - proof that kb-admin embeds with the same
--    model and settings as the bot. kb_admin_publish also refuses any new
--    vector whose model name differs from the canary's, and refuses every new
--    vector until the canary row exists. THIS FILE DOES NOT SEED IT: seeding
--    needs the bot's embedding key and belongs in a reviewed script.
--
-- 3. A safety-net audit trigger ON cb_knowledge_base_updated: every INSERT,
--    UPDATE and DELETE that does NOT come through the kb_admin_* functions
--    (the loader, hand-run SQL, anything else) is logged to cb_kb_audit as
--    external_insert / external_update / external_delete, with the database
--    role that made it and the old and new values (never the vector). It
--    never changes the row itself; it only adds audit rows. Writes through the
--    editor functions are skipped here because those functions log them with
--    the PERSON, not just the role.
--    The actor recorded: for a PostgREST request (the loader uses the
--    service-role key over REST) the role in the request's JWT, e.g.
--    'service_role'; otherwise session_user, e.g. 'postgres'. current_user is
--    NOT used - inside a SECURITY DEFINER trigger it is always the owner.
--    The skip is a transaction-local setting (kb_admin.via_function) that the
--    editor functions set around their own UPDATE. Someone with SQL access
--    could set it by hand to hide a write; the same person could drop the
--    trigger, so this is a net for mistakes, not a defence against the DBA.
--
-- The bot only READS this table, so nothing here can change a reply.
--
-- Run with:  python scripts/apply_sql.py --expect-ref <project ref> <this file>

begin;

-- =============================================================================
-- 1. Vector dimension
-- =============================================================================
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.cb_knowledge_base_updated'::regclass
           and conname = 'cb_knowledge_base_updated_embedding_dims_check'
    ) then
        alter table public.cb_knowledge_base_updated
            add constraint cb_knowledge_base_updated_embedding_dims_check
            check (embedding is null or vector_dims(embedding) = 1536) not valid;
    end if;
end
$$;

-- Validating an already-valid constraint is a no-op.
alter table public.cb_knowledge_base_updated
    validate constraint cb_knowledge_base_updated_embedding_dims_check;

-- =============================================================================
-- 2. The canary
-- =============================================================================
create table if not exists public.cb_kb_canary (
    id         smallint     not null default 1,
    sentence   text         not null,
    embedding  vector(1536) not null,
    model      text         not null,
    created_at timestamptz  not null default now(),
    constraint cb_kb_canary_pkey primary key (id),
    constraint cb_kb_canary_one_row check (id = 1),
    constraint cb_kb_canary_sentence_check check (length(btrim(sentence)) > 0),
    constraint cb_kb_canary_model_check check (length(btrim(model)) > 0)
);

comment on table public.cb_kb_canary is
    'kb-admin: the fixed sentence and the vector the BOT''s embedding produces '
    'for it. kb-admin must reproduce it before it may store a vector.';

alter table public.cb_kb_canary enable row level security;
revoke all on public.cb_kb_canary from public, anon, authenticated, service_role;

-- kb-admin reads the whole row (the vector is not sensitive: it is the
-- embedding of a fixed sentence). The function owner needs only the model.
grant select on public.cb_kb_canary to kb_admin_reader;
grant select (id, model) on public.cb_kb_canary to kb_admin_fn_owner;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_canary' and policyname = 'kb_admin_reader_select') then
        create policy kb_admin_reader_select on public.cb_kb_canary
            as permissive for select to kb_admin_reader using (true);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                    and tablename = 'cb_kb_canary' and policyname = 'kb_admin_fn_owner_select') then
        create policy kb_admin_fn_owner_select on public.cb_kb_canary
            as permissive for select to kb_admin_fn_owner using (true);
    end if;
end
$$;

-- =============================================================================
-- 3. Safety-net audit trigger on the live table
-- =============================================================================
create or replace function public.kb_admin__log_external_write()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_claims text := nullif(current_setting('request.jwt.claims', true), '');
    v_role   text;
    v_old    jsonb;
    v_new    jsonb;
begin
    if coalesce(current_setting('kb_admin.via_function', true), '') = 'on' then
        return null;
    end if;

    v_role := coalesce(
        nullif(current_setting('request.jwt.claim.role', true), ''),
        case when v_claims is not null then v_claims::jsonb ->> 'role' end,
        session_user::text);

    if tg_op in ('UPDATE', 'DELETE') then
        v_old := to_jsonb(old) - 'embedding';
    end if;
    if tg_op in ('INSERT', 'UPDATE') then
        v_new := to_jsonb(new) - 'embedding';
    end if;
    if tg_op = 'UPDATE' then
        v_new := v_new || jsonb_build_object(
            'embedding_changed', old.embedding is distinct from new.embedding);
    end if;

    insert into public.cb_kb_audit (entry_id, version_id, action, actor_id, actor_email,
                                    actor_db_role, old_values, new_values, reason)
    values (
        case when tg_op = 'DELETE' then old.id else new.id end,
        null,
        'external_' || lower(tg_op),
        null, null,
        v_role,
        v_old, v_new,
        format('out-of-band %s by %s (application_name=%s)', lower(tg_op), v_role,
               coalesce(nullif(current_setting('application_name', true), ''), 'unset')));
    return null;
end;
$$;

-- ALTER ... OWNER TO requires the new owner to hold CREATE on the schema.
-- Held only for the hand-over, then taken back in the same transaction.
grant create on schema public to kb_admin_fn_owner;
alter function public.kb_admin__log_external_write() owner to kb_admin_fn_owner;
revoke create on schema public from kb_admin_fn_owner;
revoke all on function public.kb_admin__log_external_write() from public, anon, authenticated, service_role;

create or replace trigger cb_kb_updated_audit_external
    after insert or update or delete on public.cb_knowledge_base_updated
    for each row execute function public.kb_admin__log_external_write();

commit;
