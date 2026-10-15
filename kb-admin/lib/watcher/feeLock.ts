import { LOCKED_NEEDLES, LOCKED_QUESTIONS } from './fee-locked.ts';

/**
 * The fee lock (hard rule), the code-side twin of the database's fee-lock
 * function in the watcher migration (scripts/sql, 015). PURE.
 *
 * A watcher draft may NEVER change an agency fee, a cb_kb_rules value, or a
 * row from the agency's fees document (applied 2026-10-15). Such a draft is
 * shown as "Needs agency confirmation" and has no Approve button - and the
 * database refuses to approve it whatever the page sends (KB007), checking
 * against the entry's text at the moment of approval.
 *
 * This copy decides what the review screen shows; the database decides what
 * happens. The self-check runs both rule sets on the same cases, and compares
 * the locked lists here with the arrays in the migration.
 */

const MONEY = [/(\$|₱|\b(?:sgd|rm|rp|php)\s?)\s?\d/i, /\d[\d,.]*\s*(?:dollars|sgd)\b/i];
const FEE_WORDS = /\b(?:agency|service|placement|replacement|documentation|processing|administrative|admin|recruitment)\s+fees?\b/i;

export type LockInput = {
  targetQuestion: string | null;
  targetAnswer: string | null;
  proposedQuestion: string;
  proposedAnswer: string;
};

/** Why this draft needs agency confirmation, or null when a person may approve it. */
export function feeLockReason(d: LockInput): string | null {
  const tq = d.targetQuestion?.trim() ?? null;
  if (tq !== null && LOCKED_QUESTIONS.includes(tq)) return "the entry came from the agency's fees document";
  if (d.targetAnswer && LOCKED_NEEDLES.some((n) => d.targetAnswer!.includes(n))) {
    return "the entry carries a correction from the agency's fees document";
  }
  const all = [d.targetQuestion, d.targetAnswer, d.proposedQuestion, d.proposedAnswer].filter(Boolean).join('\n');
  if (MONEY.some((re) => re.test(all))) return 'the change touches a money figure';
  if (FEE_WORDS.test(all)) return 'the change concerns an agency fee';
  return null;
}
