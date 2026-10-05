import Link from 'next/link';
import { retireDocument } from '@/app/docs/actions';
import { ConfirmAction } from '@/components/ConfirmAction';
import { Shell } from '@/components/Shell';
import { BATCH_STATUS, documentLabel } from '@/components/labels';
import { PageHeader, Pill } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { sourceProblem } from '@/lib/documents';
import { sourcesOverview, totals } from '@/lib/queries';

function Stat({ label, value, warn = false }: { label: string; value: number; warn?: boolean }) {
  return (
    <div className="card card-pad">
      <p className="stat-label">{label}</p>
      <p className="stat-value mono" style={warn ? { color: 'var(--amber-dot)' } : undefined}>
        {value.toLocaleString('en-SG')}
      </p>
    </div>
  );
}

function DocStatus({ active, inactive }: { active: number; inactive: number }) {
  if (inactive === 0) return <Pill tone="good">Live</Pill>;
  if (active === 0) return <Pill tone="bad">Not live</Pill>;
  return <Pill tone="warn">Partly live</Pill>;
}

export default async function DocumentsPage() {
  const viewer = await requireViewer();
  const [docs, sum] = await Promise.all([sourcesOverview(), totals()]);
  const docCount = docs.length;
  return (
    <Shell viewer={viewer} active="/documents">
      <PageHeader
        title="Knowledge base"
        sub={`${sum.active.toLocaleString('en-SG')} active ${sum.active === 1 ? 'entry' : 'entries'} across ${docCount} ${
          docCount === 1 ? 'document' : 'documents'
        } · the chatbot reads these live.`}
      />
      {viewer.canEdit ? (
        <div className="flex gap-3">
          <Link href="/rows/new" className="btn btn-primary">
            Add Q&amp;A
          </Link>
          <Link href="/documents/upload" className="btn btn-primary">
            Upload document
          </Link>
        </div>
      ) : null}

      <div className="grid grid-cols-4 gap-4">
        <Stat label="Active entries" value={sum.active} />
        <Stat label="Inactive entries" value={sum.inactive} warn={sum.inactive > 0} />
        <Stat label="Documents" value={docCount} />
        <Stat label="Total entries" value={sum.total} />
      </div>

      <div>
        <div className="card tbl-wrap">
          <table className="tbl" style={{ tableLayout: 'fixed' }}>
            <colgroup>
              <col />
              <col style={{ width: 100 }} />
              <col style={{ width: 90 }} />
              <col style={{ width: 90 }} />
              <col style={{ width: 130 }} />
              <col style={{ width: 150 }} />
              <col style={{ width: 230 }} />
            </colgroup>
            <thead>
              <tr>
                <th scope="col">Document name</th>
                <th scope="col" className="num">Entries</th>
                <th scope="col" className="num">Active</th>
                <th scope="col" className="num">Inactive</th>
                <th scope="col">Status</th>
                <th scope="col">Latest version</th>
                <th scope="col">Versions</th>
              </tr>
            </thead>
            <tbody>
              {docs.map((d) => {
                const name = d.source_document || 'Untitled document';
                return (
                  <tr key={name}>
                    <td className="truncate-cell">
                      <Link
                        href={`/rows?source_document=${encodeURIComponent(d.source_document)}`}
                        className="link block truncate font-medium"
                        title={name}
                      >
                        {d.source_document ? documentLabel(d.source_document) : name}
                      </Link>
                    </td>
                    <td className="num mono">{d.total}</td>
                    <td className="num mono">{d.active}</td>
                    <td className="num mono" style={{ color: d.inactive ? 'var(--amber-dot)' : 'var(--faint)' }}>
                      {d.inactive}
                    </td>
                    <td>
                      <DocStatus active={d.active} inactive={d.inactive} />
                    </td>
                    <td>
                      {d.latest_status ? (
                        <Link href={`/documents/batches/${d.latest_batch_id}`}>
                          <Pill tone={BATCH_STATUS[d.latest_status]?.tone ?? 'off'}>{BATCH_STATUS[d.latest_status]?.text ?? d.latest_status}</Pill>
                        </Link>
                      ) : (
                        <span className="text-[12px] text-muted">{d.total > d.qa_pairs ? 'Imported' : 'Q&A only'}</span>
                      )}
                    </td>
                    <td className="text-[12px]">
                      {d.source_document && (d.total > d.qa_pairs || d.document_id) ? (
                        <div className="flex flex-wrap items-center gap-2">
                          <Link href={`/documents/history?source=${encodeURIComponent(d.source_document)}`} className="link">
                            History
                          </Link>
                          {viewer.canEdit && !sourceProblem(d.source_document) ? (
                            <Link href={`/documents/upload?source=${encodeURIComponent(d.source_document)}`} className="link">
                              Replace
                            </Link>
                          ) : null}
                          {viewer.canApprove && d.document_id && d.active > d.qa_pairs ? (
                            <ConfirmAction
                              action={retireDocument}
                              hidden={{ document_id: d.document_id, source: d.source_document }}
                              openLabel="Retire"
                              submitLabel="Retire"
                              needReason
                              confirmText="Switch off every live chunk of this document. Its Q&A entries are not touched."
                            />
                          ) : null}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="mt-2.5 text-[12px] text-muted">Only active entries can be used by the chatbot.</p>
      </div>
    </Shell>
  );
}
