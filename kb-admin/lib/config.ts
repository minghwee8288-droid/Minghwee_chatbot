/**
 * Configuration validation. PURE: no imports, no process.env, no filesystem,
 * so scripts/selfcheck.mjs can run it directly under Node.
 *
 * kb-admin refuses to start unless every setting names the same project and
 * every credential is the one it is supposed to hold. It holds exactly three:
 *
 *   1. the Supabase ANON key, for sign-in only (a service-role or secret key
 *      is refused outright - it would bypass RLS on every table);
 *   2. the kb_admin_reader database login, which can read the knowledge base,
 *      the rules and the access list, and nothing else;
 *   3. the bot's preview secret.
 *
 * And, only when the editor is switched on (see `editor` below):
 *   4. the kb_admin_editor database login, which can call the six
 *      kb_admin_* functions and nothing else;
 *   5. an embedding API key, used only to embed an entry's new text.
 * With neither set the editor is off and kb-admin stays read-only.
 */

export type KbAdminConfig = {
  ref: string;
  supabaseUrl: string;
  anonKey: string;
  db: { host: string; port: number; user: string; password: string };
  previewUrl: string;
  previewSecret: string;
  /** null = the editor is off: no edit control is shown and nothing can write. */
  editor: EditorConfig | null;
  /** Set when the editor is off because its settings are incomplete (never a value). */
  editorOffReason: string;
};

export type EditorConfig = {
  db: { user: string; password: string };
  embedding: { baseUrl: string; apiKey: string };
};

/** The two embedding endpoints the bot itself can use (app/services/rag.py). */
export const EMBEDDING_BASE_URLS = ['https://api.openai.com/v1', 'https://openrouter.ai/api/v1'] as const;

export type ConfigSource = Record<string, string | undefined>;

export class ConfigError extends Error {}

function refuse(message: string): never {
  throw new ConfigError(`kb-admin refuses to start: ${message}`);
}

function jwtClaims(token: string): Record<string, unknown> | null {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const json = typeof atob === 'function' ? atob(b64) : '';
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** The anon key is the only Supabase key kb-admin may hold. */
export function checkAnonKey(key: string, ref: string): void {
  if (!key) refuse('SUPABASE_ANON_KEY is not set');
  if (key.startsWith('sb_secret_')) refuse('SUPABASE_ANON_KEY is a secret key, not the anon key');
  if (key.startsWith('sb_publishable_')) return;
  const claims = jwtClaims(key);
  if (!claims) refuse('SUPABASE_ANON_KEY is not a recognisable Supabase key');
  if (claims.role !== 'anon') refuse(`SUPABASE_ANON_KEY has role "${String(claims.role)}", not anon`);
  if (claims.ref !== ref) refuse('SUPABASE_ANON_KEY belongs to a different project');
}

export function validateConfig(
  source: ConfigSource,
  readPasswordFile: (path: string) => string,
): KbAdminConfig {
  const ref = (source.KB_ADMIN_EXPECTED_REF ?? '').trim();
  if (!/^[a-z0-9]{20}$/.test(ref)) refuse('KB_ADMIN_EXPECTED_REF is missing or malformed');

  const supabaseUrl = (source.SUPABASE_URL ?? '').trim().replace(/\/+$/, '');
  if (supabaseUrl !== `https://${ref}.supabase.co`) {
    refuse(`SUPABASE_URL does not name project ${ref}`);
  }

  const anonKey = (source.SUPABASE_ANON_KEY ?? '').trim();
  checkAnonKey(anonKey, ref);

  const host = (source.KB_ADMIN_DB_HOST ?? '').trim();
  if (!/^[a-z0-9-]+\.pooler\.supabase\.com$/.test(host)) {
    refuse('KB_ADMIN_DB_HOST must be the Supabase pooler host');
  }
  const port = Number((source.KB_ADMIN_DB_PORT ?? '6543').trim());
  if (port !== 6543 && port !== 5432) refuse('KB_ADMIN_DB_PORT must be 6543 or 5432');
  const user = (source.KB_ADMIN_DB_USER ?? '').trim();
  if (user !== `kb_admin_reader.${ref}`) {
    refuse(`KB_ADMIN_DB_USER must be kb_admin_reader.${ref}`);
  }

  // KB_ADMIN_DB_PASSWORD first: Vercel has no filesystem to hold a password
  // file, so there the value comes from the project's environment. Locally it
  // is left empty and the file is read as before. The value is never logged.
  const passwordValue = (source.KB_ADMIN_DB_PASSWORD ?? '').trim();
  const passwordFile = (source.KB_ADMIN_DB_PASSWORD_FILE ?? '').trim();
  if (!passwordValue && !passwordFile) {
    refuse('no database password: set KB_ADMIN_DB_PASSWORD or KB_ADMIN_DB_PASSWORD_FILE');
  }
  const password = passwordValue || readPasswordFile(passwordFile).trim();
  if (!password) refuse('the file named by KB_ADMIN_DB_PASSWORD_FILE is empty');

  const previewUrl = (source.BOT_PREVIEW_URL ?? '').trim().replace(/\/+$/, '');
  const previewSecret = (source.ADMIN_PREVIEW_SECRET ?? '').trim();
  if (previewSecret) {
    let url: URL;
    try {
      url = new URL(previewUrl);
    } catch {
      refuse('BOT_PREVIEW_URL is not a URL');
    }
    const local = url.hostname === '127.0.0.1' || url.hostname === 'localhost';
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) {
      refuse('BOT_PREVIEW_URL must be https (http is accepted for localhost only)');
    }
    if (previewSecret.length < 32) refuse('ADMIN_PREVIEW_SECRET is shorter than 32 characters');
  }

  const { editor, editorOffReason } = editorConfig(source, readPasswordFile, ref, password);

  return {
    ref,
    supabaseUrl,
    anonKey,
    db: { host, port, user, password },
    previewUrl,
    previewSecret,
    editor,
    editorOffReason,
  };
}

