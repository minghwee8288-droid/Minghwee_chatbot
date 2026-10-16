import { extractFacts } from '../facts.ts';
import { compareFigures } from './overlap.ts';
import { actionableKinds, isPointerSentence } from './quality.ts';

/**
 * What a proposed CHANGE to a live entry keeps, drops and adds. PURE.
 *
 * Two rules (2026-10-16, after the paying-levy dry runs):
 *   - A change must not lose facts. Every sentence of the live answer is
 *     either kept in the proposal (its figures all there, most of its content
 *     words there) or listed under "Points removed". A removed point is only
 *     acceptable when the source passage contradicts it (a figure of the same
 *     kind, period and subject that disagrees - compareFigures); otherwise the
 *     review page blocks Approve with its "drops existing information"
 *     banner (Edit stays available), and the approve action refuses it.
 *     One dry run proposed a new "What is the FDW levy?" that left out "it is
 *     not a tax on your helper" and the GIRO point.
 *   - No rewording-only changes. A change that adds no new figure and no new
 *     sentence carrying a fact a client can act on (an amount, a rule, a
 *     deadline, an eligibility condition or a step) is not proposed. The
 *     dry runs proposed "How much is the monthly levy..." with exactly the
 *     live figures, reworded.
 */

const STOP = new Set(('the and for are you your can her she his him its with from that this have has will each about '
  + 'into than then them they their there these those which what when where who whom how why also only just more most '
  + 'very such any all not but may might must should would could does did was were been being our ours per').split(' '));

/**
 * At least this share of a live sentence's content words must be in the
 * proposal for the sentence to count as kept - or KEPT_COVERAGE_WITH_FIGURES
 * when the sentence states figures and the proposal keeps every one of them
 * (the figures are its facts: "Households that qualify ... pay $60 a month"
 * is kept by "... $60 per month at the concessionary rate").
 */
export const KEPT_COVERAGE = 0.6;
export const KEPT_COVERAGE_WITH_FIGURES = 0.4;

/** A sentence the live entry does not carry, with no actionable fact, is still a new fact when it brings this many new content words. */
export const NEW_FACT_WORDS = 3;

export function sentencesOf(text: string): string[] {
  return (text ?? '').split(/(?<=[.!?])\s+|\n+/).map((s) => s.trim()).filter((s) => s.length > 0);
}

const stem = (w: string) => w.replace(/(?:ies)$/, 'y').replace(/(?:es|s)$/, '');

/** Same word, or one a longer form of the other ("concession" / "concessionary", "month" / "monthly"). */
function hasWord(have: Set<string>, w: string): boolean {
  if (have.has(w)) return true;
  if (w.length < 5) return false;
  for (const h of have) if (h.length >= 5 && (h.startsWith(w) || w.startsWith(h))) return true;
  return false;
}

export function contentWords(text: string): string[] {
  return (text.toLowerCase().match(/[a-z]{3,}/g) ?? []).filter((w) => !STOP.has(w)).map(stem);
}

const figureKeys = (text: string) => new Set(extractFacts(text).map((f) => `${f.kind}|${f.fact}`));

/** Is this sentence's content carried by `other`? */
export function sentenceCarried(sentence: string, other: string, need?: number): boolean {
  const figs = figureKeys(sentence);
  const otherFigs = figureKeys(other);
  for (const f of figs) if (!otherFigs.has(f)) return false;
  const words = [...new Set(contentWords(sentence))];
  if (!words.length) return true;
  const have = new Set(contentWords(other));
  const share = need ?? (figs.size ? KEPT_COVERAGE_WITH_FIGURES : KEPT_COVERAGE);
  return words.filter((w) => hasWord(have, w)).length / words.length >= share;
}

/**
 * An ADDITION is only refused as "already said" when the entry states all its
 * figures and this share of its content words - stricter than "kept", because
 * dropping a real new fact is the worse mistake here (dry run 2026-10-16: at
 * 60% "the Work Permit will be revoked if you miss two consecutive payments"
 * read as said by "missing levy payments can affect your ability to renew").
 */
export const ALREADY_SAID_COVERAGE = 0.8;
export function alreadySays(text: string, sentence: string): boolean {
  return sentenceCarried(sentence, text, ALREADY_SAID_COVERAGE);
}

/**
 * How the source contradicts a sentence of the live entry: a figure of the same
 * kind, period and subject that disagrees ("$350 against $300 (levy, per
 * month)"), a bare number counting too ("17 against 15 (following)"), or null.
 * The one test for both halves: a change may only REPLACE a sentence the
 * source contradicts (lib/watcher/additive.ts), and a removed point is only
 * justified by it (removedPoints, the safety net) - so a change built from
 * accepted replacements can never trip the safety net.
 */
