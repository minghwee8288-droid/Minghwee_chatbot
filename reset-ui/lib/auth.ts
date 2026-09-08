/**
 * The shared-password gate.
 *
 * The client types one password. It is checked HERE, in the serverless
 * function — the Supabase service-role key never reaches the browser, which
 * is the property that matters: a leaked UI password lets someone clear a
 * conversation, a leaked service-role key lets them do anything at all to
 * every table in a database shared with two other products.
 *
 * Comparison is timing-safe. It is a small thing over the public internet,
 * but a length-and-prefix comparison on a secret is the kind of detail that
 * is never worth arguing about afterwards.
 */

import { timingSafeEqual } from 'node:crypto';
import { env } from './env';

export const PASSWORD_HEADER = 'x-reset-password';

function safeEqual(a: string, b: string): boolean {
  const left = Buffer.from(a, 'utf8');
  const right = Buffer.from(b, 'utf8');
  // timingSafeEqual throws on a length mismatch, which would itself leak the
  // length. Compare against a padded copy and fold the length into the result.
  const length = Math.max(left.length, right.length, 1);
  const padLeft = Buffer.alloc(length);
  const padRight = Buffer.alloc(length);
  left.copy(padLeft);
  right.copy(padRight);
  return timingSafeEqual(padLeft, padRight) && left.length === right.length;
}

/**
 * Best-effort throttle on wrong passwords.
 *
 * In-memory, so it is per serverless instance and a determined attacker can
 * get more attempts than the number below by landing on cold instances. It
 * is a speed bump, not a lockout, and is documented as such rather than
 * being relied on. The real protection is that the password is not a
 * username-and-password pair on a login form anyone can enumerate.
 */
const FAILURES = new Map<string, { count: number; until: number }>();
const MAX_FAILURES = 8;
const LOCKOUT_MS = 5 * 60 * 1000;

export type AuthResult = { ok: true } | { ok: false; status: number; error: string };

export function checkPassword(request: Request): AuthResult {
  if (!env.password) {
    return {
      ok: false,
      status: 503,
      error:
        'RESET_UI_PASSWORD is not set on the server, so nothing can be authorised. ' +
        'Set it in the Vercel project settings and redeploy.',
    };
  }

  const client =
    request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() || 'unknown';
  const record = FAILURES.get(client);
  const now = Date.now();
  if (record && record.count >= MAX_FAILURES && now < record.until) {
    const seconds = Math.ceil((record.until - now) / 1000);
    return {
      ok: false,
      status: 429,
      error: `Too many incorrect passwords. Try again in ${seconds} seconds.`,
    };
  }

  const supplied = request.headers.get(PASSWORD_HEADER) ?? '';
  if (!supplied || !safeEqual(supplied, env.password)) {
    const next = record && now < record.until ? record.count + 1 : 1;
    FAILURES.set(client, { count: next, until: now + LOCKOUT_MS });
    return { ok: false, status: 401, error: 'Incorrect password.' };
  }

  FAILURES.delete(client);
  return { ok: true };
}
