'use server';

import { redirect } from 'next/navigation';
import { actorForWrite } from '@/lib/auth';
import { errorMessage, entryText, hasNric, vectorLiteral } from '@/lib/editing';
import { EmbedError, embedEntry } from '@/lib/embed';
import { row, version } from '@/lib/queries';
import { callWrite } from '@/lib/write';

/**
 * Every change kb-admin makes starts here. Each action:
 *   1. runs actorForWrite() FIRST - the Supabase session verified again and
 *      the role read from cb_kb_admin_users now (never trusted from the page);
 *   2. calls exactly one of the six kb_admin_* functions (lib/write.ts),
 *      passing the verified user id and email;
 *   3. turns the database's KB001-KB006 into a plain sentence.
 * Next.js refuses a server action whose Origin is not this site.
 */

export type ActionState = { error: string; notice?: string };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MAX_TEXT = 4000;
const MAX_REASON = 500;

function text(form: FormData, name: string, max = MAX_TEXT): string {
  return String(form.get(name) ?? '').slice(0, max);
}

function id(form: FormData, name: string): string | null {
  const value = String(form.get(name) ?? '');
  return UUID.test(value) ? value : null;
}

function log(action: string, userId: string, target: string, outcome: string) {
  // No text, no email: the id prefix tells two people apart in a log.
  console.info(`[editor] ${action} user=${userId.slice(0, 8)} target=${target.slice(0, 8)} ${outcome}`);
}

function failure(action: string, userId: string, target: string, error: unknown): ActionState {
  if (error instanceof EmbedError) {
    log(action, userId, target, `embed-failed: ${error.message}`);
    return { error: `Nothing was published: ${error.message}.` };
  }
  const code = (error as { code?: string })?.code;
  const message = (error as { message?: string })?.message;
  log(action, userId, target, `failed code=${code ?? 'none'}`);
  if (!code?.startsWith('KB')) console.error('[editor] unexpected error', code ?? '', String(message ?? '').slice(0, 200));
  return { error: errorMessage(code, message) };
}

// ---------------------------------------------------------------------------

export async function saveDraft(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const entryId = id(form, 'entry_id');
  if (!entryId) return { error: 'No entry was named.' };

  const question = text(form, 'question').trim();
  const answer = text(form, 'answer').trim();
  const heading = text(form, 'section_heading', 500).trim();
  const reason = text(form, 'reason', MAX_REASON).trim();
  if (!reason) return { error: 'Please say why you are making this change.' };
  if (!question || !answer) return { error: 'The question and the answer are both needed.' };
  if (hasNric(`${question} ${answer} ${heading}`)) return { error: errorMessage('KB006', '') };

  // A new entry (migration 008) is created switched off with no text, and its
  // first draft is switched ON. A later draft of it must stay switched on, or
  // publishing it would put the text in place and leave the entry off - so
  // keep "on" until the entry has been published once. Every other entry keeps
  // its current state (null), as before.
  const live = await row(entryId);
  const neverPublished = live !== null && live.chunk_type === 'qa_pair' && live.question === null && live.answer === null;

  let versionId: string;
  try {
    versionId = await callWrite<string>('kb_admin_save_draft', [
      actor.userId,
      actor.email,
      entryId,
      question,
      answer,
      heading,
      text(form, 'service', 60),
      text(form, 'audience', 20),
      text(form, 'nationality', 5),
      neverPublished ? true : null, // on/off is not edited here (see above)
      reason,
    ]);
  } catch (error) {
    return failure('save_draft', actor.userId, entryId, error);
  }
  log('save_draft', actor.userId, entryId, `ok draft=${versionId.slice(0, 8)}`);
  redirect(`/drafts/${versionId}?saved=1`);
}

export async function createEntry(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;

  const question = text(form, 'question').trim();
  const answer = text(form, 'answer').trim();
  const heading = text(form, 'section_heading', 500).trim();
  const reason = text(form, 'reason', MAX_REASON).trim();
  if (!reason) return { error: 'Please say why you are adding this entry.' };
  if (!question || !answer) return { error: 'The question and the answer are both needed.' };
  if (hasNric(`${question} ${answer} ${heading}`)) return { error: errorMessage('KB006', '') };

  let created: { entry_id: string; version_id: string };
  try {
    // The database creates the entry switched off and unsearchable, and its
    // text as a draft. Nothing the chatbot reads changes until it is published.
    created = await callWrite<{ entry_id: string; version_id: string }>('kb_admin_create_entry', [
      actor.userId,
      actor.email,
      question,
      answer,
      heading,
      text(form, 'service', 60),
      text(form, 'audience', 20),
      text(form, 'nationality', 5),
      reason,
    ]);
  } catch (error) {
    return failure('create_entry', actor.userId, 'new', error);
  }
  log('create_entry', actor.userId, created.entry_id, `ok draft=${created.version_id.slice(0, 8)}`);
  redirect(`/rows/${created.entry_id}?created=1`);
}

