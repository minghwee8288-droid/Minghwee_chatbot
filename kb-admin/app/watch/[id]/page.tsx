import Link from 'next/link';
import { notFound } from 'next/navigation';
import { approveWatchDraft, rejectWatchDraft } from '@/app/watch/actions';
import { ReasonAction } from '@/components/ReasonAction';
import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader, Pill, formatDate } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { wordDiff, type DiffPart } from '@/lib/editing';
import { feeLockReason } from '@/lib/watcher/feeLock';
import { watchDraft } from '@/lib/watchQueries';
import { DRAFT_STATUS } from '../status';
import { EditProposalForm } from './EditProposalForm';

// Approving embeds the text and publishes it in the same server action.
export const maxDuration = 120;

function Parts({ parts }: { parts: DiffPart[] }) {
  if (!parts.length) return <span className="text-muted">-</span>;
  return (
    <p className="whitespace-pre-wrap break-words leading-[1.6]">
      {parts.map((p, i) =>
        p.kind === 'same' ? <span key={i}>{p.text}</span> : <span key={i} className={p.kind === 'del' ? 'diff-del' : 'diff-ins'}>{p.text}</span>,
      )}
    </p>
  );
}

function Compare({ title, leftLabel, rightLabel, before, after }: { title: string; leftLabel: string; rightLabel: string; before: string; after: string }) {
  const d = wordDiff(before, after);
  return (
    <section className="card">
      <h2 className="card-pad pb-0 text-[15px] font-semibold">{title}</h2>
      <div className="grid grid-cols-2">
        <div className="min-w-0 px-5 py-4">
          <h3 className="field-label mb-1.5">{leftLabel}</h3>
          <Parts parts={d.before} />
        </div>
        <div className="min-w-0 border-l px-5 py-4" style={{ borderColor: 'var(--divider)' }}>
          <h3 className="field-label mb-1.5">{rightLabel}</h3>
          <Parts parts={d.after} />
        </div>
      </div>
    </section>
  );
}

/**
 * Stage 3: one proposal. The page that changed, what changed on it, and the
 * knowledge-base change it implies, side by side; then Approve / Edit / Reject.
 * A proposal under the fee lock shows "Needs agency confirmation" and no
 * Approve button - and the database refuses its approval anyway (KB007).
 */
