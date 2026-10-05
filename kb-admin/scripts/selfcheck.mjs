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
const SKIP_DIRS = new Set(['node_modules', '.next', '.next-test', '.vercel', 'out']);
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
const src = (p) => (existsSync(join(ROOT, p)) ? readFileSync(join(ROOT, p), 'utf8') : '');

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
const exportsOf = (text) => [...text.matchAll(/export\s+(?:async\s+)?(?:function|const|let|var|class)\s+(\w+)/g)].map((m) => m[1]);
const exported = exportsOf(db);
check('lib/db.ts exports select and nothing else', exported.join(',') === 'select', exported.join(','));
check("lib/db.ts runs every query in BEGIN READ ONLY", /\.begin\(\s*'read only'/.test(db));

console.log('forms:');
// Every action redirects on success, and after a redirect to the same route
// Next 14 re-renders the mounted form with useFormState's state undefined.
// Live 2026-09-30: switch off, then on -> "Cannot read properties of undefined
// (reading 'error')". Each form must default the state before reading it.
{
  const forms = code.filter((f) => /useFormState(<[^>]*>)?\(/.test(readFileSync(f, 'utf8')));
  check('found the forms that use useFormState', forms.length >= 4, String(forms.length));
  for (const f of forms) {
    const t = readFileSync(f, 'utf8');
    const m = t.match(/const \[(\w+),\s*\w+\]\s*=\s*useFormState/);
    const name = m?.[1];
    const guarded = name && name !== 'state' && new RegExp(`=\\s*${name}\\s*\\?\\?`).test(t);
    check(`${rel(f)} defaults an undefined form state`, Boolean(guarded), name ? `reads ${name}` : 'pattern not found');
  }
}

console.log('writes (Phase 2 editor, Phase 3 documents):');
// The ONLY writes: one of sixteen SECURITY DEFINER functions, called from two
// action modules, as one exact statement each. Everything else stays refused
// above. The five of migration 004, kb_admin_create_entry (008), the nine
// document functions (011) and kb_admin_match_live (014).
const SIXTEEN = [
  'kb_admin_create_entry', 'kb_admin_discard_draft', 'kb_admin_doc_discard', 'kb_admin_doc_edit_chunk',
  'kb_admin_doc_mark_checked', 'kb_admin_doc_publish', 'kb_admin_doc_restore', 'kb_admin_doc_retire',
  'kb_admin_doc_stage', 'kb_admin_doc_upload', 'kb_admin_match_live', 'kb_admin_match_with_batch',
  'kb_admin_publish', 'kb_admin_restore', 'kb_admin_save_draft', 'kb_admin_toggle',
];
const SIX = SIXTEEN; // the name the checks below grew up with
const ACTION_FILES = ['app/docs/actions.ts', 'app/editor/actions.ts'];
const ROLE_NAMES = new Set(['kb_admin_reader', 'kb_admin_editor']);
const named = new Map();
for (const f of code) {
  for (const m of readFileSync(f, 'utf8').matchAll(/\bkb_admin_\w+/g)) {
    if (!ROLE_NAMES.has(m[0])) named.set(m[0], [...(named.get(m[0]) ?? []), rel(f)]);
  }
}
const strays = [...named.keys()].filter((n) => !SIX.includes(n));
check('the only kb_admin_* functions named are the sixteen', strays.length === 0, strays.map((n) => `${n} in ${named.get(n)[0]}`).join(', '));
const writeTs = src('lib/write.ts');
check('lib/write.ts exports callWrite and nothing else', exportsOf(writeTs).join(',') === 'callWrite', exportsOf(writeTs).join(','));
check('lib/write.ts sends only writeStatement(fn), after isAllowedWrite()',
  /const statement = writeStatement\(fn\);/.test(writeTs) && /if \(!isAllowedWrite\(statement\)/.test(writeTs) &&
  (writeTs.match(/\.unsafe\(/g) ?? []).length === 1 && /\.unsafe\(statement, params( as postgres\.ParameterOrJSON<never>\[\])?\)/.test(writeTs) && !/\.begin\(|sql\(\)`/.test(writeTs));
const pgUsers = code.filter((f) => /from 'postgres'/.test(readFileSync(f, 'utf8'))).map(rel).sort();
check('only lib/db.ts and lib/write.ts open a database connection', pgUsers.join(',') === 'lib/db.ts,lib/write.ts', pgUsers.join(','));
const writeUsers = code.filter((f) => /from '@\/lib\/write'|from '\.\.?\/.*write'/.test(readFileSync(f, 'utf8'))).map(rel);
check('only the two action modules call lib/write.ts', writeUsers.sort().join(',') === ACTION_FILES.join(','), writeUsers.join(','));
const actionsSrc = src('app/editor/actions.ts');
for (const file of ACTION_FILES) {
  const text = src(file);
  check(`${file} is a 'use server' module`, /^'use server';/.test(text));
  const fns = [...text.matchAll(/export async function (\w+)\([^)]*\)[^{]*\{\s*\n\s*(.*)/g)];
  const unguarded = fns.filter((m) => m[2].trim() !== 'const auth = await actorForWrite();').map((m) => m[1]);
  const expected = file === 'app/editor/actions.ts' ? 6 : 9;
  check(`every server action in ${file} checks the session and role first (actorForWrite)`, fns.length === expected && unguarded.length === 0,
    `${fns.length} action(s)${unguarded.length ? `; unguarded: ${unguarded.join(', ')}` : ''}`);
}
const docActions = src('app/docs/actions.ts');
const bodyOf = (fn) => {
  const start = docActions.indexOf(`export async function ${fn}`);
  const next = docActions.indexOf('export async function', start + 10);
  return start < 0 ? '' : docActions.slice(start, next < 0 ? undefined : next);
};
for (const fn of ['publishBatch', 'restoreBatch', 'retireDocument']) {
  const body = bodyOf(fn);
  check(`${fn} checks canApprove before calling the database`,
    body.indexOf('actor.canApprove') > 0 && body.indexOf('actor.canApprove') < body.indexOf('callWrite('));
}
for (const fn of ['previewDocument', 'runImpactCheck']) {
  const calls = [...bodyOf(fn).matchAll(/callWrite[^(]*\(\s*'(\w+)'/g)].map((m) => m[1]);
  check(`${fn} calls no function that writes`, calls.every((c) => c.startsWith('kb_admin_match_')), calls.join(',') || 'none');
}
const otherServer = code.filter((f) => /^'use server';/.test(readFileSync(f, 'utf8'))).map(rel).sort();
check("no other 'use server' module than sign-in, the editor and documents", otherServer.join(',') === 'app/docs/actions.ts,app/editor/actions.ts,app/login/actions.ts', otherServer.join(','));
const toggleSrc = actionsSrc.slice(actionsSrc.indexOf('export async function toggleEntry'));
check('switching on/off also checks canApprove before calling the database',
  toggleSrc.indexOf('actor.canApprove') > 0 && toggleSrc.indexOf('actor.canApprove') < toggleSrc.indexOf("callWrite('kb_admin_toggle'"));

const pages = code.filter((f) => /^app\/.*page\.tsx$/.test(rel(f)) && rel(f) !== 'app/login/page.tsx');
for (const p of pages) check(`${rel(p)} requires a viewer`, /requireViewer(With)?\(/.test(readFileSync(p, 'utf8')));
const apis = code.filter((f) => /^app\/api\/.*route\.ts$/.test(rel(f)));
for (const a of apis) check(`${rel(a)} checks the session`, readFileSync(a, 'utf8').includes('checkRequest('));
check('found pages and API routes to check', pages.length >= 6 && apis.length >= 1, `${pages.length} pages, ${apis.length} api`);

console.log('sessions:');
const authTs = src('lib/auth.ts');
check('requireViewer redirects through denialPath()', /redirect\(\s*denialPath\(\s*result\.reason\s*\)\s*\)/.test(authTs));
check('requireViewer has no other redirect', (authTs.match(/redirect\(/g) ?? []).length === 1);
const offRoute = src('app/login/switched-off/route.ts');
check('app/login/switched-off/route.ts exists and exports GET', /export\s+(?:async\s+)?function\s+GET\b/.test(offRoute));
const clearedList = (offRoute.match(/for \(const name of \[([^\]]*)\]\)/)?.[1] ?? '').split(',').map((s) => s.trim()).sort().join(',');
check('the switched-off route clears all three session cookies',
  clearedList === 'ACCESS_COOKIE,DEADLINE_COOKIE,REFRESH_COOKIE' && /cookies\.set\(\s*name,\s*'',\s*cookieOptions\(0\)\s*\)/.test(offRoute),
  clearedList || 'no cookie loop found');
check('the switched-off route lands on /login?e=inactive', offRoute.includes("'/login?e=inactive'"));
check('the switched-off route reads nothing and calls nothing', !/fetch\(|select\(|signOut\(|from '@\/lib\/(db|queries|auth)'/.test(offRoute));
check('middleware skips /login/... (so the route runs without a session)', /\(\?!login\|/.test(src('middleware.ts')));
const sessionTs = src('lib/session.ts');
check('sign-out stays at local scope', sessionTs.includes('/auth/v1/logout?scope=local') && !/scope=global/.test(sessionTs));
const actionsTs = src('app/login/actions.ts');
check('a refused fresh sign-in still gets the one generic message', /return \{ error: GENERIC \}/.test(actionsTs) && !/switched off/i.test(actionsTs));

console.log('fonts:');
const layout = src('app/layout.tsx');
check('layout uses next/font/local', /from 'next\/font\/local'/.test(layout));
check('nothing imports next/font/google', !code.some((f) => /next\/font\/google/.test(readFileSync(f, 'utf8'))));
check('no source file names a Google Fonts host', !code.some((f) => /fonts\.(googleapis|gstatic)\.com/.test(readFileSync(f, 'utf8'))));
const fontPaths = [...layout.matchAll(/path:\s*'(\.\/fonts\/[^']+)'/g)].map((m) => m[1]);
check('layout names the six font files', fontPaths.length === 6, fontPaths.length + ' file(s)');
const missingFonts = fontPaths.filter((p) => !existsSync(join(ROOT, 'app', p)));
check('every font file the layout names exists', fontPaths.length > 0 && missingFonts.length === 0, missingFonts.join(', '));
check('every font file is woff2',
  fontPaths.length > 0 && fontPaths.every((p) => existsSync(join(ROOT, 'app', p)) && readFileSync(join(ROOT, 'app', p)).subarray(0, 4).toString('latin1') === 'wOF2'));
check('app/fonts/OFL.txt carries the SIL Open Font License 1.1', /SIL OPEN FONT LICENSE Version 1\.1/.test(src('app/fonts/OFL.txt')));

console.log('secrets:');
let tracked = '';
try {
  tracked = execFileSync('git', ['ls-files', '--', '.env.local', '.env', '.env.test.local'], { cwd: ROOT, encoding: 'utf8' }).trim();
} catch {
  tracked = '(git unavailable)';
}
check('.env / .env.local / .env.test.local are not tracked by git', tracked === '', tracked);
const SECRET_KEYS = /^\s*(SUPABASE_ANON_KEY|ADMIN_PREVIEW_SECRET|KB_ADMIN_DB_PASSWORD|KB_ADMIN_DB_PASSWORD_FILE|KB_ADMIN_DB_PASSWORD_EDITOR|KB_ADMIN_DB_PASSWORD_EDITOR_FILE|KB_ADMIN_EMBEDDING_API_KEY)\s*=\s*(.*)$/;
for (const envName of ['.env.local', '.env.test.local']) {
  const envFile = join(ROOT, envName);
  if (!existsSync(envFile)) {
    console.log(`  (no ${envName} - its secret leak check skipped)`);
    continue;
  }
  const secrets = [];
  for (const line of readFileSync(envFile, 'utf8').split(/\r?\n/)) {
    const m = line.match(SECRET_KEYS);
    if (!m || !m[2].trim()) continue;
    if (m[1].endsWith('_FILE')) {
      if (existsSync(m[2].trim())) secrets.push([`${m[1].replace(/_FILE$/, '')} (from its file)`, readFileSync(m[2].trim(), 'utf8').trim()]);
    } else secrets.push([m[1], m[2].trim()]);
  }
  const others = files.filter((f) => f !== envFile && !/\.env(\.test)?\.local$/.test(f));
  for (const [name, value] of secrets) {
    if (value.length < 8) continue;
    const leaks = others.filter((f) => readFileSync(f, 'utf8').includes(value));
    check(`${envName}: ${name} appears in no other kb-admin file`, leaks.length === 0, leaks.map(rel).join(', '));
  }
  check(`checked the secrets held in ${envName}`, secrets.length >= 2, `${secrets.length} value(s), not shown`);
}

console.log('behaviour:');
const { validateConfig } = await import(pathToFileURL(join(ROOT, 'lib/config.ts')).href);
const { decideAccess, denialPath, isSingleRead, secondsLeft, canEdit, canApprove, isAllowedWrite, writeStatement, WRITE_FUNCTIONS, ROW_FUNCTIONS } =
  await import(pathToFileURL(join(ROOT, 'lib/access.ts')).href);
const E = await import(pathToFileURL(join(ROOT, 'lib/editing.ts')).href);

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
{
  // Dummy values only. The env var wins over the file (Vercel has no file);
  // with the env var empty the file is read exactly as before.
  const DUMMY_ENV = 'dummy-env-password-not-real';
  const DUMMY_FILE = 'dummy-file-password-not-real';
  const reads = [];
  const fakeFile = (p) => {
    reads.push(p);
    return `${DUMMY_FILE}\n`;
  };
  const pw = (overrides) => {
    try {
      return validateConfig({ ...good, ...overrides }, fakeFile).db.password;
    } catch (e) {
      return `ERR:${e.message}`;
    }
  };
  check('KB_ADMIN_DB_PASSWORD is used when set', pw({ KB_ADMIN_DB_PASSWORD: DUMMY_ENV, KB_ADMIN_DB_PASSWORD_FILE: '' }) === DUMMY_ENV);
  reads.length = 0;
  check('...and wins over KB_ADMIN_DB_PASSWORD_FILE without reading the file',
    pw({ KB_ADMIN_DB_PASSWORD: DUMMY_ENV, KB_ADMIN_DB_PASSWORD_FILE: 'C:/dummy/path.pw' }) === DUMMY_ENV && reads.length === 0);
  check('with it empty, KB_ADMIN_DB_PASSWORD_FILE is read as before',
    pw({ KB_ADMIN_DB_PASSWORD: '', KB_ADMIN_DB_PASSWORD_FILE: 'C:/dummy/path.pw' }) === DUMMY_FILE);
  const neither = pw({ KB_ADMIN_DB_PASSWORD: '', KB_ADMIN_DB_PASSWORD_FILE: '' });
  check('with neither set, the error names both variables',
    neither.startsWith('ERR:') && neither.includes('KB_ADMIN_DB_PASSWORD ') && neither.includes('KB_ADMIN_DB_PASSWORD_FILE'), neither);
  const err = pw({ KB_ADMIN_DB_PASSWORD: '', KB_ADMIN_DB_PASSWORD_FILE: '' }) + pw({ KB_ADMIN_DB_PASSWORD_FILE: '', SUPABASE_URL: 'x' });
  check('no error message ever carries the password', !err.includes(DUMMY_ENV) && !err.includes('x'.repeat(24)));
}
check('refuses a plain-http preview URL off localhost', refuses({ BOT_PREVIEW_URL: 'http://example.com' }));
check('refuses a short preview secret', refuses({ ADMIN_PREVIEW_SECRET: 'short' }));
check('refuses a malformed expected ref', refuses({ KB_ADMIN_EXPECTED_REF: 'x' }));

check('no membership row -> denied', decideAccess(undefined).ok === false);
check('inactive row -> denied', decideAccess({ role: 'viewer', active: false }).ok === false);
check('unknown role -> denied', decideAccess({ role: 'admin', active: true }).ok === false);
check('active viewer -> allowed', decideAccess({ role: 'viewer', active: true }).ok === true);
check('active editor and approver -> allowed, with their role',
  decideAccess({ role: 'editor', active: true }).role === 'editor' && decideAccess({ role: 'approver', active: true }).role === 'approver');
check('inactive approver -> denied', decideAccess({ role: 'approver', active: false }).ok === false);
check('a viewer cannot edit; an editor can; only an approver approves',
  !canEdit('viewer') && canEdit('editor') && canEdit('approver') && !canApprove('viewer') && !canApprove('editor') && canApprove('approver'));

check('switched off -> via /login/switched-off (clears the cookies)', denialPath('inactive') === '/login/switched-off');
check('no access -> straight to /login?e=no-access', denialPath('no-access') === '/login?e=no-access');
check('bad session -> straight to /login?e=bad-session', denialPath('bad-session') === '/login?e=bad-session');
check('no session -> straight to /login?e=no-session', denialPath('no-session') === '/login?e=no-session');

check('a single SELECT is a read', isSingleRead('select 1'));
check('a WITH ... SELECT is a read', isSingleRead('with x as (select 1) select * from x'));
check('two statements are refused', !isSingleRead('select 1; select 2'));
check('a statement not starting with SELECT/WITH is refused', !isSingleRead('truncate t'));

const now = 1_000_000;
check('token expiry read', secondsLeft(jwt({ exp: now + 300 }), now) === 300);
check('garbage token reads as expired', secondsLeft('garbage', now) < 0);

console.log('behaviour - writes:');
check('exactly the sixteen functions are allowed', Object.keys(WRITE_FUNCTIONS).sort().join(',') === SIX.join(','), Object.keys(WRITE_FUNCTIONS).sort().join(','));
check('each of the sixteen statements is allowed', SIX.every((fn) => isAllowedWrite(writeStatement(fn))));
const bound = (fn) => WRITE_FUNCTIONS[fn].map((t, i) => `$${i + 1}::${t}`).join(', ');
check('every statement binds every value ($n) and splices nothing',
  SIX.every((fn) => writeStatement(fn) === (ROW_FUNCTIONS.includes(fn)
    ? `select coalesce(jsonb_agg(to_jsonb(m)), '[]'::jsonb) as result from public.${fn}(${bound(fn)}) m`
    : `select public.${fn}(${bound(fn)}) as result`)));
check('only the two searches return rows', ROW_FUNCTIONS.slice().sort().join(',') === 'kb_admin_match_live,kb_admin_match_with_batch');
// Built from pieces so this file itself names no write verb.
const verb = (a, b) => a + b;
const refusedWrites = [
  'select public.kb_admin__discard($1::uuid) as result',
  'select public.kb_admin__ensure_baseline($1::uuid) as result',
  `${writeStatement('kb_admin_publish')}; select 1`,
  `${writeStatement('kb_admin_toggle')} `,
  writeStatement('kb_admin_toggle').toUpperCase(),
  'select public.kb_admin_publish($1, $2, $3, $4, $5) as result',
  'with x as (select 1) select public.kb_admin_toggle($1::uuid, $2::text, $3::uuid, $4::boolean, $5::text) as result',
  `${verb('in', 'sert')} into public.cb_kb_audit default values`,
  `${verb('up', 'date')} public.cb_knowledge_base_updated set is_active = false`,
  `${verb('de', 'lete')} from public.cb_kb_entry_versions`,
  'select 1',
];
check('anything else is refused (internal helpers, a second statement, raw writes, reads)', refusedWrites.every((w) => !isAllowedWrite(w)),
  refusedWrites.filter((w) => isAllowedWrite(w)).join(' | '));
// The code's sixteen signatures are the sixteen the database grants to
// kb_admin_editor: five in 005, one in 008, nine in 011, one in 014 (014 also
// grants kb_admin_doc_publish again, which it replaces).
const sqlDir = resolve(ROOT, '..', 'scripts', 'sql');
const grantFiles = ['kb_admin_005_editor_login.sql', 'kb_admin_008_create_entry.sql', 'kb_admin_011_doc_functions.sql', 'kb_admin_014_live_match_baseline.sql'];
if (grantFiles.every((f) => existsSync(join(sqlDir, f)))) {
  const norm = (t) => t.replace(/\s+/g, '').replace(/float8\[\]/g, 'doubleprecision[]').replace(/doubleprecision/g, 'double precision');
  const sig = (m) => `${m[1]}(${norm(m[2])})`;
  const granted = new Set();
  for (const m of readFileSync(join(sqlDir, grantFiles[0]), 'utf8').matchAll(/public\.(kb_admin_\w+)\(([^)]*)\)/g)) granted.add(sig(m));
  for (const f of grantFiles.slice(1)) {
    for (const block of readFileSync(join(sqlDir, f), 'utf8').matchAll(/grant execute on function([\s\S]*?)to kb_admin_editor/g)) {
      for (const m of block[1].matchAll(/public\.(kb_admin_\w+)\(([^)]*)\)/g)) granted.add(sig(m));
    }
  }
  const ours = SIX.map((fn) => `${fn}(${norm(WRITE_FUNCTIONS[fn].join(','))})`).sort().join(' ');
  const theirs = [...granted].sort().join(' ');
  check('the sixteen signatures match the kb_admin_editor grants (005, 008, 011, 014)', theirs === ours, theirs);
} else {
  console.log('  (grant SQL files not found - grant comparison skipped)');
}

console.log('behaviour - editor settings:');
const editorOn = { ...good, KB_ADMIN_DB_PASSWORD_EDITOR: 'e'.repeat(24), KB_ADMIN_EMBEDDING_API_KEY: 'sk-dummy-not-a-real-key' };
const cfgOf = (o) => validateConfig(o, noFile);
check("no editor settings -> editor off, no warning (today's read-only site)", cfgOf(good).editor === null && cfgOf(good).editorOffReason === '');
{
  const half = cfgOf({ ...good, KB_ADMIN_DB_PASSWORD_EDITOR: 'e'.repeat(24) });
  check('half the editor settings -> still starts, editor off, reason names what is missing',
    half.editor === null && half.editorOffReason.includes('KB_ADMIN_EMBEDDING_API_KEY') && !half.editorOffReason.includes('e'.repeat(24)));
}
{
  const on = cfgOf(editorOn);
  check('all editor settings -> editor on as kb_admin_editor.<the same ref>', on.editor?.db.user === `kb_admin_editor.${REF}`);
  check('the embedding endpoint defaults to OpenAI', on.editor?.embedding.baseUrl === 'https://api.openai.com/v1');
}
check('refuses an unknown embedding endpoint', refuses({ ...editorOn, KB_ADMIN_EMBEDDING_BASE_URL: 'https://example.com/v1' }));
check('refuses an OpenRouter key sent to OpenAI', refuses({ ...editorOn, KB_ADMIN_EMBEDDING_API_KEY: 'sk-or-v1-dummy' }));
check('refuses a non-OpenRouter key sent to OpenRouter', refuses({ ...editorOn, KB_ADMIN_EMBEDDING_BASE_URL: 'https://openrouter.ai/api/v1' }));
check('accepts an OpenRouter key with the OpenRouter endpoint',
  !refuses({ ...editorOn, KB_ADMIN_EMBEDDING_BASE_URL: 'https://openrouter.ai/api/v1', KB_ADMIN_EMBEDDING_API_KEY: 'sk-or-v1-dummy' }));
check("refuses the reader's password reused for the editor", refuses({ ...editorOn, KB_ADMIN_DB_PASSWORD_EDITOR: 'x'.repeat(24) }));
check('the editor password can come from KB_ADMIN_DB_PASSWORD_EDITOR_FILE', (() => {
  try {
    const c = validateConfig({ ...editorOn, KB_ADMIN_DB_PASSWORD_EDITOR: '', KB_ADMIN_DB_PASSWORD_EDITOR_FILE: 'C:/dummy/editor.pw' }, () => 'f'.repeat(24));
    return c.editor?.db.password === 'f'.repeat(24);
  } catch {
    return false;
  }
})());
check('a mixed project still refuses with the editor on', refuses({ ...editorOn, SUPABASE_URL: 'https://zzzzzzzzzzzzzzzzzzzz.supabase.co' }));

console.log('behaviour - editing rules:');
const Q = 'Q?';
check('length: 1149 ok, 1150 warns, 1200 warns, 1201 blocks',
  E.lengthState(Q, 'a'.repeat(1149 - 3), 0).level === 'ok' && E.lengthState(Q, 'a'.repeat(1150 - 3), 0).level === 'warn' &&
  E.lengthState(Q, 'a'.repeat(1200 - 3), 0).level === 'warn' && E.lengthState(Q, 'a'.repeat(1201 - 3), 0).level === 'block');
check('length: an entry already over the limit may be shortened, never grown',
  E.lengthState(Q, 'a'.repeat(1300), 1400).level !== 'block' && E.lengthState(Q, 'a'.repeat(1300), 1250).level === 'block');
check('length counts characters as Postgres does (an emoji is one)', E.charCount('\u{1F600}') === 1);
check('NRIC/FIN spotted, ordinary codes not', E.hasNric('her FIN is G1234567X') && !E.hasNric('S$650 for 24 months'));
check('numbers: "$1,588" equals "$1588"; "300" -> "$300" is a change',
  E.numbersIn('$1,588').join() === E.numbersIn('$1588').join() && E.numbersIn('300').join() !== E.numbersIn('$300').join());
const base = { question: 'How much?', answer: 'The fee is $450.', section_heading: null, service: 'renewal', audience: 'all', nationality: 'all', active: true };
check('Option C: wording only needs no approver', E.approvalReasons(base, { ...base, answer: 'The agency fee is $450.' }).length === 0);
check('Option C: a number, the service, the audience, the nationality, on/off each need one',
  E.approvalReasons(base, { ...base, answer: 'The fee is $495.' }).join() === 'numbers' &&
  E.approvalReasons(base, { ...base, service: 'home_leave' }).join() === 'service' &&
  E.approvalReasons(base, { ...base, audience: 'employer' }).join() === 'audience' &&
  E.approvalReasons(base, { ...base, nationality: 'PH' }).join() === 'nationality' &&
  E.approvalReasons(base, { ...base, active: false }).join() === 'on_off');
const msgs = ['KB001', 'KB002', 'KB003', 'KB004', 'KB005', 'KB006'].map((c) => E.errorMessage(c, 'detail'));
check('KB001-KB006 each have their own plain message', new Set(msgs).size === 6 && msgs.every((m) => m.length > 20 && !/KB00/.test(m)));
check('KB004 says an approver is needed', /needs an approver/.test(E.errorMessage('KB004', '')));
check('an unknown error never shows the database text', !E.errorMessage('42P01', 'relation secret_table does not exist').includes('secret_table'));
{
  const v = Array.from({ length: 1536 }, (_, i) => Math.sin(i + 1));
  const b64 = Buffer.from(new Float32Array(v).buffer).toString('base64');
  const back = E.decodeEmbedding(b64);
  check('base64 float32 embeddings decode to 1536 numbers', back?.length === 1536 && Math.abs(back[7] - v[7]) < 1e-6);
  check('the canary matches the same vector after float rounding', E.canaryMatches(back, E.parseVector(E.vectorLiteral(v))).ok);
  const other = Array.from({ length: 1536 }, (_, i) => Math.cos(i * 3));
  check('the canary refuses a different vector', !E.canaryMatches(other, v).ok);
  check('the canary refuses a wrong length', !E.canaryMatches(v.slice(0, 512), v).ok);
}
{
  const d = E.wordDiff('The fee is $450 today.', 'The fee is $495 today.');
  check('the word diff marks exactly the changed word',
    d.before.filter((x) => x.kind === 'del').map((x) => x.text.trim()).join() === '$450' &&
    d.after.filter((x) => x.kind === 'ins').map((x) => x.text.trim()).join() === '$495');
}

console.log('behaviour - document chunker (Phase 3 scope B):');
{
  const pkg = JSON.parse(src('package.json'));
  check('mammoth is a runtime dependency', Boolean(pkg.dependencies?.mammoth), pkg.dependencies?.mammoth ?? 'missing');
  check('no PDF library is a dependency', !Object.keys({ ...pkg.dependencies, ...pkg.devDependencies }).some((d) => /pdf/i.test(d)));
  const C = await import(pathToFileURL(join(ROOT, 'lib/chunker.ts')).href);
  check('lib/chunker.ts exports chunkDocument', typeof C.chunkDocument === 'function');
  check('the limits are the bot\'s own (1200 passage / 3000 table)', C.MAX_PASSAGE_CHARS === 1200 && C.MAX_TABLE_CHARS === 3000);
  check('only docx, md and txt are accepted', C.FILE_TYPES.join(',') === 'docx,md,txt');
  let refused = false;
  try {
    await C.chunkDocument(Buffer.from('%PDF-1.7'), 'pdf', 'x.pdf');
  } catch (e) {
    refused = e instanceof C.ChunkerError;
  }
  check('a PDF is refused', refused);

  const sentence = 'The helper must be given one rest day every week by law. ';
  const md = [
    '# Title', '', 'Intro text.', '', '## Rest days', '', sentence.repeat(30).trim() + '[cite:12]', '',
    '## Fees', '', '| Item | Amount |', '|---|---|', '| Agency fee | $1,428 |', '| Insurance | $590 |', '',
  ].join('\n');
  const chunks = await C.chunkDocument(Buffer.from(md), 'md', 'test.md');
  const passages = chunks.filter((c) => c.chunk_type === 'passage');
  const table = chunks.find((c) => c.chunk_type === 'table_unit');
  check('no passage is over 1,200 characters', passages.every((c) => c.content.length <= 1200), passages.map((c) => c.content.length).join(','));
  check('a long paragraph splits at a sentence end', passages.filter((c) => c.section_heading === 'Rest days').length >= 2 &&
    passages.filter((c) => c.section_heading === 'Rest days').every((c) => c.content.endsWith('.')));
  check('[cite:N] markers are removed', !chunks.some((c) => /\[cite:/.test(c.content)));
  check('section_heading is the nearest heading, not the title', passages.some((c) => c.section_heading === 'Rest days') && chunks.find((c) => c.content === 'Intro text.')?.section_heading === 'Title');
  check('a table is one table_unit in the import\'s format',
    table?.content === 'Fees\nItem: Agency fee | Amount: $1,428\nItem: Insurance | Amount: $590' && table.metadata.table_column === 'Item, Amount');
  check('figures set figures_present and the suggested floor', table?.metadata.figures_present === true && table.suggested_rag_score_floor === 0.42 &&
    chunks.find((c) => c.content === 'Intro text.')?.suggested_rag_score_floor === null);
  check('metadata is the import\'s six keys, managed_by ui', chunks.every((c) =>
    Object.keys(c.metadata).sort().join(',') === 'date_valid_from,figures_present,keywords,managed_by,priority,table_column' &&
    c.metadata.managed_by === 'ui' && Array.isArray(c.metadata.keywords) && c.metadata.priority === 3));
  check('ordinals run 1..n', chunks.every((c, i) => c.ordinal === i + 1));

  const rows = Array.from({ length: 60 }, (_, i) => `| ${i + 1} | ${'Do the work carefully and well. '.repeat(3).trim()} |`);
  const big = await C.chunkDocument(Buffer.from(['## Code', '', '| # | Rule |', '|---|---|', ...rows].join('\n')), 'md', 'big.md');
  check('a table over 3,000 characters splits by rows, heading line repeated', big.length >= 2 &&
    big.every((c) => c.chunk_type === 'table_unit' && c.content.length <= 3000 && c.content.startsWith('Code\n#: ')));

  const txt = await C.chunkDocument(Buffer.from('First block.\n\nSecond block.'), 'txt', 't.txt');
  check('.txt: blank-line blocks, no headings', txt.length === 1 && txt[0].section_heading === null && txt[0].content === 'First block.\n\nSecond block.');
  let notUtf8 = false;
  try {
    await C.chunkDocument(Buffer.from([0xff, 0xfe, 0x41]), 'txt', 'bad.txt');
  } catch (e) {
    notUtf8 = e instanceof C.ChunkerError;
  }
  check('non-UTF-8 text is refused', notUtf8);
  check('the chunker reaches no database, network or environment',
    !/from '(postgres|\.\/db|\.\/write|\.\/env|\.\/embed)'|fetch\(|process\.env/.test(src('lib/chunker.ts')));

  // Headings: parent + " — " + nearest, two levels only; the nearest alone with no parent.
  const long = 'She keeps her own passport and nobody may hold it for her. '.repeat(25).trim();
  const h = await C.chunkDocument(Buffer.from([
    '# Module', '', '## 27.3 Your Passport', '', '### What is OK', '', '- You keep your passport.', '',
    '### What is NOT OK', '', long, '', '## Overview', '', 'Short text.', '',
  ].join('\n')), 'md', 'h.md');
  check('the heading join is " — "', C.HEADING_JOIN === ' — ' && C.combineHeading('A', 'B') === 'A — B' &&
    C.combineHeading(null, 'B') === 'B' && C.combineHeading('A', 'A') === 'A');
  check('section_heading is "parent — nearest"', h.some((c) => c.section_heading === '27.3 Your Passport — What is OK') &&
    h.some((c) => c.section_heading === '27.3 Your Passport — What is NOT OK'), h.map((c) => c.section_heading).join(' | '));
  check('only two levels, and never the document title as parent', !h.some((c) => (c.section_heading ?? '').split(C.HEADING_JOIN).length > 2) &&
    !h.some((c) => (c.section_heading ?? '').startsWith('Module')) && h.some((c) => c.section_heading === 'Overview'));

  // Packing: consecutive small sections under one parent share a chunk headed by the parent, never over 1,200.
  const subs = Array.from({ length: 12 }, (_, i) => [`### Point ${i + 1}`, '', `Rule ${i + 1}: ${'keep the house clean and tidy. '.repeat(4).trim()}`, '']).flat();
  const packed = await C.chunkDocument(Buffer.from(['## House rules', '', ...subs, '## Other', '', 'Unrelated.'].join('\n')), 'md', 'p.md');
  const rulesChunks = packed.filter((c) => c.section_heading === 'House rules');
  check('small sections under one parent are packed, headed by the parent', rulesChunks.length >= 2 && rulesChunks.length < 12 &&
    rulesChunks[0].content.startsWith('Point 1\nRule 1:'), `${rulesChunks.length} chunk(s)`);
  check('packing never exceeds 1,200 characters, and keeps every section', packed.every((c) => c.content.length <= 1200) &&
    Array.from({ length: 12 }, (_, i) => `Point ${i + 1}\n`).every((p) => rulesChunks.some((c) => c.content.includes(p))));
  check('a section under a different parent is not packed in', packed.some((c) => c.section_heading === 'Other' && c.content === 'Unrelated.'));

  // Lost facts: what the live rows state that the new chunks would drop.
  const F = await import(pathToFileURL(join(ROOT, 'lib/facts.ts')).href);
  const lost = F.findLostFacts(
    ['WhatsApp us on +65 6111 2222. A transfer takes about 1 to 2 weeks. From S$670/month.', 'Fee $1,428, 50% refund.[cite:49]'],
    ['WhatsApp us on +65 6999 8888. A transfer takes about 1-2 weeks. From $670 per month.', 'Fee $1,428, 50% refund.'],
  );
  const facts = lost.map((f) => `${f.kind}:${f.fact}`).sort().join(',');
  check('findLostFacts catches a changed phone number', facts === 'phone:61112222', facts || 'nothing found');
  check('findLostFacts treats "1 to 2 weeks" = "1-2 weeks" and "S$670" = "$670", and ignores [cite:N]', !/span|money|number:49/.test(facts));
  const lost2 = F.findLostFacts(['Allow about 4 to 6 weeks; the bond is S$5,000.'], ['Allow about 6-8 weeks; the bond is S$5,000.']);
  check('findLostFacts catches a changed time span', lost2.length === 1 && lost2[0].kind === 'span' && lost2[0].fact === '4-6 weeks',
    lost2.map((f) => `${f.kind}:${f.fact}`).join(','));
  check('lib/facts.ts reaches no database, network or environment',
    !/from '(postgres|\.\/db|\.\/write|\.\/env|\.\/embed)'|fetch\(|process\.env/.test(src('lib/facts.ts')));
}

console.log('behaviour - document upload rules and the impact check (Phase 3 scope B, session 3):');
{
  const D = await import(pathToFileURL(join(ROOT, 'lib/documents.ts')).href);
  check('the upload cap is 4 MB', D.MAX_UPLOAD_BYTES === 4 * 1024 * 1024 && D.fileProblem('a.md', 4 * 1024 * 1024) === null &&
    /4 MB/.test(D.fileProblem('a.md', 4 * 1024 * 1024 + 1) ?? ''));
  check('a PDF is refused before upload', /PDF/.test(D.fileProblem('report.pdf', 100) ?? ''));
  check('only .docx, .md and .txt upload', D.fileProblem('a.docx', 9) === null && D.fileProblem('a.txt', 9) === null && D.fileProblem('a.doc', 9) !== null);
  const refusedNames = ['MingHwee Hiring Pipelines Brief.docx', 'MHOS-100_Product_Blueprint', 'Notes for Vendor.md', 'Some Internal Notes',
    'Ming Hwee Service Notes', 'ming_hwee-kb  admin', ''];
  check('internal, reserved and empty source names are refused (the database rules)', refusedNames.every((n) => D.sourceProblem(n) !== null),
    refusedNames.filter((n) => D.sourceProblem(n) === null).join(' | '));
  check('ordinary source names are accepted', ['KB admin test document.md', 'Ming_Hwee_Helper_Expectation_Checklist.docx'].every((n) => D.sourceProblem(n) === null));
  check('chunks are embedded as heading + newline + content', D.embeddingText('Fees', 'Agency fee $1,428') === 'Fees\nAgency fee $1,428' &&
    D.embeddingText(null, 'x') === 'x');
  check('staff wording is flagged, client wording is not', D.internalMarkers('Owner: Sales | SLA 24h').length === 2 &&
    D.internalMarkers('The agency fee is $1,428 and your helper starts in 4 to 6 weeks.').length === 0);
  check('figures are found for highlighting', D.figureParts('Fee $1,428 in 2 weeks').filter((p) => p.figure).map((p) => p.text).join('|') === '$1,428|2');
  const w = D.factWarnings([{ section_heading: 'A', content: 'Call +65 6111 2222 within 1 to 2 weeks.' }],
    [{ section_heading: 'A', content: 'Call +65 6999 8888 within 2-3 weeks.' }]);
  check('lost and stale facts both found', w.lost.some((f) => f.fact === '61112222') && w.stale.some((f) => f.fact === '2-3 weeks'),
    `${w.lost.map((f) => f.fact)} / ${w.stale.map((f) => f.fact)}`);

  const R = await import(pathToFileURL(join(ROOT, 'lib/retrieval.ts')).href);
  const rows = [
    { id: 'a', similarity: 0.6, source_document: 'MingHwee Hiring Pipelines Brief.docx', metadata: {} },
    { id: 'b', similarity: 0.55, source_document: 'x.md', metadata: { priority: 3 } },
    { id: 'c', similarity: 0.53, source_document: 'x.md', metadata: { priority: 1 } },
    { id: 'd', similarity: 0.41, source_document: 'x.md', rag_score_floor: '0.420' },
    { id: 'e', similarity: 0.5, source_document: 'x.md', chunk_type: 'style_example' },
  ];
  check('the bot\'s rerank: internal, style and below-floor rows dropped, priority 1 lifted', R.botTop(rows).map((r) => r.id).join('') === 'cb',
    R.botTop(rows).map((r) => r.id).join(''));
  check('the search over-fetches 10 for 5, threshold 0.35, soft floor 0.40', R.FETCH_COUNT === 10 && R.MATCH_COUNT === 5 &&
    R.MATCH_THRESHOLD === 0.35 && R.SOFT_FLOOR === 0.4);
  const row = (id, s, figures = false) => ({ id, similarity: s, figures, source_document: 'x', section_heading: null, chunk_type: 'document_chunk', from_batch: false, preview: '' });
  const cmp = R.compareProbe({ question: 'q', service: null, audience: null, nationality: null },
    [row('a', 0.6), row('b', 0.5)], [row('a', 0.39), row('n', 0.38, true)]);
  check('the comparison finds rows entering and leaving, a weak best score, a new figure row',
    cmp.entering.join() === 'n' && cmp.leaving.join() === 'b' && cmp.weakAfter && cmp.newFigures.join() === 'n');
}

console.log('behaviour - small fixes (2026-10-05):');
{
  const D = await import(pathToFileURL(join(ROOT, 'lib/documents.ts')).href);
  const C = await import(pathToFileURL(join(ROOT, 'lib/chunker.ts')).href);
  const P = await import(pathToFileURL(join(ROOT, 'lib/plural.ts')).href);

  // 1. Namespace: "Usual for the service" is a fixed map, shown, and sent per chunk.
  const live = ['candidate_guidance', 'company_info', 'emergency', 'faq', 'fees', 'hiring_process', 'mdw_rights', 'mom_regulations', 'services_general'];
  check('namespace: General is services_general, hiring is hiring_process, fees is fees (not mom_regulations)',
    D.usualNamespace('general', live) === 'services_general' && D.usualNamespace('new_hiring', live) === 'hiring_process' &&
    D.usualNamespace('fee_enquiry', live) === 'fees' && D.usualNamespace('passport_renewal', live) === 'mom_regulations',
    ['general', 'new_hiring', 'fee_enquiry', 'passport_renewal'].map((s) => D.usualNamespace(s, live)).join(','));
  check('namespace: an unmapped service or a namespace no live row uses falls back to services_general, else the database default',
    D.usualNamespace('insurance', live) === 'services_general' && D.usualNamespace('general', ['mom_regulations']) === '');
  check('namespace: prepare sends the usual namespace of each chunk\'s own service, and the form shows it',
    /namespace: s\.namespace \|\| usualNamespace\(p\.service, s\.spaces\)/.test(src('app/docs/actions.ts')) &&
    /Usual for the service \(\{usual/.test(src('app/documents/upload/UploadFlow.tsx')) &&
    /Will use:/.test(src('app/documents/upload/UploadFlow.tsx')));

  // 2. A lone heading: every chunk's section_heading, and no "#" left in the text.
  const one = await C.chunkDocument(Buffer.from([
    'Preamble before the title.', '', '# KB admin test', '', 'The code word is mango.', '', '| Item | Amount |', '|---|---|', '| Fee | $123 |', '',
  ].join('\n')), 'md', 'one.md');
  check('lone heading: it is every chunk\'s section_heading (never "(no heading)")', one.length === 3 && one.every((c) => c.section_heading === 'KB admin test'),
    one.map((c) => c.section_heading).join(' | '));
  const variants = await Promise.all(['  # KB admin test\n\nText.\n', '#KB admin test\n\nText.\n', '# KB admin test\nText.\n']
    .map((m) => C.chunkDocument(Buffer.from(m), 'md', 'v.md')));
  check('lone heading: no markdown "#" marker left in any chunk (indented, no-space, no blank line)',
    variants.every((v) => v.length === 1 && v[0].section_heading === 'KB admin test' && v[0].content === 'Text.') &&
    ![...one, ...variants.flat()].some((c) => /^\s{0,3}#/m.test(c.content)), JSON.stringify(variants.map((v) => v.map((c) => c.content))));
  const notHeading = await C.chunkDocument(Buffer.from('#1 priority is safety.\n'), 'md', 'n.md');
  check('"#1 priority" stays text (a digit after "#" is not a heading)', notHeading[0]?.content === '#1 priority is safety.');
  check('docx: Word\'s Title style is mapped to a heading', /p\[style-name='Title'\] => h1/.test(src('lib/chunker.ts')) &&
    /convertToHtml\(\{ buffer: bytes \}, \{ styleMap: DOCX_STYLE_MAP \}\)/.test(src('lib/chunker.ts')));

  // 3. Currency is part of the highlighted figure.
  const money = await C.chunkDocument(Buffer.from([
    '# Fees', '', 'Courier S$3, levy SGD 300, Malaysia RM50, Indonesia Rp. 500,000, Philippines PHP 2,000 or ₱300, insurance $45 and a 12% refund.', '',
  ].join('\n')), 'md', 'money.md');
  const marked = D.figureParts(money[0]?.content ?? '').filter((p) => p.figure).map((p) => p.text);
  check('currency: S$, SGD, RM, Rp, PHP, ₱ and $ are highlighted with their number',
    marked.join('|') === 'S$3|SGD 300|RM50|Rp. 500,000|PHP 2,000|₱300|$45|12%' && money[0]?.metadata.figures_present === true, marked.join('|'));
  const edge = D.figureParts('ASGD5 and US$9, then 7,').filter((p) => p.figure).map((p) => p.text);
  check('currency: a prefix inside a word is not taken, and no trailing comma', edge.join('|') === '5|$9|7', edge.join('|'));

  // 4. Singular and plural.
  check('plural: 1 chunk / 2 chunks / 1 entry / 3 entries / "1" from the URL',
    P.plural(1, 'chunk') === '1 chunk' && P.plural(2, 'chunk') === '2 chunks' && P.plural(1, 'entry') === '1 entry' &&
    P.plural(3, 'entry') === '3 entries' && P.plural('1', 'chunk') === '1 chunk' && P.plural(0, 'question') === '0 questions' &&
    P.plural(1200, 'row') === '1,200 rows');
  const pages = code.filter((f) => /^(app|components)\//.test(rel(f)));
  const handRolled = pages.filter((f) => /\}\s+(chunks|entries|rows|questions|probes|documents|versions|drafts)\b(?!\s*=)|\}\s*(chunk|entr|row|question|document|version|draft)\$?\{[^}]*\?\s*''/.test(readFileSync(f, 'utf8')));
  check('plural: no page writes a count straight before a fixed plural noun, or hand-rolls the "s"', !handRolled.length, handRolled.map(rel).join(', '));

  // 5. 007's messages say it checks the Phase 2 functions only, and that 012 is the full check.
  const s007 = existsSync(join(ROOT, '..', 'scripts/sql/kb_admin_007_checks.sql')) ? readFileSync(join(ROOT, '..', 'scripts/sql/kb_admin_007_checks.sql'), 'utf8') : '';
  check('007 says Phase 2 only and names 012 as the full check, not "the five functions and nothing else"',
    Boolean(s007) && !/the five functions and nothing else/.test(s007) && (s007.match(/Phase 2 only; 012/g) ?? []).length === 2 &&
    /012_grants_checks\s*\n?--\s*is the full check/.test(s007));
}

console.log('speed (2026-10-05, region icn1):');
{
  const authBody = src('lib/auth.ts').slice(src('lib/auth.ts').indexOf('export async function requireViewerWith'));
  const fnBody = authBody.slice(0, authBody.indexOf('\n}\n'));
  check('requireViewerWith starts the queries, swallows a refused request\'s error, and returns data only after requireViewer()',
    /const data = load\(\);/.test(fnBody) && /data\.catch\(/.test(fnBody) &&
    fnBody.indexOf('await requireViewer()') > 0 && fnBody.indexOf('await requireViewer()') < fnBody.indexOf('await data'));
  const MAIN = ['app/documents/page.tsx', 'app/rows/page.tsx', 'app/rows/[id]/page.tsx', 'app/pending/page.tsx', 'app/activity/page.tsx', 'app/documents/history/page.tsx'];
  const notParallel = MAIN.filter((p) => !/requireViewerWith\(\(\) =>/.test(src(p)) || /await requireViewer\(\)/.test(src(p)));
  check('the six main pages start their queries alongside the sign-in check', !notParallel.length, notParallel.join(', '));
  check('Browse reads the filter lists and the entries together; entry detail reads entry, draft and versions together',
    /Promise\.all\(\[filterOptions\(\), rows\(/.test(src('app/rows/page.tsx')) &&
    /Promise\.all\(\[row\(params\.id\), openDraft\(params\.id\)/.test(src('app/rows/[id]/page.tsx')));
  check('document history reads its versions by source in the same round as the rest (no second lookup)',
    /batchesOfSource\(source\)\]/.test(src('app/documents/history/page.tsx')) && !/batchesOf\(/.test(src('app/documents/history/page.tsx')));
  check('the connection pool keeps an idle connection for 300 s', /idle_timeout: 300,/.test(src('lib/db.ts')));
  const LOADING = ['app/documents', 'app/documents/history', 'app/documents/upload', 'app/documents/batches/[id]', 'app/documents/batches/[id]/impact',
    'app/rows', 'app/rows/[id]', 'app/pending', 'app/activity'];
  const missing = LOADING.filter((d) => !/<LoadingPage /.test(src(`${d}/loading.tsx`)));
  check('every main page has a loading state', !missing.length, missing.join(', '));
  const loadingSrc = src('components/LoadingPage.tsx') + LOADING.map((d) => src(`${d}/loading.tsx`)).join('\n');
  check('a loading state reads no session, no database and shows no viewer', !/from '@\/lib\/(auth|db|queries|session)'|viewer|cookies\(/.test(loadingSrc));
  check('signing in lands straight on the Knowledge base, and the nav never links "/"',
    /redirect\('\/documents'\)/.test(src('app/login/actions.ts')) && !/href: '\/'/.test(src('components/Shell.tsx')));
}

console.log('login show/hide password (2026-10-05):');
{
  const lf = src('app/login/LoginForm.tsx');
  const btn = lf.slice(lf.indexOf('<button\n            type="button"'), lf.indexOf('</button>', lf.indexOf('<button\n            type="button"')));
  check('the toggle is a type="button" (never submits) with aria-pressed and both aria-labels',
    btn.length > 0 && /aria-pressed=\{showPassword\}/.test(btn) && /'Hide password'/.test(btn) && /'Show password'/.test(btn));
  check('the field starts hidden and switches between password and text',
    /useState\(false\)/.test(lf) && /type=\{showPassword \? 'text' : 'password'\}/.test(lf));
  check('the field is hidden again after every sign-in attempt',
    /useEffect\(\(\) => setShowPassword\(false\), \[raw\]\)/.test(lf));
  check('the password field keeps its name, autocomplete and required, and the form still posts to login',
    /name="password"/.test(lf) && /autoComplete="current-password"/.test(lf) && /useFormState<LoginState, FormData>\(login,/.test(lf) &&
    /<form action=\{action\}/.test(lf));
  check('the icons are inline SVG, no icon package', /<svg /.test(lf) && !/from '(lucide|react-icons|@heroicons)/.test(lf));
}

console.log(failures ? `RESULT: ${failures} FAIL(S)` : 'RESULT: ALL PASS');
process.exitCode = failures ? 1 : 0;