export async function publishDraft(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const versionId = id(form, 'version_id');
  if (!versionId) return { error: 'No draft was named.' };

  const draft = await version(versionId);
  if (!draft || draft.status !== 'draft') return { error: 'This draft is no longer open. Open the entry again.' };
  const live = await row(draft.entry_id);
  if (!live) return { error: 'The entry no longer exists.' };

  const textChanged = draft.question !== live.question || draft.answer !== live.answer;
  let reembedded = false;
  try {
    try {
      // First WITHOUT a vector. The database then either publishes (the text
      // is unchanged, or a restored version still carries a vector made from
      // exactly this text by the canary's model), or refuses with KB004
      // (needs an approver - checked before any vector) or KB005 (a new
      // vector is needed). So no embedding is spent on a change that cannot
      // go live yet, or that already has its vector.
      await callWrite('kb_admin_publish', [actor.userId, actor.email, versionId, null, null]);
    } catch (error) {
      if (!textChanged || (error as { code?: string })?.code !== 'KB005') throw error;
      const { vector, model } = await embedEntry(entryText(draft.question ?? '', draft.answer ?? ''));
      await callWrite('kb_admin_publish', [actor.userId, actor.email, versionId, vectorLiteral(vector), model]);
      reembedded = true;
    }
  } catch (error) {
    const state = failure('publish', actor.userId, versionId, error);
    // A change that needs an approver stays as a pending draft: not an error.
    if ((error as { code?: string })?.code === 'KB004') return { error: '', notice: state.error };
    return state;
  }
  log('publish', actor.userId, versionId, `ok reembedded=${reembedded}`);
  redirect(`/rows/${draft.entry_id}?published=1`);
}

export async function discardDraft(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const versionId = id(form, 'version_id');
  if (!versionId) return { error: 'No draft was named.' };
  const reason = text(form, 'reason', MAX_REASON).trim();
  if (!reason) return { error: 'Please say why you are discarding this draft.' };
  const draft = await version(versionId);
  if (!draft) return { error: 'No such draft.' };
  try {
    await callWrite('kb_admin_discard_draft', [actor.userId, actor.email, versionId, reason]);
  } catch (error) {
    return failure('discard', actor.userId, versionId, error);
  }
  log('discard', actor.userId, versionId, 'ok');
  redirect(`/rows/${draft.entry_id}?discarded=1`);
}

export async function restoreVersion(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const entryId = id(form, 'entry_id');
  const fromId = id(form, 'version_id');
  if (!entryId || !fromId) return { error: 'No version was named.' };
  const reason = text(form, 'reason', MAX_REASON).trim();
  if (!reason) return { error: 'Please say why you are restoring this version.' };
  let draftId: string;
  try {
    draftId = await callWrite<string>('kb_admin_restore', [actor.userId, actor.email, entryId, fromId, reason]);
  } catch (error) {
    return failure('restore', actor.userId, entryId, error);
  }
  log('restore', actor.userId, entryId, `ok draft=${draftId.slice(0, 8)}`);
  redirect(`/drafts/${draftId}?restored=1`);
}

export async function toggleEntry(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  if (!actor.canApprove) return { error: 'Only an approver can switch an entry on or off.' };
  const entryId = id(form, 'entry_id');
  if (!entryId) return { error: 'No entry was named.' };
  const target = String(form.get('active') ?? '');
  if (target !== 'on' && target !== 'off') return { error: 'Choose on or off.' };
  const reason = text(form, 'reason', MAX_REASON).trim();
  if (!reason) return { error: 'Please say why you are switching this entry.' };
  try {
    await callWrite('kb_admin_toggle', [actor.userId, actor.email, entryId, target === 'on', reason]);
  } catch (error) {
    return failure('toggle', actor.userId, entryId, error);
  }
  log('toggle', actor.userId, entryId, `ok ${target}`);
  redirect(`/rows/${entryId}?switched=${target}`);
}

