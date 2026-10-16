import { extractFacts } from '../facts.ts';

/**
 * The overlap check. PURE: the search itself (kb_admin_match_live, the bot's
 * own live search) is passed in by lib/watcher/pipeline.ts.
 *
 * Before a NEW entry is proposed, its text is searched against the live
 * knowledge base:
 *   - a live Q&A entry at or above EDIT_INSTEAD_SIMILARITY already answers
 *     the same question, so the proposal becomes a change to THAT entry (one
 *     more model call, which writes the full new text of the entry); if the
 *     entry already says everything, nothing is proposed;
 *   - every live entry at or above SIMILAR_SIMILARITY is listed on the
 *     proposal ("Similar existing entries": link, similarity, and every
 *     figure - amount, percentage, duration - stated by one and not the
 *     other);
 *   - two figures that disagree (same kind, same period, said of the same
 *     subject: $350 against $300 for the levy), or a near-duplicate that could
 *     not be turned into a change, is a CONFLICT: the review page shows the conflict banner and
 *     the approver must tick that they checked both against the source (the
 *     database refuses the approval otherwise, KB009 - migration 016).
 *
 * The thresholds were read off the live knowledge base (2026-10-15, the bot's
 * own embedding and search): the levy rows that genuinely answer a levy fact
 * score 0.68-0.88 ("How much is the monthly levy..." 0.878 for the levy-rate
 * fact); rows that only share the topic score 0.55-0.64.
 */

export const EDIT_INSTEAD_SIMILARITY = 0.72;
export const SIMILAR_SIMILARITY = 0.6;
export const MAX_SIMILAR = 5;

export type LiveEntry = { id: string; question: string; answer: string; similarity: number };

/** What is stored on the proposal (cb_kb_drafts.similar_entries, migration 016). */
export type SimilarNote = {
  id: string;
  question: string;
  similarity: number;
  /** Figures only the proposal states, and figures only the live entry states (shown on the panel). */
  differs: { proposal: string[]; entry: string[] };
  /** Pairs that disagree about the same thing ("$350 against $300 (levy, per month)") - these raise the conflict. */
  conflicts: string[];
};

const FIGURE_KINDS = new Set(['money', 'percent', 'span']);

// Words that say nothing about WHAT a figure is (every levy, salary and fee
// sentence has them), so they cannot tie two figures to the same subject.
const GENERIC = new Set(('helper helpers maid maids domestic worker workers migrant employer employers singapore month months monthly '
  + 'rate rates cost costs pay paid paying payment payments fee fees amount amounts total your with from that this have will each '
  + 'about around approximately standard normal first which what when where there their they must also more than after before only '
  + 'every year years yearly annual daily days per should would could much does need how the and for are you can her she his him '
  + 'singapore\'s usually typically including include includes charged charge').split(' '));

type Figure = { kind: string; fact: string; period: string; keys: Set<string> };

function period(sentence: string): string {
  if (/\b(?:per day|daily|a day|\/day|each day)\b/i.test(sentence)) return 'day';
  if (/\b(?:per year|yearly|annual(?:ly)?|a year|\/year|\/yr|per annum)\b/i.test(sentence)) return 'year';
  if (/\b(?:per month|monthly|a month|\/month|\/mo|each month|every month)\b/i.test(sentence)) return 'month';
  return '';
}

const contentWords = (s: string) =>
  (s.toLowerCase().match(/[a-z]{4,}/g) ?? []).map((w) => w.replace(/s$/, '')).filter((w) => !GENERIC.has(w) && !GENERIC.has(`${w}s`));

/**
 * Each figure with the words around it - up to five before and three after,
 * inside its own clause - because one sentence often lists several things:
 * "helper salary (from S$500), monthly levy (S$300 or S$60), insurance (S$200)".
 * Read per sentence, the salary would share "levy" with the levy (dry run
 * 2026-10-16: a $450 levy "against" a $500 salary).
 */
function figureList(text: string): Figure[] {
  const out: Figure[] = [];
  for (const sentence of text.split(/(?<=[.!?])\s+|\n+/)) {
    const facts = extractFacts(sentence).filter((f) => FIGURE_KINDS.has(f.kind));
    if (!facts.length) continue;
    const sentencePeriod = period(sentence);
    for (const f of facts) {
      const before = sentence.slice(0, f.at).split(/[,;:()]|\b(?:and|or)\b/).pop() ?? '';
      const after = sentence.slice(f.at + f.raw.length).split(/[,;:()]|\b(?:and|or)\b/)[0] ?? '';
      const keys = new Set([...contentWords(before).slice(-5), ...contentWords(after).slice(0, 3)]);
      out.push({ kind: f.kind, fact: f.fact, period: period(`${before} ${f.raw} ${after}`) || sentencePeriod, keys });
    }
  }
  return out;
}

/**
 * Figures of the same kind that one text states and the other does not (for
 * the panel), and the pairs among them that DISAGREE (for the conflict): same
 * kind, the same period (per day / month / year, or neither said), and said
 * of the same subject (a shared word such as "levy" or "concessionary").
 * Dry run 2026-10-15: without the subject and period, a levy-rate proposal
 * "conflicted" with salary entries ($300 levy against $650 salary) and a daily
 * rate ($1.98) with a yearly saving ($2,880).
 */
