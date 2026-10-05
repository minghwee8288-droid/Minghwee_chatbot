/**
 * Shared by check_embedding_parity.mjs and verify_chunker.mjs: a READ-ONLY
 * connection to the TEST project, and the embedding endpoint kb-admin uses.
 *
 * Safety:
 *   * the database URL comes from .env.test and must name the TEST ref; the
 *     production ref is refused outright;
 *   * every query runs inside BEGIN READ ONLY (see readOnly below), so a
 *     statement that tries to change anything fails in the database;
 *   * the embedding key and base URL come from kb-admin/.env.test.local, the
 *     same ones kb-admin's editor uses against TEST.
 *
 * The postgres client is kb-admin's own dependency, loaded from its folder,
 * so nothing new is installed at the repo root.
 */

import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const KB_ADMIN = join(REPO, 'kb-admin');
export const TEST_REF = 'cupwomqvevxravkppuxt';
const PRODUCTION_REF = 'qizcnyuzgylzoyfvymfo';
export const DIMENSIONS = 1536;

function parseEnv(path) {
  const out = {};
  for (const line of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^(['"])(.*)\1$/, '$2');
  }
  return out;
}

function stop(message) {
  console.error(`[kb-test] refusing: ${message}`);
  process.exit(2);
}

export function testDb() {
  const url = parseEnv(join(REPO, '.env.test')).SUPABASE_DB_URL ?? '';
  if (!url) stop('.env.test has no SUPABASE_DB_URL');
  if (url.includes(PRODUCTION_REF)) stop('.env.test names the PRODUCTION project');
  if (!url.includes(TEST_REF)) stop(`.env.test does not name the TEST project ${TEST_REF}`);
  const postgres = createRequire(join(KB_ADMIN, 'package.json'))('postgres');
  const sql = postgres(url, { prepare: false, max: 1, ssl: 'require', connect_timeout: 15, onnotice: () => {} });
  console.log(`[kb-test] database ref = ${TEST_REF} (read only)`);
  return {
    /** Run fn(tx) inside BEGIN READ ONLY; the transaction never commits a change. */
    readOnly: (fn) => sql.begin('read only', fn),
    end: () => sql.end({ timeout: 5 }),
  };
}

export function embeddingConfig() {
  const env = parseEnv(join(KB_ADMIN, '.env.test.local'));
  const apiKey = env.KB_ADMIN_EMBEDDING_API_KEY ?? '';
  const baseUrl = (env.KB_ADMIN_EMBEDDING_BASE_URL ?? '').replace(/\/+$/, '');
  if (!apiKey || !baseUrl) stop('kb-admin/.env.test.local lacks KB_ADMIN_EMBEDDING_API_KEY or KB_ADMIN_EMBEDDING_BASE_URL');
  return { apiKey, baseUrl };
}

/** Embed as kb-admin's lib/embed.ts does: 1536 dimensions, base64 float32. */
export async function embed(cfg, model, text) {
  const res = await fetch(`${cfg.baseUrl}/embeddings`, {
    method: 'POST',
    headers: { authorization: `Bearer ${cfg.apiKey}`, 'content-type': 'application/json' },
    body: JSON.stringify({ model, input: text, dimensions: DIMENSIONS, encoding_format: 'base64' }),
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`embedding service answered ${res.status}`);
  const data = await res.json();
  const raw = data?.data?.[0]?.embedding;
  const vector = typeof raw === 'string'
    ? Array.from(new Float32Array(new Uint8Array(Buffer.from(raw, 'base64')).buffer))
    : raw;
  if (!Array.isArray(vector) || vector.length !== DIMENSIONS) throw new Error('no 1536-number vector returned');
  return vector;
}

export const parseVector = (text) => JSON.parse(text);

export function cosine(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i += 1) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return dot / Math.sqrt(na * nb);
}

export const MATCH = 0.999;
