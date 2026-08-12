-- ============================================================================
-- cb_match_knowledge_base_updated — filtered vector search over the rebuilt
-- knowledge base.
--
-- The new schema ships the table but no match function, so this is it. It
-- mirrors the old cb_match_knowledge_base contract (query_embedding,
-- match_threshold, match_count, filter_namespace + a similarity column on
-- every row) with three differences:
--
--   * filter_category is gone. The old table's routing signal has no
--     equivalent here and is deliberately not carried over.
--   * service_type / contact_type / nationality replace it, each INCLUSIVE of
--     its catch-all bucket ('general' / 'all' / 'all') so a chunk written for
--     everyone still answers an employer's question.
--   * rag_score_floor, the per-row similarity override, is enforced here. A
--     figure-bearing row clears a higher bar than the caller's threshold
--     because a vague match on a fee table is actively dangerous.
--
-- style_example rows are excluded outright: they are tone and formatting
-- guidance, not factual sources, and must never enter the evidence context.
--
-- Apply with:  psql "$SUPABASE_DB_URL" -f scripts/cb_match_knowledge_base_updated.sql
-- ============================================================================

create or replace function public.cb_match_knowledge_base_updated(
    query_embedding      public.vector,
    match_threshold      double precision default 0.35,
    match_count          integer          default 5,
    filter_namespace     text             default null,
    filter_service_type  text             default null,
    filter_contact_type  text             default null,
    filter_nationality   text             default null
)
returns table (
    id              uuid,
    namespace       character varying(60),
    service_type    character varying(60),
    contact_type    character varying(20),
    nationality     character varying(5),
    chunk_type      character varying(30),
    question        text,
    answer          text,
    content         text,
    source_document character varying(500),
    section_heading text,
    page_or_section character varying(100),
    metadata        jsonb,
    frequency       integer,
    rag_score_floor numeric(4, 3),
    similarity      double precision
)
language sql
stable
-- Pinned so the unqualified operator lookup for <=> cannot be shadowed.
set search_path = public, pg_catalog
as $$
    select
        kb.id,
        kb.namespace,
        kb.service_type,
        kb.contact_type,
        kb.nationality,
        kb.chunk_type,
        kb.question,
        kb.answer,
        kb.content,
        kb.source_document,
        kb.section_heading,
        kb.page_or_section,
        kb.metadata,
        kb.frequency,
        kb.rag_score_floor,
        (1 - (kb.embedding <=> query_embedding))::double precision as similarity
    from public.cb_knowledge_base_updated kb
    where kb.is_active
      and kb.embedding is not null
      -- Tone samples are not evidence.
      and kb.chunk_type <> 'style_example'
      -- Routing. NULL means "do not filter on this"; every filter that IS
      -- given still lets the catch-all bucket through.
      and (filter_namespace    is null or kb.namespace    = filter_namespace)
      and (filter_service_type is null or kb.service_type in (filter_service_type, 'general'))
      and (filter_contact_type is null or kb.contact_type in (filter_contact_type, 'all'))
      and (filter_nationality  is null or kb.nationality  in (filter_nationality, 'all'))
      -- The caller's threshold, or this row's own floor when it sets a
      -- stricter one.
      and (1 - (kb.embedding <=> query_embedding))
            >= greatest(match_threshold, coalesce(kb.rag_score_floor::double precision, 0))
    -- Distance ordering, so the HNSW index (vector_cosine_ops) is usable.
    -- Priority reranking happens in the application, not here.
    order by kb.embedding <=> query_embedding
    limit greatest(match_count, 1);
$$;

comment on function public.cb_match_knowledge_base_updated is
    'Filtered cosine-similarity search over active cb_knowledge_base_updated rows. '
    'service_type/contact_type/nationality filters include the general/all buckets. '
    'style_example rows are never returned. Query embeddings must come from the '
    'same model and dimension used at ingest.';

-- The chatbot reads through PostgREST with the service role; grant to the
-- usual API roles so /rest/v1/rpc/cb_match_knowledge_base_updated resolves.
grant execute on function public.cb_match_knowledge_base_updated(
    public.vector, double precision, integer, text, text, text, text
) to anon, authenticated, service_role;

-- PostgREST caches the schema. Without this the RPC 404s until the API
-- restarts on its own, which looks exactly like "the function was never
-- created".
notify pgrst, 'reload schema';


-- ============================================================================
-- Verification — run these after applying. Both are read-only.
-- ============================================================================

-- 1. EVERY stored vector must have the same dimension as the query vector
--    (1536, text-embedding-3-small). `embedding` is a bare `vector` with no
--    declared dimension, so nothing stopped the pipeline mixing sizes — and a
--    single odd row does not degrade the search, it makes the whole query
--    ERROR with "different vector dimensions". Expect exactly one row: 1536.
--
-- SELECT vector_dims(embedding) AS dims, count(*)
--   FROM public.cb_knowledge_base_updated
--  WHERE embedding IS NOT NULL
--  GROUP BY 1 ORDER BY 2 DESC;

-- 2. The function resolves and returns rows. match_threshold 0 shows the full
--    distribution; anything at all coming back means the contract is wired up.
--
-- SELECT chunk_type, source_document, round(similarity::numeric, 3) AS sim
--   FROM public.cb_match_knowledge_base_updated(
--            (SELECT embedding FROM public.cb_knowledge_base_updated
--              WHERE embedding IS NOT NULL LIMIT 1),
--            0, 5, NULL, NULL, NULL, NULL);

-- 3. The HNSW index in the schema is only usable if pgvector could build it on
--    a dimensionless column. If it is absent the search still returns exactly
--    the same rows by sequential scan — correct, just slower, and irrelevant
--    at a few thousand chunks. Worth knowing before blaming the function.
--
-- SELECT indexname FROM pg_indexes
--  WHERE tablename = 'cb_knowledge_base_updated' AND indexdef ILIKE '%hnsw%';
