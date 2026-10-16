/**
 * Turning a page change into proposed knowledge-base entries. PURE: builds
 * the prompt and checks the model's answer; the call itself is lib/watcher/llm.ts.
 *
 * The model sees ONLY: the page's label and address, the passages that
 * changed (new and removed), and up to a few existing Q&A entries the bot's
 * own search ranks closest to those passages. It must answer JSON:
 *   { "drafts": [ a NEW entry: { "target_entry_id": null, "question": "...", "answer": "...",
 *                                "source_passage": "<copied word for word from the page>", "why": "..." },
 *                 or a CHANGE: { "target_entry_id": "<one of the listed ids>",
 *                                "add": [...], "replace": [...], "why": "..." } ] }
 * A change is never a rewritten answer: lib/watcher/additive.ts checks each
 * addition and replacement and builds the entry from the live text.
 * Every draft is then checked here, and dropped (with the reason logged)
 * unless:
 *   - target_entry_id is null (a new entry) or one of the candidate ids;
 *   - source_passage occurs in the new page text, spacing aside (locatePassage:
 *     the words and figures must match exactly) - the page's own span is kept
 *     and shown to the reviewer, so a paraphrase is refused;
 *   - question and answer are present and together at most 1,200 characters
 *     (the editor's limit, which the database enforces again);
 *   - nothing looks like an NRIC/FIN;
 *   - it carries a fact a client could act on and does not only point
 *     elsewhere (lib/watcher/quality.ts, 2026-10-15: the MOM levy index page
 *     drafted four "The MOM page provides information on..." entries).
 * A draft that survives is still only a PROPOSAL: it goes to the review
 * queue, and a person approves, edits or rejects it.
 */

import { locatePassage } from './passage.ts';
import { lowValueReason } from './quality.ts';
import { parseUpdate, type Addition, type Replacement } from './additive.ts';
import { sentenceCarried } from './factDiff.ts';
import { NEW_SHAPE, UPDATE_RULES, UPDATE_SHAPE } from './changeRules.ts';

export { locatePassage };

export type Candidate = { id: string; question: string; answer: string };

export type ProposedDraft = {
  target_entry_id: string | null;
  question: string;
  answer: string;
  source_passage: string;
  why: string;
  /** A change to an existing entry: what was added and replaced (the answer is built from these, lib/watcher/additive.ts). */
  change?: { added: Addition[]; replaced: Replacement[]; conflicts?: string[] };
};

export const MAX_DRAFTS_PER_CHANGE = 6;
const MAX_ENTRY_CHARS = 1200;
const NRIC = /\b[STFGM]\d{7}[A-Z]\b/i;

export function buildPrompt(p: {
  label: string;
  url: string;
  added: string[];
  removed: string[];
  candidates: Candidate[];
}): { system: string; user: string } {
  const system = [
    'You maintain the knowledge base of a WhatsApp assistant for Ming Hwee, a licensed Singapore domestic-helper employment agency.',
    'A watched public web page has changed. Propose knowledge-base entries that bring the assistant in line with the page.',
    'Rules:',
    '- Use ONLY what the page passages say. Never add facts, figures, dates or advice that are not in them.',
    UPDATE_RULES,
    '- For a fact no listed entry covers, set target_entry_id to null. If a listed entry already answers the same question, change THAT entry instead: never propose a second entry for a question the knowledge base already answers.',
    '- Every entry must state a fact a client can act on: an amount, a rule, a deadline, an eligibility condition or a step. Never draft an entry that only points elsewhere ("Where can I find information about...", "The MOM page provides information on...", "visit this page...").',
    '- If the page is mainly a list of links to other pages, return no drafts.',
    '- Never propose anything about agency fees or prices; leave money to the agency.',
    '- Answers are plain sentences for a client on WhatsApp: no headings, bullets or markdown; question plus answer at most 1,000 characters.',
    '- source_passage must be copied word for word from the NEW page passages, and must support the answer: consecutive lines exactly as they appear, character for character, keeping odd spacing (such as "1 st helper") and line order; never tidy, join or reorder table cells.',
    `- At most ${MAX_DRAFTS_PER_CHANGE} drafts. If nothing in the change matters to the assistant, return no drafts.`,
    `Answer with JSON only: {"drafts":[ ${NEW_SHAPE} or ${UPDATE_SHAPE} ]}`,
  ].join('\n');
  const user = [
    `Page: ${p.label} (${p.url})`,
    '',
    'NEW or reworded passages on the page:',
    ...p.added.map((a, i) => `[N${i + 1}] ${a}`),
    '',
    'Passages that are GONE or were reworded:',
    ...(p.removed.length ? p.removed.map((r, i) => `[R${i + 1}] ${r}`) : ['(none - this may be the first time the page is read)']),
    '',
    'Existing knowledge-base entries closest to the change:',
    ...(p.candidates.length
      ? p.candidates.map((c) => `- id ${c.id}\n  Q: ${c.question}\n  A: ${c.answer}`)
      : ['(none)']),
  ].join('\n');
  return { system, user };
}

const squash = (s: string) => s.replace(/\s+/g, ' ').trim();

export type ParseResult = { drafts: ProposedDraft[]; dropped: string[] };

