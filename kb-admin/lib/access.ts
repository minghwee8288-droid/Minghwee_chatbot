/**
 * Who may use kb-admin. PURE, so scripts/selfcheck.mjs can run it.
 *
 * A signed-in Supabase account is not enough: the account must have a row in
 * cb_kb_admin_users with active = true and a role kb-admin knows. No row
 * means no access. This is checked on EVERY request, so setting active to
 * false takes effect on that person's next click.
 */

export type Membership = { role: string; active: boolean } | undefined;

export type AccessDecision =
  | { ok: true; role: 'viewer' }
  | { ok: false; reason: 'no-access' | 'inactive' };

export const ROLES = ['viewer'] as const;

export function decideAccess(membership: Membership): AccessDecision {
  if (!membership) return { ok: false, reason: 'no-access' };
  if (membership.active !== true) return { ok: false, reason: 'inactive' };
  if (membership.role !== 'viewer') return { ok: false, reason: 'no-access' };
  return { ok: true, role: 'viewer' };
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
