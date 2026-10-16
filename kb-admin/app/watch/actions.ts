'use server';

import { redirect } from 'next/navigation';
import { unjustifiedRemovals } from '@/lib/watcher/factDiff';
import type { ActionState } from '@/app/editor/actions';
import { actorForWrite } from '@/lib/auth';
import { errorMessage, hasNric } from '@/lib/editing';
import { EmbedError, embedEntry, embedTexts } from '@/lib/embed';
import { env } from '@/lib/env';
import { addressProblem } from '@/lib/watcher/fetcher';
import { watchFetcher } from '@/lib/watchFetcher';
import { DraftingError, draftWithModel } from '@/lib/watcher/llm';
import { touchesMoney } from '@/lib/watcher/feeLock';
import { approveAndPublish, checkSource, redraftSource, type WatchCall } from '@/lib/watcher/pipeline';
import { lastGoodText, snapshotText, sourceById, watchDraft, watcherSchema16 } from '@/lib/watchQueries';
import { callWrite } from '@/lib/write';

/**
 * The KB watcher's actions (migration 015). Each one:
 *   1. runs actorForWrite() FIRST - the session verified with Supabase Auth and
 *      the role read from cb_kb_admin_users now;
 *   2. refuses unless the watcher is switched on (KB_SOURCES_ENABLED);
 *   3. calls only kb_admin_* functions (lib/write.ts), passing the verified
 *      person; the database checks the role again and audit-logs the action.
 * Nothing here changes what the chatbot answers until an APPROVER approves a
 * draft, and even then only through the existing publish path.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CATEGORIES = ['government_rule', 'process', 'embassy', 'other'];

function text(form: FormData, name: string, max = 4000): string {
  return String(form.get(name) ?? '').slice(0, max);
}

function id(form: FormData, name: string): string | null {
  const value = String(form.get(name) ?? '');
  return UUID.test(value) ? value : null;
}

function log(action: string, userId: string, target: string, outcome: string) {
  console.info(`[watcher] ${action} user=${userId.slice(0, 8)} target=${target.slice(0, 8)} ${outcome}`);
}

function failure(action: string, userId: string, target: string, error: unknown): ActionState {
  if (error instanceof EmbedError || error instanceof DraftingError) {
    log(action, userId, target, `failed: ${error.message}`);
    return { error: `Nothing was changed: ${error.message}.` };
  }
  const code = (error as { code?: string })?.code;
  log(action, userId, target, `failed code=${code ?? 'none'}`);
  if (!code?.startsWith('KB')) console.error('[watcher] unexpected error', code ?? '', String((error as Error)?.message ?? '').slice(0, 200));
  return { error: errorMessage(code, (error as { message?: string })?.message) };
}

const call: WatchCall = <T,>(fn: string, params: unknown[]) => callWrite<T>(fn as Parameters<typeof callWrite>[0], params as Parameters<typeof callWrite>[1]);

function watcherOff(): ActionState | null {
  return env().watcher ? null : { error: 'The page watcher is switched off.' };
}

// ---------------------------------------------------------------------------

export async function addSource(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const off = watcherOff();
  if (off) return off;
  const raw = text(form, 'url', 2000).trim();
  const problem = addressProblem(raw);
  if (problem) return { error: `That address cannot be watched: ${problem}.` };
  const url = new URL(raw);
  url.hash = '';
  const label = text(form, 'label', 200).trim();
  const category = text(form, 'category', 40);
  if (!label) return { error: 'Give the page a label, for example "MOM - rest days".' };
  if (!CATEGORIES.includes(category)) return { error: 'Choose a category.' };
  let sourceId: string;
  try {
    sourceId = await callWrite<string>('kb_admin_source_add', [actor.userId, actor.email, url.toString(), label, category]);
  } catch (error) {
    return failure('source_add', actor.userId, 'new', error);
  }
  log('source_add', actor.userId, sourceId, 'ok');
  redirect('/sources?added=1');
}

export async function removeSource(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const off = watcherOff();
  if (off) return off;
  const sourceId = id(form, 'source_id');
  if (!sourceId) return { error: 'No page was named.' };
  const reason = text(form, 'reason', 500).trim();
  if (!reason) return { error: 'Please say why this page should no longer be watched.' };
  try {
    await callWrite('kb_admin_source_remove', [actor.userId, actor.email, sourceId, reason]);
  } catch (error) {
    return failure('source_remove', actor.userId, sourceId, error);
  }
  log('source_remove', actor.userId, sourceId, 'ok');
  redirect('/sources?removed=1');
}

export async function fetchNow(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const off = watcherOff();
  if (off) return off;
  const sourceId = id(form, 'source_id');
  if (!sourceId) return { error: 'No page was named.' };
  const source = await sourceById(sourceId);
  if (!source || !source.active) return { error: 'That page is no longer watched.' };
  const { fetcher, renders } = watchFetcher();
  const has016 = await watcherSchema16();
  let result;
  try {
    result = await checkSource(source, {
      call,
      fetchPage: (u) => fetcher.fetchPage(u),
      renderPage: renders ? (u) => fetcher.renderPage(u) : undefined,
      previousText: lastGoodText,
      embed: embedTexts,
      recorder: { userId: actor.userId, email: actor.email },
      searcher: { userId: actor.userId, email: actor.email },
      llm: draftWithModel,
      model: env().watcher!.llm.model,
      has016,
      log: (line) => console.info(`[watcher] ${line}`),
    });
  } catch (error) {
    return failure('fetch_now', actor.userId, sourceId, error);
  }
  log('fetch_now', actor.userId, sourceId, `${result.outcome} drafts=${result.drafts.length} index=${result.index ? 'yes' : 'no'}`);
  redirect(`/sources?checked=${result.outcome}&drafts=${result.drafts.length}&index=${result.index ? result.index.suggestions.length : -1}&source=${sourceId}`);
}

/**
 * Re-draft from the latest snapshot (APPROVER only, migration 016): the page's
 * open proposals are superseded and drafted again with the current rules,
 * without waiting for the page to change. The database checks the role and
 * audit-logs it ('source_redrafted', with the reason).
 */
