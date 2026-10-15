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
  'kb_admin_publish', 'kb_admin_restore', 'kb_admin_save_draft',
  // Migration 015 (the KB watcher), sorted in with the rest.
  'kb_admin_source_add', 'kb_admin_source_record_fetch', 'kb_admin_source_remove',
  'kb_admin_toggle',
  'kb_admin_watch_approve', 'kb_admin_watch_edit', 'kb_admin_watch_mark_published', 'kb_admin_watch_propose', 'kb_admin_watch_reject',
];
const SIX = SIXTEEN; // the name the checks below grew up with (now twenty-four)
const ACTION_FILES = ['app/docs/actions.ts', 'app/editor/actions.ts', 'app/watch/actions.ts'];
// The one module outside the action files that may write: the watcher's scheduled route.
const SCHEDULED_WRITER = 'app/api/watcher/run/route.ts';
const ROLE_NAMES = new Set(['kb_admin_reader', 'kb_admin_editor']);
const named = new Map();
for (const f of code) {
  for (const m of readFileSync(f, 'utf8').matchAll(/\bkb_admin_\w+/g)) {
    if (!ROLE_NAMES.has(m[0])) named.set(m[0], [...(named.get(m[0]) ?? []), rel(f)]);
  }
}
const strays = [...named.keys()].filter((n) => !SIX.includes(n));
check('the only kb_admin_* functions named are the twenty-four', strays.length === 0, strays.map((n) => `${n} in ${named.get(n)[0]}`).join(', '));
const writeTs = src('lib/write.ts');
check('lib/write.ts exports callWrite and nothing else', exportsOf(writeTs).join(',') === 'callWrite', exportsOf(writeTs).join(','));
check('lib/write.ts sends only writeStatement(fn), after isAllowedWrite()',
  /const statement = writeStatement\(fn\);/.test(writeTs) && /if \(!isAllowedWrite\(statement\)/.test(writeTs) &&
  (writeTs.match(/\.unsafe\(/g) ?? []).length === 1 && /\.unsafe\(statement, params( as postgres\.ParameterOrJSON<never>\[\])?\)/.test(writeTs) && !/\.begin\(|sql\(\)`/.test(writeTs));
const pgUsers = code.filter((f) => /from 'postgres'/.test(readFileSync(f, 'utf8'))).map(rel).sort();
check('only lib/db.ts and lib/write.ts open a database connection', pgUsers.join(',') === 'lib/db.ts,lib/write.ts', pgUsers.join(','));
const writeUsers = code.filter((f) => /from '@\/lib\/write'|from '\.\.?\/.*write'/.test(readFileSync(f, 'utf8'))).map(rel);
check('only the three action modules and the scheduled watcher route call lib/write.ts',
  writeUsers.sort().join(',') === [...ACTION_FILES, SCHEDULED_WRITER].sort().join(','), writeUsers.join(','));
const actionsSrc = src('app/editor/actions.ts');
for (const file of ACTION_FILES) {
  const text = src(file);
  check(`${file} is a 'use server' module`, /^'use server';/.test(text));
  const fns = [...text.matchAll(/export async function (\w+)\([^)]*\)[^{]*\{\s*\n\s*(.*)/g)];
  const unguarded = fns.filter((m) => m[2].trim() !== 'const auth = await actorForWrite();').map((m) => m[1]);
  const expected = { 'app/editor/actions.ts': 6, 'app/docs/actions.ts': 9, 'app/watch/actions.ts': 6 }[file];
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
check("no other 'use server' module than sign-in, the editor, documents and the watcher",
  otherServer.join(',') === 'app/docs/actions.ts,app/editor/actions.ts,app/login/actions.ts,app/watch/actions.ts', otherServer.join(','));
const toggleSrc = actionsSrc.slice(actionsSrc.indexOf('export async function toggleEntry'));
check('switching on/off also checks canApprove before calling the database',
  toggleSrc.indexOf('actor.canApprove') > 0 && toggleSrc.indexOf('actor.canApprove') < toggleSrc.indexOf("callWrite('kb_admin_toggle'"));

const pages = code.filter((f) => /^app\/.*page\.tsx$/.test(rel(f)) && rel(f) !== 'app/login/page.tsx');
for (const p of pages) check(`${rel(p)} requires a viewer`, /requireViewer(With)?\(/.test(readFileSync(p, 'utf8')));
const apis = code.filter((f) => /^app\/api\/.*route\.ts$/.test(rel(f)));
for (const a of apis) {
  // The scheduled watcher route has no session: Vercel's cron call carries CRON_SECRET instead.
  if (rel(a) === SCHEDULED_WRITER) check(`${rel(a)} checks the cron secret (it has no session)`, readFileSync(a, 'utf8').includes('checkCronRequest('));
  else check(`${rel(a)} checks the session`, readFileSync(a, 'utf8').includes('checkRequest('));
}
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
const SECRET_KEYS = /^\s*(SUPABASE_ANON_KEY|ADMIN_PREVIEW_SECRET|KB_ADMIN_DB_PASSWORD|KB_ADMIN_DB_PASSWORD_FILE|KB_ADMIN_DB_PASSWORD_EDITOR|KB_ADMIN_DB_PASSWORD_EDITOR_FILE|KB_ADMIN_EMBEDDING_API_KEY|KB_ADMIN_LLM_API_KEY|CRON_SECRET)\s*=\s*(.*)$/;
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
check('exactly the twenty-four functions are allowed', Object.keys(WRITE_FUNCTIONS).sort().join(',') === SIX.join(','), Object.keys(WRITE_FUNCTIONS).sort().join(','));
check('each of the twenty-four statements is allowed', SIX.every((fn) => isAllowedWrite(writeStatement(fn))));
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
const grantFiles = ['kb_admin_005_editor_login.sql', 'kb_admin_008_create_entry.sql', 'kb_admin_011_doc_functions.sql', 'kb_admin_014_live_match_baseline.sql', 'kb_admin_015_watcher.sql'];
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
  check('the twenty-four signatures match the kb_admin_editor grants (005, 008, 011, 014, 015)', theirs === ours, theirs);
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

console.log('KB watcher, stages 1-3 (2026-10-15; migration 015 not applied yet):');
{
  const fx = (n) => readFileSync(join(ROOT, 'scripts/fixtures/watcher', n), 'utf8');
  const X = await import(pathToFileURL(join(ROOT, 'lib/watcher/extract.ts')).href);
  const Ch = await import(pathToFileURL(join(ROOT, 'lib/watcher/change.ts')).href);
  const Rb = await import(pathToFileURL(join(ROOT, 'lib/watcher/robots.ts')).href);
  const Fe = await import(pathToFileURL(join(ROOT, 'lib/watcher/fetcher.ts')).href);
  const FL = await import(pathToFileURL(join(ROOT, 'lib/watcher/feeLock.ts')).href);
  const LK = await import(pathToFileURL(join(ROOT, 'lib/watcher/fee-locked.ts')).href);
  const Dr = await import(pathToFileURL(join(ROOT, 'lib/watcher/drafting.ts')).href);
  const Pl = await import(pathToFileURL(join(ROOT, 'lib/watcher/pipeline.ts')).href);
  const Cr = await import(pathToFileURL(join(ROOT, 'lib/watcher/cron.ts')).href);
  const { validateConfig: vc } = await import(pathToFileURL(join(ROOT, 'lib/config.ts')).href);

  // --- extraction ------------------------------------------------------------
  const v1 = X.extractMainText(fx('page_v1.html'));
  const v1b = X.extractMainText(fx('page_v1_reflowed.html'));
  const v2 = X.extractMainText(fx('page_v2_changed.html'));
  const nm = X.extractMainText(fx('page_no_main.html'));
  check('extraction: the page title is read', v1.title === 'Rest days for migrant domestic workers | Ministry of Manpower');
  check('extraction: the main content is kept, paragraph by paragraph', v1.paragraphs.includes('Your migrant domestic worker (MDW) must get at least one rest day each week.') &&
    v1.paragraphs.includes('Keeping a record'), v1.paragraphs.join(' | '));
  check('extraction: nav, breadcrumbs, cookie banner, footer, aside, share links and scripts are dropped',
    !/Passes and permits|cookies|Government of Singapore|Related: Work permit|Share this page|NAV-SCRIPT-TEXT|FOOTER-SCRIPT-TEXT|STYLE-TEXT|Skip to main|Home >/i.test(v1.text), v1.text.slice(0, 200));
  check('extraction: a line wrap inside a paragraph is not a new paragraph', v1.paragraphs.some((p) => p.startsWith('If she agrees') && p.endsWith('within the same month.')));
  check('extraction: entities are decoded', v1.text.includes('day’s salary') && nm.title === 'Embassy & consular notice');
  check('extraction: a page with no <main> falls back to its body, still without nav and footer',
    nm.paragraphs.length === 3 && nm.paragraphs[1].startsWith('Applicants must bring') && !/Embassy footer|Visa/.test(nm.text), nm.paragraphs.join(' | '));
  check('extraction reaches no database, network or environment',
    !/from '(postgres|\.\.?\/(db|write|env|embed)|@\/lib\/(db|write|env|embed))'|process\.env|(?<![.\w])fetch\(/.test(src('lib/watcher/extract.ts')));

  // --- change detection ------------------------------------------------------
  check('change: whitespace, line wraps, curly quotes, a new "last updated" date and a "reviewed on" stamp are NOT a change',
    (await Ch.contentHash(v1.text)) === (await Ch.contentHash(v1b.text)) && Ch.pageChange(v1.text, v1b.text).changed === false);
  const real = Ch.pageChange(v1.text, v2.text);
  check('change: a real new rule IS a change, and only the new paragraph is reported',
    real.changed && real.added.length === 1 && real.added[0].startsWith('From 1 January 2027') && real.removed.length === 0,
    JSON.stringify(real));
  check('change: a date-only line or a date stamp is ignored in any common format',
    ['Last updated 3 March 2026', 'Last updated: March 12, 2026', 'Reviewed on 2026-04-09.', 'Page last reviewed 12/03/2026', 'As at Mar 2026', '9 April 2026']
      .every((stamp) => Ch.normalisedForm(`Rule one.\n${stamp}`) === Ch.normalisedForm('Rule one.')));
  check('change: a date INSIDE a sentence is kept - a changed effective date is a change',
    Ch.pageChange('From 1 January 2027, record it.', 'From 1 July 2027, record it.').changed === true);
  check('change: the hash is SHA-256 hex (the database refuses anything else)', /^[0-9a-f]{64}$/.test(await Ch.contentHash(v1.text)));
  check('change: a first read reports every paragraph as new', Ch.pageChange(null, v1.text).added.length === v1.paragraphs.length);
  check('change: the passages sent to the model are capped', Ch.changedPassages({ changed: true, added: Array(50).fill('x'.repeat(500)), removed: [] }).join('').length <= 6000);

  // --- robots.txt ------------------------------------------------------------
  const robots = Rb.parseRobots('User-agent: *\nDisallow: /private\nAllow: /private/ok\nCrawl-delay: 15\n\nUser-agent: BadBot\nDisallow: /', Fe.AGENT_TOKEN);
  check('robots: a disallowed path is refused, the longer Allow wins, other paths allowed',
    !Rb.robotsAllows(robots, '/private/x') && Rb.robotsAllows(robots, '/private/ok/page') && Rb.robotsAllows(robots, '/fdw'));
  check('robots: Crawl-delay is read', robots.crawlDelaySeconds === 15);
  const ours = Rb.parseRobots('User-agent: *\nDisallow:\n\nUser-agent: MingHweeKBWatcher\nDisallow: /fdw$\nDisallow: /*.pdf', Fe.AGENT_TOKEN);
  check('robots: a group naming our user-agent wins over "*", with * and $ honoured',
    !Rb.robotsAllows(ours, '/fdw') && Rb.robotsAllows(ours, '/fdw/rest-days') && !Rb.robotsAllows(ours, '/docs/a.pdf'));
  check('robots: an empty file allows everything', Rb.robotsAllows(Rb.parseRobots('', Fe.AGENT_TOKEN), '/anything'));

  // --- the polite fetcher (fake network, fake clock: no connection is opened) ---
  const fakeNet = (routes) => {
    const calls = [];
    let clock = 0;
    const sleeps = [];
    const deps = {
      now: () => clock,
      sleep: async (ms) => { sleeps.push(ms); clock += ms; },
      fetch: async (url, init) => {
        calls.push({ url, ua: init.headers['user-agent'], redirect: init.redirect, hasSignal: Boolean(init.signal), at: clock });
        const r = routes[url];
        const next = Array.isArray(r) ? (r.length > 1 ? r.shift() : r[0]) : r;
        if (!next) return { status: 404, headers: { get: () => null }, text: async () => '' };
        if (next === 'neterr') throw new Error('ECONNRESET');
        return { status: next.status, headers: { get: (h) => (next.headers ?? {})[h.toLowerCase()] ?? null }, text: async () => next.body ?? '' };
      },
    };
    return { deps, calls, sleeps };
  };
  const html = { status: 200, headers: { 'content-type': 'text/html; charset=utf-8' }, body: fx('page_v1.html') };
  {
    const n = fakeNet({ 'https://www.mom.gov.sg/robots.txt': { status: 200, body: 'User-agent: *\nDisallow: /private' }, 'https://www.mom.gov.sg/fdw': html });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://www.mom.gov.sg/fdw');
    check('fetch: robots.txt is read first, then the page, with our user-agent, a timeout and manual redirects',
      r.ok && n.calls[0].url.endsWith('/robots.txt') && n.calls[1].url === 'https://www.mom.gov.sg/fdw' &&
      n.calls.every((c) => c.ua === Fe.USER_AGENT && c.hasSignal && c.redirect === 'manual'), JSON.stringify(n.calls.map((c) => c.url)));
    check('fetch: the user-agent names the agency, the tool and robots.txt', /MingHweeKBWatcher/.test(Fe.USER_AGENT) && /Ming Hwee/.test(Fe.USER_AGENT) && /robots/.test(Fe.USER_AGENT));
    check('fetch: two requests to one host are at least the minimum gap apart (per-domain rate limit)',
      n.calls[1].at - n.calls[0].at >= Fe.MIN_GAP_MS, `${n.calls[1].at - n.calls[0].at} ms`);
  }
  {
    const n = fakeNet({ 'https://www.mom.gov.sg/robots.txt': { status: 200, body: 'User-agent: *\nDisallow: /fdw' }, 'https://www.mom.gov.sg/fdw': html });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://www.mom.gov.sg/fdw');
    check('fetch: a page robots.txt disallows is not requested at all, and the refusal is reported',
      !r.ok && /robots\.txt/.test(r.error) && n.calls.length === 1, r.error ?? '');
  }
  {
    const n = fakeNet({ 'https://a.gov.sg/robots.txt': { status: 503 }, 'https://a.gov.sg/p': html });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://a.gov.sg/p');
    check('fetch: robots.txt that cannot be read (5xx) means "not this time" - the page is not fetched',
      !r.ok && /could not be read/.test(r.error) && !n.calls.some((c) => c.url === 'https://a.gov.sg/p'));
  }
  {
    const n = fakeNet({ 'https://a.gov.sg/robots.txt': { status: 404 }, 'https://a.gov.sg/p': [{ status: 503 }, 'neterr', html] });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://a.gov.sg/p');
    check('fetch: a 5xx and a network error are retried with backoff, then succeed',
      r.ok && n.calls.filter((c) => c.url === 'https://a.gov.sg/p').length === 3 && n.sleeps.some((ms) => ms >= 2000), JSON.stringify(n.sleeps));
  }
  {
    const n = fakeNet({ 'https://a.gov.sg/robots.txt': { status: 404 }, 'https://a.gov.sg/p': [{ status: 503 }] });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://a.gov.sg/p');
    check('fetch: retries stop after MAX_ATTEMPTS and the failure is reported, not hidden',
      !r.ok && n.calls.filter((c) => c.url === 'https://a.gov.sg/p').length === Fe.MAX_ATTEMPTS && /HTTP 503/.test(r.error));
  }
  {
    const n = fakeNet({ 'https://a.gov.sg/robots.txt': { status: 404 }, 'https://a.gov.sg/p': [{ status: 404 }] });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://a.gov.sg/p');
    check('fetch: a 404 is not retried', !r.ok && n.calls.filter((c) => c.url === 'https://a.gov.sg/p').length === 1 && r.status === 404);
  }
  {
    const n = fakeNet({ 'https://a.gov.sg/robots.txt': { status: 404 }, 'https://a.gov.sg/p': { status: 301, headers: { location: 'http://a.gov.sg/p' } } });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://a.gov.sg/p');
    check('fetch: a redirect to http:// is refused', !r.ok && /redirected to a refused address/.test(r.error));
  }
  {
    const n = fakeNet({ 'https://a.gov.sg/robots.txt': { status: 404 }, 'https://a.gov.sg/p': { status: 200, headers: { 'content-type': 'application/pdf' }, body: '%PDF' } });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://a.gov.sg/p');
    check('fetch: a non-HTML document is refused', !r.ok && /not a web page/.test(r.error));
  }
  check('fetch: private and odd addresses are refused before any request',
    ['http://www.mom.gov.sg/', 'https://localhost/x', 'https://127.0.0.1/x', 'https://intranet.local/x', 'https://user:pw@mom.gov.sg/', 'not a url']
      .every((u) => Fe.addressProblem(u) !== null) && Fe.addressProblem('https://www.mom.gov.sg/passes-and-permits/work-permit-for-foreign-domestic-worker') === null);
  check('the fetcher opens no connection of its own (the network call is passed in)', !/(?<![.\w])fetch\(|process\.env/.test(src('lib/watcher/fetcher.ts')));

  // --- the fee lock -----------------------------------------------------------
  const lock = (tq, ta, q, a) => FL.feeLockReason({ targetQuestion: tq, targetAnswer: ta, proposedQuestion: q, proposedAnswer: a });
  check('fee lock: a plain rule change is approvable', lock('How many rest days?', 'One a week.', 'How many rest days?', 'One a week, plus one a month that cannot be compensated.') === null);
  check('fee lock: any money figure blocks ($, S$, SGD, RM, "dollars")',
    ['The fee is $450.', 'From S$650 a month.', 'SGD 300 levy.', 'RM 200 deposit.', 'It costs 450 dollars.'].every((a) => /money/.test(lock(null, null, 'Q?', a) ?? '')));
  check('fee lock: a figure in the CURRENT entry blocks too (a change cannot remove a price either)', /money/.test(lock('Cost?', 'The agency fee is $1,428.', 'Cost?', 'Our agent will confirm.') ?? ''));
  check('fee lock: naming an agency fee blocks even with no figure', /agency fee/.test(lock(null, null, 'Q?', 'A replacement fee applies.') ?? ''));
  check('fee lock: every row from the fees document is locked, by question',
    LK.LOCKED_QUESTIONS.length === 23 && LK.LOCKED_QUESTIONS.every((q) => /fees document/.test(lock(q, 'Our agent will confirm.', q, 'Changed wording.') ?? '')));
  check('fee lock: a row carrying a fees-document correction is locked, by its text',
    LK.LOCKED_NEEDLES.length === 7 && LK.LOCKED_NEEDLES.every((n) => /fees document/.test(lock('Some question?', `Intro. ${n} More.`, 'Some question?', 'x') ?? '')));
  {
    const sql015 = existsSync(join(ROOT, '..', 'scripts/sql/kb_admin_015_watcher.sql')) ? readFileSync(join(ROOT, '..', 'scripts/sql/kb_admin_015_watcher.sql'), 'utf8') : '';
    const arr = (marker) => {
      const m = new RegExp(`-- ${marker}:BEGIN\\n([\\s\\S]*?)-- ${marker}:END`).exec(sql015);
      return m ? [...m[1].matchAll(/'((?:[^']|'')*)'/g)].map((x) => x[1].replace(/''/g, "'")) : null;
    };
    check('fee lock: the database migration carries exactly the same locked questions and needles',
      JSON.stringify(arr('FEE_LOCKED_QUESTIONS')) === JSON.stringify([...LK.LOCKED_QUESTIONS]) &&
      JSON.stringify(arr('FEE_LOCKED_NEEDLES')) === JSON.stringify([...LK.LOCKED_NEEDLES]));
    check('fee lock: the database refuses to approve a blocked draft (KB007), re-checking the entry\'s current text',
      /raise exception 'needs agency confirmation: %', v_lock using errcode = 'KB007'/.test(sql015) &&
      /kb_admin__watch_fee_lock\(v_t\.question, v_t\.answer, v_d\.proposed_question, v_d\.proposed_answer\)/.test(sql015));
    check('fee lock: the database approve needs an APPROVER', /kb_admin_watch_approve[\s\S]*?kb_admin__require_actor\(p_actor_id, 'approver'\)/.test(sql015));
    check('fee lock: the watcher owner can never touch the pricing rules', /FAIL: kb_admin_fn_owner holds a privilege on cb_kb_rules/.test(sql015) && !/on public\.cb_kb_rules to kb_admin_fn_owner/.test(sql015));
    // Audit: every public watcher function writes an audit row.
    const fns = [...sql015.matchAll(/create or replace function public\.(kb_admin_(?:source|watch)_\w+)\(([\s\S]*?)\n\$\$;/g)];
    const unaudited = fns.filter((m) => !/insert into public\.cb_kb_audit/.test(m[2])).map((m) => m[1]);
    check('audit: all eight watcher functions write an audit row (the PGlite run proved each action lands)', fns.length === 8 && unaudited.length === 0,
      `${fns.length} functions${unaudited.length ? `; unaudited: ${unaudited.join(', ')}` : ''}`);
    check('access: RLS on and everything revoked from the portal roles for the three tables',
      /enable row level security/.test(sql015) && /revoke all on public\.cb_kb_sources, public\.cb_kb_source_snapshots, public\.cb_kb_drafts\s+from public, anon, authenticated, service_role;/.test(sql015));
    check('the live knowledge-base table gets no new column', !/alter table public\.cb_knowledge_base_updated\s+add/i.test(sql015));
    const rb = existsSync(join(ROOT, '..', 'scripts/sql/rollback/rollback_kb_admin_015_watcher.sql')) ? readFileSync(join(ROOT, '..', 'scripts/sql/rollback/rollback_kb_admin_015_watcher.sql'), 'utf8') : '';
    check('a rollback exists and refuses while watcher data exists', /ROLLBACK 015 REFUSED: sources exist/.test(rb) && /ROLLBACK 015 REFUSED: cb_kb_audit holds watcher actions/.test(rb));
  }

  // --- drafting: only verifiable proposals survive ----------------------------
  const page = v2.text;
  const cand = [{ id: '11111111-1111-1111-1111-111111111111', question: 'How many rest days?', answer: 'One a week.' }];
  const goodDraft = { target_entry_id: cand[0].id, question: 'How many rest days?', answer: 'One a week; from 2027 it goes in the contract.', source_passage: 'From 1 January 2027, the rest-day arrangement must be recorded in the employment contract.', why: 'x' };
  const parsed = Dr.parseDrafts(JSON.stringify({ drafts: [
    goodDraft,
    { ...goodDraft, target_entry_id: '99999999-9999-9999-9999-999999999999' },
    { ...goodDraft, target_entry_id: null, source_passage: 'A sentence the page never said.' },
    { ...goodDraft, target_entry_id: null, answer: 'x'.repeat(1300) },
    { ...goodDraft, target_entry_id: null, answer: 'Her FIN is G1234567X.' },
  ] }), { newText: page, candidateIds: cand.map((c) => c.id) });
  check('drafting: a draft citing the page word for word, for an offered entry, survives', parsed.drafts.length === 1 && parsed.drafts[0].target_entry_id === cand[0].id);
  check('drafting: an unoffered entry, a passage not on the page, over 1,200 characters and an NRIC are each dropped',
    parsed.dropped.length === 4 && parsed.dropped.some((d) => /not offered/.test(d)) && parsed.dropped.some((d) => /not on the page/.test(d)) &&
    parsed.dropped.some((d) => /1200/.test(d)) && parsed.dropped.some((d) => /NRIC/.test(d)), parsed.dropped.join(' | '));
  check('drafting: an answer that is not JSON produces no draft', Dr.parseDrafts('sorry, I cannot', { newText: page, candidateIds: [] }).drafts.length === 0);
  const prompt = Dr.buildPrompt({ label: 'MOM', url: 'https://www.mom.gov.sg/fdw', added: ['A'], removed: [], candidates: cand });
  check('drafting: the prompt forbids invented facts and agency fees, and asks for the passage word for word',
    /ONLY what the page passages say/.test(prompt.system) && /Never propose anything about agency fees/.test(prompt.system) && /word for word/.test(prompt.system));

  // --- the pipeline, end to end on a fake database ---------------------------
  // The fake database behaves like migration 015: it compares hashes with the
  // last good snapshot and applies the fee lock to every proposal.
  const fakeDb = () => {
    const snaps = [];
    const log = [];
    const proposals = [];
    const call = async (fn, params) => {
      log.push(fn);
      if (fn === 'kb_admin_source_record_fetch') {
        const [, , , ok, , hash, text] = params;
        if (!ok) { snaps.push({ ok: false }); return { ok: false, changed: false, first: false }; }
        const prev = [...snaps].reverse().find((s) => s.ok);
        snaps.push({ ok: true, hash, text });
        return { ok: true, snapshot_id: `snap-${snaps.length}`, changed: !prev || prev.hash !== hash, first: !prev };
      }
      if (fn === 'kb_admin_match_live') return [{ id: cand[0].id, chunk_type: 'qa_pair', question: cand[0].question, answer: cand[0].answer, similarity: 0.7 }];
      if (fn === 'kb_admin_watch_propose') {
        const [, , , target, q, a, , , , , passage] = params;
        const t = target ? cand.find((c) => c.id === target) : null;
        const reason = FL.feeLockReason({ targetQuestion: t?.question ?? null, targetAnswer: t?.answer ?? null, proposedQuestion: q, proposedAnswer: a });
        proposals.push({ target, q, a, passage, status: reason ? 'blocked' : 'pending' });
        return { draft_id: `d-${proposals.length}`, status: reason ? 'blocked' : 'pending', blocked_reason: reason };
      }
      throw new Error(`unexpected call ${fn}`);
    };
    return { call, log, proposals, lastText: () => [...snaps].reverse().find((s) => s.ok)?.text ?? null };
  };
  const pageFrom = (file) => async () => ({ ok: true, status: 200, html: fx(file), finalUrl: 'https://www.mom.gov.sg/fdw' });
  const src0 = { id: 'aaaaaaaa-0000-0000-0000-000000000000', url: 'https://www.mom.gov.sg/fdw', label: 'MOM rest days' };
  const db1 = fakeDb();
  let llmCalls = 0;
  const llm = (answer) => async () => { llmCalls += 1; return answer; };
  const deps = (fetchPage, answer) => ({
    call: db1.call, fetchPage, previousText: async () => db1.lastText(),
    embed: async (texts) => ({ vectors: texts.map(() => Array(1536).fill(0.01)), model: 'm' }),
    recorder: null, searcher: { userId: 'u', email: 'watcher' }, llm: llm(answer), model: 'test-model',
  });
  const first = await Pl.checkSource(src0, deps(pageFrom('page_v1.html'), JSON.stringify({ drafts: [] })));
  check('pipeline: the first read records a snapshot (and asks the model about the whole page)', first.outcome === 'first' && llmCalls === 1);
  const same = await Pl.checkSource(src0, deps(pageFrom('page_v1_reflowed.html'), JSON.stringify({ drafts: [goodDraft] })));
  check('pipeline: whitespace and date stamps only -> recorded as unchanged, NO model call, NO draft',
    same.outcome === 'unchanged' && llmCalls === 1 && db1.proposals.length === 0);
  const changed = await Pl.checkSource(src0, deps(pageFrom('page_v2_changed.html'), JSON.stringify({ drafts: [goodDraft] })));
  check('pipeline: a real change -> the closest entries are found, the model is asked, one proposal is made with its passage',
    changed.outcome === 'changed' && llmCalls === 2 && db1.proposals.length === 1 && db1.proposals[0].passage.startsWith('From 1 January 2027') &&
    db1.log.includes('kb_admin_match_live'), JSON.stringify(changed));
  const feeAnswer = JSON.stringify({ drafts: [{ ...goodDraft, answer: 'One a week, or S$40 in lieu.' }] });
  const db2 = fakeDb();
  const feeRun = await Pl.checkSource(src0, { ...deps(pageFrom('page_v2_changed.html'), feeAnswer), call: db2.call, previousText: async () => v1.text });
  check('pipeline: a proposal touching money is still recorded - as BLOCKED, needing agency confirmation', feeRun.drafts.length === 1 && feeRun.drafts[0].status === 'blocked');
  const db3 = fakeDb();
  const failed = await Pl.checkSource(src0, { ...deps(async () => ({ ok: false, status: 503, error: 'HTTP 503 (after 3 attempts)' }), '{}'), call: db3.call });
  check('pipeline: a failed fetch is recorded as a failed snapshot (shown in kb-admin), never silently skipped',
    failed.outcome === 'failed' && db3.log.join() === 'kb_admin_source_record_fetch');
  check('the pipeline imports no database, network or environment code (everything is passed in)',
    !/from '(postgres|\.\.?\/(db|write|env|embed)|@\/lib\/(db|write|env|embed))'|process\.env|(?<![.\w])fetch\(/.test(src('lib/watcher/pipeline.ts')));

  // --- approve -> the existing Q&A path -------------------------------------
  const approveRun = async (failOn) => {
    const calls = [];
    const call = async (fn, params) => {
      calls.push({ fn, params });
      if (fn === failOn) throw Object.assign(new Error('refused'), { code: failOn === 'kb_admin_watch_approve' ? 'KB007' : 'KB005' });
      if (fn === 'kb_admin_watch_approve') return { entry_id: 'e1', version_id: 'v1' };
      return null;
    };
    let threw = null;
    let result = null;
    try {
      result = await Pl.approveAndPublish({ userId: 'u1', email: 'a@x' }, { id: 'd1', question: 'Q?', answer: 'A.' }, 'page changed',
        { call, embed: async (t) => ({ vector: Array(1536).fill(0.5), model: 'text-embedding-3-small', text: t }) });
    } catch (e) {
      threw = e;
    }
    return { calls, result, threw };
  };
  {
    const ok = await approveRun(null);
    check('approve: approve -> kb_admin_publish (the existing path, with the vector and the canary model) -> mark published, in that order',
      ok.calls.map((c) => c.fn).join(',') === 'kb_admin_watch_approve,kb_admin_publish,kb_admin_watch_mark_published' &&
      ok.calls[1].params[2] === 'v1' && /^\[0\.5(,0\.5){1535}\]$/.test(ok.calls[1].params[3]) && ok.calls[1].params[4] === 'text-embedding-3-small' &&
      ok.result?.published === true);
    const blocked = await approveRun('kb_admin_watch_approve');
    check('approve: a refused approval (fee lock, KB007) stops everything - nothing embedded, nothing published',
      blocked.threw?.code === 'KB007' && blocked.calls.length === 1);
    const pubFail = await approveRun('kb_admin_publish');
    check('approve: a publish that fails leaves the draft approved but NOT marked published, and says so',
      pubFail.result?.published === false && pubFail.result.versionId === 'v1' && !pubFail.calls.some((c) => c.fn === 'kb_admin_watch_mark_published'));
  }

  // --- wiring: switched off unless asked for --------------------------------
  const watchActions = src('app/watch/actions.ts');
  const approveBody = watchActions.slice(watchActions.indexOf('export async function approveWatchDraft'));
  check('approving checks canApprove before anything reaches the database',
    approveBody.indexOf('actor.canApprove') > 0 && approveBody.indexOf('actor.canApprove') < approveBody.indexOf('approveAndPublish('));
  check('every watcher action refuses while the watcher is off', (watchActions.match(/const off = watcherOff\(\);/g) ?? []).length === 6);
  const watcherPages = ['app/sources/page.tsx', 'app/watch/page.tsx', 'app/watch/[id]/page.tsx'];
  check('the watcher pages are 404 unless the watcher is on', watcherPages.every((p) => /if \(!viewer\.watcherOn[^)]*\) notFound\(\);/.test(src(p))));
  const route = src('app/api/watcher/run/route.ts');
  check('the scheduled route is 404 unless the schedule is on and the cron secret matches',
    /if \(!cfg\.watcher \|\| !schedule\) return NextResponse\.json\(\{ error: 'not found' \}, \{ status: 404 \}\);/.test(route) &&
    /if \(!checkCronRequest\(req\.headers\.get\('authorization'\), schedule\.cronSecret\)\) return NextResponse\.json\(\{ error: 'not found' \}, \{ status: 404 \}\);/.test(route));
  check('the scheduled route records as the watcher (no person) and searches only as the configured account', /recorder: null,/.test(route) && /searcher,/.test(route));
  check('the cron secret is compared in constant time, and only a Bearer header counts',
    Cr.checkCronRequest(`Bearer ${'s'.repeat(40)}`, 's'.repeat(40)) && !Cr.checkCronRequest('s'.repeat(40), 's'.repeat(40)) &&
    !Cr.checkCronRequest(`Bearer ${'t'.repeat(40)}`, 's'.repeat(40)) && !Cr.checkCronRequest(null, 's'.repeat(40)) && /timingSafeEqual/.test(src('lib/watcher/cron.ts')));
  {
    const matcher = /matcher: \[('[^']*')\]/.exec(src('middleware.ts'))?.[1] ?? '';
    check('middleware lets only the scheduled route through without a session',
      matcher === "'/((?!login|api/watcher/run|_next/static|_next/image|favicon.ico).*)'", matcher);
  }
  check('vercel.json schedules the route daily (it answers 404 until WATCHER_ENABLED is set)',
    (() => { try { const v = JSON.parse(src('vercel.json')); return v.crons?.length === 1 && v.crons[0].path === '/api/watcher/run'; } catch { return false; } })());

  // --- configuration ----------------------------------------------------------
  const on = { ...good, KB_ADMIN_DB_PASSWORD_EDITOR: 'e'.repeat(24), KB_ADMIN_EMBEDDING_API_KEY: 'sk-or-v1-dummy', KB_ADMIN_EMBEDDING_BASE_URL: 'https://openrouter.ai/api/v1' };
  const cf = (o) => vc(o, noFile);
  const refusesW = (o) => { try { cf(o); return false; } catch (e) { return /refuses to start/.test(String(e.message)); } };
  check('config: the watcher is OFF by default, with no warning', cf(on).watcher === null && cf(on).watcherOffReason === '' && cf(good).watcher === null);
  check('config: KB_SOURCES_ENABLED=true turns it on, drafting with the bot\'s model via OpenRouter',
    cf({ ...on, KB_SOURCES_ENABLED: 'true' }).watcher?.llm.model === 'openai/gpt-5.6-luna' && cf({ ...on, KB_SOURCES_ENABLED: 'true' }).watcher?.llm.baseUrl === 'https://openrouter.ai/api/v1' &&
    cf({ ...on, KB_SOURCES_ENABLED: 'true' }).watcher?.schedule === null);
  check('config: without the editor the watcher stays off (with a reason)', cf({ ...good, KB_SOURCES_ENABLED: 'true' }).watcher === null && /editor/.test(cf({ ...good, KB_SOURCES_ENABLED: 'true' }).watcherOffReason));
  check('config: WATCHER_ENABLED alone does nothing without KB_SOURCES_ENABLED', cf({ ...on, WATCHER_ENABLED: 'true' }).watcher === null && /KB_SOURCES_ENABLED/.test(cf({ ...on, WATCHER_ENABLED: 'true' }).watcherOffReason));
  check('config: the schedule needs CRON_SECRET and KB_WATCHER_ACTOR_ID; weekly by default, configurable',
    cf({ ...on, KB_SOURCES_ENABLED: 'true', WATCHER_ENABLED: 'true' }).watcher?.schedule === null &&
    cf({ ...on, KB_SOURCES_ENABLED: 'true', WATCHER_ENABLED: 'true', CRON_SECRET: 'c'.repeat(40), KB_WATCHER_ACTOR_ID: '11111111-1111-1111-1111-111111111111' }).watcher?.schedule?.intervalDays === 7 &&
    cf({ ...on, KB_SOURCES_ENABLED: 'true', WATCHER_ENABLED: 'true', CRON_SECRET: 'c'.repeat(40), KB_WATCHER_ACTOR_ID: '11111111-1111-1111-1111-111111111111', WATCHER_INTERVAL_DAYS: '14' }).watcher?.schedule?.intervalDays === 14);
  check('config: a short cron secret, a malformed actor id, a non-OpenRouter drafting key or a silly interval refuses to start',
    refusesW({ ...on, CRON_SECRET: 'short' }) && refusesW({ ...on, KB_WATCHER_ACTOR_ID: 'nope' }) &&
    refusesW({ ...on, KB_ADMIN_LLM_API_KEY: 'sk-proj-x' }) && refusesW({ ...on, WATCHER_INTERVAL_DAYS: '0' }) && refusesW({ ...on, WATCHER_INTERVAL_DAYS: '2.5' }));
  check('config: an OpenAI embedding key is not used for drafting (needs KB_ADMIN_LLM_API_KEY)',
    cf({ ...good, KB_ADMIN_DB_PASSWORD_EDITOR: 'e'.repeat(24), KB_ADMIN_EMBEDDING_API_KEY: 'sk-dummy', KB_SOURCES_ENABLED: 'true' }).watcher === null);
  check('the sidebar shows the watcher pages only while it is on', /if \(viewer\.watcherOn\)/.test(src('components/Shell.tsx')));
}

console.log(failures ? `RESULT: ${failures} FAIL(S)` : 'RESULT: ALL PASS');
process.exitCode = failures ? 1 : 0;
