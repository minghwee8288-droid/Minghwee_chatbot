import { NextResponse, type NextRequest } from 'next/server';
import { env } from '@/lib/env';
import { ACCESS_COOKIE, DEADLINE_COOKIE, REFRESH_COOKIE, cookieOptions, signOut } from '@/lib/session';

export async function POST(req: NextRequest) {
  const token = req.cookies.get(ACCESS_COOKIE)?.value;
  if (token) await signOut(env(), token);
  const res = NextResponse.redirect(new URL('/login?e=signed-out', req.url), 303);
  for (const name of [ACCESS_COOKIE, REFRESH_COOKIE, DEADLINE_COOKIE]) {
    res.cookies.set(name, '', cookieOptions(0));
  }
  return res;
}
