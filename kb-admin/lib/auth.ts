import 'server-only';
import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';
import { decideAccess, denialPath } from './access';
import { env } from './env';
import { membership } from './queries';
import { ACCESS_COOKIE, userFromToken } from './session';

export type Viewer = { userId: string; email: string; role: 'viewer' };
export type AuthResult =
  | { ok: true; viewer: Viewer }
  | { ok: false; reason: 'no-session' | 'bad-session' | 'no-access' | 'inactive' };

function log(reason: string, userId?: string) {
  // The user id is truncated: enough to tell two people apart in a log, not a
  // full identifier. Emails are never logged.
  console.info(`[auth] denied reason=${reason}${userId ? ` user=${userId.slice(0, 8)}` : ''}`);
}

/**
 * The check that runs on EVERY page and API request, server-side:
 * the token is verified by Supabase Auth, then the account must have an
 * active row in cb_kb_admin_users. Nothing is cached between requests.
 */
export async function checkRequest(): Promise<AuthResult> {
  const token = cookies().get(ACCESS_COOKIE)?.value;
  if (!token) return { ok: false, reason: 'no-session' };
  const cfg = env();
  const user = await userFromToken(cfg, token);
  if (!user) {
    log('bad-session');
    return { ok: false, reason: 'bad-session' };
  }
  const decision = decideAccess(await membership(user.id));
  if (!decision.ok) {
    log(decision.reason, user.id);
    return { ok: false, reason: decision.reason };
  }
  return { ok: true, viewer: { userId: user.id, email: user.email, role: decision.role } };
}

/** For pages: the viewer, or a redirect to the sign-in page (via
 *  /login/switched-off, which clears the cookies, when access is switched off). */
export async function requireViewer(): Promise<Viewer> {
  const result = await checkRequest();
  if (!result.ok) redirect(denialPath(result.reason));
  return result.viewer;
}
