/**
 * kb-admin self-check. Reads no database and makes no network call.
 *
 *   npm run selfcheck
 *
 * Fails (exit 1) if:
 *   - any source file names a write verb (see WRITE_WORDS), the Supabase data
 *     API, a service-role key, NEXT_PUBLIC_, or imports from outside kb-admin/;
 *   - lib/db.ts exports anything but `select`;
 *   - a page or API route does not enforce auth;
 *   - .env.local is tracked by git, or any secret it holds appears in any
 *     other file under kb-admin/ (values are compared, never printed);
 *   - the config validator or the access decision behaves wrongly.
 */

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SELF = fileURLToPath(import.meta.url);
const SKIP_DIRS = new Set(['node_modules', '.next', '.vercel', 'out']);
const CODE = /\.(ts|tsx|js|mjs|cjs)$/;

let failures = 0;
function check(label, ok, detail = '') {
  if (!ok) failures += 1;
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}${detail ? ` - ${detail}` : ''}`);
}

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else out.push(full);
  }
  return out;
}

const files = walk(ROOT);
const code = files.filter((f) => CODE.test(f) && f !== SELF && !f.endsWith('next-env.d.ts'));
const rel = (f) => relative(ROOT, f).split(sep).join('/');

console.log('static:');
// The five verbs the design names, as whole words, anywhere in the source.
const WRITE_WORDS = /\b(insert|update|delete|upsert|rpc)\b/i;
for (const f of code) {
  const lines = readFileSync(f, 'utf8').split('\n');
  const hits = lines.map((l, i) => (WRITE_WORDS.test(l) ? i + 1 : 0)).filter(Boolean);
  if (hits.length) check(`no write verb in ${rel(f)}`, false, `line(s) ${hits.join(', ')}`);
}
check('no write verb in any source file', !code.some((f) => WRITE_WORDS.test(readFileSync(f, 'utf8'))));

const bad = [
  [/\.from\(\s*['"`]/, 'Supabase data API (.from)'],
  [/SERVICE_ROLE/i, 'a service-role key name'],
  [/NEXT_PUBLIC_/, 'a NEXT_PUBLIC_ variable'],
  [/sb_secret_[A-Za-z0-9]/, 'a secret key literal'],
  [/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\./, 'a JWT literal'],
  [/@supabase\//, 'the supabase-js library'],
];
for (const [re, what] of bad) {
  const hit = code.filter((f) => re.test(readFileSync(f, 'utf8')));
  check(`no ${what}`, hit.length === 0, hit.map(rel).join(', '));
}