export async function redraftSourceAction(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  if (!actor.canApprove) return { error: 'Only an approver can re-draft a page.' };
  const off = watcherOff();
  if (off) return off;
  if (!(await watcherSchema16())) return { error: 'Re-drafting needs the watcher migration 016, which is not applied yet.' };
  const sourceId = id(form, 'source_id');
  if (!sourceId) return { error: 'No page was named.' };
  const reason = text(form, 'reason', 500).trim();
  if (!reason) return { error: 'Please say why the proposals should be drafted again.' };
  const source = await sourceById(sourceId);
  if (!source || !source.active) return { error: 'That page is no longer watched.' };
  let result;
  try {
    result = await redraftSource({ ...source, suggested: source.suggested_pages ?? [] }, reason, {
      call,
      embed: embedTexts,
      recorder: { userId: actor.userId, email: actor.email },
      searcher: { userId: actor.userId, email: actor.email },
      llm: draftWithModel,
      model: env().watcher!.llm.model,
      has016: true,
      snapshotText,
      log: (line) => console.info(`[watcher] ${line}`),
    });
  } catch (error) {
    return failure('redraft', actor.userId, sourceId, error);
  }
  log('redraft', actor.userId, sourceId, `superseded=${result.superseded} drafts=${result.drafts.length} index=${result.index ? 'yes' : 'no'}`);
  redirect(`/sources?redrafted=1&drafts=${result.drafts.length}&superseded=${result.superseded}&index=${result.index ? 1 : 0}&source=${sourceId}`);
}

/**
 * "Watch this page": one of an index page's suggested sub-pages, added with
 * the parent's category through the existing kb_admin_source_add. Only an
 * address the parent actually suggests is accepted.
 */
export async function watchSuggestedPage(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const off = watcherOff();
  if (off) return off;
  const parentId = id(form, 'source_id');
  if (!parentId) return { error: 'No page was named.' };
  const url = text(form, 'url', 2000).trim();
  const parent = await sourceById(parentId);
  if (!parent || !parent.active) return { error: 'That page is no longer watched.' };
  const page = (parent.suggested_pages ?? []).find((p) => p.url === url);
  if (!page) return { error: 'That page is not one of the suggested pages.' };
  const problem = addressProblem(url);
  if (problem) return { error: `That address cannot be watched: ${problem}.` };
  const label = `${parent.label} - ${page.label}`.slice(0, 200);
  let sourceId: string;
  try {
    sourceId = await callWrite<string>('kb_admin_source_add', [actor.userId, actor.email, url, label, parent.category]);
  } catch (error) {
    return failure('source_add_suggested', actor.userId, parentId, error);
  }
  log('source_add_suggested', actor.userId, sourceId, `ok parent=${parentId.slice(0, 8)}`);
  redirect('/sources?added=1');
}

