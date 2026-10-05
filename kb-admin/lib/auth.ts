import 'server-only';
import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';
import { canApprove, canEdit, decideAccess, denialPath, type Role } from './access';
import { env } from './env';
import { membership } from './queries';
import { ACCESS_COOKIE, userFromToken } from './session';

export type Viewer = {
  userId: string;
  email: string;
  role: Role;
  /** True only when the editor is configured AND this role may edit. */
  canEdit: boolean;
  /** True only when the editor is configured AND this role is approver. */
  canApprove: boolean;
};
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
  const editorOn = cfg.editor !== null;
  return {
    ok: true,
    viewer: {
      userId: user.id,
      email: user.email,
      role: decision.role,
      canEdit: editorOn && canEdit(decision.role),
      canApprove: editorOn && canApprove(decision.role),
    },
  };
}

/** For pages: the viewer, or a redirect to the sign-in page (via
 *  /login/switched-off, which clears the cookies, when access is switched off). */
export async function requireViewer(): Promise<Viewer> {
  const result = await checkRequest();
  if (!result.ok) redirect(denialPath(result.reason));
  return result.viewer;
}

/**
 * For pages: the same check as requireViewer(), with the page's own queries
 * started at the same moment rather than after it (2026-10-05, speed). The
 * queries only read, as kb_admin_reader; their result is returned ONLY once
 * access is confirmed - a refused request redirects inside requireViewer()
 * and the data is never awaited, rendered or sent. A query that fails on a
 * refused request is swallowed here, so it cannot surface as an error.
 */
export async function requireViewerWith<T>(load: () => Promise<T>): Promise<[Viewer, T]> {
  const data = load();
  data.catch(() => undefined);
  const viewer = await requireViewer();
  return [viewer, await data];
}

/**
 * For every server action that writes: the same check as checkRequest(), run
 * afresh (the session verified with Supabase Auth, the role read from
 * cb_kb_admin_users now, not from the page that showed the button), plus the
 * editor must be configured and the role must allow editing. The id and email
 * returned are what the kb_admin_* functions are given - and each of them
 * looks the id up in cb_kb_admin_users again.
 */
export async function actorForWrite(): Promise<{ ok: true; actor: Viewer } | { ok: false; message: string }> {
  const result = await checkRequest();
  if (!result.ok) return { ok: false, message: 'Your session has ended. Sign in again.' };
  if (!result.viewer.canEdit) return { ok: false, message: 'Your account cannot make changes here.' };
  return { ok: true, actor: result.viewer };
}
