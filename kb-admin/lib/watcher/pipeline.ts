import { entryText, vectorLiteral } from '../editing.ts';
import { changedPassages, contentHash, pageChange } from './change.ts';
import { buildPrompt, parseDrafts, type Candidate } from './drafting.ts';
import { extractMainText } from './extract.ts';
import type { PageResult } from './fetcher.ts';

/**
 * The watcher's two flows, with every outside call passed in. This module
 * imports no database, network or environment code: the server action
 * (app/watch/actions.ts) and the scheduled route (app/api/watcher/run) hand
 * it the real ones, and scripts/selfcheck.mjs hands it fakes - so the whole
 * flow is checked offline, on saved HTML.
 *
 * CHECK A SOURCE (stage 1 "Fetch now", stage 2 the schedule):
 *   read the last good snapshot's text  ->  fetch politely  ->  failed? record
 *   the failure (kb_admin_source_record_fetch, ok = false) and stop  ->  extract
 *   the main text  ->  record the snapshot with its normalised hash; the
 *   database says whether it changed  ->  unchanged? stop, no draft  ->  diff
 *   the paragraphs  ->  find the closest existing Q&A entries with the bot's
 *   own live search (kb_admin_match_live)  ->  ask the model  ->  check every
 *   draft (lib/watcher/drafting.ts)  ->  propose each (kb_admin_watch_propose;
 *   the database applies the fee lock and marks a draft 'blocked').
 *
 * APPROVE A DRAFT (stage 3):
 *   kb_admin_watch_approve (approver only, never a blocked draft; writes the
 *   text through kb_admin_save_draft / kb_admin_create_entry)  ->  embed the
 *   text with the canary-checked model  ->  kb_admin_publish (the existing
 *   path, exactly what the Publish button does)  ->  kb_admin_watch_mark_published.
 *   If publishing fails the draft stays 'approved' with its Q&A version open,
 *   and the reviewer is sent to that version to publish it by hand.
 */

export type WatchCall = <T>(fn: string, params: unknown[]) => Promise<T>;
export type Actor = { userId: string; email: string };

export type CheckDeps = {
  call: WatchCall;
  fetchPage: (url: string) => Promise<PageResult>;
  previousText: (sourceId: string) => Promise<string | null>;
  embed: (texts: string[]) => Promise<{ vectors: number[][]; model: string }>;
  /** Who records the snapshot and the drafts: a person (Fetch now) or null (the schedule). */
  recorder: Actor | null;
  /** Who runs the read-only live search (kb_admin_match_live needs an editor). Null = no search. */
  searcher: Actor | null;
  llm: (system: string, user: string) => Promise<string>;
  model: string;
  log?: (line: string) => void;
};

export type SourceRef = { id: string; url: string; label: string };

export type CheckResult = {
  outcome: 'failed' | 'unchanged' | 'changed' | 'first';
  error?: string;
  drafts: { id: string; status: string; blocked_reason: string | null }[];
  dropped: string[];
};

const CANDIDATES_PER_PASSAGE = 3;
const MAX_CANDIDATES = 8;

type MatchRow = { id: string; chunk_type: string; question: string | null; answer: string | null; similarity: number };

