'use client';

import { useFormState, useFormStatus } from 'react-dom';
import type { ActionState } from '@/app/editor/actions';
import { addSource, fetchNow, watchSuggestedPage } from '@/app/watch/actions';
import { CATEGORY_LABELS } from './categories';

function Submit({ label, busy, tone = 'primary' }: { label: string; busy: string; tone?: 'primary' | 'secondary' }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className={`btn btn-${tone}`}>
      {pending ? busy : label}
    </button>
  );
}

/** Register a page to watch. The address must be https:// and public; the database checks it again. */
export function AddSourceForm() {
  const [raw, formAction] = useFormState(addSource, { error: '' });
  const state: ActionState = raw ?? { error: '' };
  return (
    <form action={formAction} className="card card-pad space-y-3">
      <h2 className="text-[15px] font-semibold">Watch a new page</h2>
      <div>
        <label className="field-label mb-1.5 block" htmlFor="src-url">Page address</label>
        <input id="src-url" name="url" type="url" required maxLength={2000} className="input" placeholder="https://www.mom.gov.sg/..." />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className="field-label mb-1.5 block" htmlFor="src-label">Label</label>
          <input id="src-label" name="label" required maxLength={200} className="input" placeholder="MOM - rest days" />
        </div>
        <div>
          <label className="field-label mb-1.5 block" htmlFor="src-cat">Category</label>
          <select id="src-cat" name="category" required className="select" defaultValue="government_rule">
            {Object.entries(CATEGORY_LABELS).map(([value, text]) => (
              <option key={value} value={value}>{text}</option>
            ))}
          </select>
        </div>
      </div>
      {state.error ? <div className="notice notice-red" role="alert">{state.error}</div> : null}
      <Submit label="Start watching" busy="Saving…" />
    </form>
  );
}

/** Fetch one page now: snapshot, compare, and draft proposals for review if it changed. */
export function FetchNowButton({ sourceId }: { sourceId: string }) {
  const [raw, formAction] = useFormState(fetchNow, { error: '' });
  const fetchState: ActionState = raw ?? { error: '' };
  return (
    <form action={formAction} className="inline-flex flex-col items-start gap-1">
      <input type="hidden" name="source_id" value={sourceId} />
      <Submit label="Fetch now" busy="Fetching…" tone="secondary" />
      {fetchState.error ? <span className="text-[12px]" style={{ color: 'var(--bad)' }} role="alert">{fetchState.error}</span> : null}
    </form>
  );
}

/** One click: watch a suggested sub-page, with the parent page's category. */
export function WatchSuggestedButton({ sourceId, url }: { sourceId: string; url: string }) {
  const [raw, formAction] = useFormState(watchSuggestedPage, { error: '' });
  const state: ActionState = raw ?? { error: '' };
  return (
    <form action={formAction} className="inline-flex flex-col items-start gap-1">
      <input type="hidden" name="source_id" value={sourceId} />
      <input type="hidden" name="url" value={url} />
      <Submit label="Watch this page" busy="Adding…" tone="secondary" />
      {state.error ? <span className="text-[12px]" style={{ color: 'var(--bad)' }} role="alert">{state.error}</span> : null}
    </form>
  );
}
