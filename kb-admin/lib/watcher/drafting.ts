/**
 * Turning a page change into proposed knowledge-base entries. PURE: builds
 * the prompt and checks the model's answer; the call itself is lib/watcher/llm.ts.
 *
 * The model sees ONLY: the page's label and address, the passages that
 * changed (new and removed), and up to a few existing Q&A entries the bot's
 * own search ranks closest to those passages. It must answer JSON:
 *   { "drafts": [ { "target_entry_id": "<one of the listed ids>" | null,
 *                   "question": "...", "answer": "...",
 *                   "source_passage": "<copied word for word from the page>",
 *                   "why": "..." } ] }
 * Every draft is then checked here, and dropped (with the reason logged)
 * unless:
 *   - target_entry_id is null (a new entry) or one of the candidate ids;
 *   - source_passage occurs in the new page text (whitespace aside) - the
 *     reviewer is shown that exact passage, so a paraphrase is refused;
 *   - question and answer are present and together at most 1,200 characters
 *     (the editor's limit, which the database enforces again);
 *   - nothing looks like an NRIC/FIN;
 *   - it carries a fact a client could act on and does not only point
 *     elsewhere (lib/watcher/quality.ts, 2026-10-15: the MOM levy index page
 *     drafted four "The MOM page provides information on..." entries).
 * A draft that survives is still only a PROPOSAL: it goes to the review
 * queue, and a person approves, edits or rejects it.
 */

import { lowValueReason } from './quality.ts';

export type Candidate = { id: string; question: string; answer: string };

export type ProposedDraft = {
  target_entry_id: string | null;
  question: string;
  answer: string;
  source_passage: string;
  why: string;
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
    '- To change an existing entry, set target_entry_id to its id and give the full new question and answer. Keep its wording where the page does not require a change.',
    '- For a fact no listed entry covers, set target_entry_id to null. If a listed entry already answers the same question, change THAT entry instead: never propose a second entry for a question the knowledge base already answers.',
    '- Every entry must state a fact a client can act on: an amount, a rule, a deadline, an eligibility condition or a step. Never draft an entry that only points elsewhere ("Where can I find information about...", "The MOM page provides information on...", "visit this page...").',
    '- If the page is mainly a list of links to other pages, return no drafts.',
    '- Never propose anything about agency fees or prices; leave money to the agency.',
    '- Answers are plain sentences for a client on WhatsApp: no headings, bullets or markdown; question plus answer at most 1,000 characters.',
    '- source_passage must be copied word for word from the NEW page passages, and must support the answer: consecutive lines exactly as they appear, character for character, keeping odd spacing (such as "1 st helper") and line order; never tidy, join or reorder table cells.',
    `- At most ${MAX_DRAFTS_PER_CHANGE} drafts. If nothing in the change matters to the assistant, return no drafts.`,
    'Answer with JSON only: {"drafts":[{"target_entry_id":string|null,"question":string,"answer":string,"source_passage":string,"why":string}]}',
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

export function parseDrafts(raw: string, ctx: { newText: string; candidateIds: string[] }): ParseResult {
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
  const page = squash(ctx.newText);
  const drafts: ProposedDraft[] = [];
  for (const item of list.slice(0, MAX_DRAFTS_PER_CHANGE * 2)) {
    const d = item as Record<string, unknown>;
    const question = typeof d.question === 'string' ? d.question.trim() : '';
    const answer = typeof d.answer === 'string' ? d.answer.trim() : '';
    const passage = typeof d.source_passage === 'string' ? d.source_passage.trim() : '';
    const target = d.target_entry_id == null || d.target_entry_id === '' ? null : String(d.target_entry_id);
    if (!question || !answer) { dropped.push('a draft without a question or an answer'); continue; }
    if ((question + '\n' + answer).length > MAX_ENTRY_CHARS) { dropped.push(`a draft over ${MAX_ENTRY_CHARS} characters`); continue; }
    if (target !== null && !ctx.candidateIds.includes(target)) { dropped.push('a draft naming an entry that was not offered'); continue; }
    if (!passage || !page.includes(squash(passage))) { dropped.push(`a draft whose source passage is not on the page ("${squash(passage).slice(0, 80)}")`); continue; }
    if (NRIC.test(`${question} ${answer} ${passage}`)) { dropped.push('a draft containing what looks like an NRIC/FIN'); continue; }
    const low = lowValueReason(question, answer);
    if (low) { dropped.push(low); continue; }
    if (drafts.some((x) => x.target_entry_id !== null && x.target_entry_id === target)) { dropped.push('a second draft for the same entry'); continue; }
    drafts.push({ target_entry_id: target, question, answer, source_passage: passage, why: typeof d.why === 'string' ? d.why.slice(0, 500) : '' });
    if (drafts.length >= MAX_DRAFTS_PER_CHANGE) break;
  }
  return { drafts, dropped };
}