export function contradicts(sourcePassage: string, sentence: string): string | null {
  return compareFigures(sourcePassage, sentence, { numbers: true }).conflicts[0] ?? null;
}

/**
 * Why the source justifies replacing `oldSentence` with `newSentence`, or null.
 * Either a figure contradiction (contradicts()), or the page states the same
 * thing differently - a rule or a date, which has no figure to compare
 * (dry run 2026-10-16: the live "The levy starts when your helper's Work
 * Permit is issued and stops when it is cancelled" against the page's "The
 * levy ends when the Work Permit is cancelled or expires"). For the second,
 * all must hold: the page supports the new sentence, the page does NOT say
 * the old one, the two share at least two content words (the same subject),
 * and every figure of the old sentence is still in the new one. A reworded
 * sentence the page does not say is never a replacement.
 */
/**
 * For a rule stated differently, the new sentence must share at least this
 * share of the old sentence's content words: "starts when ... issued and stops
 * when it is cancelled" against "ends when ... cancelled or expires" shares 4
 * of 8; "pay it through GIRO, the simplest way to avoid missing a payment"
 * against "Apply for GIRO to pay the levy" only 2 of 8 - it would lose a point.
 */
export const SAME_SUBJECT_SHARE = 0.5;

export function replacementJustification(sourcePassage: string, oldSentence: string, newSentence: string): string | null {
  const figure = contradicts(sourcePassage, oldSentence);
  if (figure) return figure;
  if (!newSentence || !sentenceCarried(newSentence, sourcePassage, KEPT_COVERAGE)) return null;
  if (sentenceCarried(oldSentence, sourcePassage, ALREADY_SAID_COVERAGE)) return null;
  const newFigs = figureKeys(newSentence);
  for (const f of figureKeys(oldSentence)) if (!newFigs.has(f)) return null;
  const oldWords = new Set(contentWords(oldSentence));
  const shared = [...new Set(contentWords(newSentence))].filter((w) => oldWords.has(w));
  if (shared.length < 2 || shared.length / oldWords.size < SAME_SUBJECT_SHARE) return null;
  return `the page states it differently (${shared.slice(0, 3).join(', ')})`;
}

export type RemovedPoint = { sentence: string; contradictedBy: string | null };

/** Sentences of the live answer the proposal no longer carries, each with the source contradiction that justifies dropping it (or null). */
export function removedPoints(liveAnswer: string, proposedAnswer: string, sourcePassage: string, proposedQuestion = ''): RemovedPoint[] {
  const proposal = `${proposedQuestion}\n${proposedAnswer}`;
  // A sentence replaced in place is justified by the same test that let the replacement in (lib/watcher/additive.ts).
  const replacedBy = new Map<string, string>();
  for (const seg of entryDiff(liveAnswer, proposedAnswer)) if (seg.kind === 'replaced') replacedBy.set(seg.old, seg.new);
  return sentencesOf(liveAnswer)
    .filter((s) => !isPointerSentence(s) && !sentenceCarried(s, proposal))
    .map((sentence) => ({
      sentence,
      contradictedBy: replacedBy.has(sentence)
        ? replacementJustification(sourcePassage, sentence, replacedBy.get(sentence)!)
        : contradicts(sourcePassage, sentence),
    }));
}

/** The removed points no source passage justifies - each one blocks Approve. */
export function unjustifiedRemovals(liveAnswer: string, proposedAnswer: string, sourcePassage: string, proposedQuestion = ''): RemovedPoint[] {
  return removedPoints(liveAnswer, proposedAnswer, sourcePassage, proposedQuestion).filter((r) => !r.contradictedBy);
}

export type NewFacts = { figures: string[]; sentences: string[] };

/**
 * What the proposal adds over the live entry: figures it does not state, and
 * sentences it does not carry that hold an actionable fact or at least
 * NEW_FACT_WORDS content words the entry never uses (a reworded sentence uses
 * the entry's own words).
 */
