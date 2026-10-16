import { alreadySays, contradicts, replacementJustification, sentenceCarried, KEPT_COVERAGE } from './factDiff.ts';

export { entryDiff, type DiffSegment } from './factDiff.ts';
import { locatePassage, locateSpan } from './passage.ts';
import { isPointerSentence } from './quality.ts';

/**
 * Additive changes (2026-10-16). PURE.
 *
 * A proposal that changes an EXISTING entry never carries a rewritten answer.
 * The model returns only:
 *   add:     [{ sentence, after?, source_passage }]  - new facts, each with
 *            the exact page passage it comes from; `after` names the live
 *            sentence it belongs after (else it goes at the end);
 *   replace: [{ old, new, source_passage }]  - a live sentence and its
 *            replacement, ONLY where the source passage directly contradicts
 *            it (contradicts(): a figure of the same kind, period and subject
 *            that disagrees, a bare number counting too).
 * kb-admin then BUILDS the proposed answer deterministically: the live
 * answer, character for character, with only the accepted replacements
 * swapped in and the accepted additions inserted. The question never changes.
 * Nothing else in the live entry can change, so the safety net (the "drops
 * existing information" block, lib/watcher/factDiff.ts) should never trigger.
 * If nothing is left to add or replace, nothing is proposed.
 *
 * Why: in the 2026-10-16 dry runs the model rewrote whole entries and dropped
 * live facts every time ("What is the FDW levy?" lost 7 points), so every
 * such change was blocked at Approve.
 */

export type Addition = { sentence: string; passage: string };
export type Replacement = { old: string; new: string; passage: string; contradiction: string };

export type BuiltUpdate = {
  answer: string;
  added: Addition[];
  replaced: Replacement[];
  /** The page span covering every passage used: what is stored as the proposal's source passage. */
  sourcePassage: string;
};

export type UpdateParse = { built: BuiltUpdate | null; dropped: string[] };

const MAX_OPS = 8;
const MIN_OLD_CHARS = 12;