export function parseDrafts(
  raw: string,
  ctx: { newText: string; candidateIds: string[]; live?: Map<string, { question: string; answer: string }> },
): ParseResult {
  const dropped: string[] = [];
  let data: unknown;
  try {
    const start = raw.indexOf('{');
    const end = raw.lastIndexOf('}');
    data = JSON.parse(start >= 0 && end > start ? raw.slice(start, end + 1) : raw);
  } catch {
    return { drafts: [], dropped: ['the model did not answer valid JSON'] };
  }
  const list = (data as { drafts?: unknown })?.drafts;
  if (!Array.isArray(list)) return { drafts: [], dropped: ['the model answer has no "drafts" list'] };
  const drafts: ProposedDraft[] = [];
  // ONE FACT, ONE PROPOSAL (2026-10-16): new entries are read first, and an
  // addition to an existing entry that a new entry in this run already states is
  // dropped - a dedicated entry beats appending to a general one (the 17th-of-the-
  // month rule went into both "What is the FDW levy?" and its own new entry).
  const items = list.slice(0, MAX_DRAFTS_PER_CHANGE * 2).map((x) => (x ?? {}) as Record<string, unknown>);
  const isNew = (d: Record<string, unknown>) => d.target_entry_id == null || d.target_entry_id === '';
  // Then changes that replace a live sentence (they own the facts they settle), then add-only changes.
  const replaces = (d: Record<string, unknown>) => Array.isArray(d.replace) && d.replace.length > 0;
  const updates = items.filter((x) => !isNew(x));
  for (const d of [...items.filter(isNew), ...updates.filter(replaces), ...updates.filter((x) => !replaces(x))]) {
    const target = d.target_entry_id == null || d.target_entry_id === '' ? null : String(d.target_entry_id);
    if (target !== null) {
      // A change to an existing entry: additions and contradicted sentences only; the answer is built here.
      if (!ctx.candidateIds.includes(target)) { dropped.push('a draft naming an entry that was not offered'); continue; }
      const live = ctx.live?.get(target);
      if (!live) { dropped.push('a change to an entry whose live text was not given'); continue; }
      if (drafts.some((x) => x.target_entry_id === target)) { dropped.push('a second draft for the same entry'); continue; }
      if (!Array.isArray(d.add) && !Array.isArray(d.replace)) {
        dropped.push(`a change to "${live.question.slice(0, 80)}" written as a whole new answer instead of additions and replacements`);
        continue;
      }
      // Facts already placed in this run: a new entry's answer, another change's replacements and additions.
      const owned = drafts.flatMap((x) => (x.target_entry_id === null ? [x.answer] : [...(x.change?.replaced.map((r) => r.new) ?? []), ...(x.change?.added.map((a) => a.sentence) ?? [])]));
      const adds = Array.isArray(d.add) ? (d.add as Record<string, unknown>[]) : [];
      const own = adds.filter((a) => {
        const s = typeof a?.sentence === 'string' ? a.sentence : '';
        const other = s ? owned.find((ans) => sentenceCarried(s, ans)) : undefined;
        if (other) dropped.push(`an addition to "${live.question.slice(0, 60)}" that another proposal in this run already states ("${squash(s).slice(0, 80)}") - one fact, one proposal`);
        return !other;
      });
      const elsewhere = [...(ctx.live?.entries() ?? [])].filter(([id]) => id !== target).map(([, e]) => e);
      const u = parseUpdate(adds.length === own.length ? d : { ...d, add: own }, live, ctx.newText, MAX_ENTRY_CHARS, elsewhere);
      dropped.push(...u.dropped);
      if (!u.built) continue;
      if ((live.question + '\n' + u.built.answer).length > MAX_ENTRY_CHARS) {
        dropped.push(`a change that would take "${live.question.slice(0, 80)}" over ${MAX_ENTRY_CHARS} characters`);
        continue;
      }
      const text = [...u.built.added.map((x) => x.sentence), ...u.built.replaced.map((x) => x.new)].join(' ');
      if (NRIC.test(`${text} ${u.built.sourcePassage}`)) { dropped.push('a draft containing what looks like an NRIC/FIN'); continue; }
      drafts.push({
        target_entry_id: target,
        question: live.question,
        answer: u.built.answer,
        source_passage: u.built.sourcePassage,
        why: typeof d.why === 'string' ? d.why.slice(0, 500) : '',
        change: { added: u.built.added, replaced: u.built.replaced, conflicts: u.built.conflicts },
      });
      if (drafts.length >= MAX_DRAFTS_PER_CHANGE) break;
      continue;
    }
    const question = typeof d.question === 'string' ? d.question.trim() : '';
    const answer = typeof d.answer === 'string' ? d.answer.trim() : '';
    const given = typeof d.source_passage === 'string' ? d.source_passage.trim() : '';
    if (!question || !answer) { dropped.push('a draft without a question or an answer'); continue; }
    if ((question + '\n' + answer).length > MAX_ENTRY_CHARS) { dropped.push(`a draft over ${MAX_ENTRY_CHARS} characters`); continue; }
    const passage = given ? locatePassage(ctx.newText, given) : null;
    if (!passage) { dropped.push(`a draft whose source passage is not on the page ("${squash(given).slice(0, 80)}")`); continue; }
    if (NRIC.test(`${question} ${answer} ${passage}`)) { dropped.push('a draft containing what looks like an NRIC/FIN'); continue; }
    const low = lowValueReason(question, answer);
    if (low) { dropped.push(low); continue; }
    drafts.push({ target_entry_id: null, question, answer, source_passage: passage, why: typeof d.why === 'string' ? d.why.slice(0, 500) : '' });
    if (drafts.length >= MAX_DRAFTS_PER_CHANGE) break;
  }
  return { drafts, dropped };
}