const escapes = [];
for (const f of code) {
  for (const m of readFileSync(f, 'utf8').matchAll(/(?:from|import)\s*\(?\s*['"](\.[^'"]*)['"]/g)) {
    const target = resolve(dirname(f), m[1]);
    if (!target.startsWith(ROOT + sep)) escapes.push(`${rel(f)} -> ${m[1]}`);
  }
}
check('no import reaches outside kb-admin/', escapes.length === 0, escapes.join(', '));

const db = readFileSync(join(ROOT, 'lib/db.ts'), 'utf8');
const exported = [...db.matchAll(/export\s+(?:async\s+)?(?:function|const|let|var|class)\s+(\w+)/g)].map((m) => m[1]);
check('lib/db.ts exports select and nothing else', exported.join(',') === 'select', exported.join(','));
check("lib/db.ts runs every query in BEGIN READ ONLY", /\.begin\(\s*'read only'/.test(db));

const pages = code.filter((f) => /^app\/.*page\.tsx$/.test(rel(f)) && rel(f) !== 'app/login/page.tsx');
for (const p of pages) check(`${rel(p)} requires a viewer`, readFileSync(p, 'utf8').includes('requireViewer('));
const apis = code.filter((f) => /^app\/api\/.*route\.ts$/.test(rel(f)));
for (const a of apis) check(`${rel(a)} checks the session`, readFileSync(a, 'utf8').includes('checkRequest('));
check('found pages and API routes to check', pages.length >= 6 && apis.length >= 1, `${pages.length} pages, ${apis.length} api`);

console.log('secrets:');
const envLocal = join(ROOT, '.env.local');
let tracked = '';
try {
  tracked = execFileSync('git', ['ls-files', '--', '.env.local', '.env'], { cwd: ROOT, encoding: 'utf8' }).trim();
} catch {
  tracked = '(git unavailable)';
}
check('.env / .env.local are not tracked by git', tracked === '', tracked);
if (existsSync(envLocal)) {
  const secrets = [];
  for (const line of readFileSync(envLocal, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*(SUPABASE_ANON_KEY|ADMIN_PREVIEW_SECRET|KB_ADMIN_DB_PASSWORD|KB_ADMIN_DB_PASSWORD_FILE)\s*=\s*(.*)$/);
    if (!m || !m[2].trim()) continue;
    if (m[1] === 'KB_ADMIN_DB_PASSWORD_FILE') {
      if (existsSync(m[2].trim())) secrets.push(['database password', readFileSync(m[2].trim(), 'utf8').trim()]);
    } else secrets.push([m[1], m[2].trim()]);
  }
  const others = files.filter((f) => f !== envLocal);
  for (const [name, value] of secrets) {
    if (value.length < 8) continue;
    const leaks = others.filter((f) => readFileSync(f, 'utf8').includes(value));
    check(`${name} appears in no other kb-admin file`, leaks.length === 0, leaks.map(rel).join(', '));
  }
  check('checked the secrets held in .env.local', secrets.length >= 2, `${secrets.length} value(s), not shown`);
} else {
  console.log('  (no .env.local - secret leak check skipped)');
}

console.log('behaviour:');
const { validateConfig } = await import(pathToFileURL(join(ROOT, 'lib/config.ts')).href);
const { decideAccess, isSingleRead, secondsLeft } = await import(pathToFileURL(join(ROOT, 'lib/access.ts')).href);

const REF = 'abcdefghijklmnopqrst';
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
const jwt = (claims) => `${b64({ alg: 'HS256' })}.${b64(claims)}.sig`;
const good = {
  KB_ADMIN_EXPECTED_REF: REF,
  SUPABASE_URL: `https://${REF}.supabase.co`,
  SUPABASE_ANON_KEY: jwt({ role: 'anon', ref: REF }),
  KB_ADMIN_DB_HOST: 'aws-0-ap-southeast-1.pooler.supabase.com',
  KB_ADMIN_DB_PORT: '6543',
  KB_ADMIN_DB_USER: `kb_admin_reader.${REF}`,
  KB_ADMIN_DB_PASSWORD: 'x'.repeat(24),
  BOT_PREVIEW_URL: 'http://127.0.0.1:8000',
  ADMIN_PREVIEW_SECRET: 'y'.repeat(40),
};
const noFile = () => {
  throw new Error('no file in selfcheck');
};
const refuses = (overrides) => {
  try {
    validateConfig({ ...good, ...overrides }, noFile);
    return false;
  } catch (e) {
    return /refuses to start/.test(String(e.message));
  }
};
let accepted = false;
try {
  accepted = validateConfig(good, noFile).ref === REF;
} catch {}
check('accepts a correct configuration', accepted);
check('refuses a SUPABASE_URL for another project', refuses({ SUPABASE_URL: 'https://zzzzzzzzzzzzzzzzzzzz.supabase.co' }));
check('refuses a service-role key', refuses({ SUPABASE_ANON_KEY: jwt({ role: 'service_role', ref: REF }) }));
check('refuses an anon key from another project', refuses({ SUPABASE_ANON_KEY: jwt({ role: 'anon', ref: 'zzzzzzzzzzzzzzzzzzzz' }) }));
check('refuses a secret key', refuses({ SUPABASE_ANON_KEY: 'sb_secret_abcdef' }));
check('refuses a database user other than kb_admin_reader', refuses({ KB_ADMIN_DB_USER: `postgres.${REF}` }));
check('refuses a non-pooler database host', refuses({ KB_ADMIN_DB_HOST: `db.${REF}.supabase.co` }));
check('refuses a missing database password', refuses({ KB_ADMIN_DB_PASSWORD: '' }));
check('refuses a plain-http preview URL off localhost', refuses({ BOT_PREVIEW_URL: 'http://example.com' }));
check('refuses a short preview secret', refuses({ ADMIN_PREVIEW_SECRET: 'short' }));
check('refuses a malformed expected ref', refuses({ KB_ADMIN_EXPECTED_REF: 'x' }));

check('no membership row -> denied', decideAccess(undefined).ok === false);
check('inactive row -> denied', decideAccess({ role: 'viewer', active: false }).ok === false);
check('unknown role -> denied', decideAccess({ role: 'admin', active: true }).ok === false);
check('active viewer -> allowed', decideAccess({ role: 'viewer', active: true }).ok === true);

check('a single SELECT is a read', isSingleRead('select 1'));
check('a WITH ... SELECT is a read', isSingleRead('with x as (select 1) select * from x'));
check('two statements are refused', !isSingleRead('select 1; select 2'));
check('a statement not starting with SELECT/WITH is refused', !isSingleRead('truncate t'));

const now = 1_000_000;
check('token expiry read', secondsLeft(jwt({ exp: now + 300 }), now) === 300);
check('garbage token reads as expired', secondsLeft('garbage', now) < 0);

console.log(failures ? `RESULT: ${failures} FAIL(S)` : 'RESULT: ALL PASS');
process.exitCode = failures ? 1 : 0;