const str = (x: unknown) => (typeof x === 'string' ? x.trim() : '');
const short = (s: string) => s.replace(/\s+/g, ' ').slice(0, 80);
const asSentence = (s: string) => {
  const t = s.replace(/\s+/g, ' ').trim();
  return /[.!?]["')\]]?$/.test(t) ? t : `${t}.`;
};

/** The live answer's sentences as [start, end) spans - the same split as factDiff.sentencesOf. */
export function sentenceSpans(text: string): { start: number; end: number }[] {
  const spans: { start: number; end: number }[] = [];
  const re = /(?<=[.!?])\s+|\n+/g;
  let from = 0;
  for (const m of text.matchAll(re)) {
    const at = m.index ?? 0;
    if (text.slice(from, at).trim()) spans.push({ start: from, end: at });
    from = at + m[0].length;
  }
  if (text.slice(from).trim()) spans.push({ start: from, end: text.length });
  return spans.map((s) => {
    let { start, end } = s;
    while (start < end && /\s/.test(text[start])) start += 1;
    while (end > start && /\s/.test(text[end - 1])) end -= 1;
    return { start, end };
  });
}

/** The whole live sentence(s) a quoted `old` falls in, or null when it is not there or covers too little of them. */
function locateSentences(answer: string, old: string): { start: number; end: number } | null {
  const at = locateSpan(answer, old);
  if (!at) return null;
  const spans = sentenceSpans(answer).filter((s) => s.end > at.start && s.start < at.end);
  // One sentence at a time: the review page and the safety net pair a replacement with the one sentence it stands in for.
  if (spans.length !== 1) return null;
  const whole = { start: spans[0].start, end: spans[spans.length - 1].end };
  // A quoted clause is not a sentence: replacing "the levy" alone would rewrite a whole sentence nobody quoted.
  if ((at.end - at.start) / (whole.end - whole.start) < 0.6) return null;
  return whole;
}

/**
 * Check the model's add/replace lists against the live entry and the page,
 * and build the proposed answer. Every item is checked on its own; one that
 * fails is dropped with its reason and the rest still count.
 */
export function parseUpdate(
  item: Record<string, unknown>,
  live: { question: string; answer: string },
  pageText: string,
  /** Question plus answer may not grow past this (the editor's 1,200): additions are taken in order while they fit. */
  maxChars = Number.POSITIVE_INFINITY,
): UpdateParse {
  const dropped: string[] = [];
  const liveText = `${live.question}\n${live.answer}`;
  const passageAt: { start: number; end: number }[] = [];
  const onPage = (given: string) => {
    const at = given ? locateSpan(pageText, given) : null;
    if (at) passageAt.push(at);
    return at ? pageText.slice(at.start, at.end) : null;
  };

  // Replacements first, so an addition that only restates a replacement can be recognised.
  const replaced: (Replacement & { start: number; end: number })[] = [];
  const replaceList = Array.isArray(item.replace) ? item.replace.slice(0, MAX_OPS) : [];
  for (const raw of replaceList) {
    const r = (raw ?? {}) as Record<string, unknown>;
    const old = str(r.old);
    const next = str(r.new);
    const given = str(r.source_passage);
    if (!old || !next) { dropped.push('a replacement without the old or the new sentence'); continue; }
    if (old.length < MIN_OLD_CHARS) { dropped.push(`a replacement of a fragment, not a sentence ("${short(old)}")`); continue; }
    const at = locateSentences(live.answer, old);
    if (!at) { dropped.push(`a replacement whose old sentence is not in the live entry ("${short(old)}")`); continue; }
    if (replaced.some((x) => x.start < at.end && at.start < x.end)) { dropped.push(`a second replacement of the same sentence ("${short(old)}")`); continue; }
    const whole = live.answer.slice(at.start, at.end);
    const passage = onPage(given);
    if (!passage) { dropped.push(`a replacement whose source passage is not on the page ("${short(given)}")`); continue; }
    const contradiction = replacementJustification(passage, whole, asSentence(next));
    if (!contradiction) {
      passageAt.pop();
      dropped.push(`a replacement the source passage does not contradict ("${short(whole)}") - the live sentence is kept`);
      continue;
    }
    replaced.push({ old: whole, new: asSentence(next), passage, contradiction, start: at.start, end: at.end });
  }

  const added: (Addition & { pos: number; order: number })[] = [];
  const addList = Array.isArray(item.add) ? item.add.slice(0, MAX_OPS) : [];
  const spans = sentenceSpans(live.answer);
  const afterReplacements = `${liveText}\n${replaced.map((r) => r.new).join(' ')}`;
  for (const raw of addList) {
    const a = (raw ?? {}) as Record<string, unknown>;
    const sentence = str(a.sentence);
    const given = str(a.source_passage);
    if (!sentence) { dropped.push('an addition without a sentence'); continue; }
    if (isPointerSentence(sentence)) { dropped.push(`an addition that only points elsewhere ("${short(sentence)}")`); continue; }
    if (alreadySays(`${afterReplacements}\n${added.map((x) => x.sentence).join(' ')}`, sentence)) {
      dropped.push(`an addition the entry already says ("${short(sentence)}")`);
      continue;
    }
    const grown = live.question.length + 1 + live.answer.trimEnd().length
      + replaced.reduce((n, r) => n + r.new.length - (r.end - r.start), 0)
      + added.reduce((n, x) => n + 1 + x.sentence.length, 0) + 1 + asSentence(sentence).length;
    if (grown > maxChars) { dropped.push(`an addition that would take the entry over ${maxChars} characters ("${short(sentence)}") - add it by hand if it matters more`); continue; }
    const passage = onPage(given);
    if (!passage) { dropped.push(`an addition whose source passage is not on the page ("${short(given)}")`); continue; }
    // A natural place: right after the live sentence it follows, else at the end.
    const anchor = str(a.after) ? locateSpan(live.answer, str(a.after)) : null;
    const host = anchor ? spans.find((s) => s.end >= anchor.end && s.start < anchor.end) : undefined;
    added.push({ sentence: asSentence(sentence), passage, pos: host ? host.end : live.answer.trimEnd().length, order: added.length });
  }

  if (!replaced.length && !added.length) {
    dropped.push(`a change to "${short(live.question)}" that adds nothing and replaces nothing - not proposed`);
    return { built: null, dropped };
  }

  // Build: the live answer, with only these spans touched (applied from the end, so positions stay true).
  type Edit = { start: number; end: number; text: string; order: number };
  const edits: Edit[] = [
    ...replaced.map((r, i) => ({ start: r.start, end: r.end, text: r.new, order: i })),
    ...added.map((a) => ({ start: a.pos, end: a.pos, text: ` ${a.sentence}`, order: 100 + a.order })),
  ].sort((x, y) => y.start - x.start || y.order - x.order);
  let answer = live.answer.trimEnd();
  for (const e of edits) answer = answer.slice(0, e.start) + e.text + answer.slice(e.end);

  const from = Math.min(...passageAt.map((p) => p.start));
  const to = Math.max(...passageAt.map((p) => p.end));
  return {
    built: {
      answer,
      added: added.map(({ sentence, passage }) => ({ sentence, passage })),
      replaced: replaced.map(({ old, new: n, passage, contradiction }) => ({ old, new: n, passage, contradiction })),
      sourcePassage: pageText.slice(from, to),
    },
    dropped,
  };
}

/** The line of the stored source passage that contradicts a replaced sentence, for the review page. */
export function contradictingLine(sourcePassage: string, oldSentence: string, newSentence = ''): string | null {
  const lines = sourcePassage.split(/\n+|(?<=[.!?])\s+/).map((l) => l.trim()).filter(Boolean);
  for (const line of lines) if (contradicts(line, oldSentence)) return line;
  // A rule stated differently: the page's line that says the new sentence.
  if (newSentence) for (const line of lines) if (sentenceCarried(newSentence, line, KEPT_COVERAGE)) return line;
  return replacementJustification(sourcePassage, oldSentence, newSentence);
}

export { locatePassage };
