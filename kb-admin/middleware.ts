import { NextResponse, type NextRequest } from 'next/server';
import { secondsLeft } from './lib/access';
import {
  ACCESS_COOKIE,
  DEADLINE_COOKIE,
  REFRESH_COOKIE,
  cookieOptions,
  refreshSession,
} from './lib/session';

/**
 * Runs before every request except the sign-in page and static assets.
 *
 * It does two things only: turns away a request with no session cookies, and
 * refreshes an access token that is about to expire. It does NOT decide
 * access - every page and API route does that server-side against Supabase
 * Auth and cb_kb_admin_users (lib/auth.ts), because the edge runtime cannot
 * reach the database.
 */

export const config = {
  matcher: ['/((?!login|_next/static|_next/image|favicon.ico).*)'],
};

function turnAway(req: NextRequest): NextResponse {
  const res = req.nextUrl.pathname.startsWith('/api/')
    ? NextResponse.json({ error: 'not signed in' }, { status: 401 })
    : NextResponse.redirect(new URL('/login?e=no-session', req.url));
  for (const name of [ACCESS_COOKIE, REFRESH_COOKIE, DEADLINE_COOKIE]) {
    res.cookies.set(name, '', cookieOptions(0));
  }
  return res;
}

export async function middleware(req: NextRequest) {
  const access = req.cookies.get(ACCESS_COOKIE)?.value;
  const refresh = req.cookies.get(REFRESH_COOKIE)?.value;
  const until = Number(req.cookies.get(DEADLINE_COOKIE)?.value ?? '0');
  const now = Math.floor(Date.now() / 1000);

  if (!access || !refresh || !until || now >= until) return turnAway(req);
  if (secondsLeft(access, now) > 120) return NextResponse.next();

  const supabaseUrl = (process.env.SUPABASE_URL ?? '').replace(/\/+$/, '');
  const anonKey = process.env.SUPABASE_ANON_KEY ?? '';
  const tokens = await refreshSession({ supabaseUrl, anonKey }, refresh);
  if (!tokens) return turnAway(req);

  // Hand the new token to THIS request as well as the browser.
  req.cookies.set(ACCESS_COOKIE, tokens.access_token);
  req.cookies.set(REFRESH_COOKIE, tokens.refresh_token);
  const res = NextResponse.next({ request: { headers: req.headers } });
  const remaining = until - now;
  res.cookies.set(ACCESS_COOKIE, tokens.access_token, cookieOptions(remaining));
  res.cookies.set(REFRESH_COOKIE, tokens.refresh_token, cookieOptions(remaining));
  return res;
}
