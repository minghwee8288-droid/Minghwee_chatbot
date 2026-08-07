-- ============================================================================
-- Knowledge base hygiene — issues found by running real client questions
-- against the live cb_knowledge_base (791 chunks, all namespace
-- 'document_chunks').
--
-- The chatbot only READS this table. Nothing here is applied automatically;
-- every statement is commented out pending your decision.
-- Re-check afterwards with:  python scripts/check_retrieval.py
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. CONTRADICTORY MOM FIGURE  (highest priority — the bot will quote it)
--
-- "do i need to buy insurance for her" retrieves both of these:
--
--   0.492  minghwee_scrape.md
--          "Medical insurance with minimum coverage of S$15,000/year"   <- STALE
--   0.480  (MOM guide)
--          "Medical insurance of at least $60,000 a year"               <- CORRECT
--
-- The stale chunk ranks HIGHER, so that is what the bot answers with. Your own
-- 48-version-control.md already records this as a known error:
--   "Medical insurance minimum stated as $15,000 | $60,000/year since 1 July 2023"
--
-- The website copy at minghwee.com is the root cause — it still states the
-- pre-July-2023 figure. Fix the page and re-run the RAG pipeline, or deactivate
-- just the affected chunks:
-- ----------------------------------------------------------------------------

-- Inspect first:
SELECT id, source_id, left(content, 200) AS preview
FROM cb_knowledge_base
WHERE is_active
  AND content ILIKE '%insurance%'
  AND (content LIKE '%15,000%' OR content LIKE '%15000%')
ORDER BY source_id;

-- Then, to retire only the stale website chunks (keeps the co-payment chunks,
-- where $15,000 is still the correct threshold):
-- UPDATE cb_knowledge_base
--    SET is_active = FALSE, updated_at = now()
--  WHERE source_id = 'minghwee_scrape.md'
--    AND content ILIKE '%minimum coverage of S$15,000%';


-- ----------------------------------------------------------------------------
-- 2. INTERNAL DOCUMENTS ARE CLIENT-RETRIEVABLE
--
-- 176 of the 791 chunks (22%) come from internal delivery documents rather
-- than client-facing content. They already surface in real searches — an
-- off-topic client question returned "Honest Findings & Issues to Resolve" and
-- a "| # | Blocker | Owner | Why it blocks |" table.
--
--    55  MHOS-100_Product_Delivery_and_Admin_Console_Blueprint.docx
--    57  MingHwee_Hiring_Pipelines_Brief_v1_0_20260609.docx
--    51  MHOS_Feature_List_for_Vendor.md
--     8  48-version-control.md
--     5  01-chatbot-identity-guardrails.md   <- the bot's own guardrails
--
-- The last one matters most: if a chunk describing the bot's rules is ever
-- retrieved and paraphrased, the bot can reveal that it is a bot — the one
-- thing it must never do.
--
-- Client-facing sources to KEEP:
--   391  minghwee_scrape.md
--    85  Ming_Hwee_Client_Service_Agreement_SOURCE.docx
--    36  27-helper-rights-simple-english.md
--    34  MOM_MDW_Eligibility_Hiring_Guide.docx
--    28  20260627_-_Ming_Hwee_Biodata_V13.docx
--    24  20260701_-_Ming_Hwee_Helper_Expectation_Checklist_v4.docx
--    14  20260627_-_Ming_Hwee_Application_Form_FDW_v4.docx
-- ----------------------------------------------------------------------------

-- Review what would be retired:
SELECT source_id, count(*) AS chunks
FROM cb_knowledge_base
WHERE is_active
  AND source_id IN (
      'MHOS-100_Product_Delivery_and_Admin_Console_Blueprint.docx',
      'MingHwee_Hiring_Pipelines_Brief_v1_0_20260609.docx',
      'MHOS_Feature_List_for_Vendor.md',
      '48-version-control.md',
      '01-chatbot-identity-guardrails.md'
  )
GROUP BY source_id
ORDER BY chunks DESC;

-- Retire them (reversible — set is_active back to TRUE to restore):
-- UPDATE cb_knowledge_base
--    SET is_active = FALSE, updated_at = now()
--  WHERE source_id IN (
--      'MHOS-100_Product_Delivery_and_Admin_Console_Blueprint.docx',
--      'MingHwee_Hiring_Pipelines_Brief_v1_0_20260609.docx',
--      'MHOS_Feature_List_for_Vendor.md',
--      '48-version-control.md',
--      '01-chatbot-identity-guardrails.md'
--  );

-- Note: the hiring pipelines brief does contain genuine process detail. If you
-- would rather keep it, review its chunks individually rather than retiring the
-- whole source:
-- SELECT id, left(content, 160) FROM cb_knowledge_base
--  WHERE source_id = 'MingHwee_Hiring_Pipelines_Brief_v1_0_20260609.docx' AND is_active;
