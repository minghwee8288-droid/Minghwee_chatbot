'use client';

import { useState } from 'react';
import { useFormState, useFormStatus } from 'react-dom';
import type { ActionState } from '@/app/editor/actions';

type Action = (prev: ActionState, form: FormData) => Promise<ActionState>;

function Submit({ label, busyLabel, tone }: { label: string; busyLabel: string; tone: 'primary' | 'secondary' }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className={`btn btn-${tone}`}>
      {pending ? busyLabel : label}
    </button>
  );
}

/**
 * A button that opens a short form asking why, then runs a server action.
 * Every change kb-admin makes carries a reason; the database refuses one
 * without it. The action re-checks the session and role itself.
 */
export function ReasonAction({
  action,
  hidden,
  openLabel,
  submitLabel,
  busyLabel = 'Working…',
  help,
  tone = 'secondary',
}: {
  action: Action;
  hidden: Record<string, string>;
  openLabel: string;
  submitLabel: string;
  busyLabel?: string;
  help?: string;
  tone?: 'primary' | 'secondary';
}) {
  const [open, setOpen] = useState(false);
  const [raw, formAction] = useFormState(action, { error: '' });
  // After a redirect to the same route (every action here ends in one), Next 14
  // re-renders this still-mounted form with the state set to undefined.
  const state: ActionState = raw ?? { error: '' };
  if (!open) {
    return (
      <button type="button" className={`btn btn-${tone}`} onClick={() => setOpen(true)}>
        {openLabel}
      </button>
    );
  }
  return (
    <form action={formAction} className="card card-pad space-y-3">
      {Object.entries(hidden).map(([name, value]) => (
        <input key={name} type="hidden" name={name} value={value} />
      ))}
      {help ? <p className="text-[13px] text-muted">{help}</p> : null}
      <div>
        <label className="field-label mb-1.5 block" htmlFor={`reason-${openLabel}`}>
          Reason (required)
        </label>
        <textarea
          id={`reason-${openLabel}`}
          name="reason"
          required
          maxLength={500}
          rows={2}
          className="textarea"
          placeholder="Why this change is being made"
        />
      </div>
      {state.error ? (
        <div className="notice notice-red" role="alert">
          {state.error}
        </div>
      ) : null}
      {state.notice ? (
        <div className="notice notice-amber" role="status">
          {state.notice}
        </div>
      ) : null}
      <div className="flex gap-2">
        <Submit label={submitLabel} busyLabel={busyLabel} tone={tone} />
        <button type="button" className="btn btn-secondary" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}