export function newFacts(live: { question: string; answer: string }, proposed: { question: string; answer: string }): NewFacts {
  const liveText = `${live.question}\n${live.answer}`;
  const liveFigs = figureKeys(liveText);
  const figures = [...new Set(extractFacts(`${proposed.question}\n${proposed.answer}`)
    .filter((f) => !liveFigs.has(`${f.kind}|${f.fact}`)).map((f) => f.raw.trim()))];
  const liveWords = new Set(contentWords(liveText));
  const sentences = sentencesOf(proposed.answer)
    .filter((s) => !isPointerSentence(s) && !sentenceCarried(s, liveText))
    .filter((s) => actionableKinds(s).length > 0
      || new Set(contentWords(s).filter((w) => !hasWord(liveWords, w))).size >= NEW_FACT_WORDS);
  return { figures, sentences };
}

/** Why a change to a live entry is rewording only, or null when it adds something. */
export function rewordingOnlyReason(live: { question: string; answer: string }, proposed: { question: string; answer: string }): string | null {
  const n = newFacts(live, proposed);
  if (n.figures.length || n.sentences.length) return null;
  return `a change that only rewords the live entry ("${live.question.slice(0, 80)}") - no new fact, figure, rule, deadline or step`;
}

export type DiffSegment =
  | { kind: 'same'; text: string }
  | { kind: 'added'; text: string }
  | { kind: 'replaced'; old: string; new: string }
  | { kind: 'removed'; text: string };

const norm = (s: string) => s.toLowerCase().replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, ' ').trim();

/**
 * The review page's view of a change: the live answer against the proposed
 * one, sentence by sentence - unchanged (grey), added (green), replaced
 * (old -> new) and, if a person's Edit removed one, removed. A replacement is
 * built at its old sentence's place, so a removed sentence followed in the
 * same gap by a new one is shown as that sentence replaced.
 */
export function entryDiff(liveAnswer: string, proposedAnswer: string): DiffSegment[] {
  const a = sentencesOf(liveAnswer);
  const b = sentencesOf(proposedAnswer);
  const na = a.map(norm);
  const nb = b.map(norm);
  const L: number[][] = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      L[i][j] = na[i] === nb[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    }
  }
  const out: DiffSegment[] = [];
  let gapOld: string[] = [];
  let gapNew: string[] = [];
  const flush = () => {
    const n = Math.min(gapOld.length, gapNew.length);
    // One live sentence replaced by several (rule 1, 2026-10-16: the begin and end lines replace the start/stop sentence): one replacement, not a replacement plus additions.
    if (n > 0 && gapNew.length > gapOld.length) gapNew = [...gapNew.slice(0, n - 1), gapNew.slice(n - 1).join(' ')];
    for (let k = 0; k < n; k += 1) out.push({ kind: 'replaced', old: gapOld[k], new: gapNew[k] });
    for (const t of gapOld.slice(n)) out.push({ kind: 'removed', text: t });
    for (const t of gapNew.slice(n)) out.push({ kind: 'added', text: t });
    gapOld = [];
    gapNew = [];
  };
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && na[i] === nb[j]) { flush(); out.push({ kind: 'same', text: b[j] }); i += 1; j += 1; }
    else if (j < b.length && (i >= a.length || L[i][j + 1] >= L[i + 1][j])) { gapNew.push(b[j]); j += 1; }
    else { gapOld.push(a[i]); i += 1; }
  }
  flush();
  return out;
}

/**
 * Who removed each unjustified point (2026-10-16, "Can I hire a second
 * helper?"): the drop block exists to stop the MODEL losing facts. An
 * approver who used Edit to take out a live sentence that is wrong (the live
 * "standard levy rate (S$300/month)" for a second helper; the correct $450 is
 * on another page, so this page could not contradict it) is not blocked: the
 * point is shown under "Points removed" and Approve asks for a tick ("I
 * confirm these points are wrong or outdated") and the reason. A point the
 * model's own draft already dropped stays a hard block. `modelAnswer` is the
 * proposal as the watcher drafted it (null when nobody edited it).
 * EDIT_REMOVAL_CONFIRM = false: every unjustified removal blocks, as before.
 */
export const EDIT_REMOVAL_CONFIRM = true;
export function removalsByOrigin(liveAnswer: string, modelAnswer: string | null, proposedAnswer: string, sourcePassage: string,
  proposedQuestion = ''): { model: RemovedPoint[]; edit: RemovedPoint[] } {
  const unjustified = unjustifiedRemovals(liveAnswer, proposedAnswer, sourcePassage, proposedQuestion);
  if (!EDIT_REMOVAL_CONFIRM || modelAnswer === null) return { model: unjustified, edit: [] };
  const byEdit = (r: RemovedPoint) => sentenceCarried(r.sentence, `${proposedQuestion}\n${modelAnswer}`);
  return { model: unjustified.filter((r) => !byEdit(r)), edit: unjustified.filter(byEdit) };
}
