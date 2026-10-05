/**
 * Which text did the original import embed for a document chunk?
 *
 *   node scripts/check_embedding_parity.mjs [--per-source N]
 *
 * READ ONLY, TEST only (scripts/kb_test_readonly.mjs). For imported
 * document_chunk rows that have a section_heading and an embedding, it embeds
 *   A: content alone
 *   B: section_heading + '\n' + content
 * (plus three diagnostic variants, in case neither matches) and reports the
 * cosine of each against the stored vector. >= 0.999 is a match.
 *
 * Rows are taken from documents the loader has never re-embedded
 * (load_service_notes.py re-embeds a row it edits on its content alone, which
 * would make A match by construction), one or more per source.
 *
 * The canary is checked first: if the endpoint and model do not reproduce the
 * bot's vector for the fixed sentence, no other result means anything. Then two
 * CALIBRATION rows whose embedded text is known must reproduce at 1.0: a row the
 * loader wrote (content), and an imported FAQ Q&A row (question + ' ' + answer).
 * If those match and the document chunks do not, the method is sound and the
 * stored chunk text is simply not the text that was embedded.
 */

import { MATCH, cosine, embed, embeddingConfig, parseVector, testDb } from './kb_test_readonly.mjs';

const perSource = Number(process.argv[process.argv.indexOf('--per-source') + 1]) || 1;
const SOURCES = [
  'Ming_Hwee_Helper_Expectation_Checklist.docx',
  'MOM_MDW_Eligibility_Hiring_Guide.docx',
  'Ming_Hwee_Client_Service_Agreement_SOURCE.docx',
];

const db = testDb();
const cfg = embeddingConfig();
try {
  const { canary, rows, calibration } = await db.readOnly(async (tx) => {
    const [canary] = await tx`select model, sentence, embedding::text as e from public.cb_kb_canary order by created_at desc limit 1`;
    const rows = await tx`
      select id, source_document, section_heading, content, embedding::text as e
        from public.cb_knowledge_base_updated
       where chunk_type = 'document_chunk' and is_active
         and section_heading is not null and embedding is not null and content is not null
         and metadata->>'managed_by' = 'loader'
         and source_document in ${tx(SOURCES)}
       order by source_document, id`;
    const calibration = [
      ...(await tx`select id, 'loader row: content' as label, content as t, embedding::text as e from public.cb_knowledge_base_updated
                    where source_document = 'Ming Hwee Service Notes' and content is not null and embedding is not null order by id limit 1`),
      ...(await tx`select id, 'imported FAQ Q&A: question || '' '' || answer' as label, question || ' ' || answer as t, embedding::text as e
                    from public.cb_knowledge_base_updated where chunk_type = 'qa_pair' and source_document = 'minghwee FAQs and Overview.md'
                     and metadata->>'managed_by' = 'loader' and embedding is not null order by id limit 1`),
    ];
    return { canary, rows, calibration };
  });
  if (!canary) throw new Error('cb_kb_canary is empty');

  const c = cosine(await embed(cfg, canary.model, canary.sentence), parseVector(canary.e));
  console.log(`canary  model=${canary.model}  cosine=${c.toFixed(6)}  ${c >= MATCH ? 'MATCH' : 'MISMATCH'}`);
  if (c < MATCH) throw new Error('the canary does not match - the endpoint/model do not reproduce the bot, so stop here');

  for (const r of calibration) {
    const s = cosine(await embed(cfg, canary.model, r.t), parseVector(r.e));
    console.log(`calibration  ${r.label.padEnd(44)} row ${r.id.slice(0, 8)}  cosine=${s.toFixed(6)}  ${s >= MATCH ? 'MATCH' : 'MISMATCH'}`);
  }

  const picked = SOURCES.flatMap((s) => rows.filter((r) => r.source_document === s).slice(0, perSource));
  const variants = {
    'A content': (r) => r.content,
    'B heading\\ncontent': (r) => `${r.section_heading}\n${r.content}`,
    '(C heading\\n\\ncontent)': (r) => `${r.section_heading}\n\n${r.content}`,
    '(D source\\nheading\\ncontent)': (r) => `${r.source_document}\n${r.section_heading}\n${r.content}`,
    '(E content, whitespace flattened)': (r) => r.content.replace(/\s+/g, ' ').trim(),
  };
  const wins = Object.fromEntries(Object.keys(variants).map((k) => [k, 0]));
  for (const r of picked) {
    const stored = parseVector(r.e);
    const starts = r.content.startsWith(r.section_heading) ? ' (content already starts with the heading)' : '';
    console.log(`\nrow ${r.id.slice(0, 8)}  ${r.source_document}\n  heading: ${JSON.stringify(r.section_heading)}${starts}  content ${r.content.length} chars`);
    for (const [name, make] of Object.entries(variants)) {
      const s = cosine(await embed(cfg, canary.model, make(r)), stored);
      if (s >= MATCH) wins[name] += 1;
      console.log(`  ${name.padEnd(30)} cosine=${s.toFixed(6)}  ${s >= MATCH ? 'MATCH' : ''}`);
    }
  }
  console.log(`\nRESULT over ${picked.length} rows: ${Object.entries(wins).map(([k, v]) => `${k}=${v}`).join('  ')}`);
  const winner = Object.entries(wins).find(([, v]) => v === picked.length);
  console.log(winner ? `PARITY: the import embedded "${winner[0]}"` : 'PARITY: no single variant matches every row');
} finally {
  await db.end();
}
