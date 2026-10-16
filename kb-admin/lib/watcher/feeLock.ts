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
 * Government figures (decision 2026-10-15): a money figure from a page in the
 * "government rule" category (levy, security bond, MOM fees, minimum income,
 * insurance minimums) is approvable, but only with a warning and the
 * approver's tick that they checked it against the source (KB008 without it).
 * From any other category a money figure stays locked.
 *
 * This copy decides what the review screen shows; the database decides what
 * happens. The self-check runs both rule sets on the same cases, and compares
 * the locked lists here with the arrays in the migration.
 */

const MONEY = [/(\$|₱|\b(?:sgd|rm|rp|php)\s?)\s?\d/i, /\d[\d,.]*\s*(?:dollars|sgd)\b/i];
const FEE_WORDS = /\b(?:agency|service|placement|replacement|documentation|processing|administrative|admin|recruitment)\s+fees?\b/i;
// A government fee named as such ("MOM processing fee", "work permit fee") is not an agency fee.
const GOVERNMENT_FEE = /\b(?:mom|ministry of manpower|government|work permit|application|issuance|levy)\s+(?:processing\s+)?fees?\b/gi;
const SALARY = /\bsalar(?:y|ies)\b/i;
const FEE = /\bfees?\b/i;

/** false = the 015 rule: a money figure and "salary" anywhere in the old or new text lock the change. */
export const SENTENCE_SCOPED_SALARY_LOCK = true;

const sentencesOf = (...parts: (string | null)[]) =>
  parts.filter(Boolean).join('\n').split(/(?<=[.!?])\s+|\n+/).map((x) => x.replace(/\s+/g, ' ').trim()).filter(Boolean);

/** The sentences a change adds, replaces or removes (on one side and not the other); a new entry is all of them. */
export function changedSentences(d: Omit<LockInput, 'category'>): string[] {
  const before = sentencesOf(d.targetQuestion, d.targetAnswer);
  const after = sentencesOf(d.proposedQuestion, d.proposedAnswer);
  return [...after.filter((x) => !before.includes(x)), ...before.filter((x) => !after.includes(x))];
}

export type LockInput = {
  targetQuestion: string | null;
  targetAnswer: string | null;
  proposedQuestion: string;
  proposedAnswer: string;
  /** The source page's category (government_rule / process / embassy / other). */
  category: string | null;
};

function allText(d: Omit<LockInput, 'category'>): string {
  return [d.targetQuestion, d.targetAnswer, d.proposedQuestion, d.proposedAnswer].filter(Boolean).join('\n');
}

/** Does the change carry a money figure (in the current or the proposed text)? */
export function touchesMoney(d: Omit<LockInput, 'category'>): boolean {
  const all = allText(d);
  return MONEY.some((re) => re.test(all));
}

/** Why this draft needs agency confirmation, or null when a person may approve it. */
export function feeLockReason(d: LockInput): string | null {
  const tq = d.targetQuestion?.trim() ?? null;
  if (tq !== null && LOCKED_QUESTIONS.includes(tq)) return "the entry came from the agency's fees document";
  if (d.targetAnswer && LOCKED_NEEDLES.some((n) => d.targetAnswer!.includes(n))) {
    return "the entry carries a correction from the agency's fees document";
  }
  const all = allText(d);
  if (FEE_WORDS.test(all.replace(GOVERNMENT_FEE, ' '))) return 'the change concerns an agency fee';
  const money = touchesMoney(d);
  if (money && SALARY.test(all)) {
    // 2026-10-16: a salary or fee figure locks only where it is CHANGED - a sentence added, replaced or
    // removed that itself carries a money figure and salary/fee wording. An unchanged sentence never does
    // ("Her salary is S$650." kept as it was no longer locks a new rest-day line). Twin: migration 018.
    const changed = SENTENCE_SCOPED_SALARY_LOCK ? changedSentences(d) : [all];
    const hit = changed.find((s) => MONEY.some((re) => re.test(s)) && (SALARY.test(s) || FEE.test(s.replace(GOVERNMENT_FEE, ' '))));
    if (hit) return SALARY.test(hit) ? 'the change touches a helper salary figure (an agency pricing rule)' : 'the change states a fee figure (an agency pricing rule)';
  }
  if (money && d.category !== 'government_rule') return 'the change touches a money figure and the page is not a government rule page';
  return null;
}

/** What the review screen needs: the lock, or (when approvable) whether a figure must be confirmed. */
export function reviewState(d: LockInput): { lock: string | null; figure: boolean } {
  const lock = feeLockReason(d);
  return { lock, figure: lock === null && touchesMoney(d) };
}
