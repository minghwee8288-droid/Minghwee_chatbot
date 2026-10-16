import { entryText, vectorLiteral } from '../editing.ts';
import { changedPassages, contentHash, pageChange } from './change.ts';
import { buildPrompt, parseDrafts, type Candidate, type ProposedDraft } from './drafting.ts';
import { extractMainText } from './extract.ts';
import { classifyEmptyPage, emptyPageMessage } from './emptyPage.ts';
import type { PageResult } from './fetcher.ts';
import { findSubPages, indexVerdict, looksLikeIndexText, type SubPage } from './indexPage.ts';
import { EDIT_INSTEAD_SIMILARITY, assessOverlap, buildMergePrompt, type LiveEntry, type Overlap } from './overlap.ts';

/**
 * The watcher's flows, with every outside call passed in. This module
 * imports no database, network or environment code: the server actions
 * (app/watch/actions.ts) and the scheduled route (app/api/watcher/run) hand
 * it the real ones, and scripts/selfcheck.mjs hands it fakes - so the whole
 * flow is checked offline, on saved HTML.
 *
 * CHECK A SOURCE (stage 1 "Fetch now", stage 2 the schedule):
 *   read the last good snapshot's text  ->  fetch politely  ->  failed? record
 *   the failure (kb_admin_source_record_fetch, ok = false) and stop  ->  extract
 *   the main text  ->  is it mainly an INDEX? record its sub-pages as
 *   "Suggested pages to watch" (kb_admin_source_set_suggestions, 016)  ->
 *   record the snapshot with its normalised hash; the database says whether it
 *   changed  ->  unchanged? stop, no draft  ->  an index? stop, no draft  ->
 *   DRAFT FROM THE SNAPSHOT (below).
 *
 * RE-DRAFT (approver, 016): kb_admin_source_redraft supersedes the page's
 *   open proposals and names its latest snapshot  ->  DRAFT FROM THE SNAPSHOT
 *   (an index is recognised by its stored suggestions, or from the text).
 *
 * DRAFT FROM THE SNAPSHOT:
 *   diff the paragraphs  ->  find the closest existing Q&A entries with the
 *   bot's own live search (kb_admin_match_live)  ->  ask the model  ->  check
 *   every draft (lib/watcher/drafting.ts: passage on the page, an actionable
 *   fact, not a pointer)  ->  OVERLAP CHECK (lib/watcher/overlap.ts): search
 *   each draft's text; a NEW draft that duplicates a live entry becomes a
 *   change to that entry (one more model call), and every draft carries its
 *   similar entries and any figure conflict  ->  propose each
 *   (kb_admin_watch_propose_checked with 016, else kb_admin_watch_propose;
 *   the database applies the fee lock and marks a draft 'blocked').
 *
 * APPROVE A DRAFT (stage 3):
 *   kb_admin_watch_approve (approver only, never a blocked draft; a figure or
 *   a conflict needs the tick; writes the text through kb_admin_save_draft /
 *   kb_admin_create_entry)  ->  embed the text with the canary-checked model
 *   ->  kb_admin_publish (the existing path, exactly what the Publish button
 *   does)  ->  kb_admin_watch_mark_published. If publishing fails the draft
 *   stays 'approved' with its Q&A version open, and the reviewer is sent to
 *   that version to publish it by hand.
 */

export type WatchCall = <T>(fn: string, params: unknown[]) => Promise<T>;
export type Actor = { userId: string; email: string };

export type DraftDeps = {
  call: WatchCall;
  embed: (texts: string[]) => Promise<{ vectors: number[][]; model: string }>;
  /** Who records the snapshot and the drafts: a person (Fetch now, Re-draft) or null (the schedule). */
  recorder: Actor | null;
  /** Who runs the read-only live search (kb_admin_match_live needs an editor). Null = no search. */
  searcher: Actor | null;
  llm: (system: string, user: string) => Promise<string>;
  model: string;
  /** Migration 016 is installed: the overlap results and suggested pages can be recorded. */
  has016?: boolean;
  log?: (line: string) => void;
};

