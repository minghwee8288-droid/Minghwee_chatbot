import Link from 'next/link';
import { Shell } from '@/components/Shell';
import { documentLabel, label } from '@/components/labels';
import { AmberNotice, PageHeader, Pill, formatDate, previewText } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { APPROVAL_WORDS, approvalReasons } from '@/lib/editing';
import { plural } from '@/lib/plural';
import { batchesAwaitingApproval, openDrafts } from '@/lib/queries';

/** Every open draft, and every document version whose impact check is recorded. Approvers open one and publish it; the list itself changes nothing. */
export default async function PendingPage() {
  // Started with the sign-in check; a non-approver is shown none of it.
  const [viewer, [drafts, batches]] = await requireViewerWith(() => Promise.all([openDrafts(), batchesAwaitingApproval()]));
  if (!viewer.canApprove) {
    return (
      <Shell viewer={viewer} active="/pending">
        <PageHeader title="Pending approval" />
        <AmberNotice title="For approvers">Only an approver can see and publish pending drafts.</AmberNotice>
      </Shell>
    );
  }
  return (
    <Shell viewer={viewer} active="/pending">
      <PageHeader
        title="Pending approval"
        sub={`${plural(drafts.length, 'open draft')} and ${plural(batches.length, 'document version')}, oldest first.`}
      />
      <h2 className="text-[15px] font-semibold">Documents checked and waiting to be published</h2>
      {batches.length ? (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Document</th>
                <th className="num">Chunks</th>
                <th>Prepared by</th>
                <th>Impact check</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((b) => (
                <tr key={b.id} data-batch={b.id}>
                  <td>
                    <Link href={`/documents/batches/${b.id}`} className="link font-medium" title={b.source_name}>
                      {documentLabel(b.source_name)}
                    </Link>
                  </td>
                  <td className="num mono">{b.chunk_count}</td>
                  <td>{b.prepared_by_email}</td>
                  <td className="mono whitespace-nowrap text-[12px]">{formatDate(b.impact_check_run_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="card card-pad text-[13px] text-muted">No document version is waiting.</div>
      )}
      <h2 className="text-[15px] font-semibold">Q&amp;A drafts</h2>
      {drafts.length ? (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Entry</th>
                <th>Why it needs review</th>
                <th>Started by</th>
                <th>When</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {drafts.map((d) => {
                const why = approvalReasons(
                  {
                    question: d.live_question,
                    answer: d.live_answer,
                    section_heading: d.live_section_heading,
                    service: d.live_service,
                    audience: d.live_audience,
                    nationality: d.live_nationality,
                    active: d.live_active,
                  },
                  d,
                );
                return (
                  <tr key={d.id}>
                    <td className="max-w-[380px]">
                      <Link href={`/drafts/${d.id}`} className="link font-medium">
                        {previewText(d.question || d.live_question || 'Untitled entry', 110)}
                      </Link>
                      <p className="mt-0.5 text-[12px] text-muted">{label('service', d.service)}</p>
                    </td>
                    <td>
                      {why.length ? (
                        <>
                          <Pill tone="lock">Needs an approver</Pill>
                          <p className="mt-1 text-[12px] text-muted">{why.map((w) => APPROVAL_WORDS[w]).join(', ')}</p>
                        </>
                      ) : (
                        <Pill tone="off">Wording only</Pill>
                      )}
                    </td>
                    <td>{d.created_by_email ?? '—'}</td>
                    <td className="mono whitespace-nowrap text-[12px]">{formatDate(d.created_at)}</td>
                    <td className="max-w-[320px] break-words">{d.reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="card card-pad text-[13px] text-muted">Nothing is waiting. Every draft has been published or discarded.</div>
      )}
    </Shell>
  );
}
