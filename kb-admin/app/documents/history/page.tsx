import Link from 'next/link';
import { restoreBatch, retireDocument } from '@/app/docs/actions';
import { ConfirmAction } from '@/components/ConfirmAction';
import { Shell } from '@/components/Shell';
import { BATCH_STATUS, documentLabel } from '@/components/labels';
import { AmberNotice, PageHeader, Pill, formatDate } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { plural } from '@/lib/plural';
import { batchesOfSource, documentBySource, liveDocRows, qaPairCount } from '@/lib/queries';

/** Every version of one document, newest first: restore an earlier one, or retire the document. */
export default async function HistoryPage({ searchParams }: { searchParams: Record<string, string | undefined> }) {
  const source = (searchParams.source ?? '').slice(0, 500);
  // All four at once, with the sign-in check: the versions are read by source name, not after the document.
  const [viewer, [doc, live, qa, versions]] = await requireViewerWith(() =>
    Promise.all([documentBySource(source), liveDocRows(source), qaPairCount(source), batchesOfSource(source)]),
  );
  return (
    <Shell viewer={viewer} active="/documents">
      <PageHeader
        title={source ? documentLabel(source) : 'Document history'}
        sub={`${plural(live.length, 'chunk')} live now${qa ? ` · ${plural(qa, 'Q&A entry')} from this source, managed separately` : ''}`}
      />
      <p className="text-[13px]">
        <Link href="/documents" className="link">
          ← Knowledge base
        </Link>
      </p>
      {searchParams.restored ? (
        <AmberNotice title="Restored">
          {plural(searchParams.restored, 'chunk')} switched back on, {plural(searchParams.off, 'chunk')} switched off.
        </AmberNotice>
      ) : null}
      {searchParams.retired ? <AmberNotice title="Retired">{plural(searchParams.retired, 'chunk')} switched off. Restore an earlier version to bring the document back.</AmberNotice> : null}
      {searchParams.discarded ? <AmberNotice title="Discarded">The batch was discarded. The live document did not change.</AmberNotice> : null}

      <div className="flex flex-wrap gap-3">
        {viewer.canEdit && source ? (
          <Link href={`/documents/upload?source=${encodeURIComponent(source)}`} className="btn btn-secondary">
            Upload a new version
          </Link>
        ) : null}
        {viewer.canApprove && doc && live.length ? (
          <ConfirmAction
            action={retireDocument}
            hidden={{ document_id: doc.id, source }}
            openLabel="Retire document"
            submitLabel="Retire"
            needReason
            confirmText={`Switch off ${live.length === 1 ? 'the live chunk' : `all ${plural(live.length, 'live chunk')}`} of this document. Its Q&A entries are not touched.`}
            help="The chatbot stops using this document straight away. An earlier version can be restored afterwards."
          />
        ) : null}
      </div>

      {!doc ? (
        <div className="card card-pad text-[13px] text-muted">
          This document was imported before KB Admin. It has no versions here yet; the first upload records the imported chunks as an earlier version you can restore.
        </div>
      ) : (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Version</th>
                <th>Status</th>
                <th className="num">Chunks</th>
                <th>Prepared</th>
                <th>Published</th>
                <th>Reason</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {versions.map((v) => {
                const st = BATCH_STATUS[v.status] ?? { text: v.status, tone: 'off' as const };
                return (
                  <tr key={v.id} data-batch={v.id}>
                    <td>
                      <Link href={`/documents/batches/${v.id}`} className="link mono">
                        {v.id.slice(0, 8)}
                      </Link>
                      {v.is_baseline ? (
                        <span className="ml-2">
                          <Pill tone="lock">Imported original</Pill>
                        </span>
                      ) : null}
                    </td>
                    <td>
                      <Pill tone={st.tone}>{st.text}</Pill>
                    </td>
                    <td className="num mono">{v.chunk_count}</td>
                    <td className="text-[12px]">
                      {v.prepared_by_email}
                      <br />
                      <span className="mono text-muted">{formatDate(v.prepared_at)}</span>
                    </td>
                    <td className="text-[12px]">
                      {v.published_by_email ?? '—'}
                      <br />
                      <span className="mono text-muted">{v.published_at ? formatDate(v.published_at) : ''}</span>
                    </td>
                    <td className="max-w-[260px] break-words text-[12px]">{v.reason ?? ''}</td>
                    <td>
                      {v.status === 'superseded' && viewer.canApprove ? (
                        <ConfirmAction
                          action={restoreBatch}
                          hidden={{ batch_id: v.id }}
                          openLabel="Restore"
                          submitLabel="Restore this version"
                          needReason
                          help="Switches this version's chunks back on and switches off what is live for this document now."
                        />
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Shell>
  );
}
