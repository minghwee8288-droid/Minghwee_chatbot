import { timingSafeEqual } from 'node:crypto';

/**
 * Is this request Vercel's scheduled call? PURE. Vercel sends
 * "Authorization: Bearer <CRON_SECRET>"; anything else is not the schedule.
 * Compared in constant time.
 */
export function checkCronRequest(authorization: string | null, cronSecret: string): boolean {
  const m = /^Bearer (.+)$/.exec(authorization ?? '');
  if (!m || !cronSecret) return false;
  const a = Buffer.from(m[1]);
  const b = Buffer.from(cronSecret);
  return a.length === b.length && timingSafeEqual(a, b);
}
