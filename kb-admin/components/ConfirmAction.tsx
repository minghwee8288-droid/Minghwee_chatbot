'use client';

import { useState } from 'react';
import { useFormState, useFormStatus } from 'react-dom';

type State = { error: string; notice?: string };
type Action = (prev: State, form: FormData) => Promise<State>;

function Submit({ label, busyLabel, tone }: { label: string; busyLabel: string; tone: 'primary' | 'secondary' }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className={`btn btn-${tone}`}>
      {pending ? busyLabel : label}
    </button>
  );
}

/**
 * A button that opens a short form - an optional reason, an optional "I
 * understand" tick box - then runs a server action. The action checks the
 * session, the role and both fields again itself; this only asks first.
 */
export function ConfirmAction({
  action,
  hidden,
  openLabel,
  submitLabel,
  busyLabel = 'Working…',
  help,
  confirmText,
  needReason = false,
  tone = 'secondary',
  disabled = false,
  disabledNote,
}: {
  action: Action;
  hidden: Record<string, string>;
  openLabel: string;
  submitLabel: string;
  busyLabel?: string;
  help?: string;
  confirmText?: string;
  needReason?: boolean;
  tone?: 'primary' | 'secondary';
  disabled?: boolean;
  disabledNote?: string;
}) {
  const [open, setOpen] = useState(false);
  const [raw, formAction] = useFormState(action, { error: '' });
  // After a redirect to the same route Next 14 re-renders a mounted form with undefined state.
  const state: State = raw ?? { error: '' };
  const key = openLabel.replace(/\W+/g, '-').toLowerCase();
  if (!open) {
    return (
      <span className="inline-flex flex-col gap-1">
        <button type="button" className={`btn btn-${tone}`} disabled={disabled} onClick={() => setOpen(true)}>
          {openLabel}
        </button>
        {disabled && disabledNote ? <span className="text-[12px] text-muted">{disabledNote}</span> : null}
      </span>
    );
  }
  return (
    <form action={formAction} className="card card-pad space-y-3">
      {Object.entries(hidden).map(([name, value]) => (
        <input key={name} type="hidden" name={name} value={value} />
      ))}
      {help ? <p className="text-[13px] text-muted">{help}</p> : null}
      {needReason ? (
        <div>
          <label className="field-label mb-1.5 block" htmlFor={`reason-${key}`}>
            Reason (required)
          </label>
          <textarea id={`reason-${key}`} name="reason" required maxLength={500} rows={2} className="textarea" placeholder="Why this is being done" />
        </div>
      ) : null}
      {confirmText ? (
        <label className="flex items-start gap-2 text-[13px]">
          <input type="checkbox" name="confirm" value="yes" required className="mt-0.5" />
          <span>{confirmText}</span>
        </label>
      ) : null}
      {state.error ? (
        <div className="notice notice-red" role="alert">
          {state.error}
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