export function compareFigures(proposal: string, entry: string): Pick<SimilarNote, 'differs' | 'conflicts'> {
  const a = figureList(proposal);
  const b = figureList(entry);
  const factsA = new Set(a.map((f) => `${f.kind}|${f.fact}`));
  const factsB = new Set(b.map((f) => `${f.kind}|${f.fact}`));
  const kindsA = new Set(a.map((f) => f.kind));
  const kindsB = new Set(b.map((f) => f.kind));
  const onlyA = a.filter((f) => kindsB.has(f.kind) && !factsB.has(`${f.kind}|${f.fact}`));
  const onlyB = b.filter((f) => kindsA.has(f.kind) && !factsA.has(`${f.kind}|${f.fact}`));
  const conflicts: string[] = [];
  for (const x of onlyA) {
    for (const y of onlyB) {
      if (x.kind !== y.kind || x.period !== y.period) continue;
      const shared = [...x.keys].filter((k) => y.keys.has(k));
      if (!shared.length) continue;
      const line = `${x.fact} against ${y.fact} (${shared.slice(0, 2).join(', ')}${x.period ? `, per ${x.period}` : ''})`;
      if (!conflicts.includes(line)) conflicts.push(line);
    }
  }
  const uniq = (xs: Figure[]) => [...new Set(xs.map((f) => f.fact))];
  return { differs: { proposal: uniq(onlyA), entry: uniq(onlyB) }, conflicts: conflicts.slice(0, 6) };
}

/** Backwards-compatible name for the panel half. */
export function differingFigures(proposal: string, entry: string): SimilarNote['differs'] {
  return compareFigures(proposal, entry).differs;
}

export type Overlap = { similar: SimilarNote[]; conflict: boolean; note: string | null };

/**
 * The panel and the conflict for one proposal. `matches` is the live search
 * for the proposal's text; the entry it changes (if any) is left out.
 */
export function assessOverlap(p: { question: string; answer: string; targetId: string | null }, matches: LiveEntry[]): Overlap {
  const proposal = `${p.question}\n${p.answer}`;
  const similar: SimilarNote[] = matches
    .filter((m) => m.id !== p.targetId && m.similarity >= SIMILAR_SIMILARITY)
    .sort((x, y) => y.similarity - x.similarity)
    .slice(0, MAX_SIMILAR)
    .map((m) => ({
      id: m.id,
      question: m.question.slice(0, 300),
      similarity: Math.round(m.similarity * 1000) / 1000,
      ...compareFigures(proposal, `${m.question}\n${m.answer}`),
    }));
  const notes = similar.filter((s) => s.conflicts.length).map((s) => `"${s.question.slice(0, 80)}" says ${s.conflicts.join('; ')}`);
  // A new entry kept although a live entry already answers the same question.
  const dup = p.targetId === null ? similar.find((s) => s.similarity >= EDIT_INSTEAD_SIMILARITY) : undefined;
  if (dup) notes.unshift(`a live entry already answers this ("${dup.question.slice(0, 80)}", similarity ${dup.similarity.toFixed(2)})`);
  return { similar, conflict: notes.length > 0, note: notes.length ? notes.join(' | ').slice(0, 1000) : null };
}

/**
 * The second model call: turn new drafts that duplicate a live entry into
 * changes to it. Same JSON shape as the first call; target_entry_id must be
 * one of the listed entries.
 */
export function buildMergePrompt(p: {
  label: string;
  url: string;
  pairs: { entry: { id: string; question: string; answer: string }; draft: { question: string; answer: string; source_passage: string } }[];
}): { system: string; user: string } {
  const system = [
    'You maintain the knowledge base of a WhatsApp assistant for Ming Hwee, a licensed Singapore domestic-helper employment agency.',
    'Each EXISTING entry below already answers the question of a NEW draft made from a watched public page. Instead of a second entry, give the full new question and answer for the EXISTING entry.',
    'Rules:',
    '- Keep the existing wording, and its question, wherever the page does not require a change. Change only what the page passage says differently, or add what it adds.',
    '- Use ONLY what the page passage says. Never add facts, figures, dates or advice that are not in it or in the existing entry.',
    '- Keep EVERY fact the existing entry states (each figure, rule, condition, step) unless the page passage contradicts it; add the new fact from the page to it. Never drop a point just to shorten or tidy it.',
    '- If the existing entry already says everything the passage says, return no draft for it: never return a change that only rewords it.',
    '- Never propose anything about agency fees or prices; leave money to the agency.',
    '- Plain sentences for a client on WhatsApp: no headings, bullets or markdown; question plus answer at most 1,000 characters.',
    '- source_passage must be copied word for word from the page passage given for that entry, character for character (keep odd spacing; never tidy or reorder it).',
    'Answer with JSON only: {"drafts":[{"target_entry_id":string,"question":string,"answer":string,"source_passage":string,"why":string}]}',
  ].join('\n');
  const user = [
    `Page: ${p.label} (${p.url})`,
    ...p.pairs.map((x, i) => [
      '',
      `[${i + 1}] EXISTING entry id ${x.entry.id}`,
      `  Q: ${x.entry.question}`,
      `  A: ${x.entry.answer}`,
      `  New draft from the page: Q: ${x.draft.question} A: ${x.draft.answer}`,
      `  Page passage: ${x.draft.source_passage}`,
    ].join('\n')),
  ].join('\n');
  return { system, user };
}
