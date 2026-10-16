import Link from 'next/link';
import { notFound } from 'next/navigation';
import { approveWatchDraft, rejectWatchDraft } from '@/app/watch/actions';
import { ReasonAction } from '@/components/ReasonAction';
import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader, Pill, formatDate } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { wordDiff, type DiffPart } from '@/lib/editing';
import { removedPoints, type RemovedPoint } from '@/lib/watcher/factDiff';
import { reviewState } from '@/lib/watcher/feeLock';
import { contradictingLine, entryDiff, type DiffSegment } from '@/lib/watcher/additive';
import { watchDraft, type SimilarEntry } from '@/lib/watchQueries';
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
 * 016: the live entries the overlap check found close to this proposal, with
 * the similarity and every figure one states and the other does not. Each
 * links to the entry, so the reviewer can read it before approving.
 */
function SimilarEntries({ entries }: { entries: SimilarEntry[] }) {
  if (!entries.length) return null;
  return (
    <section className="card card-pad space-y-2 text-[13px]" data-similar>
      <h2 className="text-[15px] font-semibold">Similar existing entries</h2>
      <p className="text-muted">Live entries the bot's own search ranks close to this proposal. Read them before approving, so the assistant does not end up with two answers to one question.</p>
      <ul className="space-y-2">
        {entries.map((e) => {
          const onlyHere = e.differs?.proposal ?? [];
          const onlyThere = e.differs?.entry ?? [];
          return (
            <li key={e.id} className="border-l-2 pl-3" style={{ borderColor: e.conflicts?.length ? 'var(--bad)' : 'var(--divider)' }}>
              <Link className="link" href={`/rows/${e.id}`}>{e.question}</Link>{' '}
              <span className="mono text-muted">similarity {e.similarity.toFixed(2)}</span>
              {onlyHere.length || onlyThere.length ? (
                <p className="text-[12px] text-muted">
                  Figures that differ:
                  {onlyHere.length ? ` only in the proposal ${onlyHere.join(', ')}` : ''}
                  {onlyHere.length && onlyThere.length ? ';' : ''}
                  {onlyThere.length ? ` only in this entry ${onlyThere.join(', ')}` : ''}
                </p>
              ) : null}
              {e.conflicts?.length ? <p className="text-[12px]" style={{ color: 'var(--bad)' }}>Disagrees: {e.conflicts.join('; ')}</p> : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * Sentences of the live entry the proposed change no longer carries. Each
 * one the source does not contradict blocks Approve (the "drops
 * existing information" banner); Edit is still allowed.
 */
function PointsRemoved({ points }: { points: RemovedPoint[] }) {
  if (!points.length) return null;
  return (
    <section className="card card-pad space-y-2 text-[13px]">
      <h2 className="text-[15px] font-semibold">Points removed</h2>
      <p className="text-muted">Said by the live entry now, and no longer by the proposal.</p>
      <ul className="space-y-1.5">
        {points.map((p, i) => (
          <li key={i} className="border-l-2 pl-3" style={{ borderColor: 'var(--bad)' }}>
            <p style={{ color: 'var(--bad)' }}>{p.sentence}</p>
            <p className="text-[12px] text-muted">{p.contradictedBy ? `The source page contradicts it: ${p.contradictedBy}.` : 'The source page does not contradict it.'}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * A change to a live entry, sentence by sentence (2026-10-16, additive
 * changes): the live entry is kept as it is; new facts are added (green) and
 * a sentence is replaced only where the source page contradicts it (old ->
 * new, with the page words that contradict it). Everything else is unchanged
 * (grey). A sentence a person's Edit removed is shown struck out in red.
 */
function EntryChanges({ question, segments, sourcePassage }: { question: string; segments: DiffSegment[]; sourcePassage: string }) {
  const added = segments.filter((x) => x.kind === 'added').length;
  const replaced = segments.filter((x) => x.kind === 'replaced').length;
  return (
    <section className="card card-pad space-y-2 text-[13px]" data-entry-changes>
      <h2 className="text-[15px] font-semibold">The knowledge-base change</h2>
      <p className="text-muted">
        {question} - {added} added, {replaced} replaced; everything else in the live entry stays exactly as it is.
      </p>
      <ul className="space-y-1.5">
        {segments.map((x, i) => {
          if (x.kind === 'same') return <li key={i} className="border-l-2 pl-3 text-muted" style={{ borderColor: 'var(--divider)' }} data-seg="same">{x.text}</li>;
          if (x.kind === 'added') {
            return (
              <li key={i} className="border-l-2 pl-3" style={{ borderColor: 'var(--good)', color: 'var(--good)', background: 'var(--good-bg)' }} data-seg="added">
                <span className="field-label">Added: </span>{x.text}
              </li>
            );
          }
          if (x.kind === 'removed') {
            return <li key={i} className="border-l-2 pl-3 line-through" style={{ borderColor: 'var(--bad)', color: 'var(--bad)' }} data-seg="removed">{x.text}</li>;
          }
          const why = contradictingLine(sourcePassage, x.old, x.new);
          return (
            <li key={i} className="border-l-2 pl-3" style={{ borderColor: 'var(--good)' }} data-seg="replaced">
              <p><span className="field-label">Replaced: </span><span className="line-through" style={{ color: 'var(--bad)' }}>{x.old}</span></p>
              <p style={{ color: 'var(--good)' }}>&rarr; {x.new}</p>
              <p className="text-[12px] text-muted">{why ? `The source page contradicts it: ${why}` : 'The source page does not contradict it.'}</p>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * Stage 3: one proposal. The page that changed, what changed on it, and the
 * knowledge-base change it implies, side by side; then Approve / Edit / Reject.
 * A proposal under the fee lock shows "Needs agency confirmation" and no
 * Approve button - and the database refuses its approval anyway (KB007).
 * A government page's money figure is approvable, with a warning and a
 * required tick (the database refuses without it, KB008).
 */
export default async function WatchDraftPage({ params, searchParams }: { params: { id: string }; searchParams: Record<string, string | undefined> }) {
  const id = /^[0-9a-f-]{36}$/i.test(params.id) ? params.id : null;
  const [viewer, draft] = await requireViewerWith(() => (id ? watchDraft(id).catch(() => null) : Promise.resolve(null)));
  if (!viewer.watcherOn || !draft) notFound();

  const open = draft.status === 'pending' || draft.status === 'blocked';
  const review = reviewState({
    targetQuestion: draft.current_question,
    targetAnswer: draft.current_answer,
    proposedQuestion: draft.proposed_question,
    proposedAnswer: draft.proposed_answer,
    category: draft.source_category,
  });
  const lock = draft.blocked_reason ?? (open ? review.lock : null);
  const figure = open && !lock && (review.figure || draft.figure_check);
  const conflict = open && !lock && draft.conflict_check;
  const stale = draft.kind === 'edit' && open && (draft.current_question !== draft.old_question || draft.current_answer !== draft.old_answer);
  const removed = draft.kind === 'edit' && draft.current_answer
    ? removedPoints(draft.current_answer, draft.proposed_answer, draft.source_passage, draft.proposed_question)
    : [];
  const drops = open && removed.some((r) => !r.contradictedBy);
  const segments = draft.kind === 'edit' && draft.current_answer ? entryDiff(draft.current_answer, draft.proposed_answer) : null;
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
      {figure ? (
        <AmberNotice title="This changes a figure">
          This changes a figure: check it against the source before approving. Open the source page above and confirm the amount word for word; approving asks you to tick that you did.
        </AmberNotice>
      ) : null}
      {conflict ? (
        <AmberNotice title="This disagrees with an existing entry">
          Approving this could leave the assistant with two different answers: {draft.conflict_note ?? 'a similar live entry states a different figure'}. Compare it with the entries under &quot;Similar existing entries&quot; and the source page; approving asks you to tick that you did. If the live entry is the one that is wrong, change that entry instead.
        </AmberNotice>
      ) : null}
      {drops ? (
        <AmberNotice title="This update drops existing information">
          This update drops existing information: the points in red under &quot;Points removed&quot; are in the live entry now, and the source page does not contradict them. Approving is not possible until they are put back: use Edit to add them to the proposal, or reject it.
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
      {segments ? (
        <>
          {draft.proposed_question !== draft.current_question ? (
            <Compare title="The question" leftLabel="The entry now" rightLabel="Proposed" before={draft.current_question ?? ''} after={draft.proposed_question} />
          ) : null}
          <EntryChanges question={draft.proposed_question} segments={segments} sourcePassage={draft.source_passage} />
        </>
      ) : (
        <Compare title="The knowledge-base change" leftLabel="No entry yet" rightLabel="Proposed" before={oldEntry} after={newEntry} />
      )}
      {drops ? <PointsRemoved points={removed.filter((r) => !r.contradictedBy)} /> : null}
      <SimilarEntries entries={draft.similar_entries ?? []} />

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
          {draft.figure_confirmed ? <p>The approver confirmed the figure against the source.</p> : null}
          {draft.conflict_confirmed ? <p>The approver confirmed it against the similar existing entries.</p> : null}
          {draft.entry_id_after ? <p><Link className="link" href={`/rows/${draft.entry_id_after}`}>Open the entry</Link></p> : null}
        </section>
      ) : null}
      {draft.review_reason && (draft.status === 'rejected' || draft.status === 'published') ? (
        <p className="text-[13px] text-muted">{draft.status === 'rejected' ? 'Rejected' : 'Approved'} by {draft.reviewed_by_email}: {draft.review_reason}</p>
      ) : null}

      {open && viewer.canEdit ? (
        <div className="flex flex-wrap items-start gap-3">
          {viewer.canApprove && !lock && !stale && !drops ? (
            <ReasonAction
              action={approveWatchDraft}
              hidden={{ draft_id: draft.id }}
              confirm={figure || conflict ? {
                name: 'confirm_figure',
                label: figure && conflict
                  ? 'I have checked this figure against the source page and against the similar existing entries.'
                  : conflict
                    ? 'I have checked this against the similar existing entries and the source page.'
                    : 'I have checked this figure against the source page.',
              } : undefined}
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
