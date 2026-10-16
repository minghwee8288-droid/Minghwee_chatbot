/**
 * Who may use kb-admin. PURE, so scripts/selfcheck.mjs can run it.
 *
 * A signed-in Supabase account is not enough: the account must have a row in
 * cb_kb_admin_users with active = true and a role kb-admin knows. No row
 * means no access. This is checked on EVERY request, so setting active to
 * false takes effect on that person's next click.
 */

export type Membership = { role: string; active: boolean } | undefined;

export const ROLES = ['viewer', 'editor', 'approver'] as const;
export type Role = (typeof ROLES)[number];

export type AccessDecision =
  | { ok: true; role: Role }
  | { ok: false; reason: 'no-access' | 'inactive' };

export function decideAccess(membership: Membership): AccessDecision {
  if (!membership) return { ok: false, reason: 'no-access' };
  if (membership.active !== true) return { ok: false, reason: 'inactive' };
  const role = (ROLES as readonly string[]).includes(membership.role) ? (membership.role as Role) : null;
  if (!role) return { ok: false, reason: 'no-access' };
  return { ok: true, role };
}

/** May this role save, restore, publish and discard drafts? The database
 *  checks the role again on every call: this only decides what a page shows. */
export function canEdit(role: Role): boolean {
  return role === 'editor' || role === 'approver';
}

/** May this role switch an entry on or off, or publish a change that needs approval? */
export function canApprove(role: Role): boolean {
  return role === 'approver';
}

/** Where a page sends a refused request. A switched-off account goes through
 *  /login/switched-off, which clears the session cookies (a page cannot set
 *  cookies while redirecting) and then shows the same notice as ?e=inactive. */
export const SWITCHED_OFF_PATH = '/login/switched-off';

export function denialPath(reason: string): string {
  return reason === 'inactive' ? SWITCHED_OFF_PATH : `/login?e=${reason}`;
}

/** kb-admin runs single read statements only; anything else is refused. */
export function isSingleRead(query: string): boolean {
  const q = query.trim().replace(/;\s*$/, '');
  return /^(select|with)\b/i.test(q) && !q.includes(';');
}

/**
 * The ONLY database calls kb-admin makes as kb_admin_editor: one call to one of
 * these thirty-one functions. Each is SECURITY DEFINER in the database and checks
 * the caller's role itself; the editor login can call these and nothing else.
 * This list is the code-side twin of that grant: the same thirty-one signatures
 * that migrations 005, 008, 011, 014, 015, 016 and 017 grant to kb_admin_editor. The
 * eight of 015 exist only once that migration is applied; until then nothing
 * calls them, because the watcher pages are off unless KB_SOURCES_ENABLED=true.
 */