export type CheckDeps = DraftDeps & {
  fetchPage: (url: string) => Promise<PageResult>;
  /** The JavaScript fallback (render-service/), used only when the plain page has no readable text. Absent = not set up. */
  renderPage?: (url: string) => Promise<PageResult>;
  previousText: (sourceId: string) => Promise<string | null>;
};

export type SourceRef = { id: string; url: string; label: string };

export type DraftOutcome = { id: string; status: string; blocked_reason: string | null; kind: 'new' | 'edit'; conflict: boolean; similar: number };

export type CheckResult = {
  outcome: 'failed' | 'unchanged' | 'changed' | 'first';
  error?: string;
  drafts: DraftOutcome[];
  dropped: string[];
  /** The page is mainly an index: nothing drafted, these are its suggested sub-pages. */
  index?: { reason: string; suggestions: SubPage[] };
};

const CANDIDATES_PER_PASSAGE = 3;
const MAX_CANDIDATES = 8;
const OVERLAP_MATCH_COUNT = 12;
const OVERLAP_THRESHOLD = 0.5;

type MatchRow = { id: string; chunk_type: string; question: string | null; answer: string | null; similarity: number };

const liveQa = (rows: MatchRow[]): LiveEntry[] =>
  rows.filter((x) => x.chunk_type === 'qa_pair' && x.question && x.answer)
    .map((x) => ({ id: x.id, question: x.question!, answer: x.answer!, similarity: x.similarity }));

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
  let extracted = extractMainText(page.html);
  let html = page.html;
  let pageUrl = page.finalUrl;
  let status = page.status;
  if (extracted.text.length < 40) {
    // Say WHY there is no text (2026-10-15): bot protection is reported and
    // never worked around; maintenance is reported; a JavaScript-built page is
    // rendered by the page renderer when one is set up.
    let kind = classifyEmptyPage(page.html, page.finalUrl);
    let finalUrl: string | null = page.finalUrl !== source.url ? page.finalUrl : null;
    let rendered = false;
    let renderError = '';
    if ((kind === 'script' || kind === 'empty') && deps.renderPage) {
      const r = await deps.renderPage(page.finalUrl);
      if (r.ok) {
        rendered = true;
        extracted = extractMainText(r.html);
        html = r.html;
        pageUrl = r.finalUrl;
        status = r.status;
        if (extracted.text.length < 40) {
          kind = classifyEmptyPage(r.html, r.finalUrl);
          finalUrl = r.finalUrl !== source.url ? r.finalUrl : finalUrl;
        }
      } else {
        renderError = r.error;
      }
    }
    if (extracted.text.length < 40) {
      const error = renderError.startsWith('Blocked by site')
        ? renderError
        : renderError
          ? `${emptyPageMessage(kind, finalUrl, { renderer: true, rendered: false })} (${renderError})`
          : emptyPageMessage(kind, finalUrl, { renderer: Boolean(deps.renderPage), rendered });
      await deps.call('kb_admin_source_record_fetch', [who?.userId ?? null, who?.email ?? null, source.id, false, status, null, null, null, error]);
      log(`no readable text source=${source.id.slice(0, 8)} kind=${kind} rendered=${rendered}`);
      return { outcome: 'failed', error, drafts: [], dropped: [] };
    }
    log(`rendered source=${source.id.slice(0, 8)} (plain fetch had no readable text)`);
  }

  // An index page drafts nothing; its sub-pages are suggested instead. Recorded
  // on every fetch (changed or not), so a page read before 016 gets them too.
  const subPages = findSubPages(html, pageUrl, extracted.paragraphs);
  const verdict = indexVerdict(extracted.paragraphs, subPages);
  if (deps.has016) {
    // A jsonb parameter goes as the JS value (lib/write.ts); an empty list as
    // null, which the database reads as [] (an empty JS array may not serialise as JSON).
    await deps.call('kb_admin_source_set_suggestions', [who?.userId ?? null, who?.email ?? null, source.id, verdict.index && subPages.length ? subPages : null]);
  }
  const index = verdict.index ? { reason: verdict.reason, suggestions: subPages } : undefined;

  const hash = await contentHash(extracted.text);
  const recorded = await deps.call<{ snapshot_id: string; changed: boolean; first: boolean }>('kb_admin_source_record_fetch', [
    who?.userId ?? null, who?.email ?? null, source.id, true, status, hash, extracted.text, extracted.title, '',
  ]);
  if (!recorded.changed) return { outcome: 'unchanged', drafts: [], dropped: [], index };
  const outcome = recorded.first ? 'first' : 'changed';
  if (index) {
    log(`index page source=${source.id.slice(0, 8)}: nothing drafted, ${subPages.length} sub-pages suggested`);
    return { outcome, drafts: [], dropped: [`the page is mainly an index (${index.reason}): nothing drafted; its sub-pages are suggested to watch`], index };
  }
  const drafted = await draftFromSnapshot(source, { snapshotId: recorded.snapshot_id, text: extracted.text, previousText: recorded.first ? null : prev }, deps);
  return { outcome, ...drafted };
}

