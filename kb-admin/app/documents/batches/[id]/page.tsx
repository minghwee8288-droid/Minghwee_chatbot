import Link from 'next/link';
import { notFound } from 'next/navigation';
import { discardBatch, publishBatch, restoreBatch } from '@/app/docs/actions';
import { ConfirmAction } from '@/components/ConfirmAction';
import { FigureText } from '@/components/FigureText';
import { Shell } from '@/components/Shell';
import { BATCH_STATUS, documentLabel, label } from '@/components/labels';
import { AmberNotice, PageHeader, Pill, formatDate, type Tone } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { batch, editableServices, namespaces, stagedChunks } from '@/lib/queries';
import { plural } from '@/lib/plural';
import { ChunkEditor } from './ChunkEditor';

export default async function BatchPage({ params, searchParams }: { params: { id: string }; searchParams: Record<string, string | undefined> }) {
  const viewer = await requireViewer();
  const b = await batch(params.id);
  if (!b) notFound();
  const [chunks, services, spaces] = await Promise.all([stagedChunks(b.id), editableServices(), namespaces()]);
  const staged = b.status === 'staged';
  const checked = Boolean(b.impact_check_run_at);
  const mine = b.prepared_by === viewer.userId;
  const status = BATCH_STATUS[b.status] ?? { text: b.status, tone: 'off' as Tone };
  return (
    <Shell viewer={viewer} active="/documents">
      <PageHeader
        title={documentLabel(b.source_name)}
        sub={
          <>
            {b.is_baseline ? 'The imported version, recorded when this document was first replaced' : 'A version prepared in KB Admin'} ·{' '}
            {plural(b.chunk_count, 'chunk')} · prepared by {b.prepared_by_email} on {formatDate(b.prepared_at)}
          </>
        }
      />
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <Pill tone={status.tone}>{status.text}</Pill>
        {b.is_baseline ? <Pill tone="lock">Imported original</Pill> : null}
        {staged ? checked ? <Pill tone="good">Impact check recorded {formatDate(b.impact_check_run_at)}</Pill> : <Pill tone="warn">Impact check not run</Pill> : null}
        <Link href={`/documents/history?source=${encodeURIComponent(b.source_name)}`} className="link">
          All versions of this document
        </Link>
      </div>

      {searchParams.prepared ? <AmberNotice title="Prepared">{plural(searchParams.prepared, 'chunk')} {searchParams.prepared === '1' ? 'was' : 'were'} staged. Review them, run the impact check, then an approver publishes.</AmberNotice> : null}
      {searchParams.edited ? <AmberNotice title="Chunk saved">Chunk {searchParams.edited} was saved. The impact check has to run again before publishing.</AmberNotice> : null}
      {searchParams.checked ? <AmberNotice title="Impact check recorded">An approver can now publish this version.</AmberNotice> : null}
      {searchParams.published ? (
        <div className="notice notice-amber" role="status" data-result="published">
          <span className="notice-dot" aria-hidden="true" />
          <div>
            <p className="notice-head">Published</p>
            <p>
              {plural(searchParams.added, 'chunk')} added to what the chatbot reads, {plural(searchParams.off, 'earlier chunk')} switched off.
              {searchParams.baseline ? ' The imported version was kept as an earlier version you can restore.' : ''}
            </p>
          </div>
        </div>
      ) : null}

      {staged ? (
        <div className="card card-pad space-y-3">
          <div className="flex flex-wrap items-start gap-3">
            <Link href={`/documents/batches/${b.id}/impact`} className="btn btn-primary">
              {checked ? 'Run the impact check again' : 'Run the impact check'}
            </Link>
            {viewer.canEdit ? (
              <ConfirmAction
                action={discardBatch}
                hidden={{ batch_id: b.id }}
                openLabel="Discard"
                submitLabel="Discard this batch"
                confirmText="Discard every chunk of this version. The live document does not change."
                help="To drop or re-split chunks, discard the batch and upload again."
              />
            ) : null}
            {viewer.canApprove ? (
              <ConfirmAction
                action={publishBatch}
                hidden={{ batch_id: b.id }}
                openLabel="Publish"
                submitLabel="Publish now"
                busyLabel="Publishing…"
                tone="primary"
                needReason
                disabled={!checked}
                disabledNote="Run the impact check and mark it as checked first."
                help={
                  mine
                    ? 'You prepared this version yourself; publishing it is recorded as self-published.'
                    : `Prepared by ${b.prepared_by_email}. Publishing switches off every other chunk of this document and makes these live.`
                }
              />
            ) : (
              <p className="text-[13px] text-muted">An approver publishes this version once the impact check is recorded.</p>
            )}
          </div>
        </div>
      ) : null}
      {b.status === 'superseded' && viewer.canApprove ? (
        <ConfirmAction
          action={restoreBatch}
          hidden={{ batch_id: b.id }}
          openLabel="Restore this version"
          submitLabel="Restore"
          needReason
          help="Switches this version's chunks back on and switches off whatever is live for this document now."
        />
      ) : null}

      <ol className="space-y-3">
        {chunks.map((c) => (
          <li key={c.id} className="card card-pad space-y-2" data-chunk={c.ordinal}>
            <div className="flex flex-wrap items-center gap-3 text-[12px]">
              <span className="font-medium">Chunk {c.ordinal}</span>
              <span className="pill pill-off">{c.chunk_type === 'table_unit' ? 'Table' : 'Passage'}</span>
              <span className="mono text-muted">{c.content.length} characters</span>
              <span className="text-muted">
                {label('service', c.service)} · {label('audience', c.audience)} · {label('nationality', c.nationality)} · {c.namespace}
              </span>
            </div>
            <p className="text-[13px] font-semibold">{c.section_heading ?? <span className="text-muted">(no heading)</span>}</p>
            <FigureText text={c.content} />
            {staged && viewer.canEdit && (mine || viewer.canApprove) ? (
              <ChunkEditor chunk={c} services={services} namespaces={spaces} />
            ) : null}
          </li>
        ))}
      </ol>
      {!chunks.length ? <div className="card card-pad text-[13px] text-muted">This version holds no chunks.</div> : null}
    </Shell>
  );
}
