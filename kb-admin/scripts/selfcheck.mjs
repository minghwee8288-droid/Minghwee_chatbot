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

console.log('writes (Phase 2 editor):');
// The ONLY writes: one of five SECURITY DEFINER functions, called from one
// module, as one exact statement each. Everything else stays refused above.
const FIVE = ['kb_admin_discard_draft', 'kb_admin_publish', 'kb_admin_restore', 'kb_admin_save_draft', 'kb_admin_toggle'];
const ROLE_NAMES = new Set(['kb_admin_reader', 'kb_admin_editor']);
const named = new Map();
for (const f of code) {
  for (const m of readFileSync(f, 'utf8').matchAll(/\bkb_admin_\w+/g)) {
    if (!ROLE_NAMES.has(m[0])) named.set(m[0], [...(named.get(m[0]) ?? []), rel(f)]);
  }
}
const strays = [...named.keys()].filter((n) => !FIVE.includes(n));
check('the only kb_admin_* functions named are the five', strays.length === 0, strays.map((n) => `${n} in ${named.get(n)[0]}`).join(', '));
const writeTs = src('lib/write.ts');
check('lib/write.ts exports callWrite and nothing else', exportsOf(writeTs).join(',') === 'callWrite', exportsOf(writeTs).join(','));
check('lib/write.ts sends only writeStatement(fn), after isAllowedWrite()',
  /const statement = writeStatement\(fn\);/.test(writeTs) && /if \(!isAllowedWrite\(statement\)/.test(writeTs) &&
  (writeTs.match(/\.unsafe\(/g) ?? []).length === 1 && /\.unsafe\(statement, params\)/.test(writeTs) && !/\.begin\(|sql\(\)`/.test(writeTs));
const pgUsers = code.filter((f) => /from 'postgres'/.test(readFileSync(f, 'utf8'))).map(rel).sort();
check('only lib/db.ts and lib/write.ts open a database connection', pgUsers.join(',') === 'lib/db.ts,lib/write.ts', pgUsers.join(','));
const writeUsers = code.filter((f) => /from '@\/lib\/write'|from '\.\.?\/.*write'/.test(readFileSync(f, 'utf8'))).map(rel);
check('only app/editor/actions.ts calls lib/write.ts', writeUsers.join(',') === 'app/editor/actions.ts', writeUsers.join(','));
const actionsSrc = src('app/editor/actions.ts');
check("app/editor/actions.ts is a 'use server' module", /^'use server';/.test(actionsSrc));
const actionFns = [...actionsSrc.matchAll(/export async function (\w+)\([^)]*\)[^{]*\{\s*\n\s*(.*)/g)];
const unguarded = actionFns.filter((m) => m[2].trim() !== 'const auth = await actorForWrite();').map((m) => m[1]);
check('every server action checks the session and role first (actorForWrite)', actionFns.length === 5 && unguarded.length === 0,
  `${actionFns.length} action(s)${unguarded.length ? `; unguarded: ${unguarded.join(', ')}` : ''}`);
const otherServer = code.filter((f) => /^'use server';/.test(readFileSync(f, 'utf8'))).map(rel).sort();
check("no other 'use server' module than sign-in and the editor", otherServer.join(',') === 'app/editor/actions.ts,app/login/actions.ts', otherServer.join(','));
const toggleSrc = actionsSrc.slice(actionsSrc.indexOf('export async function toggleEntry'));
check('switching on/off also checks canApprove before calling the database',
  toggleSrc.indexOf('actor.canApprove') > 0 && toggleSrc.indexOf('actor.canApprove') < toggleSrc.indexOf("callWrite('kb_admin_toggle'"));

const pages = code.filter((f) => /^app\/.*page\.tsx$/.test(rel(f)) && rel(f) !== 'app/login/page.tsx');
for (const p of pages) check(`${rel(p)} requires a viewer`, readFileSync(p, 'utf8').includes('requireViewer('));
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
const { decideAccess, denialPath, isSingleRead, secondsLeft, canEdit, canApprove, isAllowedWrite, writeStatement, WRITE_FUNCTIONS } =
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
check('exactly the five functions are allowed', Object.keys(WRITE_FUNCTIONS).sort().join(',') === FIVE.join(','), Object.keys(WRITE_FUNCTIONS).sort().join(','));
check('each of the five statements is allowed', FIVE.every((fn) => isAllowedWrite(writeStatement(fn))));
check('every statement binds every value ($n) and splices nothing',
  FIVE.every((fn) => writeStatement(fn) === `select public.${fn}(${WRITE_FUNCTIONS[fn].map((t, i) => `$${i + 1}::${t}`).join(', ')}) as result`));
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
// The code's five signatures are the five the database grants to kb_admin_editor.
const grantFile = resolve(ROOT, '..', 'scripts', 'sql', 'kb_admin_005_editor_login.sql');
if (existsSync(grantFile)) {
  const granted = [...readFileSync(grantFile, 'utf8').matchAll(/public\.(kb_admin_\w+)\(([^)]*)\)/g)]
    .map((m) => `${m[1]}(${m[2].replace(/\s+/g, '')})`).sort().join(' ');
  const ours = FIVE.map((fn) => `${fn}(${WRITE_FUNCTIONS[fn].join(',')})`).sort().join(' ');
  check('the five signatures match the kb_admin_editor grant (kb_admin_005)', granted === ours, granted);
} else {
  console.log('  (scripts/sql/kb_admin_005_editor_login.sql not found - grant comparison skipped)');
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

console.log(failures ? `RESULT: ${failures} FAIL(S)` : 'RESULT: ALL PASS');
process.exitCode = failures ? 1 : 0;
