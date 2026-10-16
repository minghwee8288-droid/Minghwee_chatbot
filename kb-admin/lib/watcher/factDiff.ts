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
 *   - No rewording-only updates. A change that adds no new figure and no new
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
export function sentenceCarried(sentence: string, other: string): boolean {
  const figs = figureKeys(sentence);
  const otherFigs = figureKeys(other);
  for (const f of figs) if (!otherFigs.has(f)) return false;
  const words = [...new Set(contentWords(sentence))];
  if (!words.length) return true;
  const have = new Set(contentWords(other));
  const need = figs.size ? KEPT_COVERAGE_WITH_FIGURES : KEPT_COVERAGE;
  return words.filter((w) => hasWord(have, w)).length / words.length >= need;
}

export type RemovedPoint = { sentence: string; contradictedBy: string | null };

/** Sentences of the live answer the proposal no longer carries, each with the source contradiction that justifies dropping it (or null). */
export function removedPoints(liveAnswer: string, proposedAnswer: string, sourcePassage: string, proposedQuestion = ''): RemovedPoint[] {
  const proposal = `${proposedQuestion}\n${proposedAnswer}`;
  return sentencesOf(liveAnswer)
    .filter((s) => !isPointerSentence(s) && !sentenceCarried(s, proposal))
    .map((sentence) => ({ sentence, contradictedBy: compareFigures(sourcePassage, sentence).conflicts[0] ?? null }));
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