export type RedraftResult = { superseded: number; drafts: DraftOutcome[]; dropped: string[]; index: boolean };

/**
 * Re-draft from the latest snapshot (approver only, 016). The database checks
 * the role, supersedes the page's open proposals and audit-logs it; then the
 * snapshot is drafted from exactly as after a fetch.
 */
export async function redraftSource(
  source: SourceRef & { suggested: SubPage[] },
  reason: string,
  deps: DraftDeps & { snapshotText: (snapshotId: string) => Promise<string | null> },
): Promise<RedraftResult> {
  const who = deps.recorder;
  if (!who) throw new Error('a re-draft needs a person');
  const r = await deps.call<{ snapshot_id: string; previous_snapshot_id: string | null; superseded: number }>('kb_admin_source_redraft', [who.userId, who.email, source.id, reason]);
  const text = await deps.snapshotText(r.snapshot_id);
  if (!text) return { superseded: r.superseded, drafts: [], dropped: ['the latest snapshot could not be read'], index: false };
  if (source.suggested.length || looksLikeIndexText(text.split('\n').map((l) => l.trim()).filter(Boolean))) {
    return { superseded: r.superseded, drafts: [], dropped: ['the page is mainly an index: nothing drafted; its sub-pages are suggested to watch'], index: true };
  }
  const previousText = r.previous_snapshot_id ? await deps.snapshotText(r.previous_snapshot_id) : null;
  const drafted = await draftFromSnapshot(source, { snapshotId: r.snapshot_id, text, previousText }, deps);
  return { superseded: r.superseded, ...drafted, index: false };
}