export const WRITE_FUNCTIONS = {
  kb_admin_save_draft: ['uuid', 'text', 'uuid', 'text', 'text', 'text', 'text', 'text', 'text', 'boolean', 'text'],
  kb_admin_discard_draft: ['uuid', 'text', 'uuid', 'text'],
  kb_admin_publish: ['uuid', 'text', 'uuid', 'vector', 'text'],
  kb_admin_restore: ['uuid', 'text', 'uuid', 'uuid', 'text'],
  kb_admin_toggle: ['uuid', 'text', 'uuid', 'boolean', 'text'],
  // Migration 008: a new entry, switched off and unsearchable, plus its draft.
  kb_admin_create_entry: ['uuid', 'text', 'text', 'text', 'text', 'text', 'text', 'text', 'text'],
  // Migration 011: documents.
  kb_admin_doc_upload: ['uuid', 'text', 'text', 'text', 'text', 'bytea', 'text'],
  kb_admin_doc_stage: ['uuid', 'text', 'uuid', 'jsonb', 'text'],
  kb_admin_doc_edit_chunk: ['uuid', 'text', 'uuid', 'text', 'text', 'text', 'text', 'text', 'text', 'double precision[]', 'text'],
  kb_admin_doc_mark_checked: ['uuid', 'text', 'uuid'],
  kb_admin_doc_publish: ['uuid', 'text', 'uuid', 'text'],
  kb_admin_doc_restore: ['uuid', 'text', 'uuid', 'text'],
  kb_admin_doc_discard: ['uuid', 'text', 'uuid'],
  kb_admin_doc_retire: ['uuid', 'text', 'uuid', 'text'],
  // Read-only searches (011, 014): what the bot's search would return with a
  // staged batch in place, and what it returns now.
  kb_admin_match_with_batch: ['uuid', 'text', 'uuid', 'vector', 'text', 'text', 'text', 'integer', 'double precision'],
  kb_admin_match_live: ['uuid', 'text', 'vector', 'text', 'text', 'text', 'integer', 'double precision'],
  // Migration 015: the KB watcher (watched pages, snapshots, proposals). None of
  // these changes the knowledge base; kb_admin_watch_approve writes the text
  // only through kb_admin_save_draft / kb_admin_create_entry, and it goes live
  // only through kb_admin_publish above.
  kb_admin_source_add: ['uuid', 'text', 'text', 'text', 'text'],
  kb_admin_source_remove: ['uuid', 'text', 'uuid', 'text'],
  kb_admin_source_record_fetch: ['uuid', 'text', 'uuid', 'boolean', 'integer', 'text', 'text', 'text', 'text'],
  kb_admin_watch_propose: ['uuid', 'text', 'uuid', 'uuid', 'text', 'text', 'text', 'text', 'text', 'text', 'text', 'text', 'text'],
  kb_admin_watch_edit: ['uuid', 'text', 'uuid', 'text', 'text', 'text', 'text'],
  kb_admin_watch_reject: ['uuid', 'text', 'uuid', 'text'],
  kb_admin_watch_approve: ['uuid', 'text', 'uuid', 'text', 'boolean'],
  kb_admin_watch_mark_published: ['uuid', 'text', 'uuid'],
  // Migration 016: the overlap results on a proposal, an index page's suggested
  // sub-pages, and the approver's re-draft. Called only once 016 is installed
  // (lib/watchQueries.ts watcherSchema16); none touches the knowledge base.
  kb_admin_watch_propose_checked: ['uuid', 'text', 'uuid', 'uuid', 'text', 'text', 'text', 'text', 'text', 'text', 'text', 'text', 'text', 'jsonb', 'boolean', 'text'],
  kb_admin_source_set_suggestions: ['uuid', 'text', 'uuid', 'jsonb'],
  kb_admin_source_redraft: ['uuid', 'text', 'uuid', 'text'],
  // Migration 017: a page read through the page renderer; a page read without a
  // certificate check (approver only, with a reason); a failed draft retried.
  // Called only once 017 is installed (lib/watchQueries.ts watcherSchema17).
  kb_admin_source_set_render: ['uuid', 'text', 'uuid', 'boolean'],
  kb_admin_source_set_unverified: ['uuid', 'text', 'uuid', 'boolean', 'text'],
  kb_admin_source_add_unverified: ['uuid', 'text', 'text', 'text', 'text', 'text'],
  kb_admin_source_record_draft: ['uuid', 'text', 'uuid', 'boolean', 'text'],
} as const;
export type WriteFunction = keyof typeof WRITE_FUNCTIONS;

/** The two that return rows, not one value: their rows come back as one jsonb array. */
export const ROW_FUNCTIONS: readonly WriteFunction[] = ['kb_admin_match_with_batch', 'kb_admin_match_live'];

/** The exact text of the one statement allowed for a function. Every value is
 *  a bound, typed parameter ($1::uuid ...); nothing is ever spliced in. */
export function writeStatement(fn: WriteFunction): string {
  const args = WRITE_FUNCTIONS[fn].map((type, i) => `$${i + 1}::${type}`).join(', ');
  if (ROW_FUNCTIONS.includes(fn)) return `select coalesce(jsonb_agg(to_jsonb(m)), '[]'::jsonb) as result from public.${fn}(${args}) m`;
  return `select public.${fn}(${args}) as result`;
}

/** True only for exactly one of the statements above, character for character. */
export function isAllowedWrite(query: string): boolean {
  return (Object.keys(WRITE_FUNCTIONS) as WriteFunction[]).some((fn) => writeStatement(fn) === query);
}

/** Seconds until a JWT expires, read WITHOUT verifying it. Used only to decide
 *  whether to refresh; the token itself is always verified by Supabase Auth. */
export function secondsLeft(token: string, nowSeconds: number): number {
  const parts = token.split('.');
  if (parts.length !== 3) return -1;
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const claims = JSON.parse(atob(b64)) as { exp?: unknown };
    return typeof claims.exp === 'number' ? claims.exp - nowSeconds : -1;
  } catch {
    return -1;
  }
}
