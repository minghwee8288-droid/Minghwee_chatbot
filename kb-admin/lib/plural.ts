/**
 * "1 chunk", "2 chunks", "1 entry", "3 entries". Every count shown in kb-admin
 * goes through this, so "1 chunks switched off" cannot come back (the
 * self-check fails on a count written straight before a fixed plural noun).
 * PURE: no imports, so scripts/selfcheck.mjs can run it.
 */

const IRREGULAR: Readonly<Record<string, string>> = { entry: 'entries', batch: 'batches', match: 'matches' };

export function plural(n: number | string | null | undefined, word: string): string {
  const count = Number(n ?? 0);
  const shown = typeof n === 'string' ? n : count.toLocaleString('en-SG');
  return `${shown} ${count === 1 ? word : IRREGULAR[word] ?? `${word}s`}`;
}
