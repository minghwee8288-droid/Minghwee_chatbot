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
// The one sentence of prose allowed to use a verb: the watcher's banner, worded by the user (2026-10-16).
const PROSE_OK = /This update drops existing information/g;
const scanText = (f) => readFileSync(f, 'utf8').replace(PROSE_OK, '');
for (const f of code) {
  const lines = scanText(f).split('\n');
  const hits = lines.map((l, i) => (WRITE_WORDS.test(l) ? i + 1 : 0)).filter(Boolean);
  if (hits.length) check(`no write verb in ${rel(f)}`, false, `line(s) ${hits.join(', ')}`);
}
check('no write verb in any source file', !code.some((f) => WRITE_WORDS.test(scanText(f))));

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
  'kb_admin_source_add', 'kb_admin_source_record_fetch', 'kb_admin_source_redraft', 'kb_admin_source_remove', 'kb_admin_source_set_suggestions',
  'kb_admin_toggle',
  'kb_admin_watch_approve', 'kb_admin_watch_edit', 'kb_admin_watch_mark_published', 'kb_admin_watch_propose', 'kb_admin_watch_propose_checked', 'kb_admin_watch_reject',
];
const SIX = SIXTEEN; // the name the checks below grew up with (now twenty-seven: + 016's three)
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
check('the only kb_admin_* functions named are the twenty-seven', strays.length === 0, strays.map((n) => `${n} in ${named.get(n)[0]}`).join(', '));
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
  const expected = { 'app/editor/actions.ts': 6, 'app/docs/actions.ts': 9, 'app/watch/actions.ts': 8 }[file];
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
check('exactly the twenty-seven functions are allowed', Object.keys(WRITE_FUNCTIONS).sort().join(',') === SIX.join(','), Object.keys(WRITE_FUNCTIONS).sort().join(','));
check('each of the twenty-seven statements is allowed', SIX.every((fn) => isAllowedWrite(writeStatement(fn))));
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
const grantFiles = ['kb_admin_005_editor_login.sql', 'kb_admin_008_create_entry.sql', 'kb_admin_011_doc_functions.sql', 'kb_admin_014_live_match_baseline.sql', 'kb_admin_015_watcher.sql', 'kb_admin_016_watcher_overlap.sql'];
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
  check('the twenty-seven signatures match the kb_admin_editor grants (005, 008, 011, 014, 015, 016)', theirs === ours, theirs);
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
  const lock = (tq, ta, q, a, category = 'process') => FL.feeLockReason({ targetQuestion: tq, targetAnswer: ta, proposedQuestion: q, proposedAnswer: a, category });
  const gov = (tq, ta, q, a) => FL.reviewState({ targetQuestion: tq, targetAnswer: ta, proposedQuestion: q, proposedAnswer: a, category: 'government_rule' });
  check('fee lock: a plain rule change is approvable, with no figure to confirm',
    lock('How many rest days?', 'One a week.', 'How many rest days?', 'One a week, plus one a month that cannot be compensated.') === null &&
    !gov('How many rest days?', 'One a week.', 'How many rest days?', 'One a week, plus one.').figure);
  check('fee lock: any money figure blocks from a page that is NOT a government rule page ($, S$, SGD, RM, "dollars")',
    ['process', 'embassy', 'other', null].every((c) =>
      ['The levy is $450.', 'From S$650 a month.', 'SGD 300 levy.', 'RM 200 deposit.', 'It costs 450 dollars.'].every((a) => /money figure/.test(lock(null, null, 'Q?', a, c) ?? ''))));
  check('fee lock: a figure in the CURRENT entry blocks too (a change cannot remove a price either)', /money/.test(lock('Levy?', 'The levy is $300.', 'Levy?', 'Our agent will confirm.') ?? ''));
  check('government figures: levy, security bond, MOM fee, minimum income and insurance minimum from a government rule page are APPROVABLE, with the figure warning',
    ['The monthly levy is $300 ($60 with a concession).', 'The security bond is S$5,000.', 'The MOM processing fee is $35.',
      'The work permit fee is $35.', 'Employers need a monthly household income of at least S$3,000.', 'Medical insurance must cover at least S$60,000 a year.']
      .every((a) => { const r = gov(null, null, 'Q?', a); return r.lock === null && r.figure === true; }));
  check('government figures: a figure only in the CURRENT entry still asks for the confirmation', gov('Levy?', 'The levy is $300.', 'Levy?', 'Our agent will confirm the levy.').figure === true);
  check('fee lock: naming an agency fee blocks even with no figure, and even from a government rule page',
    /agency fee/.test(lock(null, null, 'Q?', 'A replacement fee applies.') ?? '') && /agency fee/.test(gov(null, null, 'Q?', 'The agency fee is S$1,428.').lock ?? '') &&
    /agency fee/.test(gov(null, null, 'Q?', 'Our processing fee is $120.').lock ?? ''));
  check('fee lock: a helper salary figure is locked even from a government rule page (cb_kb_rules)', /salary/.test(gov(null, null, 'Salary?', 'The minimum salary is S$700.').lock ?? ''));
  check('fee lock: every row from the fees document is locked, by question, whatever the category',
    LK.LOCKED_QUESTIONS.length === 23 && LK.LOCKED_QUESTIONS.every((q) => /fees document/.test(lock(q, 'Our agent will confirm.', q, 'Changed wording.', 'government_rule') ?? '')));
  check('fee lock: a row carrying a fees-document correction is locked, by its text, whatever the category',
    LK.LOCKED_NEEDLES.length === 7 && LK.LOCKED_NEEDLES.every((n) => /fees document/.test(lock('Some question?', `Intro. ${n} More.`, 'Some question?', 'x', 'government_rule') ?? '')));
  {
    const sql015 = existsSync(join(ROOT, '..', 'scripts/sql/kb_admin_015_watcher.sql')) ? readFileSync(join(ROOT, '..', 'scripts/sql/kb_admin_015_watcher.sql'), 'utf8') : '';
    const arr = (marker) => {
      const m = new RegExp(`-- ${marker}:BEGIN\\n([\\s\\S]*?)-- ${marker}:END`).exec(sql015);
      return m ? [...m[1].matchAll(/'((?:[^']|'')*)'/g)].map((x) => x[1].replace(/''/g, "'")) : null;
    };
    check('fee lock: the database migration carries exactly the same locked questions and needles',
      JSON.stringify(arr('FEE_LOCKED_QUESTIONS')) === JSON.stringify([...LK.LOCKED_QUESTIONS]) &&
      JSON.stringify(arr('FEE_LOCKED_NEEDLES')) === JSON.stringify([...LK.LOCKED_NEEDLES]));
    check('fee lock: the database refuses to approve a blocked draft (KB007), re-checking the entry\'s current text and the page category',
      /raise exception 'needs agency confirmation: %', v_lock using errcode = 'KB007'/.test(sql015) &&
      /kb_admin__watch_fee_lock\(v_t\.question, v_t\.answer, v_d\.proposed_question, v_d\.proposed_answer, v_cat\)/.test(sql015));
    check('government figures: the database refuses an unconfirmed figure (KB008) and records the confirmation',
      /if v_figure and not coalesce\(p_confirm_figure, false\) then\s+raise exception '[^']*' using errcode = 'KB008'/.test(sql015) &&
      /figure_confirmed = v_figure/.test(sql015) && /'figure_confirmed', v_figure/.test(sql015) &&
      /kb_admin_watch_approve\(\s*p_actor_id uuid, p_actor_email text, p_draft_id uuid, p_reason text, p_confirm_figure boolean\)/.test(sql015));
    check('government figures: only a government_rule page may carry a money figure in the database rule too',
      /if money and coalesce\(p_category, ''\) <> 'government_rule' then/.test(sql015) && /if money and all_text ~\* '\\msalar\(y\|ies\)\\M' then/.test(sql015));
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
  const restPassage = 'From 1 January 2027, the rest-day arrangement must be recorded in the employment contract.';
  // A change to an existing entry is additive (2026-10-16): what to add, and what the page contradicts.
  const goodDraft = { target_entry_id: cand[0].id, add: [{ sentence: 'From 1 January 2027 the rest-day arrangement must be recorded in the employment contract.', after: '', source_passage: restPassage }], replace: [], why: 'x' };
  const newBase = { target_entry_id: null, question: 'How many rest days?', answer: 'One a week; from 2027 it goes in the contract.', source_passage: restPassage, why: 'x' };
  const liveCand = new Map(cand.map((c) => [c.id, c]));
  const parsed = Dr.parseDrafts(JSON.stringify({ drafts: [
    goodDraft,
    { ...goodDraft, target_entry_id: '99999999-9999-9999-9999-999999999999' },
    { ...newBase, source_passage: 'A sentence the page never said.' },
    { ...newBase, answer: 'x'.repeat(1300) },
    { ...newBase, answer: 'Her FIN is G1234567X.' },
  ] }), { newText: page, candidateIds: cand.map((c) => c.id), live: liveCand });
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
      // The REAL shape of kb_admin_match_live (014): a preview, never question/answer.
      if (fn === 'kb_admin_match_live') return [{ id: cand[0].id, chunk_type: 'qa_pair', similarity: 0.7, preview: cand[0].question }];
      if (fn === 'kb_admin_watch_propose') {
        const [, , , target, q, a, , , , , passage] = params;
        const t = target ? cand.find((c) => c.id === target) : null;
        const reason = FL.feeLockReason({ targetQuestion: t?.question ?? null, targetAnswer: t?.answer ?? null, proposedQuestion: q, proposedAnswer: a, category: 'process' });
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
    entryTexts: async (ids) => cand.filter((c) => ids.includes(c.id)).map((c) => ({ id: c.id, question: c.question, answer: c.answer })),
  });
  // This fake is a 015-only database: a 016 call without has016 is a named FAIL, not a crash.
  const first = await Pl.checkSource(src0, deps(pageFrom('page_v1.html'), JSON.stringify({ drafts: [] }))).catch((e) => ({ outcome: `crashed: ${e.message}` }));
  check('pipeline: the first read records a snapshot (and asks the model about the whole page), calling only 015\'s functions',
    first.outcome === 'first' && llmCalls === 1, first.outcome);
  const same = await Pl.checkSource(src0, deps(pageFrom('page_v1_reflowed.html'), JSON.stringify({ drafts: [goodDraft] })));
  check('pipeline: whitespace and date stamps only -> recorded as unchanged, NO model call, NO draft',
    same.outcome === 'unchanged' && llmCalls === 1 && db1.proposals.length === 0);
  const changed = await Pl.checkSource(src0, deps(pageFrom('page_v2_changed.html'), JSON.stringify({ drafts: [goodDraft] })));
  check('pipeline: a real change -> the closest entries are found, the model is asked, one proposal is made with its passage',
    changed.outcome === 'changed' && llmCalls === 2 && db1.proposals.length === 1 && db1.proposals[0].passage.startsWith('From 1 January 2027') &&
    db1.log.includes('kb_admin_match_live'), JSON.stringify(changed));
  const feeAnswer = JSON.stringify({ drafts: [{ ...goodDraft, add: [{ sentence: 'From 2027 she may be paid S$40 in lieu.', after: '', source_passage: restPassage }] }] });
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
  const approveRun = async (failOn, confirmFigure = false) => {
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
      result = await Pl.approveAndPublish({ userId: 'u1', email: 'a@x' }, { id: 'd1', question: 'Q?', answer: 'A.', confirmFigure }, 'page changed',
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
    const ticked = await approveRun(null, true);
    check('approve: the approver\'s figure confirmation reaches the database (the 5th, boolean argument)',
      ok.calls[0].params.length === 5 && ok.calls[0].params[4] === false && ticked.calls[0].params[4] === true);
    const pubFail = await approveRun('kb_admin_publish');
    check('approve: a publish that fails leaves the draft approved but NOT marked published, and says so',
      pubFail.result?.published === false && pubFail.result.versionId === 'v1' && !pubFail.calls.some((c) => c.fn === 'kb_admin_watch_mark_published'));
  }

  // --- wiring: switched off unless asked for --------------------------------
  const watchActions = src('app/watch/actions.ts');
  const approveBody = watchActions.slice(watchActions.indexOf('export async function approveWatchDraft'));
  check('approving checks canApprove before anything reaches the database',
    approveBody.indexOf('actor.canApprove') > 0 && approveBody.indexOf('actor.canApprove') < approveBody.indexOf('approveAndPublish('));
  check('approving a money figure without the tick is refused before anything reaches the database (KB008)',
    /if \(figure && !confirmFigure\) return \{ error: errorMessage\('KB008', ''\) \};/.test(approveBody) &&
    approveBody.indexOf('!confirmFigure') < approveBody.indexOf('approveAndPublish(') && /confirmFigure \}/.test(approveBody));
  {
    const page = src('app/watch/[id]/page.tsx');
    check('the review page shows the figure warning and makes the approver tick the box',
      /This changes a figure: check it against the source before approving/.test(page) &&
      /confirm=\{figure \|\| conflict \? \{\s*name: 'confirm_figure'/.test(page) && /category: draft\.source_category/.test(page));
    check('the tick box is required in the form', /type="checkbox" name=\{confirm\.name\} value="yes" required/.test(src('components/ReasonAction.tsx')));
  }
  check('every watcher action refuses while the watcher is off', (watchActions.match(/const off = watcherOff\(\);/g) ?? []).length === 8);
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
      matcher === "'/((?!login|api/watcher/run|_next/static|_next/image|favicon.ico|brand/).*)'", matcher);
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

  // --- pages that gave "no readable main text" (2026-10-15) ---------------------
  const EP = await import(pathToFileURL(join(ROOT, 'lib/watcher/emptyPage.ts')).href);
  const RC = await import(pathToFileURL(join(ROOT, 'lib/watcher/renderClient.ts')).href);
  const momIndex = X.extractMainText(fx('mom_levy_aspnet_form.html'));
  check('extraction: the real MOM levy page (the whole page inside one ASP.NET <form>) is read, not dropped',
    momIndex.text.length >= 40 && momIndex.text.includes('You must pay a monthly levy for your migrant domestic worker') && momIndex.title === 'Migrant domestic worker levy',
    `${momIndex.text.length} chars`);
  check('extraction: on the MOM page, the hidden search keywords and the search form outside the main content are still dropped',
    !/PH pay, COMPASS|popular-keyword/i.test(momIndex.text));
  const momRates = X.extractMainText(fx('mom_paying_levy.html'));
  check('extraction: the real MOM "paying levy" page keeps its rate table ($300, $450, $60)',
    momRates.paragraphs.includes('Levy rate') && ['$300', '$450', '$60'].every((f) => momRates.paragraphs.includes(f)), `${momRates.paragraphs.length} paragraphs`);
  check('extraction: a form that does NOT wrap the main content (a search box) is still dropped whole, inputs and all',
    X.extractMainText('<body><main><p>Rule one applies to every employer in Singapore today.</p><form><label>SEARCH-LABEL</label><input value="INPUT-VALUE"><textarea>TEXTAREA</textarea></form></main></body>').text === 'Rule one applies to every employer in Singapore today.');
  {
    const listing = X.extractMainText('<body><article><h2>First news item headline</h2><p>The embassy will be closed on the national day holiday this year.</p></article><article><h2>Second news item headline</h2><p>Passport renewal appointments now open online for all Filipino workers.</p></article><div><p>Consular services and opening hours for the public.</p></div></body>');
    check('extraction: a listing page with several <article>s is read whole, not cut to the first teaser',
      listing.paragraphs.includes('Second news item headline') && listing.paragraphs.includes('Consular services and opening hours for the public.'), listing.paragraphs.join(' | '));
  }
  check('extraction: a "Loading..." / "Please wait" line is never content', X.extractMainText('<body><main><p>Loading...</p><p>Please wait</p></main></body>').text === '');
  check('empty page: the real Indonesian embassy page is a JavaScript app shell with no readable text',
    X.extractMainText(fx('kemlu_spa_shell.html')).text.length < 40 && EP.classifyEmptyPage(fx('kemlu_spa_shell.html'), 'https://kemlu.go.id/singapore') === 'script');
  const cfPage = '<html><head><title>Just a moment...</title></head><body><div id="cf-challenge-running"></div><script src="https://challenges.cloudflare.com/x.js"></script></body></html>';
  check('empty page: a bot-protection page (Cloudflare, Incapsula, Akamai) is "blocked"',
    EP.classifyEmptyPage(cfPage, 'https://a.gov.sg/p') === 'blocked' && EP.classifyEmptyPage('<html><body>Request unsuccessful. Incapsula incident ID: 123</body></html>', null) === 'blocked' &&
    EP.classifyEmptyPage('<html><head><title>Access Denied</title></head><body>Reference #18.abc12.1712.de3</body></html>', null) === 'blocked');
  check('empty page: a redirect to a /maintenance/ page, or a maintenance title, is "maintenance"',
    EP.classifyEmptyPage('<html><body></body></html>', 'https://www.mom.gov.sg/maintenance/index.html') === 'maintenance' &&
    EP.classifyEmptyPage('<html><head><title>Site under maintenance</title></head><body></body></html>', 'https://a.gov.sg/p') === 'maintenance');
  check('empty page: a page with nothing and none of those marks is just "empty"', EP.classifyEmptyPage('<html><body><p>Hi</p></body></html>', 'https://a.gov.sg/p') === 'empty');
  check('empty page: the messages say what to do, and bot protection is never worked around',
    /Blocked by site/.test(EP.emptyPageMessage('blocked', null, { renderer: true, rendered: false })) && /does not try to get past it/.test(EP.emptyPageMessage('blocked', null, { renderer: true, rendered: false })) &&
    /PDF or print version/.test(EP.emptyPageMessage('blocked', null, { renderer: false, rendered: false })) &&
    /under maintenance/.test(EP.emptyPageMessage('maintenance', 'https://x.gov.sg/maintenance/', { renderer: false, rendered: false })) &&
    /needs the page renderer/.test(EP.emptyPageMessage('script', null, { renderer: false, rendered: false })));
  {
    const n = fakeNet({ 'https://a.gov.sg/robots.txt': { status: 404 }, 'https://a.gov.sg/p': [{ status: 403, body: cfPage }] });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://a.gov.sg/p');
    check('fetch: a 403 bot-protection answer is reported as "Blocked by site" and NOT retried',
      !r.ok && /^Blocked by site/.test(r.error) && n.calls.filter((c) => c.url === 'https://a.gov.sg/p').length === 1, r.error ?? '');
  }
  {
    const recaptcha = { status: 200, headers: { 'content-type': 'text/html' }, body: fx('page_v1.html').replace('</body>', '<div class="g-recaptcha"></div></body>') };
    const n = fakeNet({ 'https://a.gov.sg/robots.txt': { status: 404 }, 'https://a.gov.sg/p': recaptcha });
    const r = await new Fe.PoliteFetcher(n.deps).fetchPage('https://a.gov.sg/p');
    check('fetch: a working page that carries a reCAPTCHA form (the Philippine embassy\'s) is NOT called blocked', r.ok);
  }
  {
    const n = fakeNet({ 'https://kemlu.go.id/robots.txt': { status: 200, body: 'User-agent: *\nDisallow: /private\nCrawl-delay: 20' } });
    const renders = [];
    n.deps.render = async (u) => { renders.push({ u, at: n.deps.now() }); return { ok: true, status: 200, html: fx('page_v1.html'), finalUrl: u }; };
    const f = new Fe.PoliteFetcher(n.deps);
    const denied = await f.renderPage('https://kemlu.go.id/private/x');
    const r1 = await f.renderPage('https://kemlu.go.id/singapore');
    const r2 = await f.renderPage('https://kemlu.go.id/singapore/faq');
    check('render: robots.txt is read first and obeyed - a disallowed page is never sent to the renderer',
      !denied.ok && /robots\.txt/.test(denied.error) && n.calls[0].url === 'https://kemlu.go.id/robots.txt' && !renders.some((x) => x.u.includes('/private')));
    check('render: two renders of one host keep the per-host gap (and the Crawl-delay)', r1.ok && r2.ok && renders[1].at - renders[0].at >= 20_000, `${renders[1]?.at - renders[0]?.at} ms`);
    check('render: without a renderer it reports, and a refused address is refused before any call',
      !(await new Fe.PoliteFetcher(fakeNet({}).deps).renderPage('https://kemlu.go.id/x')).ok && !(await f.renderPage('https://127.0.0.1/x')).ok);
  }
  {
    // The pipeline: a JavaScript shell is rendered; a challenge is never sent to the renderer.
    const run = async (page, renderPage) => {
      const rec = [];
      let renders = 0;
      const r = await Pl.checkSource(src0, {
        call: async (fn, p) => { if (fn === 'kb_admin_source_record_fetch') { rec.push(p); return { snapshot_id: 's', changed: true, first: true }; } return []; },
        fetchPage: async () => page, renderPage: renderPage ? async (u) => { renders += 1; return renderPage(u); } : undefined,
        previousText: async () => null, embed: async (t) => ({ vectors: t.map(() => []), model: 'm' }), recorder: null, searcher: { userId: 'u', email: 'w' }, entryTexts: async () => [],
        llm: async () => '{"drafts":[]}', model: 'm',
      });
      return { r, rec, renders };
    };
    const shell = { ok: true, status: 200, html: fx('kemlu_spa_shell.html'), finalUrl: 'https://kemlu.go.id/singapore' };
    const ok = await run(shell, async (u) => ({ ok: true, status: 200, html: fx('page_v1.html'), finalUrl: u }));
    check('pipeline: a JavaScript-built page is rendered and its text recorded as a good snapshot',
      ok.r.outcome === 'first' && ok.renders === 1 && ok.rec[0][3] === true && ok.rec[0][6].includes('must get at least one rest day'));
    const none = await run(shell, null);
    check('pipeline: with no renderer set up, a JavaScript-built page is recorded as failed, saying it needs the renderer',
      none.r.outcome === 'failed' && /needs the page renderer/.test(none.r.error) && none.rec[0][3] === false);
    const blocked = await run({ ok: true, status: 200, html: cfPage, finalUrl: 'https://a.gov.sg/p' }, async () => ({ ok: true, status: 200, html: fx('page_v1.html'), finalUrl: 'x' }));
    check('pipeline: a bot-protection page is recorded as "Blocked by site" and is NEVER sent to the renderer',
      blocked.r.outcome === 'failed' && /^Blocked by site/.test(blocked.r.error) && blocked.renders === 0);
    const maint = await run({ ok: true, status: 200, html: '<html><body></body></html>', finalUrl: 'https://www.mom.gov.sg/maintenance/' }, null);
    check('pipeline: a redirect to a maintenance page says "under maintenance", with where it landed',
      maint.r.outcome === 'failed' && /under maintenance/.test(maint.r.error) && maint.r.error.includes('https://www.mom.gov.sg/maintenance/'));
    const failedRender = await run(shell, async () => ({ ok: false, status: null, error: 'the page renderer: timed out after 30s' }));
    check('pipeline: a renderer failure is recorded with its reason, never hidden', failedRender.r.outcome === 'failed' && /timed out/.test(failedRender.r.error));
  }
  {
    const sent = [];
    const renderer = RC.makeRenderer({ url: 'https://bot.example.sg/kb-render/render', secret: 's'.repeat(40) }, async (u, init) => {
      sent.push({ u, init });
      return { status: 200, json: async () => ({ ok: true, status: 200, finalUrl: 'https://kemlu.go.id/id/singapore', html: '<p>x</p>' }) };
    });
    const r = await renderer('https://kemlu.go.id/singapore');
    check('render client: POSTs {url} with "Bearer <KB_RENDER_SECRET>" and a timeout, and reads the rendered page',
      r.ok && r.finalUrl === 'https://kemlu.go.id/id/singapore' && sent[0].init.headers.authorization === `Bearer ${'s'.repeat(40)}` &&
      JSON.parse(sent[0].init.body).url === 'https://kemlu.go.id/singapore' && Boolean(sent[0].init.signal));
    const refused = await RC.makeRenderer({ url: 'https://x.sg/r', secret: 's'.repeat(40) }, async () => ({ status: 404, json: async () => ({ error: 'not found' }) }))('https://a.gov.sg/');
    check('render client: a refused key says so', !refused.ok && /KB_RENDER_SECRET/.test(refused.error));
  }
  check('config: the page renderer is off by default; KB_RENDER_URL + KB_RENDER_SECRET turn it on; one alone leaves it off with a reason',
    cf({ ...on, KB_SOURCES_ENABLED: 'true' }).watcher?.render === null &&
    cf({ ...on, KB_SOURCES_ENABLED: 'true', KB_RENDER_URL: 'https://bot.example.sg/kb-render/render', KB_RENDER_SECRET: 'r'.repeat(40) }).watcher?.render?.url === 'https://bot.example.sg/kb-render/render' &&
    cf({ ...on, KB_SOURCES_ENABLED: 'true', KB_RENDER_URL: 'https://bot.example.sg/kb-render/render' }).watcher?.render === null &&
    /KB_RENDER_SECRET not set/.test(cf({ ...on, KB_SOURCES_ENABLED: 'true', KB_RENDER_URL: 'https://bot.example.sg/kb-render/render' }).watcherOffReason));
  check('config: a non-https renderer address or a short renderer secret refuses to start',
    refusesW({ ...on, KB_RENDER_URL: 'http://1.2.3.4:8100/render' }) && refusesW({ ...on, KB_RENDER_SECRET: 'short' }));
  check('both fetch paths (Fetch now and the schedule) use the shared fetcher with the renderer, never their own',
    /renderPage: renders \? \(u\) => fetcher\.renderPage\(u\) : undefined/.test(src('app/watch/actions.ts')) &&
    /renderPage: renders \? \(u\) => fetcher\.renderPage\(u\) : undefined/.test(src('app/api/watcher/run/route.ts')) &&
    !/new PoliteFetcher/.test(src('app/watch/actions.ts') + src('app/api/watcher/run/route.ts')));
  {
    const RS = join(ROOT, '..', 'render-service');
    const G = await import(pathToFileURL(join(RS, 'guard.mjs')).href);
    const server = readFileSync(join(RS, 'server.mjs'), 'utf8');
    const compose = readFileSync(join(RS, 'docker-compose.yml'), 'utf8');
    const refusesR = (e) => { try { G.readConfig(e); return false; } catch (x) { return /refuses to start/.test(x.message); } };
    check('render service: refuses to start without a 32+ character secret, or with a per-host gap under 10 s',
      refusesR({}) && refusesR({ RENDER_SECRET: 'short' }) && refusesR({ RENDER_SECRET: 'x'.repeat(40), RENDER_MIN_GAP_MS: '1000' }) && !refusesR({ RENDER_SECRET: 'x'.repeat(40) }));
    check('render service: the key is checked in constant time and anything without it is a 404',
      G.secretMatches(`Bearer ${'k'.repeat(40)}`, 'k'.repeat(40)) && !G.secretMatches(`Bearer ${'k'.repeat(39)}`, 'k'.repeat(40)) && !G.secretMatches('k'.repeat(40), 'k'.repeat(40)) &&
      /!secretMatches\(req\.headers\.authorization, cfg\.secret\)\) return send\(res, 404/.test(server) && /timingSafeEqual/.test(readFileSync(join(RS, 'guard.mjs'), 'utf8')));
    check('render service: private, loopback, link-local and metadata addresses are refused (the browser runs beside the bot)',
      ['127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1', '169.254.169.254', '100.64.0.1', '0.0.0.0', '::1', 'fd00::1', 'fe80::1', '::ffff:127.0.0.1'].every(G.isPrivateAddress) &&
      !['8.8.8.8', '202.10.51.219', '2404:6800:4002:81b::200e'].some(G.isPrivateAddress));
    check('render service: every request the page makes goes through the public-host check; images, media and fonts are not loaded',
      /context\.route\('\*\*\/\*'[\s\S]*?hostIsPublic\(u\.hostname\)[\s\S]*?route\.abort/.test(server) && /\['image', 'media', 'font'\]\.includes\(req\.resourceType\(\)\)/.test(server));
    check('render service: the watcher\'s user-agent, one page at a time, and a per-host gap',
      G.USER_AGENT === Fe.USER_AGENT && /userAgent: USER_AGENT/.test(server) && /queue = queue\.then\(/.test(server) && /lastHit\.set\(host, Date\.now\(\)\)/.test(server));
    check('render service: its own container, reachable only from the server (127.0.0.1), never part of the bot\'s compose file',
      /"127\.0\.0\.1:8100:8100"/.test(compose) && !/render/.test(readFileSync(join(ROOT, '..', 'docker-compose.yml'), 'utf8')) && !/from '\.\.\/app|import .*app\//.test(server));
  }
}

console.log('KB watcher drafting: overlap check, low-value proposals, index pages, re-draft (2026-10-16; migration 016 on production):');
{
  const fx = (n) => readFileSync(join(ROOT, 'scripts/fixtures/watcher', n), 'utf8');
  const X = await import(pathToFileURL(join(ROOT, 'lib/watcher/extract.ts')).href);
  const Q = await import(pathToFileURL(join(ROOT, 'lib/watcher/quality.ts')).href);
  const IP = await import(pathToFileURL(join(ROOT, 'lib/watcher/indexPage.ts')).href);
  const OV = await import(pathToFileURL(join(ROOT, 'lib/watcher/overlap.ts')).href);
  const Dr = await import(pathToFileURL(join(ROOT, 'lib/watcher/drafting.ts')).href);
  const Pl = await import(pathToFileURL(join(ROOT, 'lib/watcher/pipeline.ts')).href);
  const LEVY = 'https://www.mom.gov.sg/passes-and-permits/work-permit-for-foreign-domestic-worker/foreign-domestic-worker-levy';
  const indexHtml = fx('mom_levy_aspnet_form.html');
  const payingHtml = fx('mom_paying_levy.html');
  const indexX = X.extractMainText(indexHtml);
  const payingX = X.extractMainText(payingHtml);

  // --- 2. low-value proposals -------------------------------------------------
  // The four production proposals pointed elsewhere like these.
  const pointers = [
    ['Where can I find information about the MDW levy?', 'The MOM page provides information on paying levy, levy concession and levy waiver.'],
    ['What does the MOM levy page cover?', 'The MOM page provides information on paying levy, levy concession and levy waiver.'],
    ['What is the levy concession?', 'You may also qualify for a levy concession. Find out if you qualify for levy concession for your MDW.'],
    ['How do I pay the levy?', 'For more information on how to pay the levy, visit the MOM website.'],
  ];
  check('quality: a proposal that only points elsewhere is dropped (the production MOM-index shape, 4 of 4)',
    pointers.every(([q, a]) => /points elsewhere/.test(Q.lowValueReason(q, a) ?? '')), pointers.map(([q, a]) => Q.lowValueReason(q, a)).join(' | '));
  check('quality: a proposal with no fact a client can act on is dropped',
    /no fact a client could act on/.test(Q.lowValueReason('What is the levy?', 'The levy is a pricing mechanism to regulate the number of foreigners in Singapore.') ?? ''));
  const keep = [
    ['How much is the monthly levy?', 'The levy is $300 a month for the first helper and $450 for each subsequent helper.'],
    ['When must I pay the levy?', 'You must pay the levy for each month by the 17th of the following month. For more information, visit the MOM website.'],
    ['Who qualifies for the concessionary rate?', 'The concessionary rate applies to families with a young child, elderly member or person with disability.'],
    ['How can I pay the levy?', 'You can apply for GIRO, or pay using PayNow QR if you have no GIRO arrangement.'],
  ];
  check('quality: an amount, a rule or deadline, an eligibility condition and a step each survive (a trailing pointer sentence is set aside)',
    keep.every(([q, a]) => Q.lowValueReason(q, a) === null), keep.map(([q, a]) => Q.lowValueReason(q, a)).join(' | '));
  {
    const pagePassage = 'You must pay a monthly levy for your migrant domestic worker (MDW).';
    const parsed = Dr.parseDrafts(JSON.stringify({ drafts: [
      { target_entry_id: null, question: pointers[0][0], answer: pointers[0][1], source_passage: pagePassage, why: '' },
      { target_entry_id: null, question: 'Must I pay a levy for my helper?', answer: 'Yes, you must pay a monthly levy for your helper.', source_passage: pagePassage, why: '' },
    ] }), { newText: indexX.text, candidateIds: [] });
    check('drafting: parseDrafts drops the pointer and keeps the fact', parsed.drafts.length === 1 && parsed.dropped.some((d) => /points elsewhere/.test(d)), parsed.dropped.join(' | '));
    const pr = Dr.buildPrompt({ label: 'MOM', url: LEVY, added: ['A'], removed: [], candidates: [] });
    check('drafting: the prompt asks for an actionable fact, no pointers, nothing from an index, and a change instead of a second entry',
      /an amount, a rule, a deadline, an eligibility condition or a step/.test(pr.system) && /The MOM page provides information on/.test(pr.system) &&
      /mainly a list of links to other pages, return no drafts/.test(pr.system) && /never propose a second entry/.test(pr.system) && /character for character/.test(pr.system));
  }

  // --- 2. index pages ----------------------------------------------------------
  const subs = IP.findSubPages(indexHtml, LEVY, indexX.paragraphs);
  check('index: the real MOM levy index offers its three sub-pages, paying-levy (the levy rates) among them',
    subs.length === 3 && subs[0].url === `${LEVY}/paying-levy` && subs[0].label === 'Paying levy' && subs.every((s) => s.url.startsWith(`${LEVY}/`)), JSON.stringify(subs));
  check('index: ...and is judged mainly an index', IP.indexVerdict(indexX.paragraphs, subs).index === true);
  check('index: the real paying-levy page is NOT an index (no sub-pages below it, 4,000+ characters of facts)',
    IP.findSubPages(payingHtml, `${LEVY}/paying-levy`, payingX.paragraphs).length === 0 && !IP.indexVerdict(payingX.paragraphs, []).index && !IP.looksLikeIndexText(payingX.paragraphs));
  check('index: from the text alone (a re-draft has no HTML) the index is still recognised, and paying-levy is not',
    IP.looksLikeIndexText(indexX.paragraphs) && !IP.looksLikeIndexText(X.extractMainText(fx('page_v1.html')).paragraphs));
  {
    const nav = `<nav><a href="${LEVY}/secret">Secret</a></nav><main><p>Intro</p><a href="${LEVY}/a"><h2>Alpha page</h2><p>x</p></a>
      <a href="https://evil.example.com/x">Alpha page</a><a href="${LEVY}/b.pdf">Beta page</a><a href="${LEVY}/c?x=1">Gamma page</a><a href="/other/d">Delta page</a></main>`;
    const got = IP.findSubPages(nav, LEVY, ['Intro', 'Alpha page', 'Beta page', 'Gamma page', 'Delta page']);
    check('index: only links below the page, on the same site, https, no file, no query, whose label is in the main text',
      got.length === 1 && got[0].url === `${LEVY}/a`, JSON.stringify(got));
  }

  // --- 1. the overlap check ------------------------------------------------
  const RATES = 'The monthly levy is $300 for a normal first helper, $450 for subsequent helpers, or $60 at the concessionary rate. If a helper does not complete a full calendar month, the daily rates are $9.87, $14.80 and $1.98.';
  check('overlap: a levy rate against salary figures is NOT a conflict (different subject)',
    OV.compareFigures(RATES, 'Helper salary: Filipino from S$650 a month, Indonesian from S$550.').conflicts.length === 0);
  check('overlap: one sentence listing salary, levy and insurance is read figure by figure - a $450 levy is not "against" a $500 salary',
    OV.compareFigures(RATES, 'Your ongoing monthly costs include: helper salary (from S$500 depending on nationality), monthly levy (S$300 or S$60 concessionary), medical insurance (about S$200 a year) and personal expenses (S$950).').conflicts.length === 0);
  check('overlap: a daily rate against a yearly saving is NOT a conflict (different period)',
    OV.compareFigures(RATES, 'The concessionary levy of S$60/month saves S$2,880/year compared to the standard S$300/month.').conflicts.length === 0);
  check('overlap: the same subject over a different period is NOT a conflict ($1.98 a day against $60 a month for the concessionary levy)',
    OV.compareFigures('The concessionary levy is $1.98 a day.', 'The concessionary levy is $60 a month.').conflicts.length === 0 &&
    OV.compareFigures('The concessionary levy is $55 a month.', 'The concessionary levy is $60 a month.').conflicts.length === 1);
  check('overlap: one text stating MORE figures than the other is listed but is not a conflict',
    OV.compareFigures(RATES, 'The levy is $300 a month at the normal rate and $60 at the concessionary rate.').conflicts.length === 0 &&
    OV.compareFigures(RATES, 'The levy is $300 a month at the normal rate and $60 at the concessionary rate.').differs.proposal.includes('$450'));
  {
    const c = OV.compareFigures(RATES, 'The monthly levy is $265 for your first helper.');
    check('overlap: $300 against $265 for the same monthly levy IS a conflict', c.conflicts.some((x) => /\$300 against \$265 \(levy/.test(x)), JSON.stringify(c));
  }
  {
    const live = (id, sim, question = 'Q', answer = 'A') => ({ id, question, answer, similarity: sim });
    const a = OV.assessOverlap({ question: 'How much is the levy?', answer: 'The monthly levy is $300 for the first helper.', targetId: null }, [
      live('11111111-1111-1111-1111-111111111111', 0.81, 'How much is the monthly levy?', 'The monthly levy is $265 for the first helper.'),
      live('22222222-2222-2222-2222-222222222222', 0.55),
    ]);
    check('overlap: a NEW proposal duplicating a live entry (0.81) with a different levy figure is a conflict, noted, and the 0.55 entry is not listed',
      a.conflict && a.similar.length === 1 && /already answers this/.test(a.note) && /\$300 against \$265/.test(a.note), JSON.stringify(a));
    const e = OV.assessOverlap({ question: 'How much is the levy?', answer: 'It is $300.', targetId: '11111111-1111-1111-1111-111111111111' }, [
      live('11111111-1111-1111-1111-111111111111', 0.95, 'x', 'It is $265 a month for the levy.'),
    ]);
    check('overlap: a change to an entry never lists (or conflicts with) that entry itself', !e.conflict && e.similar.length === 0);
    const many = OV.assessOverlap({ question: 'q', answer: 'a', targetId: '99999999-9999-9999-9999-999999999999' }, Array.from({ length: 9 }, (_, i) => live(`aaaaaaaa-0000-0000-0000-00000000000${i}`, 0.7 + i / 100)));
    check('overlap: at most five similar entries, best first', many.similar.length === 5 && many.similar[0].similarity === 0.78);
    check('overlap: the thresholds are the measured ones (change instead >= 0.72, listed >= 0.60)', OV.EDIT_INSTEAD_SIMILARITY === 0.72 && OV.SIMILAR_SIMILARITY === 0.6);
  }

  // --- the pipeline with all three, on a fake database ------------------------
  const ENTRY = { id: 'cccccccc-0000-0000-0000-000000000001', question: 'How much is the monthly levy?', answer: 'At the normal rate it is $300 a month for your first helper.' };
  const fake = (opts = {}) => {
    const calls = [];
    const call = async (fn, params) => {
      calls.push({ fn, params });
      if (opts.no016 && ['kb_admin_source_set_suggestions', 'kb_admin_watch_propose_checked', 'kb_admin_source_redraft'].includes(fn)) throw new Error(`function ${fn} does not exist`);
      if (fn === 'kb_admin_source_record_fetch') return { snapshot_id: 'snap-1', changed: true, first: true };
      if (fn === 'kb_admin_source_set_suggestions') return (params[3] ?? []).length;
      if (fn === 'kb_admin_match_live') {
        if (opts.searchFails) throw new Error('permission denied for function kb_admin_match_live');
        return opts.match ? [{ id: ENTRY.id, chunk_type: 'qa_pair', similarity: opts.match, preview: ENTRY.question }] : [];
      }
      if (fn === 'kb_admin_watch_propose' || fn === 'kb_admin_watch_propose_checked') return { draft_id: `d-${calls.length}`, status: 'pending', blocked_reason: null };
      if (fn === 'kb_admin_source_redraft') return { snapshot_id: 'snap-9', previous_snapshot_id: opts.prev ? 'snap-8' : null, superseded: 2 };
      throw new Error(`unexpected ${fn}`);
    };
    return { calls, call, fns: () => calls.map((c) => c.fn) };
  };
  const llmSeq = (answers) => { const asked = []; return { asked, llm: async (system, user) => { asked.push({ system, user }); return answers.shift() ?? '{"drafts":[]}'; } }; };
  const baseDeps = (db, m, has016 = true) => ({
    call: db.call, previousText: async () => null, embed: async (texts) => ({ vectors: texts.map(() => Array(1536).fill(0.01)), model: 'm' }),
    recorder: { userId: 'u', email: 'e@x' }, searcher: { userId: 'u', email: 'e@x' }, llm: m.llm, model: 'test-model', has016,
    entryTexts: async (ids) => (ids.includes(ENTRY.id) ? [{ id: ENTRY.id, question: ENTRY.question, answer: ENTRY.answer }] : []),
  });
  const src16 = { id: 'aaaaaaaa-0000-0000-0000-000000000016', url: LEVY, label: 'MOM levy' };
  {
    const db = fake(); const m = llmSeq([]);
    const r = await Pl.checkSource(src16, { ...baseDeps(db, m), fetchPage: async () => ({ ok: true, status: 200, html: indexHtml, finalUrl: LEVY }) });
    const sug = db.calls.find((c) => c.fn === 'kb_admin_source_set_suggestions');
    check('pipeline: the MOM index drafts NOTHING - no model call, no proposal - and records its three sub-pages as suggestions',
      r.drafts.length === 0 && m.asked.length === 0 && !db.fns().some((f) => /propose/.test(f)) && r.index?.suggestions.length === 3 &&
      sug?.params[3]?.length === 3 && sug.params[3][0].url === `${LEVY}/paying-levy`, JSON.stringify(r));
    // Before 016 the fake refuses the 016 functions, as the database would (they do not exist yet).
    const db0 = fake({ no016: true });
    await Pl.checkSource(src16, { ...baseDeps(db0, llmSeq([]), false), fetchPage: async () => ({ ok: true, status: 200, html: indexHtml, finalUrl: LEVY }) }).catch(() => null);
    check('pipeline: before 016 is applied nothing new is called (no suggestions), and still nothing is drafted from the index',
      !db0.fns().includes('kb_admin_source_set_suggestions') && !db0.fns().some((f) => /propose/.test(f)));
  }
  const payingPage = async () => ({ ok: true, status: 200, html: payingHtml, finalUrl: `${LEVY}/paying-levy` });
  const passage = 'You must pay the levy for each month by the 17th of the following month (or the next working day if it falls on a Saturday, Sunday or public holiday).';
  const newDraft = { target_entry_id: null, question: 'When must I pay the levy?', answer: 'By the 17th of the following month, or the next working day.', source_passage: passage, why: '' };
  {
    const db = fake(); const m = llmSeq([JSON.stringify({ drafts: [newDraft] })]);
    await Pl.checkSource({ ...src16, url: `${LEVY}/paying-levy` }, { ...baseDeps(db, m), fetchPage: payingPage });
    const p = db.calls.find((c) => c.fn === 'kb_admin_watch_propose_checked');
    check('pipeline (016): the paying-levy page is drafted from, and a proposal goes through kb_admin_watch_propose_checked with its overlap (16 arguments; no similar entry = null)',
      m.asked.length === 1 && p && p.params.length === 16 && p.params[13] === null && p.params[14] === false && db.calls.find((c) => c.fn === 'kb_admin_source_set_suggestions')?.params[3] === null);
    const db0 = fake({ no016: true }); await Pl.checkSource({ ...src16, url: `${LEVY}/paying-levy` }, { ...baseDeps(db0, llmSeq([JSON.stringify({ drafts: [newDraft] })]), false), fetchPage: payingPage }).catch(() => null);
    check('pipeline (before 016): the same proposal goes through 015\'s kb_admin_watch_propose with its 13 arguments',
      db0.calls.find((c) => c.fn === 'kb_admin_watch_propose')?.params.length === 13 && !db0.fns().includes('kb_admin_watch_propose_checked'));
  }
  {
    const merged = { target_entry_id: ENTRY.id, add: [{ sentence: 'Pay it by the 17th of the following month.', after: '', source_passage: passage }], replace: [], why: '' };
    const db = fake({ match: 0.81 }); const m = llmSeq([JSON.stringify({ drafts: [newDraft] }), JSON.stringify({ drafts: [merged] })]);
    const r = await Pl.checkSource({ ...src16, url: `${LEVY}/paying-levy` }, { ...baseDeps(db, m), fetchPage: payingPage });
    const p = db.calls.find((c) => c.fn === 'kb_admin_watch_propose_checked');
    check('pipeline: a NEW draft that duplicates a live entry (0.81) becomes a CHANGE to that entry (a second model call says what to add; the entry is built from it)',
      m.asked.length === 2 && /EXISTING entry id cccccccc/.test(m.asked[1].user) && p?.params[3] === ENTRY.id && r.drafts[0]?.kind === 'edit' &&
      p?.params[5] === `${ENTRY.answer} Pay it by the 17th of the following month.` && p?.params[4] === ENTRY.question, JSON.stringify(r));
    check('pipeline: ...and the entry it changes is not listed as "similar" to itself', p?.params[13] === null);
    const db2 = fake({ match: 0.81 }); const m2 = llmSeq([JSON.stringify({ drafts: [newDraft] }), '{"drafts":[]}']);
    const r2 = await Pl.checkSource({ ...src16, url: `${LEVY}/paying-levy` }, { ...baseDeps(db2, m2), fetchPage: payingPage });
    check('pipeline: if the live entry already says it all, nothing is proposed (never a second answer)',
      r2.drafts.length === 0 && r2.dropped.some((d) => /already answers/.test(d)) && !db2.fns().some((f) => /propose/.test(f)));
    const db3 = fake({ match: 0.65 }); const m3 = llmSeq([JSON.stringify({ drafts: [newDraft] })]);
    await Pl.checkSource({ ...src16, url: `${LEVY}/paying-levy` }, { ...baseDeps(db3, m3), fetchPage: payingPage });
    const p3 = db3.calls.find((c) => c.fn === 'kb_admin_watch_propose_checked');
    check('pipeline: a partial overlap (0.65) stays a new entry, with the similar entry recorded on it',
      m3.asked.length === 1 && p3?.params[3] === null && p3.params[13]?.[0]?.id === ENTRY.id && p3.params[13][0].similarity === 0.65);
  }

  // --- 3. re-draft ------------------------------------------------------------
  {
    const db = fake(); const m = llmSeq([JSON.stringify({ drafts: [newDraft] })]);
    // One timeline for database calls and snapshot reads, so the ORDER is checked.
    const r = await Pl.redraftSource({ ...src16, suggested: [] }, 'rules improved', { ...baseDeps(db, m), snapshotText: async (id) => { db.calls.push({ fn: 'snapshotText', params: [id] }); return id === 'snap-9' ? payingX.text : null; } });
    // Only the read-only preflight search (prove the existing-entry check works) may come before it.
    const rd = db.calls.findIndex((c) => c.fn === 'kb_admin_source_redraft');
    check('re-draft: kb_admin_source_redraft runs FIRST (after only the read-only search preflight; the database checks the approver and audits the reason), then the latest snapshot is drafted from',
      rd >= 0 && db.calls.slice(0, rd).every((c) => c.fn === 'kb_admin_match_live') && db.calls[rd].params[3] === 'rules improved' && db.calls[rd + 1].fn === 'snapshotText' && db.calls[rd + 1].params[0] === 'snap-9' && m.asked.length === 1 &&
      db.calls.find((c) => c.fn === 'kb_admin_watch_propose_checked')?.params[2] === 'snap-9' && r.superseded === 2, JSON.stringify(r));
    const dbI = fake(); const mI = llmSeq([JSON.stringify({ drafts: [newDraft] })]);
    const rI = await Pl.redraftSource({ ...src16, suggested: [{ url: `${LEVY}/paying-levy`, label: 'Paying levy' }] }, 'x', { ...baseDeps(dbI, mI), snapshotText: async () => payingX.text });
    const dbT = fake(); const mT = llmSeq([]);
    const rT = await Pl.redraftSource({ ...src16, suggested: [] }, 'x', { ...baseDeps(dbT, mT), snapshotText: async () => indexX.text });
    check('re-draft: an index page (by its stored suggestions, or from the text) drafts nothing - the production MOM index would now propose nothing',
      rI.index && rI.drafts.length === 0 && mI.asked.length === 0 && rT.index && mT.asked.length === 0);
    const dbP = fake({ prev: true }); const mP = llmSeq([]);
    await Pl.redraftSource({ ...src16, suggested: [] }, 'x', { ...baseDeps(dbP, mP), snapshotText: async (id) => (id === 'snap-9' ? payingX.text : payingX.text) });
    check('re-draft: with an earlier snapshot it drafts only from what CHANGED (identical texts -> no model call)', mP.asked.length === 0);
  }

  // --- wiring -------------------------------------------------------------------
  const acts = src('app/watch/actions.ts');
  const redraft = acts.slice(acts.indexOf('export async function redraftSourceAction'), acts.indexOf('export async function watchSuggestedPage'));
  check('re-draft action: an approver only, checked before anything reaches the database, and only with 016',
    redraft.indexOf('actor.canApprove') > 0 && redraft.indexOf('actor.canApprove') < redraft.indexOf('redraftSource(') &&
    redraft.indexOf('watcherSchema16()') < redraft.indexOf('redraftSource(') && /Please say why/.test(redraft));
  const suggested = acts.slice(acts.indexOf('export async function watchSuggestedPage'));
  check('"Watch this page": only an address the parent suggests, added with the PARENT\'s category through kb_admin_source_add',
    /\(parent\.suggested_pages \?\? \[\]\)\.find\(\(p\) => p\.url === url\)/.test(suggested) && /kb_admin_source_add', \[actor\.userId, actor\.email, url, label, parent\.category\]/.test(suggested));
  const approve = acts.slice(acts.indexOf('export async function approveWatchDraft'));
  check('approve: a conflicting proposal without the tick is refused before anything reaches the database (KB009)',
    /if \(draft\.conflict_check && !confirmFigure\) return \{ error: errorMessage\('KB009', ''\) \};/.test(approve) &&
    approve.indexOf('draft.conflict_check') < approve.indexOf('approveAndPublish(') && /'KB009'/.test(src('lib/editing.ts')));
  const review = src('app/watch/[id]/page.tsx');
  check('the review page shows "Similar existing entries" (links, similarity, differing figures) and the conflict banner, and asks for the tick',
    /Similar existing entries/.test(review) && /href=\{`\/rows\/\$\{e\.id\}`\}/.test(review) && /similarity \{e\.similarity\.toFixed\(2\)\}/.test(review) &&
    /This disagrees with an existing entry/.test(review) && /const conflict = open && !lock && draft\.conflict_check;/.test(review));
  const sources = src('app/sources/page.tsx');
  check('Watched pages: "Suggested pages to watch" with one-click "Watch this page", and "Re-draft from latest snapshot" for approvers only (with 016)',
    /Suggested pages to watch/.test(sources) && /<WatchSuggestedButton sourceId=\{s\.id\} url=\{p\.url\} \/>/.test(sources) &&
    /viewer\.canApprove && has16 && s\.last_checked_at/.test(sources) && /Re-draft from latest snapshot/.test(sources) && /Watch this page/.test(src('app/sources/SourceForms.tsx')));
  const qs = src('lib/watchQueries.ts');
  check('before 016 is applied the queries read what they did with 015 alone (no new column named)',
    /'\[\]'::jsonb as suggested_pages/.test(qs) && /'\[\]'::jsonb as similar_entries, false as conflict_check/.test(qs) && /export async function watcherSchema16/.test(qs));
  check('the schedule passes the 016 switch too', /has016,/.test(src('app/api/watcher/run/route.ts')));
  const llmTs = src('lib/watcher/llm.ts');
  check('the drafting call switches thinking off (as the bot does) and refuses an answer cut off at the token limit',
    /reasoning: \{ enabled: false \}/.test(llmTs) && /finish_reason === 'length'/.test(llmTs) && /max_tokens: 6000/.test(llmTs));
  check('the pipeline still imports no database, network or environment code',
    !/from '(postgres|\.\.?\/(db|write|env|embed)|@\/lib\/(db|write|env|embed))'|process\.env|(?<![.\w])fetch\(/.test(src('lib/watcher/pipeline.ts') + src('lib/watcher/overlap.ts') + src('lib/watcher/indexPage.ts') + src('lib/watcher/quality.ts')));

  // --- refinements (2026-10-16, after 016 went to production) ---------------
  // 1. Quote matching: spacing aside, the words and figures exactly.
  const FD = await import(pathToFileURL(join(ROOT, 'lib/watcher/factDiff.ts')).href);
  const payText = payingX.text;
  {
    const tidied = 'Normal (1st helper) $300 $9.87 Normal (Subsequent helpers) $450 $14.80';
    const span = Dr.locatePassage(payText, tidied);
    check('quote: the MOM table passage the model tidied ("1st" for "1 st", cells on one line) is found, and the PAGE\'s own span is kept',
      span !== null && /Normal \(1 st helper\)\n\$300/.test(span) && payText.includes(span), JSON.stringify(span));
    check('quote: tabs, line breaks, non-breaking and zero-width spaces are all spacing',
      Dr.locatePassage(payText, 'the 17th\tof the\nfollowing​ month') !== null && Dr.locatePassage(payText, 'apply for GIRO. If you do') !== null);
    check('quote: a different figure is NOT found ($350 for $300; $9.78 for $9.87)',
      Dr.locatePassage(payText, 'Normal (1st helper) $350') === null && Dr.locatePassage(payText, 'Normal (1st helper) $300 $9.78') === null);
    check('quote: a different or extra word is NOT found, and two words run together are not the same as two words',
      Dr.locatePassage(payText, 'Normal (2nd helper) $300') === null && Dr.locatePassage(payText, 'Normal (Subsequenthelpers) $450') === null &&
      Dr.locatePassage(payText, 'You must pay the levy for each week by the 17th') === null);
    const collapse = (s) => s.replace(/\s+/g, ' ');
    check('quote: the kept span passes the database\'s own check (whitespace collapsed only, KB002)', span !== null && collapse(payText).includes(collapse(span)));
    const parsed = Dr.parseDrafts(JSON.stringify({ drafts: [{ target_entry_id: null, question: 'How much is the levy for my first helper?', answer: 'The normal levy for your first helper is $300 a month, or $9.87 a day.', source_passage: 'Normal (1st helper) $300 $9.87', why: '' }] }), { newText: payText, candidateIds: [] });
    check('quote: parseDrafts keeps that draft and stores the page\'s span as its source passage',
      parsed.drafts.length === 1 && parsed.drafts[0].source_passage === 'Normal (1 st helper)\n$300\n$9.87', JSON.stringify(parsed));
  }

  // 2. A change must not lose facts.
  const FDW = { question: 'What is the FDW levy?', answer: 'The levy is a monthly fee the Government charges employers of Migrant Domestic Workers. It is a pricing mechanism to manage the number of foreign workers in Singapore — it is not a tax on your helper and it is never deducted from her salary. You will usually pay it through GIRO, which is the simplest way to avoid missing a payment. You must never recover it from your helper’s wages.' };
  // The dry run's proposal (2026-10-16), shortened.
  const FDW_NEW = 'The foreign worker levy, commonly known as “levy”, is a pricing mechanism to regulate the number of foreigners in Singapore. As an employer, you must pay a monthly levy for her. For first-time helpers, the levy begins on the 5th day of their arrival.';
  {
    const rp = FD.removedPoints(FDW.answer, FDW_NEW, 'The foreign worker levy, commonly known as “levy”, is a pricing mechanism', FDW.question);
    check('facts: the dry run\'s "What is the FDW levy?" change lists the points it drops (not a tax, GIRO, never from her wages) and none is justified',
      rp.some((r) => /not a tax/.test(r.sentence)) && rp.some((r) => /GIRO/.test(r.sentence)) && rp.some((r) => /recover it/.test(r.sentence)) &&
      FD.unjustifiedRemovals(FDW.answer, FDW_NEW, 'x', FDW.question).length === rp.length, JSON.stringify(rp));
    check('facts: a sentence the proposal still carries is not "removed" (same figure, paraphrased)',
      FD.removedPoints('Households that qualify for the levy concession pay $60 a month.', 'The levy is $300 per month for a first helper and $60 per month at the concessionary rate.', '').length === 0);
    check('facts: a sentence whose figure changed is removed - justified when the source states the new figure for the same thing, blocking when not',
      FD.removedPoints('The monthly levy is $300 for the first helper.', 'The monthly levy is $350 for the first helper.', 'Normal (1 st helper) monthly levy $350')[0]?.contradictedBy !== null &&
      FD.unjustifiedRemovals('The monthly levy is $300 for the first helper.', 'The monthly levy is $350 for the first helper.', 'Normal (1 st helper) monthly levy $350').length === 0 &&
      FD.unjustifiedRemovals('The monthly levy is $300 for the first helper.', 'The monthly levy is $350 for the first helper.', 'Apply for GIRO online.').length === 1);
    check('facts: nothing removed when the change only adds', FD.removedPoints(FDW.answer, `${FDW.answer} The levy ends when the Work Permit is cancelled.`, '').length === 0);
  }
  const review2 = src('app/watch/[id]/page.tsx');
  check('facts: the review page lists "Points removed" in red and blocks Approve with "This update drops existing information" (Edit stays)',
    /<h2 className="text-\[15px\] font-semibold">Points removed<\/h2>/.test(review2) && /color: 'var\(--bad\)'/.test(review2) &&
    /title="This update drops existing information"/.test(review2) && /const drops = open && removed\.some\(\(r\) => !r\.contradictedBy\);/.test(review2) &&
    /viewer\.canApprove && !lock && !stale && !drops \?/.test(review2) && /<EditProposalForm draftId=/.test(review2) &&
    /\{drops \? <PointsRemoved points=\{removed\.filter\(\(r\) => !r\.contradictedBy\)\} \/> : null\}/.test(review2));
  const actions2 = src('app/watch/actions.ts');
  check('facts: the approve action refuses it too (the page is not the only lock)',
    /unjustifiedRemovals\(draft\.current_answer, draft\.proposed_answer, draft\.source_passage, draft\.proposed_question\)\.length\) \{\n    return \{ error: 'This update drops existing information\./.test(actions2) &&
    actions2.indexOf('unjustifiedRemovals(draft.current_answer') < actions2.indexOf('result = await approveAndPublish('));
  {
    const p1 = Dr.buildPrompt({ label: 'l', url: 'u', added: [], removed: [], candidates: [] }).system;
    const p2 = OV.buildMergePrompt({ label: 'l', url: 'u', pairs: [] }).system;
    check('additive: both drafting prompts ask for additions and contradicted replacements only, never a rewritten entry',
      [p1, p2].every((x) => /DO NOT rewrite it/.test(x) && /"replace": only where the page DIRECTLY CONTRADICTS/.test(x) && /"add":/.test(x) &&
        x.includes('"add":[{"sentence":string,"after":string,"source_passage":string}]')) && !p2.includes('"answer":string'));
  }

  // 3. No rewording-only changes.
  {
    const live = { question: 'How much is the monthly levy?', answer: 'The monthly levy is $300 for the first helper and $450 for each subsequent helper. The concessionary rate is $60.' };
    check('reword: the same figures reworded is rewording only (the dry runs\' monthly-levy change before it added the daily rates)',
      /only rewords/.test(FD.rewordingOnlyReason(live, { question: 'How much is the monthly levy for a helper?', answer: 'The levy is $300 a month for your first helper, $450 for subsequent helpers, and $60 at the concessionary rate.' }) ?? ''));
    check('reword: a new deadline, a new figure or a new rule is something added',
      FD.rewordingOnlyReason(live, { ...live, answer: `${live.answer} Pay it by the 17th of the following month.` }) === null &&
      FD.rewordingOnlyReason(live, { ...live, answer: `${live.answer} The daily rate is $9.87.` }) === null &&
      FD.rewordingOnlyReason(live, { ...live, answer: `${live.answer} The concessionary rate applies to families with a young child, an elderly member or a person with a disability.` }) === null);
    const reworded = { target_entry_id: ENTRY.id, add: [{ sentence: 'For your first helper the normal rate is $300 a month.', after: '', source_passage: 'Normal (1st helper) $300' }], replace: [], why: '' };
    const db = fake({ match: 0.5 }); const m = llmSeq([JSON.stringify({ drafts: [reworded] })]);
    const r = await Pl.checkSource({ ...src16, url: `${LEVY}/paying-levy` }, { ...baseDeps(db, m), fetchPage: payingPage });
    check('reword: the pipeline does not propose a change that only rewords the live entry', r.drafts.length === 0 && r.dropped.some((d) => /already says/.test(d)) && r.dropped.some((d) => /adds nothing and replaces nothing/.test(d)) && !db.fns().some((f) => /propose/.test(f)), JSON.stringify(r));
    const adds = { ...reworded, add: [...reworded.add, { sentence: 'Pay it by the 17th of the following month.', after: '', source_passage: passage }] };
    const db2 = fake({ match: 0.5 }); const m2 = llmSeq([JSON.stringify({ drafts: [adds] })]);
    const r2 = await Pl.checkSource({ ...src16, url: `${LEVY}/paying-levy` }, { ...baseDeps(db2, m2), fetchPage: payingPage });
    check('reword: ...and does propose one that adds a deadline', r2.drafts.length === 1 && r2.drafts[0].kind === 'edit', JSON.stringify(r2));
    check('reword: both drafting prompts forbid rewording and restating',
      [Dr.buildPrompt({ label: 'l', url: 'u', added: [], removed: [], candidates: [] }).system, OV.buildMergePrompt({ label: 'l', url: 'u', pairs: [] }).system]
        .every((x) => /Never replace a sentence to reword, shorten or tidy it, and never add a fact the entry already states/.test(x)));
  }
  // --- the existing-entry search must work, or nothing is proposed (production 2026-10-16) ---
  // kb_admin_match_live returns no question/answer; the pipeline silently dropped every
  // match and the paying-levy page was proposed as five NEW entries duplicating live ones.
  {
    const sqlMatch = readFileSync(join(ROOT, '..', 'scripts/sql/kb_admin_014_live_match_baseline.sql'), 'utf8');
    const ret = (sqlMatch.match(/function public\.kb_admin_match_live\([\s\S]*?returns table \(([\s\S]*?)\)\s*language/) ?? [])[1] ?? '';
    check('overlap-fail: kb_admin_match_live really returns no question/answer (so the pipeline must read them itself)',
      ret.length > 0 && !/\bquestion\b/.test(ret) && !/\banswer\b/.test(ret) && /\bpreview\b/.test(ret), ret.replace(/\s+/g, ' ').slice(0, 200));
    const newDraftX = { target_entry_id: null, question: 'When must I pay the monthly levy?', answer: 'You must pay the levy for each month by the 17th of the following month.', source_passage: passage, why: 'deadline' };
    const pay = { ...src16, url: `${LEVY}/paying-levy` };
    // 1. the joined text reaches the model and the overlap check: a 0.8 duplicate becomes a CHANGE, never a new entry
    {
      const db = fake({ match: 0.8 }); const m = llmSeq([JSON.stringify({ drafts: [newDraftX] }), JSON.stringify({ drafts: [] })]);
      const read = [];
      const r = await Pl.checkSource(pay, { ...baseDeps(db, m), entryTexts: async (ids) => { read.push(...ids); return [{ id: ENTRY.id, question: ENTRY.question, answer: ENTRY.answer }]; }, fetchPage: payingPage });
      check('overlap-fail: the live search\'s matches are joined to their text - the model is shown the live entry, and a duplicate is not proposed as new',
        read.includes(ENTRY.id) && m.asked[0]?.user.includes(ENTRY.question) && m.asked[0]?.user.includes(`id ${ENTRY.id}`) && !r.drafts.some((d) => d.kind === 'new'), JSON.stringify({ read, drafts: r.drafts }));
    }
    const stops = async (label, dbOpts, extra) => {
      const db = fake(dbOpts); const m = llmSeq([JSON.stringify({ drafts: [newDraftX] })]);
      const err = await Pl.checkSource(pay, { ...baseDeps(db, m), ...extra, fetchPage: payingPage }).then(() => null, (e) => e);
      check(`overlap-fail: ${label} -> stops with "couldn't check existing entries" and proposes NOTHING`,
        err instanceof Pl.OverlapCheckError && /couldn't check existing entries/.test(err.message) && !db.fns().some((f) => /propose/.test(f)),
        err ? err.message : `no error; calls ${db.fns().join(',')}`);
    };
    await stops('the live search errors', { searchFails: true, match: 0.8 }, {});
    await stops('a match comes back but its text cannot be read', { match: 0.8 }, { entryTexts: async () => [] });
    await stops('reading the entries\' text errors', { match: 0.8 }, { entryTexts: async () => { throw new Error('timeout'); } });
    await stops('the embedding for the search fails', { match: 0.8 }, { embed: async () => { throw new Error('401'); } });
    await stops('there is no account to search with', { match: 0.8 }, { searcher: null });
    // the OVERLAP search (after the model) failing also stops: first search fine, second fails
    {
      const db = fake({ match: 0.8 }); const m = llmSeq([JSON.stringify({ drafts: [newDraftX] })]);
      const err = await Pl.checkSource(pay, { ...baseDeps(db, m), entryTexts: async (ids) => { if (m.asked.length > 0) throw new Error('connection reset'); return [{ id: ENTRY.id, question: ENTRY.question, answer: ENTRY.answer }]; }, fetchPage: payingPage }).then(() => null, (e) => e);
      check('overlap-fail: the overlap check after drafting fails -> stops before proposing, no "New entry"',
        err instanceof Pl.OverlapCheckError && m.asked.length >= 1 && !db.fns().some((f) => /propose/.test(f)), err ? err.message : 'no error');
    }
    // a re-draft proves the search works BEFORE superseding the open proposals
    {
      const db = fake({ searchFails: true }); const m = llmSeq([]);
      const err = await Pl.redraftSource({ ...src16, suggested: [] }, 'x', { ...baseDeps(db, m), snapshotText: async () => payingX.text }).then(() => null, (e) => e);
      check('overlap-fail: a re-draft whose search fails stops BEFORE kb_admin_source_redraft (the open proposals are left alone)',
        err instanceof Pl.OverlapCheckError && !db.fns().includes('kb_admin_source_redraft'), err ? err.message : db.fns().join(','));
    }
    // the person sees it; every live caller passes the reader
    const actsSrc = readFileSync(join(ROOT, 'app/watch/actions.ts'), 'utf8');
    const routeSrc = readFileSync(join(ROOT, 'app/api/watcher/run/route.ts'), 'utf8');
    check('overlap-fail: the actions show "Couldn\'t check existing entries" and every live caller passes the entry reader',
      /function failure\([^)]*\): ActionState \{\s*if \(error instanceof OverlapCheckError\) \{/.test(actsSrc) && /Couldn't check existing entries/.test(actsSrc) &&
      (actsSrc.match(/entryTexts: liveEntryTexts/g) ?? []).length === 2 && /entryTexts: liveEntryTexts/.test(routeSrc));
    const wq = readFileSync(join(ROOT, 'lib/watchQueries.ts'), 'utf8');
    check('overlap-fail: the entry reader reads question and answer of the live table, read-only (select)',
      /export async function liveEntryTexts/.test(wq) && /select id::text as id, question, answer from public\.cb_knowledge_base_updated/.test(wq));
    const plSrc = readFileSync(join(ROOT, 'lib/watcher/pipeline.ts'), 'utf8');
    check('overlap-fail: the pipeline never filters matches on question/answer the search does not return',
      !/x\.question && x\.answer/.test(plSrc) && !/if \(deps\.searcher\) \{/.test(plSrc) && !/deps\.searcher && planned/.test(plSrc));
  }
  // --- additive changes (2026-10-16): the entry is BUILT from additions and contradicted replacements
  {
    const AD = await import(pathToFileURL(join(ROOT, 'lib/watcher/additive.ts')).href);
    const live = { question: FDW.question, answer: FDW.answer };
    const liveSentences = FD.sentencesOf(FDW.answer);
    const a1 = AD.parseUpdate({ add: [{ sentence: 'Pay it by the 17th of the following month', after: 'You will usually pay it through GIRO', source_passage: passage }] }, live, payText);
    const out1 = a1.built?.answer ?? '';
    const pos = liveSentences.map((x) => out1.indexOf(x));
    check('additive: an addition keeps every live sentence verbatim and in order, and goes right after the sentence it names',
      a1.built !== null && pos.every((x, i) => x >= 0 && (i === 0 || x > pos[i - 1])) &&
      out1.includes('payment. Pay it by the 17th of the following month. You must never') && out1.length === FDW.answer.length + ' Pay it by the 17th of the following month.'.length, out1);
    check('additive: an addition with no anchor goes at the end; the question never changes',
      AD.parseUpdate({ add: [{ sentence: 'Pay it by the 17th of the following month.', after: '', source_passage: passage }] }, live, payText).built?.answer === `${FDW.answer} Pay it by the 17th of the following month.`);
    check('additive: the source passage stored is the page\'s own span (on the page, the database finds it verbatim)',
      a1.built !== null && payText.includes(a1.built.sourcePassage) && a1.built.sourcePassage.includes('17'));
    check('additive: nothing removed from a built change, so the safety net never fires on it',
      FD.unjustifiedRemovals(FDW.answer, out1, a1.built?.sourcePassage ?? '', FDW.question).length === 0 && FD.removedPoints(FDW.answer, out1, '', FDW.question).length === 0);
    const page2 = 'Levy rates. The monthly levy for the 1 st helper is $350. You must pay the levy by the 17th of the following month.';
    const live2 = { question: 'How much is the levy?', answer: 'The monthly levy is $300 for the first helper. Pay the levy by the 15th of the following month. It is never deducted from her salary.' };
    const r2 = AD.parseUpdate({ replace: [
      { old: 'The monthly levy is $300 for the first helper.', new: 'The monthly levy is $350 for the first helper.', source_passage: 'The monthly levy for the 1st helper is $350.' },
      { old: 'Pay the levy by the 15th of the following month.', new: 'Pay the levy by the 17th of the following month.', source_passage: 'You must pay the levy by the 17th of the following month.' },
    ] }, live2, page2);
    check('additive: a sentence the page contradicts is replaced in place (a figure, and a bare date number), with the contradiction recorded',
      r2.built?.answer === 'The monthly levy is $350 for the first helper. Pay the levy by the 17th of the following month. It is never deducted from her salary.' &&
      r2.built.replaced.length === 2 && r2.built.replaced.every((x) => x.contradiction), JSON.stringify(r2));
    check('additive: ...the safety net does not fire on contradicted replacements (the same test decides both)',
      FD.unjustifiedRemovals(live2.answer, r2.built?.answer ?? '', r2.built?.sourcePassage ?? '', live2.question).length === 0);
    const r3 = AD.parseUpdate({ replace: [{ old: 'It is never deducted from her salary.', new: 'It must never be taken from her pay.', source_passage: 'You must pay the levy by the 17th of the following month.' }] }, live2, page2);
    check('additive: a replacement the source does not contradict (rewording) is refused, and the live sentence kept',
      r3.built === null && r3.dropped.some((d) => /does not contradict/.test(d)) && r3.dropped.some((d) => /adds nothing and replaces nothing/.test(d)), JSON.stringify(r3));
    const r4 = AD.parseUpdate({ replace: [
      { old: 'The monthly levy is $250 for the first helper.', new: 'The monthly levy is $350 for the first helper.', source_passage: 'The monthly levy for the 1st helper is $350.' },
      { old: 'the levy', new: 'The levy', source_passage: 'The monthly levy for the 1st helper is $350.' },
      { old: 'The monthly levy is $300', new: 'The monthly levy is $350 a month', source_passage: 'The monthly levy for the 1st helper is $350.' },
    ], add: [{ sentence: 'It is never deducted from her salary at all.', after: '', source_passage: 'You must pay the levy by the 17th of the following month.' }, { sentence: 'Pay online.', after: '', source_passage: 'Pay online through the portal.' }] }, live2, page2);
    check('additive: refused - an "old" sentence not in the entry, a fragment, a clause of a sentence, an addition the entry already says, a passage not on the page',
      r4.built === null && r4.dropped.some((d) => /not in the live entry/.test(d)) && r4.dropped.some((d) => /fragment/.test(d)) &&
      r4.dropped.filter((d) => /not in the live entry/.test(d)).length === 2 && r4.dropped.some((d) => /already says/.test(d)) && r4.dropped.some((d) => /not on the page/.test(d)), JSON.stringify(r4.dropped));
    // The live entry as it is on production, plus the two additions run 1 of the dry run made before these sentences.
    const fdwFull = "The levy is a monthly fee the Government charges employers of Migrant Domestic Workers. It is a pricing mechanism to manage the number of foreign workers in Singapore - it is not a tax on your helper and it is never deducted from her salary. As the employer, you pay it on top of everything else. The levy starts when your helper's Work Permit is issued and stops when it is cancelled. You will usually pay it through GIRO, which is the simplest way to avoid missing a payment. Important: the levy is your responsibility as the employer. You must never recover it from your helper's wages. Missing levy payments can affect your ability to renew or hire in future, so a GIRO arrangement is strongly recommended. For a first-time helper, the levy begins on the fifth day of her arrival, including the arrival date; otherwise, it begins the day after she arrives. You must pay the levy for each month by the 17th of the following month, or the next working day if the date falls on a Saturday, Sunday or public holiday.";
    check('additive: "already says" is strict - a genuinely new fact near an existing one is added (dry run: revocation after two missed payments), a restatement is not',
      !FD.alreadySays(`${FDW.question}\n${fdwFull}`, 'Your helper\'s Work Permit will be revoked the following month if you miss levy payments for two consecutive months.') &&
      !FD.alreadySays(`${FDW.question}\n${fdwFull}`, 'If you do not pay the full levy on time, you will be charged a late payment penalty.') &&
      FD.alreadySays(`${FDW.question}\n${fdwFull}`, 'You must never recover the levy from your helper\'s wages.'));
    // A rule the page states differently (no figure to compare) - the dry runs' levy start/end sentence.
    const page3 = 'For first-time helpers , the levy will begin on the 5th day of their arrival. The levy ends when the Work Permit is cancelled or expires. Apply for GIRO to pay the levy.';
    const live3 = { question: 'What is the FDW levy?', answer: 'The levy is a monthly fee. The levy starts when your helper\'s Work Permit is issued and stops when it is cancelled. You will usually pay it through GIRO, which is the simplest way to avoid missing a payment.' };
    const r5 = AD.parseUpdate({ replace: [{ old: 'The levy starts when your helper\'s Work Permit is issued and stops when it is cancelled.', new: 'The levy ends when the Work Permit is cancelled or expires.', source_passage: 'The levy ends when the Work Permit is cancelled or expires.' }] }, live3, page3);
    check('additive: a rule the page states differently (no figure) is replaced, with the page line shown, and the safety net accepts it',
      r5.built?.answer === 'The levy is a monthly fee. The levy ends when the Work Permit is cancelled or expires. You will usually pay it through GIRO, which is the simplest way to avoid missing a payment.' &&
      /states it differently/.test(r5.built.replaced[0].contradiction) &&
      AD.contradictingLine(r5.built.sourcePassage, r5.built.replaced[0].old, r5.built.replaced[0].new) === 'The levy ends when the Work Permit is cancelled or expires.' &&
      FD.unjustifiedRemovals(live3.answer, r5.built.answer, r5.built.sourcePassage, live3.question).length === 0, JSON.stringify(r5));
    const r6 = AD.parseUpdate({ replace: [
      { old: 'You will usually pay it through GIRO, which is the simplest way to avoid missing a payment.', new: 'Apply for GIRO to pay the levy.', source_passage: 'Apply for GIRO to pay the levy.' },
      { old: 'The levy is a monthly fee.', new: 'The levy ends when the Work Permit is cancelled or expires.', source_passage: 'The levy ends when the Work Permit is cancelled or expires.' },
      { old: 'The levy is a monthly fee. The levy starts when your helper\'s Work Permit is issued and stops when it is cancelled.', new: 'The levy ends when the Work Permit is cancelled or expires.', source_passage: 'The levy ends when the Work Permit is cancelled or expires.' },
    ] }, live3, page3);
    const live4 = { question: 'How much is the levy?', answer: 'The monthly levy is $300 for the first helper.' };
    const r7 = AD.parseUpdate({ replace: [{ old: 'The monthly levy is $300 for the first helper.', new: 'The levy for the first helper is charged every month.', source_passage: 'The levy for the first helper is charged every month.' }] }, live4, 'The levy for the first helper is charged every month.');
    const live5 = { question: 'Can I take the levy from her pay?', answer: 'You must never recover the levy from your helper\'s wages.' };
    const page5 = 'Employers must not recover the levy from the helper\'s salary or wages.';
    const r8 = AD.parseUpdate({ replace: [{ old: live5.answer, new: page5, source_passage: page5 }] }, live5, page5);
    check('additive: refused as replacements - a page line on the same topic that drops a point, one on another subject, two sentences at once, one that drops a figure, and rewording a sentence the page agrees with',
      r6.built === null && r6.dropped.filter((d) => /does not contradict/.test(d)).length === 2 && r6.dropped.some((d) => /not in the live entry/.test(d)) &&
      r7.built === null && r8.built === null, JSON.stringify([r6.dropped, r7.dropped, r8.dropped]));
    const fit = AD.parseUpdate({ add: [
      { sentence: 'Pay it by the 17th of the following month.', after: '', source_passage: passage },
      { sentence: 'x '.repeat(400) + 'end.', after: '', source_passage: passage },
    ] }, live, payText, live.question.length + live.answer.length + 60);
    check('additive: over the 1,200-character limit, additions are taken in order while they fit and the rest dropped by name (not the whole change)',
      fit.built?.answer === `${FDW.answer} Pay it by the 17th of the following month.` && fit.dropped.some((d) => /over \d+ characters/.test(d)), JSON.stringify(fit.dropped));
    const segs = AD.entryDiff(live2.answer, r2.built?.answer ?? '');
    check('additive: the review diff shows replaced (old -> new) and unchanged sentences',
      segs.filter((x) => x.kind === 'replaced').length === 2 && segs.filter((x) => x.kind === 'same').length === 1 && segs[0].kind === 'replaced' && segs[0].old.includes('$300') && segs[0].new.includes('$350'));
    const segs1 = AD.entryDiff(FDW.answer, out1);
    check('additive: ...and an added sentence where it was placed, everything else unchanged',
      segs1.map((x) => x.kind).join(',') === `${liveSentences.slice(0, 3).map(() => 'same').join(',')},added,same` && segs1[3].text === 'Pay it by the 17th of the following month.', JSON.stringify(segs1));
    check('additive: a removal by a person\'s Edit is shown as removed', AD.entryDiff('A one. B two.', 'A one.').some((x) => x.kind === 'removed' && x.text === 'B two.'));
    check('additive: the contradicting line of the source is shown for a replacement',
      /\$350/.test(AD.contradictingLine(r2.built?.sourcePassage ?? '', 'The monthly levy is $300 for the first helper.') ?? ''));
    const whole = Dr.parseDrafts(JSON.stringify({ drafts: [{ target_entry_id: ENTRY.id, question: ENTRY.question, answer: 'A whole new answer of $300 a month.', source_passage: passage }] }),
      { newText: payText, candidateIds: [ENTRY.id], live: new Map([[ENTRY.id, ENTRY]]) });
    check('additive: a change written as a whole new answer is refused (never rewritten)', whole.drafts.length === 0 && /whole new answer/.test(whole.dropped.join(' ')));
    const built = Dr.parseDrafts(JSON.stringify({ drafts: [{ target_entry_id: ENTRY.id, question: 'A question the model made up?', answer: 'Ignored.', add: [{ sentence: 'Pay it by the 17th of the following month.', after: '', source_passage: passage }], why: 'w' }] }),
      { newText: payText, candidateIds: [ENTRY.id], live: new Map([[ENTRY.id, ENTRY]]) });
    check('additive: parseDrafts builds the change - the live question (never one the model wrote), live answer plus the addition, what was added recorded',
      built.drafts.length === 1 && built.drafts[0].question === ENTRY.question && built.drafts[0].answer === `${ENTRY.answer} Pay it by the 17th of the following month.` &&
      built.drafts[0].change?.added.length === 1 && built.drafts[0].change.replaced.length === 0, JSON.stringify(built));
    const review3 = src('app/watch/[id]/page.tsx');
    check('additive: the review page shows a change as added (green), replaced (old -> new with the source) and unchanged (grey)',
      /const segments = draft\.kind === 'edit' && draft\.current_answer \? entryDiff\(draft\.current_answer, draft\.proposed_answer\) : null;/.test(review3) &&
      /<EntryChanges question=\{draft\.proposed_question\} segments=\{segments\} sourcePassage=\{draft\.source_passage\} \/>/.test(review3) &&
      /data-seg="added"/.test(review3) && /var\(--good-bg\)/.test(review3) && /data-seg="replaced"/.test(review3) && /contradictingLine\(sourcePassage, x\.old, x\.new\)/.test(review3) &&
      /data-seg="same"/.test(review3) && /className="border-l-2 pl-3 text-muted"/.test(review3));
    check('additive: additive.ts, passage.ts and changeRules.ts import no database, network or environment code',
      ['lib/watcher/additive.ts', 'lib/watcher/passage.ts', 'lib/watcher/changeRules.ts'].every((f) => !/from '(postgres|\.\.?\/(db|write|env|embed)|@\/lib\/(db|write|env|embed))'|process\.env|(?<![.\w])fetch\(/.test(src(f))));
  }
  check('factDiff imports no database, network or environment code',
    !/from '(postgres|\.\.?\/(db|write|env|embed)|@\/lib\/(db|write|env|embed))'|process\.env|(?<![.\w])fetch\(/.test(src('lib/watcher/factDiff.ts')));

  // --- migration 016 --------------------------------------------------------
  const sqlPath = (f) => join(ROOT, '..', 'scripts/sql', f);
  const s16 = existsSync(sqlPath('kb_admin_016_watcher_overlap.sql')) ? readFileSync(sqlPath('kb_admin_016_watcher_overlap.sql'), 'utf8') : '';
  const s15 = existsSync(sqlPath('kb_admin_015_watcher.sql')) ? readFileSync(sqlPath('kb_admin_015_watcher.sql'), 'utf8') : '';
  if (s16 && s15) {
    const body = (t) => /create or replace function public\.kb_admin_watch_approve\([\s\S]*?\n\$\$;\n/.exec(t)?.[0] ?? '';
    const unpatched = body(s16)
      .replace(/    -- 016: the proposal states[\s\S]*?using errcode = 'KB009';\n    end if;\n/, '')
      .replace(',\n           conflict_confirmed = v_d.conflict_check\n', '\n')
      .replace(",\n                               'conflict_confirmed', v_d.conflict_check),\n", '),\n');
    check('016: kb_admin_watch_approve is 015\'s, character for character, plus the KB009 tick and its record', unpatched === body(s15) && /errcode = 'KB009'/.test(body(s16)));
    check('016: the re-draft is approver-only and audit-logged as source_redrafted',
      /kb_admin_source_redraft[\s\S]*?kb_admin__require_actor\(p_actor_id, 'approver'\)[\s\S]*?'source_redrafted'/.test(s16));
    check('016: proposals still go through 015\'s kb_admin_watch_propose (passage check, fee lock, figure check)',
      /v := public\.kb_admin_watch_propose\(p_actor_id/.test(s16));
    check('016: a suggested page must be https, on the page\'s own site, labelled; at most 20',
      /<> v_host/.test(s16) && /jsonb_array_length\(v_pages\) > 20/.test(s16) && /\^https:\/\//.test(s16));
    check('016: additive - the live knowledge-base table is not altered, nothing is deleted, editor ends with 27 functions',
      !/alter table public\.cb_knowledge_base_updated/i.test(s16) && !/\bdelete from\b/i.test(s16) && /if n <> 27 then/.test(s16));
    const rb16 = existsSync(sqlPath('rollback/rollback_kb_admin_016_watcher_overlap.sql')) ? readFileSync(sqlPath('rollback/rollback_kb_admin_016_watcher_overlap.sql'), 'utf8') : '';
    check('016: a rollback exists, restores 015\'s approve, and refuses once 016 has been used',
      /ROLLBACK 016 REFUSED: cb_kb_audit holds 016 actions/.test(rb16) && rb16.includes(body(s15)));
    const s12 = readFileSync(sqlPath('kb_admin_012_grants_checks.sql'), 'utf8');
    check('012 follows what is installed: 16, 24 after 015, 27 after 016',
      /kb_admin_watch_propose_checked\(uuid,text,uuid,uuid/.test(s12) && /column_name = 'conflict_check'/.test(s12));
  } else {
    console.log('  (migration SQL not found - 016 checks skipped)');
  }
}

console.log(failures ? `RESULT: ${failures} FAIL(S)` : 'RESULT: ALL PASS');
process.exitCode = failures ? 1 : 0;
