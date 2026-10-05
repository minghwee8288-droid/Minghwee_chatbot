/**
 * Run kb-admin's chunker over the seven source documents and compare its
 * chunks with the rows the original import stored for each.
 *
 *   node --no-warnings scripts/verify_chunker.mjs [--detail] [--docs DIR]
 *
 * READ ONLY, TEST only (scripts/kb_test_readonly.mjs). Writes nothing anywhere.
 *
 * The chunker is meant to differ where the import was defective, so every
 * stored row is classified rather than simply diffed:
 *
 *   same text        normalised text equal to one chunker chunk
 *   cites removed    equal once [cite:N] is taken out (defect fix)
 *   split            the row was over the bot's limit (1,200 passage /
 *                    3,000 table) and its text now spans several chunks
 *                    (defect fix)
 *   re-cut           its text is all there, divided at different places
 *                    (boundaries follow headings now)
 *   NOT COVERED      under 90% of its text appears anywhere in the chunker's
 *                    output - the one class that would mean lost content
 *
 * and, the other way round, chunker text the import never stored is counted.
 * Comparison is on words only (case, punctuation, markdown and whitespace
 * ignored), in overlapping runs of three words.
 *
 * Lost facts (kb-admin/lib/facts.ts) are listed per document: every phone
 * number, amount, time span, percentage and number the ACTIVE live rows state
 * that the chunker's output does not - what a re-upload of this file would drop.
 *
 * If a document comes out with every row "same text", its chunks are embedded
 * and compared with the stored vectors (content alone; see
 * check_embedding_parity.mjs for why no concatenation reproduces the import).
 */

import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { KB_ADMIN, MATCH, cosine, embed, embeddingConfig, parseVector, testDb } from './kb_test_readonly.mjs';

const arg = (name) => (process.argv.includes(name) ? process.argv[process.argv.indexOf(name) + 1] : null);
const DOCS = arg('--docs') ?? 'C:/Users/DELL/Minghwee_backups/kb-admin-handover/source-docs';
const DETAIL = process.argv.includes('--detail');
const { chunkDocument, MAX_PASSAGE_CHARS, MAX_TABLE_CHARS } = await import(pathToFileURL(join(KB_ADMIN, 'lib', 'chunker.ts')).href);
const { findLostFacts } = await import(pathToFileURL(join(KB_ADMIN, 'lib', 'facts.ts')).href);

const CITE = /\[cite:\d+\]/g;
const words = (s) => (s ?? '').replace(CITE, ' ').toLowerCase().normalize('NFKC').replace(/[^\p{L}\p{N}$%]+/gu, ' ').trim().split(' ').filter(Boolean);
const norm = (s) => words(s).join(' ');
const shingles = (w) => {
  const out = new Set();
  if (w.length < 3) { if (w.length) out.add(w.join(' ')); return out; }
  for (let i = 0; i + 3 <= w.length; i += 1) out.add(`${w[i]} ${w[i + 1]} ${w[i + 2]}`);
  return out;
};
const share = (a, b) => { if (!a.size) return 1; let n = 0; for (const x of a) if (b.has(x)) n += 1; return n / a.size; };
/** A table row's content starts with its heading line; compare bodies too. */
const body = (content, heading) => (heading && content.startsWith(heading) ? content.slice(heading.length) : content);

const db = testDb();
const files = readdirSync(DOCS).filter((f) => /\.(docx|md|txt)$/i.test(f)).sort();
const stored = await db.readOnly((tx) => tx`
  select id, source_document, chunk_type, section_heading, content, is_active, embedding::text as e
    from public.cb_knowledge_base_updated
   where chunk_type in ('document_chunk', 'table_unit') and source_document in ${tx(files)}
   order by source_document, id`);
await db.end();