export async function checkSource(source: SourceRef, deps: CheckDeps): Promise<CheckResult> {
  const log = deps.log ?? (() => undefined);
  const who = deps.recorder;
  const prev = await deps.previousText(source.id);
  const page = await deps.fetchPage(source.url);

  if (!page.ok) {
    await deps.call('kb_admin_source_record_fetch', [who?.userId ?? null, who?.email ?? null, source.id, false, page.status, null, null, null, page.error]);
    log(`fetch failed source=${source.id.slice(0, 8)} ${page.error}`);
    return { outcome: 'failed', error: page.error, drafts: [], dropped: [] };
  }
  const extracted = extractMainText(page.html);
  if (extracted.text.length < 40) {
    const error = 'the page has no readable main text';
    await deps.call('kb_admin_source_record_fetch', [who?.userId ?? null, who?.email ?? null, source.id, false, page.status, null, null, null, error]);
    return { outcome: 'failed', error, drafts: [], dropped: [] };
  }
  const hash = await contentHash(extracted.text);
  const recorded = await deps.call<{ snapshot_id: string; changed: boolean; first: boolean }>('kb_admin_source_record_fetch', [
    who?.userId ?? null, who?.email ?? null, source.id, true, page.status, hash, extracted.text, extracted.title, '',
  ]);
  if (!recorded.changed) return { outcome: 'unchanged', drafts: [], dropped: [] };

  const change = pageChange(recorded.first ? null : prev, extracted.text);
  const passages = changedPassages(change);
  if (!passages.length) return { outcome: recorded.first ? 'first' : 'changed', drafts: [], dropped: ['nothing new on the page to draft from'] };

  let candidates: Candidate[] = [];
  if (deps.searcher) {
    const { vectors } = await deps.embed(passages);
    const seen = new Map<string, Candidate & { sim: number }>();
    for (const v of vectors) {
      const rows = await deps.call<MatchRow[]>('kb_admin_match_live', [deps.searcher.userId, deps.searcher.email, vectorLiteral(v), null, null, null, 10, 0.35]);
      for (const r of rows.filter((x) => x.chunk_type === 'qa_pair' && x.question && x.answer).slice(0, CANDIDATES_PER_PASSAGE)) {
        const had = seen.get(r.id);
        if (!had || had.sim < r.similarity) seen.set(r.id, { id: r.id, question: r.question!, answer: r.answer!, sim: r.similarity });
      }
    }
    candidates = [...seen.values()].sort((a, b) => b.sim - a.sim).slice(0, MAX_CANDIDATES).map(({ id, question, answer }) => ({ id, question, answer }));
  }

  const prompt = buildPrompt({ label: source.label, url: source.url, added: passages, removed: change.removed.slice(0, 40), candidates });
  const raw = await deps.llm(prompt.system, prompt.user);
  const parsed = parseDrafts(raw, { newText: extracted.text, candidateIds: candidates.map((c) => c.id) });
  for (const d of parsed.dropped) log(`draft dropped source=${source.id.slice(0, 8)}: ${d}`);

  const oldSource = change.removed.join('\n').slice(0, 20000) || null;
  const drafts: CheckResult['drafts'] = [];
  for (const d of parsed.drafts) {
    const r = await deps.call<{ draft_id: string; status: string; blocked_reason: string | null }>('kb_admin_watch_propose', [
      who?.userId ?? null, who?.email ?? null, recorded.snapshot_id, d.target_entry_id, d.question, d.answer,
      null, null, null, null, d.source_passage, oldSource, deps.model,
    ]);
    drafts.push({ id: r.draft_id, status: r.status, blocked_reason: r.blocked_reason });
  }
  return { outcome: recorded.first ? 'first' : 'changed', drafts, dropped: parsed.dropped };
}

export type ApproveDeps = {
  call: WatchCall;
  embed: (text: string) => Promise<{ vector: number[]; model: string }>;
};

export type ApproveResult =
  | { published: true; entryId: string; versionId: string }
  | { published: false; entryId: string; versionId: string; error: unknown };

/** Approve, publish through the existing path, then record it. Throws only if the approval itself is refused. */
export async function approveAndPublish(
  actor: Actor,
  draft: { id: string; question: string; answer: string; confirmFigure: boolean },
  reason: string,
  deps: ApproveDeps,
): Promise<ApproveResult> {
  const approved = await deps.call<{ entry_id: string; version_id: string }>('kb_admin_watch_approve', [actor.userId, actor.email, draft.id, reason, draft.confirmFigure]);
  try {
    const { vector, model } = await deps.embed(entryText(draft.question, draft.answer));
    await deps.call('kb_admin_publish', [actor.userId, actor.email, approved.version_id, vectorLiteral(vector), model]);
    await deps.call('kb_admin_watch_mark_published', [actor.userId, actor.email, draft.id]);
    return { published: true, entryId: approved.entry_id, versionId: approved.version_id };
  } catch (error) {
    return { published: false, entryId: approved.entry_id, versionId: approved.version_id, error };
  }
}