export async function editWatchDraft(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const off = watcherOff();
  if (off) return off;
  const draftId = id(form, 'draft_id');
  if (!draftId) return { error: 'No draft was named.' };
  const question = text(form, 'question').trim();
  const answer = text(form, 'answer').trim();
  const heading = text(form, 'section_heading', 500).trim();
  const reason = text(form, 'reason', 500).trim();
  if (!reason) return { error: 'Please say why you are changing the proposal.' };
  if (!question || !answer) return { error: 'The question and the answer are both needed.' };
  if (hasNric(`${question} ${answer} ${heading}`)) return { error: errorMessage('KB006', '') };
  try {
    await callWrite('kb_admin_watch_edit', [actor.userId, actor.email, draftId, question, answer, heading, reason]);
  } catch (error) {
    return failure('watch_edit', actor.userId, draftId, error);
  }
  log('watch_edit', actor.userId, draftId, 'ok');
  redirect(`/watch/${draftId}?edited=1`);
}

export async function rejectWatchDraft(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const off = watcherOff();
  if (off) return off;
  const draftId = id(form, 'draft_id');
  if (!draftId) return { error: 'No draft was named.' };
  const reason = text(form, 'reason', 500).trim();
  if (!reason) return { error: 'Please say why you are rejecting this proposal.' };
  try {
    await callWrite('kb_admin_watch_reject', [actor.userId, actor.email, draftId, reason]);
  } catch (error) {
    return failure('watch_reject', actor.userId, draftId, error);
  }
  log('watch_reject', actor.userId, draftId, 'ok');
  redirect(`/watch/${draftId}?rejected=1`);
}

export async function approveWatchDraft(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  if (!actor.canApprove) return { error: 'Only an approver can approve a page change.' };
  const off = watcherOff();
  if (off) return off;
  const draftId = id(form, 'draft_id');
  if (!draftId) return { error: 'No draft was named.' };
  const reason = text(form, 'reason', 500).trim();
  if (!reason) return { error: 'Please say why you are approving this change.' };
  const draft = await watchDraft(draftId);
  if (!draft || (draft.status !== 'pending' && draft.status !== 'blocked')) return { error: 'This proposal is no longer open.' };
  // A money figure (only approvable from a government rule page) needs the
  // approver's tick; the database checks it again (KB008).
  const confirmFigure = String(form.get('confirm_figure') ?? '') === 'yes';
  const figure = touchesMoney({ targetQuestion: draft.current_question, targetAnswer: draft.current_answer, proposedQuestion: draft.proposed_question, proposedAnswer: draft.proposed_answer });
  if (figure && !confirmFigure) return { error: errorMessage('KB008', '') };
  // A change must not lose facts: a live sentence the proposal drops, which the source does not contradict, blocks Approve.
  if (draft.kind === 'edit' && draft.current_answer
    && unjustifiedRemovals(draft.current_answer, draft.proposed_answer, draft.source_passage, draft.proposed_question).length) {
    return { error: 'This update drops existing information. Use Edit to put the removed points back, or reject it.' };
  }
  // 016: it disagrees with a similar live entry (or duplicates one): the same tick (KB009 in the database).
  if (draft.conflict_check && !confirmFigure) return { error: errorMessage('KB009', '') };
  let result;
  try {
    result = await approveAndPublish(
      { userId: actor.userId, email: actor.email },
      { id: draftId, question: draft.proposed_question, answer: draft.proposed_answer, confirmFigure },
      reason,
      { call, embed: embedEntry },
    );
  } catch (error) {
    return failure('watch_approve', actor.userId, draftId, error);
  }
  if (!result.published) {
    const state = failure('watch_publish', actor.userId, draftId, result.error);
    return { error: '', notice: `Approved, but not yet live: ${state.error} Open the Q&A draft to publish it.` };
  }
  log('watch_approve', actor.userId, draftId, `ok entry=${result.entryId.slice(0, 8)} published`);
  redirect(`/watch/${draftId}?published=1`);
}
