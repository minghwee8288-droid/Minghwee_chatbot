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
 * these sixteen functions. Each is SECURITY DEFINER in the database and checks
 * the caller's role itself; the editor login can call these and nothing else.
 * This list is the code-side twin of that grant: the same sixteen signatures
 * that migrations 005, 008, 011 and 014 grant to kb_admin_editor.
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
