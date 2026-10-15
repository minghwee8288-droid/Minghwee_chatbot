'use client';

import { useState } from 'react';
import { useFormState, useFormStatus } from 'react-dom';
import type { ActionState } from '@/app/editor/actions';
import { editWatchDraft } from '@/app/watch/actions';

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className="btn btn-primary">
      {pending ? 'Saving…' : 'Save the proposal'}
    </button>
  );
}

/** Edit a proposal's wording before it is approved. The fee lock is checked again on save. */
export function EditProposalForm({ draftId, question, answer, heading }: { draftId: string; question: string; answer: string; heading: string }) {
  const [open, setOpen] = useState(false);
  const [raw, formAction] = useFormState(editWatchDraft, { error: '' });
  const editState: ActionState = raw ?? { error: '' };
  const [q, setQ] = useState(question);
  const [a, setA] = useState(answer);
  if (!open) {
    return (
      <button type="button" className="btn btn-secondary" onClick={() => setOpen(true)}>
        Edit
      </button>
    );
  }
  const count = [...`${q}\n${a}`].length;
  return (
    <form action={formAction} className="card card-pad space-y-3">
      <input type="hidden" name="draft_id" value={draftId} />
      <div>
        <label className="field-label mb-1.5 block" htmlFor="wq">Question</label>
        <input id="wq" name="question" className="input" value={q} onChange={(e) => setQ(e.target.value)} maxLength={1200} required />
      </div>
      <div>
        <label className="field-label mb-1.5 block" htmlFor="wa">Answer</label>
        <textarea id="wa" name="answer" className="textarea" rows={6} value={a} onChange={(e) => setA(e.target.value)} required />
        <p className="mt-1 text-[12px] text-muted">{count} / 1200 characters</p>
      </div>
      <div>
        <label className="field-label mb-1.5 block" htmlFor="wh">Section heading (optional)</label>
        <input id="wh" name="section_heading" className="input" defaultValue={heading} maxLength={500} />
      </div>
      <div>
        <label className="field-label mb-1.5 block" htmlFor="wr">Reason (required)</label>
        <textarea id="wr" name="reason" className="textarea" rows={2} maxLength={500} required placeholder="Why the proposal is being changed" />
      </div>
      {editState.error ? <div className="notice notice-red" role="alert">{editState.error}</div> : null}
      <Submit />
    </form>
  );
}
