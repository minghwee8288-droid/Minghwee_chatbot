'use client';

import { useFormState, useFormStatus } from 'react-dom';
import { publishDraft, type ActionState } from '@/app/editor/actions';

function Submit({ label }: { label: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className="btn btn-primary">
      {pending ? 'Publishing…' : label}
    </button>
  );
}

/** Publish: the one button that changes what the chatbot reads. */
export function PublishButton({ versionId, label }: { versionId: string; label: string }) {
  const [raw, formAction] = useFormState(publishDraft, { error: '' });
  // After a redirect to the same route (every action here ends in one), Next 14
  // re-renders this still-mounted form with the state set to undefined.
  const state: ActionState = raw ?? { error: '' };
  return (
    <form action={formAction} className="contents">
      <input type="hidden" name="version_id" value={versionId} />
      <Submit label={label} />
      {state.error ? (
        <div className="notice notice-red order-last basis-full" role="alert">
          {state.error}
        </div>
      ) : null}
      {state.notice ? (
        <div className="notice notice-amber order-last basis-full" role="status">
          <span className="notice-dot" aria-hidden="true" />
          <div>
            <p className="notice-head">This change needs an approver</p>
            <p>It stays here as a pending draft. An approver will see it under Pending approval and can publish it.</p>
          </div>
        </div>
      ) : null}
    </form>
  );
}