const totals = { rows: 0, chunks: 0, NOT_COVERED: 0 };
const zeroDiff = [];
for (const file of files) {
  const type = file.split('.').pop().toLowerCase();
  const chunks = await chunkDocument(readFileSync(join(DOCS, file)), type, file);
  const rows = stored.filter((r) => r.source_document === file);
  const cNorm = chunks.map((c) => norm(c.content));
  const cBody = chunks.map((c) => norm(body(c.content, c.section_heading)));
  const cSh = chunks.map((c) => shingles(words(c.content)));
  // Coverage reads the chunker's whole output as one stream, in order, with
  // each heading where it changes: the import often kept a sub-heading inside
  // the text ("What is OK"), which the chunker makes a heading, and a stored
  // row spanning two chunks has word runs across the join.
  const stream = [];
  let lastHead = null;
  for (const c of chunks) {
    if (c.section_heading !== lastHead && c.section_heading) stream.push(c.section_heading);
    lastHead = c.section_heading;
    stream.push(c.chunk_type === 'table_unit' ? body(c.content, c.section_heading) : c.content);
  }
  const allSh = shingles(words(stream.join('\n')));
  const rowsSh = new Set(rows.flatMap((r) => [...shingles(words(r.content))]));
  const kinds = {};
  const heads = { same: 0, differs: 0, title: 0 };
  const lines = [];
  const titleish = rows.length ? Object.entries(rows.reduce((m, r) => ((m[r.section_heading] = (m[r.section_heading] ?? 0) + 1), m), {})).sort((a, b) => b[1] - a[1])[0] : null;
  for (const r of rows) {
    const rn = norm(r.content);
    const rb = norm(body(r.content, r.section_heading));
    const rSh = shingles(words(r.content));
    const limit = r.chunk_type === 'table_unit' ? MAX_TABLE_CHARS : MAX_PASSAGE_CHARS;
    const scores = cSh.map((s) => share(rSh, s));
    const best = scores.indexOf(Math.max(...scores));
    const covered = share(rSh, allSh);
    let kind;
    const exact = cNorm.findIndex((c, i) => c === rn || cBody[i] === rb || cBody[i] === rn || c === rb);
    if (exact >= 0) kind = /\[cite:\d+\]/.test(r.content) ? 'cites removed' : 'same text';
    else if (covered < 0.9) kind = 'NOT COVERED';
    else if (r.content.length > limit) kind = 'split';
    else kind = 're-cut';
    kinds[kind] = (kinds[kind] ?? 0) + 1;
    const twin = exact >= 0 ? exact : best;
    if (chunks[twin]?.section_heading === r.section_heading) heads.same += 1;
    else heads.differs += 1;
    lines.push(`    ${kind.padEnd(13)} ${r.chunk_type === 'table_unit' ? 'table' : 'pass.'} ${String(r.content.length).padStart(5)} ch ${r.is_active ? ' ' : 'off'} cover ${(covered * 100).toFixed(0).padStart(3)}% -> #${chunks[twin]?.ordinal ?? '-'}  heading ${JSON.stringify(r.section_heading)?.slice(0, 50)}${chunks[twin] && chunks[twin].section_heading !== r.section_heading ? ` => ${JSON.stringify(chunks[twin].section_heading)?.slice(0, 50)}` : ''}`);
  }
  if (titleish && titleish[1] > 1 && rows.length >= 10 && titleish[1] / rows.length > 0.25) heads.title = titleish[1];
  const newText = chunks.filter((c, i) => share(cSh[i], rowsSh) < 0.5).length;
  const overDb = rows.filter((r) => r.content.length > (r.chunk_type === 'table_unit' ? MAX_TABLE_CHARS : MAX_PASSAGE_CHARS)).length;
  const citesDb = rows.filter((r) => /\[cite:\d+\]/.test(r.content)).length;
  totals.rows += rows.length;
  totals.chunks += chunks.length;
  totals.NOT_COVERED += kinds['NOT COVERED'] ?? 0;
  const p = chunks.filter((c) => c.chunk_type === 'passage');
  const t = chunks.filter((c) => c.chunk_type === 'table_unit');
  console.log(`\n${file}`);
  console.log(`  DB: ${rows.length} rows (${rows.filter((r) => r.chunk_type === 'document_chunk').length} passage, ${rows.filter((r) => r.chunk_type === 'table_unit').length} table; ${rows.filter((r) => !r.is_active).length} switched off)` +
    ` | over the limit ${overDb}, with [cite:N] ${citesDb}, distinct headings ${new Set(rows.map((r) => r.section_heading)).size}`);
  console.log(`  chunker: ${chunks.length} chunks (${p.length} passage, ${t.length} table) | longest passage ${Math.max(0, ...p.map((c) => c.content.length))}, longest table ${Math.max(0, ...t.map((c) => c.content.length))}, distinct headings ${new Set(chunks.map((c) => c.section_heading)).size}, null headings ${chunks.filter((c) => !c.section_heading).length}`);
  console.log(`  stored rows: ${Object.entries(kinds).map(([k, v]) => `${k} ${v}`).join(', ') || 'none'}`);
  console.log(`  headings vs best twin: same ${heads.same}, differ ${heads.differs}${heads.title ? ` (the DB puts one heading on ${heads.title} rows: ${JSON.stringify(titleish[0]).slice(0, 60)})` : ''}`);
  console.log(`  chunker chunks whose text the import never stored (under 50% shared): ${newText}`);
  if (DETAIL || kinds['NOT COVERED']) for (const l of lines) if (DETAIL || l.includes('NOT COVERED')) console.log(l);
  // Lost facts: what the ACTIVE live rows state that the new chunks would drop.
  const lost = findLostFacts(rows.filter((r) => r.is_active).map((r) => `${r.section_heading ?? ''}
${r.content}`),
    chunks.map((c) => `${c.section_heading ?? ''}
${c.content}`));
  const byKind = lost.reduce((m, f) => ((m[f.kind] = (m[f.kind] ?? 0) + 1), m), {});
  console.log(`  lost facts (live rows -> new chunks): ${lost.length ? Object.entries(byKind).map(([k, v]) => `${k} ${v}`).join(', ') : 'none'}`);
  for (const f of lost.filter((x) => x.kind !== 'number' || DETAIL)) console.log(`    ${f.kind.padEnd(7)} ${f.fact.padEnd(16)} in ${f.liveRows} row(s): "...${f.example}..."`);
  if (!DETAIL && byKind.number) console.log(`    numbers: ${lost.filter((x) => x.kind === 'number').map((x) => x.fact).join(', ')}`);
  if (rows.length && rows.length === chunks.length && (kinds['same text'] ?? 0) === rows.length) zeroDiff.push({ file, rows, chunks });
}

console.log(`\nTOTAL: ${totals.rows} stored rows, ${totals.chunks} chunker chunks, NOT COVERED ${totals.NOT_COVERED}`);
if (!zeroDiff.length) {
  console.log('No document has zero differences, so no vectors were compared.');
} else {
  const cfg = embeddingConfig();
  for (const { file, rows, chunks } of zeroDiff) {
    let ok = 0;
    for (const r of rows) {
      const c = chunks.find((x) => norm(x.content) === norm(r.content));
      const s = cosine(await embed(cfg, 'openai/text-embedding-3-small', c.content), parseVector(r.e));
      if (s >= MATCH) ok += 1;
    }
    console.log(`vectors: ${file} - ${ok}/${rows.length} reproduce the stored vector (content alone)`);
  }
}