/**
 * The editor's settings. ALL absent: the editor is off (today's read-only
 * site). SOME absent: off too, with a reason logged at start - never a
 * half-working editor. Present but wrong (an unknown endpoint, a key for the
 * other endpoint, the reader's own password): refuse to start.
 *
 * The editor login is not configurable: it is kb_admin_editor.<the same ref>,
 * so it can never name a different project from the reader.
 */
function editorConfig(
  source: ConfigSource,
  readPasswordFile: (path: string) => string,
  ref: string,
  readerPassword: string,
): { editor: EditorConfig | null; editorOffReason: string } {
  const pwValue = (source.KB_ADMIN_DB_PASSWORD_EDITOR ?? '').trim();
  const pwFile = (source.KB_ADMIN_DB_PASSWORD_EDITOR_FILE ?? '').trim();
  const apiKey = (source.KB_ADMIN_EMBEDDING_API_KEY ?? '').trim();
  const baseRaw = (source.KB_ADMIN_EMBEDDING_BASE_URL ?? '').trim().replace(/\/+$/, '');

  const baseUrl = baseRaw || EMBEDDING_BASE_URLS[0];
  if (!(EMBEDDING_BASE_URLS as readonly string[]).includes(baseUrl)) {
    refuse(`KB_ADMIN_EMBEDDING_BASE_URL must be one of ${EMBEDDING_BASE_URLS.join(', ')} (or unset)`);
  }
  if (apiKey) {
    const openRouterKey = apiKey.startsWith('sk-or-');
    if (baseUrl.includes('openrouter.ai') && !openRouterKey) {
      refuse('KB_ADMIN_EMBEDDING_API_KEY is not an OpenRouter key, but the base URL is OpenRouter');
    }
    if (baseUrl.includes('api.openai.com') && openRouterKey) {
      refuse('KB_ADMIN_EMBEDDING_API_KEY is an OpenRouter key; set KB_ADMIN_EMBEDDING_BASE_URL=https://openrouter.ai/api/v1');
    }
  }

  const missing: string[] = [];
  if (!pwValue && !pwFile) missing.push('KB_ADMIN_DB_PASSWORD_EDITOR (or _FILE)');
  if (!apiKey) missing.push('KB_ADMIN_EMBEDDING_API_KEY');
  if (missing.length === 2) return { editor: null, editorOffReason: '' };
  if (missing.length) return { editor: null, editorOffReason: `editor off: ${missing.join(', ')} not set` };

  const password = pwValue || readPasswordFile(pwFile).trim();
  if (!password) refuse('the file named by KB_ADMIN_DB_PASSWORD_EDITOR_FILE is empty');
  if (password === readerPassword) refuse('the editor and reader database passwords are the same');

  return {
    editor: { db: { user: `kb_admin_editor.${ref}`, password }, embedding: { baseUrl, apiKey } },
    editorOffReason: '',
  };
}
