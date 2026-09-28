'use server';

import { cookies, headers } from 'next/headers';
import { redirect } from 'next/navigation';
import { decideAccess } from '@/lib/access';
import { env } from '@/lib/env';
import { membership } from '@/lib/queries';
import {
  ACCESS_COOKIE,
  DEADLINE_COOKIE,
  REFRESH_COOKIE,
  SESSION_HOURS,
  cookieOptions,
  passwordSignIn,
  signOut,
} from '@/lib/session';

export type LoginState = { error: string };

// One message for every failure, so the form does not reveal which accounts
// exist or which have access. The reason is logged server-side.
const GENERIC = 'Sign-in failed. Check your email and password, or ask for KB Admin access.';

// A speed bump against guessing, per server instance. Supabase Auth applies
// its own rate limits as well.
const WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 10;
const attempts = new Map<string, number[]>();

function limited(key: string): boolean {
  const now = Date.now();
  const recent = (attempts.get(key) ?? []).filter((t) => now - t < WINDOW_MS);
  recent.push(now);
  attempts.set(key, recent);
  if (attempts.size > 5000) attempts.clear();
  return recent.length > MAX_ATTEMPTS;
}

export async function login(_prev: LoginState, form: FormData): Promise<LoginState> {
  const email = String(form.get('email') ?? '').trim().toLowerCase();
  const password = String(form.get('password') ?? '');
  const ip = headers().get('x-forwarded-for')?.split(',')[0]?.trim() || 'local';

  if (limited(ip)) {
    console.info('[auth] login refused reason=rate-limited');
    return { error: 'Too many attempts. Wait a few minutes and try again.' };
  }
  if (!email || !password || email.length > 254 || password.length > 256) {
    return { error: GENERIC };
  }

  const cfg = env();
  const tokens = await passwordSignIn(cfg, email, password);
  if (!tokens) {
    console.info('[auth] login refused reason=bad-credentials');
    return { error: GENERIC };
  }

  const decision = decideAccess(await membership(tokens.user.id));
  if (!decision.ok) {
    // A valid Supabase account without kb-admin access: end the session it
    // was just given, and set no cookie.
    await signOut(cfg, tokens.access_token);
    console.info(`[auth] login refused reason=${decision.reason} user=${tokens.user.id.slice(0, 8)}`);
    return { error: GENERIC };
  }

  const seconds = SESSION_HOURS * 3600;
  const jar = cookies();
  jar.set(ACCESS_COOKIE, tokens.access_token, cookieOptions(seconds));
  jar.set(REFRESH_COOKIE, tokens.refresh_token, cookieOptions(seconds));
  jar.set(DEADLINE_COOKIE, String(Math.floor(Date.now() / 1000) + seconds), cookieOptions(seconds));
  console.info(`[auth] login ok user=${tokens.user.id.slice(0, 8)}`);
  redirect('/documents');
}
