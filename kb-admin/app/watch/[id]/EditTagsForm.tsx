'use client';

import { useState } from 'react';
import { useFormState, useFormStatus } from 'react-dom';
import type { ActionState } from '@/app/editor/actions';
import { setWatchDraftTags } from '@/app/watch/actions';
import { label } from '@/components/labels';
import { TAG_AUDIENCES, TAG_NATIONALITIES, type Tags } from '@/lib/watcher/tags';

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className="btn btn-primary">
      {pending ? 'Saving…' : 'Save the tags'}
    </button>
  );
}

/**
 * Change a proposal's service / audience / nationality before it is approved
 * (migration 019). The database checks the role and the values again and
 * audit-logs the change with the reason.
 */
export function EditTagsForm({ draftId, tags, services }: { draftId: string; tags: Tags; services: string[] }) {
  const [open, setOpen] = useState(false);
  const [raw, formAction] = useFormState(setWatchDraftTags, { error: '' });
  // After a redirect to the same route Next 14 re-renders this form with the state set to undefined.
  const state: ActionState = raw ?? { error: '' };
  const [v, setV] = useState(tags);
  if (!open) {
    return (
      <button type="button" className="btn btn-secondary" onClick={() => setOpen(true)} data-edit-tags>
        Change the tags
      </button>
    );
  }
  const serviceOptions = services.includes(v.service) ? services : [v.service, ...services];
  const set = (key: keyof Tags) => (e: { target: { value: string } }) => setV({ ...v, [key]: e.target.value });
  return (
    <form action={formAction} className="space-y-3" data-tags-form>
      <input type="hidden" name="draft_id" value={draftId} />
      <div className="grid grid-cols-3 gap-3">
        <div>
          <label htmlFor="tag_service" className="field-label mb-1.5 block">Service</label>
          <select id="tag_service" name="service" value={v.service} onChange={set('service')} className="select">
            {serviceOptions.map((s) => <option key={s} value={s}>{label('service', s)}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="tag_audience" className="field-label mb-1.5 block">Audience</label>
          <select id="tag_audience" name="audience" value={v.audience} onChange={set('audience')} className="select">
            {TAG_AUDIENCES.map((a) => <option key={a} value={a}>{label('audience', a)}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="tag_nationality" className="field-label mb-1.5 block">Nationality</label>
          <select id="tag_nationality" name="nationality" value={v.nationality} onChange={set('nationality')} className="select">
            {TAG_NATIONALITIES.map((n) => <option key={n} value={n}>{label('nationality', n)}</option>)}
          </select>
        </div>
      </div>
      <div>
        <label className="field-label mb-1.5 block" htmlFor="tag_reason">Reason (required)</label>
        <textarea id="tag_reason" name="reason" className="textarea" rows={2} maxLength={500} required placeholder="Why the tags are being changed" />
      </div>
      {state.error ? <div className="notice notice-red" role="alert">{state.error}</div> : null}
      <Submit />
    </form>
  );
}
