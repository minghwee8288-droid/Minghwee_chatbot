import Link from 'next/link';
import { UnverifiedBadge } from '@/app/sources/UnverifiedBadge';
import { detectLanguage } from '@/lib/watcher/language';
import { notFound } from 'next/navigation';
import { approveWatchDraft, rejectWatchDraft } from '@/app/watch/actions';
import { ReasonAction } from '@/components/ReasonAction';
import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader, Pill, formatDate } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { wordDiff, type DiffPart } from '@/lib/editing';
import { removalsByOrigin, removedPoints, type RemovedPoint } from '@/lib/watcher/factDiff';
import { reviewState } from '@/lib/watcher/feeLock';
import { contradictingLine, entryDiff, type DiffSegment } from '@/lib/watcher/additive';
import { modelDraftAnswer, watchDraft, watcherSchema19, type SimilarEntry, type WatchDraftRow } from '@/lib/watchQueries';
import { editableServices } from '@/lib/queries';
import { label } from '@/components/labels';
import { changedTags, tagEvidence, type TagKey, type Tags } from '@/lib/watcher/tags';
import { DRAFT_STATUS } from '../status';
import { EditProposalForm } from './EditProposalForm';
import { EditTagsForm } from './EditTagsForm';

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

const TAG_ROWS: { key: TagKey; name: string }[] = [
  { key: 'service', name: 'Service' },
  { key: 'audience', name: 'Audience' },
  { key: 'nationality', name: 'Nationality' },
];

/**
 * 2026-10-28: the service / audience / nationality the proposal is filed
 * under (the bot filters its search on them), why, and - for an edit - the
 * live entry's tags beside them, a change highlighted (an approver publishes
 * it, as any change of routing). With migration 019 an editor can change them
 * before approval; without it they are shown read-only.
 */
function TagsPanel({ draft, open, canEdit, has019, services }: { draft: WatchDraftRow; open: boolean; canEdit: boolean; has019: boolean; services: string[] }) {
  const proposed: Tags = { service: draft.service, audience: draft.audience, nationality: draft.nationality };
  const live: Tags | null = draft.kind === 'edit' && draft.current_service
    ? { service: draft.current_service, audience: draft.current_audience ?? 'all', nationality: draft.current_nationality ?? 'all' }
    : null;
  const changed = changedTags(live, proposed);
  const evidence = tagEvidence({
    sourceLabel: draft.source_label, sourceUrl: draft.source_url, sourceCategory: draft.source_category,
    question: draft.proposed_question, answer: draft.proposed_answer, sourcePassage: draft.source_passage,
  });
  return (
    <section className="card card-pad space-y-2 text-[13px]" data-tags>
      <h2 className="text-[15px] font-semibold">Who this answer is for</h2>
      <p className="text-muted">The chatbot only finds an entry for questions with a matching service, audience and nationality, so a wrong tag answers somebody else&apos;s question.</p>
      <table className="w-full">
        <thead>
          <tr className="text-left">
            <th className="field-label py-1">Tag</th>
            <th className="field-label py-1">Proposed</th>
            {live ? <th className="field-label py-1">Live entry now</th> : null}
            <th className="field-label py-1">Why</th>
          </tr>
        </thead>
        <tbody>
          {TAG_ROWS.map(({ key, name }) => {
            const e = evidence[key];
            const differs = changed.includes(key);
            const why = e
              ? e.value === proposed[key] ? e.why : `The rules suggest ${label(key, e.value)}: ${e.why}`
              : live && !differs ? 'Kept from the live entry.' : draft.kind === 'new' && proposed[key] === (key === 'service' ? 'general' : 'all') ? 'No evidence for anything narrower.' : 'Proposed by the drafting model or set by a reviewer.';
            return (
              <tr key={key} data-tag={key} data-changed={differs ? 'yes' : 'no'} style={differs ? { background: 'var(--warn-bg)' } : undefined}>
                <td className="py-1 pr-3 font-medium">{name}</td>
                <td className="py-1 pr-3"><span className="mono">{label(key, proposed[key])}</span>{differs ? <> <Pill tone="warn">changes</Pill></> : null}</td>
                {live ? <td className="py-1 pr-3 mono">{label(key, live[key])}</td> : null}
                <td className="py-1 text-muted">{why}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {changed.length ? (
        <p data-tags-changed style={{ color: 'var(--warn)' }}>Approving re-files the live entry under the proposed {changed.join(', ')}. Check it is right for every question the entry answers.</p>
      ) : null}
      {open && canEdit && has019 ? <EditTagsForm draftId={draft.id} tags={proposed} services={services} /> : null}
      {open && canEdit && !has019 ? <p className="text-muted" data-tags-readonly>Tag editing needs migration 019; until it is applied a new entry is filed as shown and an edit keeps the live entry&apos;s tags.</p> : null}
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
  const has019 = await watcherSchema19().catch(() => false);
  const services = has019 && viewer.canEdit ? await editableServices().catch(() => []) : [];

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
  // Removed by the model's draft: hard block. Removed by an approver's Edit: shown, and Approve asks for the "wrong or outdated" tick.
  const modelAnswer = draft.kind === 'edit' && draft.current_answer && draft.edited_by_email ? await modelDraftAnswer(draft.id).catch(() => null) : null;
  const byOrigin = draft.kind === 'edit' && draft.current_answer
    ? removalsByOrigin(draft.current_answer, modelAnswer, draft.proposed_answer, draft.source_passage, draft.proposed_question)
    : { model: [], edit: [] };
  const drops = open && byOrigin.model.length > 0;
  const editDrops = open && !drops && byOrigin.edit.length > 0;
  const segments = draft.kind === 'edit' && draft.current_answer ? entryDiff(draft.current_answer, draft.proposed_answer) : null;
  // A page not in English (2026-10-16): the passage is quoted as it is; the proposal is the translation.
  const passageLanguage = detectLanguage(draft.source_passage);
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
      {editDrops ? (
        <AmberNotice title="Your edit removes existing information">
          The points in red under &quot;Points removed&quot; are in the live entry now and the source page does not contradict them; they were taken out by an Edit, not by the watcher. Approving asks you to tick that they are wrong or outdated, and to say why in the reason.
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
          {draft.source_unverified ? <span className="ml-2 inline-block align-middle"><UnverifiedBadge /></span> : null}
        </p>
        {draft.source_unverified ? (
          <p style={{ color: 'var(--bad)' }}>This page was read without a verified connection (an approver allowed it for this site only). Check the facts against the page itself before approving.</p>
        ) : null}
        <p><span className="field-label">Fetched: </span>{formatDate(draft.source_fetched_at)}</p>
        {!passageLanguage.english ? (
          <p data-translated>
            <Pill tone="warn">Translated from {passageLanguage.name}</Pill>{' '}
            The page is in {passageLanguage.name}: the passage below is quoted exactly as it appears; the proposal is the watcher&apos;s English translation of it. Check the translation before approving.
          </p>
        ) : null}
        <p className="pt-2"><span className="field-label">The exact passage this came from{passageLanguage.english ? '' : ` (in ${passageLanguage.name})`}:</span></p>
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
      {drops || editDrops ? <PointsRemoved points={removed.filter((r) => !r.contradictedBy)} /> : null}
      <TagsPanel draft={draft} open={draft.status === 'pending' || draft.status === 'blocked'} canEdit={viewer.canEdit} has019={has019} services={services} />
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
              confirmAlso={editDrops ? { name: 'confirm_removed', label: 'I confirm these points are wrong or outdated.' } : undefined}
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
