import { NextResponse, type NextRequest } from 'next/server';
import { ACCESS_COOKIE, DEADLINE_COOKIE, REFRESH_COOKIE, cookieOptions } from '@/lib/session';

/**
 * Where requireViewer() sends a signed-in account whose access has been
 * switched off (active = false). A page cannot set cookies while redirecting,
 * so this route does it: it clears the three session cookies and then shows
 * the sign-in page with the "switched off" notice.
 *
 * It reads nothing and calls nothing, so anyone reaching it can only clear
 * their own cookies. It sits under /login, so the middleware skips it just as
 * it skips the sign-in page. The Supabase session is not ended here: sign-out
 * stays at local scope, and the leftover session is refused on every request.
 */

export const dynamic = 'force-dynamic';

export function GET(req: NextRequest) {
  const res = NextResponse.redirect(new URL('/login?e=inactive', req.url), 303);
  for (const name of [ACCESS_COOKIE, REFRESH_COOKIE, DEADLINE_COOKIE]) {
    res.cookies.set(name, '', cookieOptions(0));
  }
  res.headers.set('cache-control', 'no-store');
  return res;
}
