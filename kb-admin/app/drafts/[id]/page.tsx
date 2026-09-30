import Link from 'next/link';
import { notFound } from 'next/navigation';
import { discardDraft } from '@/app/editor/actions';
import { ReasonAction } from '@/components/ReasonAction';
import { Shell } from '@/components/Shell';
import { label } from '@/components/labels';
import { ActivePill, AmberNotice, Pill, formatDate } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { APPROVAL_WORDS, approvalReasons, charCount, entryText, wordDiff, type DiffPart } from '@/lib/editing';
import { row, version, versions } from '@/lib/queries';
import { PublishButton } from './PublishButton';

function Parts({ parts }: { parts: DiffPart[] }) {
  if (!parts.length) return <span className="text-muted">—</span>;
  return (
    <p className="whitespace-pre-wrap break-words leading-[1.6]">
      {parts.map((p, i) =>
        p.kind === 'same' ? (
          <span key={i}>{p.text}</span>
        ) : (
          <span key={i} className={p.kind === 'del' ? 'diff-del' : 'diff-ins'}>
            {p.text}
          </span>
        ),
      )}
    </p>
  );
}

function Compare({ name, before, after }: { name: string; before: string; after: string }) {
  const d = wordDiff(before, after);
  const changed = before !== after;
  return (
    <div className="grid grid-cols-2 border-t first:border-t-0" style={{ borderColor: 'var(--divider)' }}>
      <div className="min-w-0 px-5 py-4">
        <h3 className="field-label mb-1.5">
          {name} {changed ? <Pill tone="warn">Changed</Pill> : null}
        </h3>
        <Parts parts={d.before} />
      </div>
      <div className="min-w-0 border-l px-5 py-4" style={{ borderColor: 'var(--divider)' }}>
        <h3 className="field-label mb-1.5">{name}</h3>
        <Parts parts={d.after} />
      </div>
    </div>
  );
}

const STATUS: Record<string, string> = {
  published: 'This version is live.',
  superseded: 'This version was live once and has since been replaced.',
  discarded: 'This draft was discarded.',
};

export default async function DraftPage({ params, searchParams }: { params: { id: string }; searchParams: Record<string, string | undefined> }) {
  const viewer = await requireViewer();
  const draft = await version(params.id);
  if (!draft) notFound();
  const live = await row(draft.entry_id);
  if (!live) notFound();

  const history = await versions(draft.entry_id);
  const current = history.find((v) => v.status === 'published');
  const stale = draft.status === 'draft' && current !== undefined && draft.based_on_version !== current.version_number;
  const liveFields = {
    question: live.question,
    answer: live.answer,
    section_heading: live.section_heading,
    service: live.service_type,
    audience: live.contact_type,
    nationality: live.nationality,
    active: live.is_active,
  };
  const why = approvalReasons(liveFields, draft);
  const isAuthor = draft.created_by === viewer.userId;
  const length = charCount(entryText(draft.question ?? '', draft.answer ?? ''));

  return (
    <Shell viewer={viewer} active={viewer.canApprove ? '/pending' : '/rows'}>
      <div>
        <Link href={`/rows/${live.id}`} className="link text-[13px]">
          ← Back to the entry
        </Link>
        <h1 className="page-title mt-3">
          {draft.status === 'draft' ? 'Review draft' : `Version ${draft.version_number}`}
        </h1>
        <p className="page-sub">
          Version {draft.version_number} by {draft.created_by_email ?? 'the original import'}, {formatDate(draft.created_at)}.
          Reason: {draft.reason}
        </p>
      </div>

      {searchParams.saved ? <AmberNotice title="Draft saved">Nothing the chatbot reads has changed yet. Check the changes below, then publish.</AmberNotice> : null}
      {searchParams.restored ? <AmberNotice title="Restored as a new draft">Publishing it makes that earlier version live again.</AmberNotice> : null}
      {draft.status !== 'draft' ? <AmberNotice title="Not an open draft">{STATUS[draft.status] ?? draft.status}</AmberNotice> : null}
      {stale ? (
        <div className="notice notice-red" role="alert">
          The entry has changed since this draft was started, so it cannot be published. Discard it and edit the entry again.
        </div>
      ) : null}

      <section className="card" aria-label="Changes">
        <div className="card-head grid grid-cols-2">
          <span className="field-label">Live now</span>
          <span className="field-label">This draft</span>
        </div>
        <Compare name="Question" before={live.question ?? ''} after={draft.question ?? ''} />
        <Compare name="Answer" before={live.answer ?? ''} after={draft.answer ?? ''} />
        <Compare name="Section heading" before={live.section_heading ?? ''} after={draft.section_heading ?? ''} />
        <Compare name="Service" before={label('service', live.service_type)} after={label('service', draft.service)} />
        <Compare name="Audience" before={label('audience', live.contact_type)} after={label('audience', draft.audience)} />
        <Compare name="Nationality" before={label('nationality', live.nationality)} after={label('nationality', draft.nationality)} />
        <div className="grid grid-cols-2 border-t" style={{ borderColor: 'var(--divider)' }}>
          <div className="px-5 py-4">
            <ActivePill active={live.is_active} />
          </div>
          <div className="border-l px-5 py-4" style={{ borderColor: 'var(--divider)' }}>
            <ActivePill active={draft.active} />
            <span className="ml-3 text-[12px] text-muted mono">{length} / 1200 characters</span>
          </div>
        </div>
      </section>

      {draft.status === 'draft' && !stale ? (
        <section className="card card-pad space-y-4" aria-label="Publish">
          {why.length ? (
            <p className="text-[13px]">
              <Pill tone="lock">Needs an approver</Pill>{' '}
              <span className="text-muted">because {why.map((w) => APPROVAL_WORDS[w]).join(', ')}.</span>
            </p>
          ) : (
            <p className="text-[13px] text-muted">Wording only: an editor or an approver can publish this.</p>
          )}
          {viewer.canEdit ? (
            <div className="flex flex-wrap items-start gap-3">
              <PublishButton versionId={draft.id} label={why.length && viewer.canApprove ? 'Approve and publish' : 'Publish'} />
              {isAuthor || viewer.canApprove ? (
                <ReasonAction
                  action={discardDraft}
                  hidden={{ version_id: draft.id }}
                  openLabel="Discard draft"
                  submitLabel="Discard draft"
                  busyLabel="Discarding…"
                  help="The draft is kept in the history as discarded. Nothing the chatbot reads changes."
                />
              ) : null}
            </div>
          ) : (
            <p className="text-[13px] text-muted">Your account can view drafts but not publish them.</p>
          )}
        </section>
      ) : draft.status === 'draft' && viewer.canEdit && (isAuthor || viewer.canApprove) ? (
        <ReasonAction action={discardDraft} hidden={{ version_id: draft.id }} openLabel="Discard draft" submitLabel="Discard draft" busyLabel="Discarding…" />
      ) : null}
    </Shell>
  );
}
