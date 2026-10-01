'use client';

import { useState } from 'react';
import { useFormState, useFormStatus } from 'react-dom';
import { label } from '@/components/labels';
import { createEntry, saveDraft } from '@/app/editor/actions';
import { AUDIENCES, CHAR_LIMIT, NATIONALITIES, hasNric, lengthState } from '@/lib/editing';

export type EditValues = {
  question: string;
  answer: string;
  section_heading: string;
  service: string;
  audience: string;
  nationality: string;
};

function SaveButton({ blocked }: { blocked: boolean }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn-primary" disabled={pending || blocked}>
      {pending ? 'Saving…' : 'Save draft'}
    </button>
  );
}

const COUNTER_COLOUR = { ok: 'var(--muted)', warn: 'var(--warn)', block: 'var(--bad)' } as const;

/**
 * mode 'edit' (Phase 2): a draft of an existing entry.
 * mode 'create' (migration 008): a brand-new entry - the database creates it
 * switched off and unsearchable, with this text as its first draft.
 */
export function EditForm({
  entryId,
  initial,
  services,
  liveLength,
  mode = 'edit',
}: {
  entryId?: string;
  initial: EditValues;
  services: string[];
  liveLength: number;
  mode?: 'edit' | 'create';
}) {
  const creating = mode === 'create';
  const [v, setV] = useState(initial);
  const [raw, formAction] = useFormState(creating ? createEntry : saveDraft, { error: '' });
  // After a redirect to the same route (every action here ends in one), Next 14
  // re-renders this still-mounted form with the state set to undefined.
  const state = raw ?? { error: '' };
  const set = (key: keyof EditValues) => (e: { target: { value: string } }) => setV({ ...v, [key]: e.target.value });

  const len = lengthState(v.question, v.answer, liveLength);
  const nric = hasNric(`${v.question} ${v.answer} ${v.section_heading}`);
  const unchanged = !creating && (Object.keys(initial) as (keyof EditValues)[]).every((k) => initial[k].trim() === v[k].trim());
  const serviceOptions = services.includes(v.service) ? services : [v.service, ...services];

  return (
    <form action={formAction} className="card card-pad space-y-5">
      {creating ? null : <input type="hidden" name="entry_id" value={entryId} />}

      <div>
        <label htmlFor="question" className="field-label mb-1.5 block">
          Question
        </label>
        <textarea id="question" name="question" value={v.question} onChange={set('question')} rows={2} required maxLength={4000} className="textarea" />
      </div>

      <div>
        <label htmlFor="answer" className="field-label mb-1.5 block">
          Answer
        </label>
        <textarea id="answer" name="answer" value={v.answer} onChange={set('answer')} rows={9} required maxLength={4000} className="textarea" />
        <p className="mt-1.5 text-[12px]" style={{ color: COUNTER_COLOUR[len.level] }} aria-live="polite">
          <span className="mono">
            {len.count} / {CHAR_LIMIT}
          </span>{' '}
          characters (question + answer)
          {len.level === 'warn' ? ` — close to the limit; the chatbot reads at most ${CHAR_LIMIT}.` : ''}
          {len.level === 'block' ? ` — too long. Shorten it to ${CHAR_LIMIT} or fewer to save.` : ''}
        </p>
      </div>

      <div>
        <label htmlFor="section_heading" className="field-label mb-1.5 block">
          Section heading
        </label>
        <input id="section_heading" name="section_heading" value={v.section_heading} onChange={set('section_heading')} maxLength={500} className="input" />
      </div>

      <div className="grid grid-cols-3 gap-3">
        <div>
          <label htmlFor="service" className="field-label mb-1.5 block">
            Service
          </label>
          <select id="service" name="service" value={v.service} onChange={set('service')} className="select">
            {serviceOptions.map((s) => (
              <option key={s} value={s}>
                {label('service', s)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="audience" className="field-label mb-1.5 block">
            Audience
          </label>
          <select id="audience" name="audience" value={v.audience} onChange={set('audience')} className="select">
            {AUDIENCES.map((a) => (
              <option key={a} value={a}>
                {label('audience', a)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="nationality" className="field-label mb-1.5 block">
            Nationality
          </label>
          <select id="nationality" name="nationality" value={v.nationality} onChange={set('nationality')} className="select">
            {NATIONALITIES.map((n) => (
              <option key={n} value={n}>
                {label('nationality', n)}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <label htmlFor="reason" className="field-label mb-1.5 block">
          {creating ? 'Why are you adding this entry? (required)' : 'Reason for the change (required)'}
        </label>
        <textarea
          id="reason"
          name="reason"
          rows={2}
          required
          maxLength={500}
          className="textarea"
          placeholder={creating ? 'e.g. Clients keep asking this and the knowledge base has no answer' : 'e.g. The agency changed the fee on 1 October'}
        />
      </div>

      {nric ? (
        <div className="notice notice-red" role="alert">
          The text contains what looks like an NRIC or FIN number. Remove it: knowledge base text is shown to clients.
        </div>
      ) : null}
      {state.error ? (
        <div className="notice notice-red" role="alert">
          {state.error}
        </div>
      ) : null}

      <div className="flex items-center gap-3">
        <SaveButton blocked={len.level === 'block' || nric || unchanged} />
        <p className="text-[12px] text-muted">
          {unchanged
            ? 'Nothing has changed yet.'
            : creating
              ? 'Saving creates the entry switched off, with this text as a draft. The chatbot cannot see it until an approver publishes it.'
              : 'Saving a draft changes nothing the chatbot reads. You review it next, then publish.'}
        </p>
      </div>
    </form>
  );
}
