/**
 * Run kb-admin locally against the TEST project, leaving .env.local (the
 * production values) untouched.
 *
 *   npm run dev:test
 *
 * Reads .env.test.local (git-ignored) and starts `next dev` on port 5177 with
 * its own build folder (.next-test), so it can run beside the normal dev
 * server without sharing a cache.
 *
 * NOTHING FALLS THROUGH FROM .env.local. Next.js fills in any variable the
 * process does not already have from .env.local - so a key missing from the
 * test file would silently take its production value. This launcher sets
 * EVERY key named in any kb-admin env file to the test file's value, or to ''
 * when the test file lacks it, and Next.js never overrides a variable that is
 * already set (even to ''). kb-admin's own start-up check then refuses unless
 * the URL, the anon key and both database logins all name the test ref.
 *
 * It also refuses outright if the test file names the production project.
 */

import { spawn } from 'node:child_process';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const TEST_FILE = join(ROOT, '.env.test.local');
const PRODUCTION_REF = 'qizcnyuzgylzoyfvymfo';

function parse(path) {
  const out = {};
  for (const line of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^(['"])(.*)\1$/, '$2');
  }
  return out;
}

function stop(message) {
  console.error(`[dev:test] refusing: ${message}`);
  process.exit(1);
}

if (!existsSync(TEST_FILE)) stop('no .env.test.local (see .env.example, "Local testing against the TEST project")');
const test = parse(TEST_FILE);
const ref = (test.KB_ADMIN_EXPECTED_REF ?? '').trim();
if (!/^[a-z0-9]{20}$/.test(ref)) stop('.env.test.local has no KB_ADMIN_EXPECTED_REF');
if (ref === PRODUCTION_REF) stop('.env.test.local names the PRODUCTION project');
for (const [key, value] of Object.entries(test)) {
  if (value.includes(PRODUCTION_REF)) stop(`${key} in .env.test.local names the production project`);
}

// Every key any env file here could supply, plus every key kb-admin reads.
const keys = new Set([
  'KB_ADMIN_EXPECTED_REF', 'SUPABASE_URL', 'SUPABASE_ANON_KEY', 'KB_ADMIN_DB_HOST', 'KB_ADMIN_DB_PORT',
  'KB_ADMIN_DB_USER', 'KB_ADMIN_DB_PASSWORD', 'KB_ADMIN_DB_PASSWORD_FILE', 'BOT_PREVIEW_URL', 'ADMIN_PREVIEW_SECRET',
  'KB_ADMIN_DB_PASSWORD_EDITOR', 'KB_ADMIN_DB_PASSWORD_EDITOR_FILE', 'KB_ADMIN_EMBEDDING_API_KEY', 'KB_ADMIN_EMBEDDING_BASE_URL',
]);
for (const name of readdirSync(ROOT)) {
  if (/^\.env/.test(name) && name !== '.env.test.local') for (const k of Object.keys(parse(join(ROOT, name)))) keys.add(k);
}

const env = { ...process.env, KB_ADMIN_DIST_DIR: '.next-test' };
for (const k of keys) env[k] = test[k] ?? '';
console.info(`[dev:test] project ${ref}; ${keys.size} settings pinned from .env.test.local (missing ones blank, never from .env.local)`);

const next = join(ROOT, 'node_modules', 'next', 'dist', 'bin', 'next');
const child = spawn(process.execPath, [next, 'dev', '-p', '5177', '-H', '127.0.0.1'], { cwd: ROOT, env, stdio: 'inherit' });
child.on('exit', (code) => process.exit(code ?? 1));
