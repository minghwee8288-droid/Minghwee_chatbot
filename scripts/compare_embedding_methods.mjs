/**
 * Which text should kb-admin embed for a document chunk: its content alone,
 * or its section heading + '\n' + content?
 *
 *   node --no-warnings scripts/compare_embedding_methods.mjs [--doc FILE] [--docs DIR]
 *
 * READ ONLY, TEST only (scripts/kb_test_readonly.mjs). Nothing is written
 * anywhere; vectors live in memory for the length of the run.
 *
 * For ONE document (default the Helper Expectation Checklist) it chunks the
 * source file with kb-admin's chunker, embeds every chunk both ways, and runs
 * the 42 probe questions (cb_kb_probe_questions) against a pool that is
 * exactly what kb_admin_match_with_batch (011) would search: every active live
 * row with an embedding except style examples and except this document's own
 * document_chunk / table_unit rows, plus the new chunks in their place. Same
 * filters (service + 'general', audience + 'all', nationality + 'all'), same
 * threshold (greatest of 0.35 and the row's floor), top 5.
 *
 * Reported per method: how many probes put one of this document's chunks in
 * the top 5, and the average best score of this document's chunks over those
 * probes. The same for the live rows as they are today, as a baseline. As
 * few probes may reach one document, also: this document's best score on
 * every probe (in the top 5 or not), averaged, and on how many probes the
 * heading method scores higher than content alone.
 *
 * The new chunks are routed service 'general', audience 'candidate',
 * nationality 'all' - what most of this document's live rows carry (the
 * upload screen will let an editor set it per chunk).
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { KB_ADMIN, cosine, embed, embeddingConfig, parseVector, testDb } from './kb_test_readonly.mjs';

const arg = (name, dflt) => (process.argv.includes(name) ? process.argv[process.argv.indexOf(name) + 1] : dflt);
const DOCS = arg('--docs', 'C:/Users/DELL/Minghwee_backups/kb-admin-handover/source-docs');
const DOC = arg('--doc', 'Ming_Hwee_Helper_Expectation_Checklist.docx');
const ROUTING = { service_type: 'general', contact_type: 'candidate', nationality: 'all' };
const THRESHOLD = 0.35;
const TOP = 5;

const { chunkDocument } = await import(pathToFileURL(join(KB_ADMIN, 'lib', 'chunker.ts')).href);

const db = testDb();
const cfg = embeddingConfig();
let data;
try {
  data = await db.readOnly(async (tx) => ({
    canary: (await tx`select model from public.cb_kb_canary order by created_at desc limit 1`)[0],
    probes: await tx`select question_text, service, audience, nationality from public.cb_kb_probe_questions where is_active order by question_text`,
    live: await tx`
      select id, source_document, chunk_type, service_type, contact_type, nationality,
             rag_score_floor::float8 as floor, embedding::text as e
        from public.cb_knowledge_base_updated
       where is_active and embedding is not null and chunk_type <> 'style_example'`,
  }));
} finally {
  await db.end();
}
const model = data.canary.model;
const live = data.live.map((r) => ({ ...r, v: parseVector(r.e), mine: r.source_document === DOC && ['document_chunk', 'table_unit'].includes(r.chunk_type) }));
const others = live.filter((r) => !r.mine);
const baselineMine = live.filter((r) => r.mine);

const type = DOC.split('.').pop().toLowerCase();
const chunks = await chunkDocument(readFileSync(join(DOCS, DOC)), type, DOC);
const floorOf = (c) => c.suggested_rag_score_floor;
console.log(`document ${DOC}: ${chunks.length} chunks; ${baselineMine.length} live rows today; ${others.length} other live rows; ${data.probes.length} probes; model ${model}`);

const methods = {
  'content alone': (c) => c.content,
  'heading + \\n + content': (c) => (c.section_heading ? `${c.section_heading}\n${c.content}` : c.content),
};
const pools = { 'live rows today (baseline)': baselineMine };
for (const [name, text] of Object.entries(methods)) {
  const rows = [];
  for (const c of chunks) {
    rows.push({ id: `new#${c.ordinal}`, source_document: DOC, chunk_type: c.chunk_type, ...ROUTING, floor: floorOf(c), v: await embed(cfg, model, text(c)), mine: true });
  }
  pools[name] = rows;
}

const probeVectors = [];
for (const p of data.probes) probeVectors.push(await embed(cfg, model, p.question_text));

const passes = (r, p) =>
  (p.service == null || r.service_type === p.service || r.service_type === 'general') &&
  (p.audience == null || r.contact_type === p.audience || r.contact_type === 'all') &&
  (p.nationality == null || r.nationality === p.nationality || r.nationality === 'all');

const results = {};
const bests = {};
for (const [name, mine] of Object.entries(pools)) {
  const pool = [...others, ...mine];
  let hits = 0;
  let sum = 0;
  let sumAll = 0;
  let wins = 0;
  const hitProbes = [];
  data.probes.forEach((p, i) => {
    const top = pool
      .filter((r) => passes(r, p))
      .map((r) => ({ r, s: cosine(r.v, probeVectors[i]) }))
      .filter(({ r, s }) => s >= Math.max(THRESHOLD, r.floor ?? 0))
      .sort((a, b) => b.s - a.s)
      .slice(0, TOP);
    const ours = top.filter(({ r }) => r.mine);
    // This document's best raw similarity to the probe, in the top 5 or not
    // and before routing filters (routing is the same for both methods).
    const bestMine = Math.max(...mine.map((r) => cosine(r.v, probeVectors[i])));
    sumAll += bestMine;
    bests[name] = [...(bests[name] ?? []), bestMine];
    if (ours.length) {
      hits += 1;
      sum += ours[0].s;
      hitProbes.push(`${p.question_text} (${ours[0].s.toFixed(3)}, rank ${top.findIndex(({ r }) => r.mine) + 1})`);
    }
  });
  results[name] = { hits, avg: hits ? sum / hits : 0, avgAll: sumAll / data.probes.length, hitProbes };
}

console.log('');
for (const [name, r] of Object.entries(results)) {
  console.log(`${name.padEnd(28)} probes with this document in the top ${TOP}: ${String(r.hits).padStart(2)}/${data.probes.length}   average best score: ${r.avg.toFixed(4)}   (raw best similarity, all ${data.probes.length} probes: ${r.avgAll.toFixed(4)})`);
  for (const h of r.hitProbes) console.log(`      ${h}`);
}
const [ka, kb] = Object.keys(methods);
const higher = bests[kb].filter((x, i) => x > bests[ka][i]).length;
console.log(`
probe by probe, "${kb}" scores this document higher on ${higher}/${data.probes.length} probes`);
const [a, b] = Object.keys(methods).map((k) => results[k]);
const pick = b.hits > a.hits || (b.hits === a.hits && b.avg > a.avg) ? Object.keys(methods)[1] : Object.keys(methods)[0];
console.log(`\nBETTER ON THIS DOCUMENT: ${pick}`);