export default async function WatchDraftPage({ params, searchParams }: { params: { id: string }; searchParams: Record<string, string | undefined> }) {
  const id = /^[0-9a-f-]{36}$/i.test(params.id) ? params.id : null;
  const [viewer, draft] = await requireViewerWith(() => (id ? watchDraft(id).catch(() => null) : Promise.resolve(null)));
  if (!viewer.watcherOn || !draft) notFound();

  const open = draft.status === 'pending' || draft.status === 'blocked';
  const lock =
    draft.blocked_reason ??
    (open
      ? feeLockReason({
          targetQuestion: draft.current_question,
          targetAnswer: draft.current_answer,
          proposedQuestion: draft.proposed_question,
          proposedAnswer: draft.proposed_answer,
        })
      : null);
  const stale = draft.kind === 'edit' && open && (draft.current_question !== draft.old_question || draft.current_answer !== draft.old_answer);
  const status = DRAFT_STATUS[draft.status] ?? { text: draft.status, tone: 'off' as const };
  const oldEntry = draft.kind === 'edit' ? `${draft.current_question ?? ''}\n\n${draft.current_answer ?? ''}` : '';
  const newEntry = `${draft.proposed_question}\n\n${draft.proposed_answer}`;

  return (
    <Shell viewer={viewer} active="/watch">
      <p className="text-[13px]">
        <Link href="/watch" className="link">Page changes to review</Link>
      </p>
      <PageHeader
        title={draft.kind === 'new' ? 'Proposed new entry' : 'Proposed change to an entry'}
        sub={
          <>
            <Pill tone={status.tone}>{status.text}</Pill>{' '}
            <span className="text-muted">drafted {formatDate(draft.created_at)} by {draft.created_by_email ?? 'the watcher'}</span>
          </>
        }
      />
      {searchParams.published ? <AmberNotice title="Live">Approved and published through the usual Q&amp;A path. The chatbot now answers from it.</AmberNotice> : null}
      {searchParams.rejected ? <AmberNotice title="Rejected">The proposal was rejected. Nothing changed.</AmberNotice> : null}
      {searchParams.edited ? <AmberNotice title="Saved">The proposal's wording was changed. The fee lock was checked again.</AmberNotice> : null}
      {lock ? (
        <AmberNotice title="Needs agency confirmation">
          This proposal cannot be approved here: {lock}. A page change may never alter an agency fee, a pricing rule or a row from the agency&apos;s fees document. Reject it, or raise it with Ming Hwee.
        </AmberNotice>
      ) : null}
      {stale ? (
        <AmberNotice title="The entry has changed since this was drafted">Someone has edited the entry since. Approving will be refused; reject this proposal and fetch the page again.</AmberNotice>
      ) : null}

      <section className="card card-pad space-y-1 text-[13px]">
        <p>
          <span className="field-label">Source page: </span>
          {draft.source_label} -{' '}
          <a href={draft.source_url} className="link break-all" target="_blank" rel="noreferrer noopener">{draft.source_url}</a>
        </p>
        <p><span className="field-label">Fetched: </span>{formatDate(draft.source_fetched_at)}</p>
        <p className="pt-2"><span className="field-label">The exact passage this came from:</span></p>
        <blockquote className="whitespace-pre-wrap border-l-2 pl-3" style={{ borderColor: 'var(--divider)' }}>{draft.source_passage}</blockquote>
      </section>

      <Compare
        title="What changed on the page"
        leftLabel="Before (gone or reworded)"
        rightLabel="After (new or reworded)"
        before={draft.old_source_text ?? ''}
        after={draft.new_source_text ?? draft.source_passage}
      />
      <Compare
        title="The knowledge-base change"
        leftLabel={draft.kind === 'edit' ? 'The entry now' : 'No entry yet'}
        rightLabel="Proposed"
        before={oldEntry}
        after={newEntry}
      />

      {draft.status === 'approved' && draft.version_id ? (
        <AmberNotice title="Approved, not yet live">
          The text is saved as a Q&amp;A draft but publishing did not finish. <Link className="link" href={`/drafts/${draft.version_id}`}>Open the Q&amp;A draft</Link> to publish it.
        </AmberNotice>
      ) : null}
      {draft.status === 'published' || draft.status === 'approved' ? (
        <section className="card card-pad space-y-1 text-[13px]">
          <h2 className="text-[15px] font-semibold">Provenance</h2>
          <p>Approved by {draft.approved_by_email} on {formatDate(draft.approved_at)}{draft.published_at ? `; live since ${formatDate(draft.published_at)}` : ''}.</p>
          <p>From {draft.source_url}, fetched {formatDate(draft.source_fetched_at)}.</p>
          {draft.entry_id_after ? <p><Link className="link" href={`/rows/${draft.entry_id_after}`}>Open the entry</Link></p> : null}
        </section>
      ) : null}
      {draft.review_reason && (draft.status === 'rejected' || draft.status === 'published') ? (
        <p className="text-[13px] text-muted">{draft.status === 'rejected' ? 'Rejected' : 'Approved'} by {draft.reviewed_by_email}: {draft.review_reason}</p>
      ) : null}

      {open && viewer.canEdit ? (
        <div className="flex flex-wrap items-start gap-3">
          {viewer.canApprove && !lock && !stale ? (
            <ReasonAction
              action={approveWatchDraft}
              hidden={{ draft_id: draft.id }}
              openLabel="Approve"
              submitLabel="Approve and publish"
              busyLabel="Publishing…"
              tone="primary"
              help="The text goes through the usual Q&A path: saved as a draft, given its search fingerprint, and published. The chatbot answers from it straight away."
            />
          ) : null}
          <EditProposalForm draftId={draft.id} question={draft.proposed_question} answer={draft.proposed_answer} heading={draft.section_heading ?? ''} />
          <ReasonAction action={rejectWatchDraft} hidden={{ draft_id: draft.id }} openLabel="Reject" submitLabel="Reject" help="Nothing changes. The proposal is kept as rejected." />
        </div>
      ) : null}
      {open && !viewer.canApprove ? <p className="text-[13px] text-muted">Only an approver can approve a proposal.</p> : null}
    </Shell>
  );
}