/** Diff, find candidates, ask the model, check, overlap-check, propose. */
export async function draftFromSnapshot(
  source: SourceRef,
  snap: { snapshotId: string; text: string; previousText: string | null },
  deps: DraftDeps,
): Promise<{ drafts: DraftOutcome[]; dropped: string[] }> {
  const log = deps.log ?? (() => undefined);
  const who = deps.recorder;
  const change = pageChange(snap.previousText, snap.text);
  const passages = changedPassages(change);
  if (!passages.length) return { drafts: [], dropped: ['nothing new on the page to draft from'] };

  const search = async (vector: number[], count: number, threshold: number): Promise<LiveEntry[]> =>
    liveQa(await deps.call<MatchRow[]>('kb_admin_match_live', [deps.searcher!.userId, deps.searcher!.email, vectorLiteral(vector), null, null, null, count, threshold]));

  let candidates: Candidate[] = [];
  if (deps.searcher) {
    const { vectors } = await deps.embed(passages);
    const seen = new Map<string, Candidate & { sim: number }>();
    const found = await Promise.all(vectors.map((v) => search(v, 10, 0.35)));
    for (const rows of found) {
      for (const r of rows.slice(0, CANDIDATES_PER_PASSAGE)) {
        const had = seen.get(r.id);
        if (!had || had.sim < r.similarity) seen.set(r.id, { id: r.id, question: r.question, answer: r.answer, sim: r.similarity });
      }
    }
    candidates = [...seen.values()].sort((a, b) => b.sim - a.sim).slice(0, MAX_CANDIDATES).map(({ id, question, answer }) => ({ id, question, answer }));
  }

  const prompt = buildPrompt({ label: source.label, url: source.url, added: passages, removed: change.removed.slice(0, 40), candidates });
  const raw = await deps.llm(prompt.system, prompt.user);
  const parsed = parseDrafts(raw, { newText: snap.text, candidateIds: candidates.map((c) => c.id) });
  const dropped = [...parsed.dropped];

  // --- the overlap check ------------------------------------------------------
  type Planned = { draft: ProposedDraft; overlap: Overlap };
  let planned: Planned[] = parsed.drafts.map((draft) => ({ draft, overlap: { similar: [], conflict: false, note: null } }));
  if (deps.searcher && planned.length) {
    const { vectors } = await deps.embed(planned.map((p) => entryText(p.draft.question, p.draft.answer)));
    const matches = await Promise.all(vectors.map((v) => search(v, OVERLAP_MATCH_COUNT, OVERLAP_THRESHOLD)));
    const taken = new Set(planned.map((p) => p.draft.target_entry_id).filter(Boolean) as string[]);
    // New drafts that duplicate a live entry -> a change to that entry instead.
    const retarget: { i: number; entry: LiveEntry }[] = [];
    planned.forEach((p, i) => {
      if (p.draft.target_entry_id !== null) return;
      const top = matches[i][0];
      if (top && top.similarity >= EDIT_INSTEAD_SIMILARITY && !taken.has(top.id)) {
        taken.add(top.id);
        retarget.push({ i, entry: top });
      }
    });
    const replaced = new Map<number, ProposedDraft | null>();
    if (retarget.length) {
      const merge = buildMergePrompt({ label: source.label, url: source.url, pairs: retarget.map((r) => ({ entry: r.entry, draft: planned[r.i].draft })) });
      const merged = parseDrafts(await deps.llm(merge.system, merge.user), { newText: snap.text, candidateIds: retarget.map((r) => r.entry.id) });
      dropped.push(...merged.dropped.map((d) => `(change instead of a new entry) ${d}`));
      for (const r of retarget) {
        const m = merged.drafts.find((d) => d.target_entry_id === r.entry.id);
        replaced.set(r.i, m ?? null);
        if (!m) dropped.push(`a new entry the knowledge base already answers (live entry "${r.entry.question.slice(0, 80)}", similarity ${r.entry.similarity.toFixed(2)}); the entry already says it`);
      }
    }
    planned = planned.flatMap((p, i) => {
      const draft = replaced.has(i) ? replaced.get(i)! : p.draft;
      if (!draft) return [];
      return [{ draft, overlap: assessOverlap({ question: draft.question, answer: draft.answer, targetId: draft.target_entry_id }, matches[i]) }];
    });
  }
  for (const d of dropped) log(`draft dropped source=${source.id.slice(0, 8)}: ${d}`);

  const oldSource = change.removed.join('\n').slice(0, 20000) || null;
  const drafts: DraftOutcome[] = [];
  for (const { draft: d, overlap } of planned) {
    const base = [who?.userId ?? null, who?.email ?? null, snap.snapshotId, d.target_entry_id, d.question, d.answer,
      null, null, null, null, d.source_passage, oldSource, deps.model];
    const r = deps.has016
      ? await deps.call<{ draft_id: string; status: string; blocked_reason: string | null }>('kb_admin_watch_propose_checked', [
        ...base, overlap.similar.length ? overlap.similar : null, overlap.conflict, overlap.note])
      : await deps.call<{ draft_id: string; status: string; blocked_reason: string | null }>('kb_admin_watch_propose', base);
    drafts.push({ id: r.draft_id, status: r.status, blocked_reason: r.blocked_reason, kind: d.target_entry_id ? 'edit' : 'new', conflict: overlap.conflict, similar: overlap.similar.length });
  }
  return { drafts, dropped };
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
