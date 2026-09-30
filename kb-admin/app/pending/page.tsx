import Link from 'next/link';
import { Shell } from '@/components/Shell';
import { label } from '@/components/labels';
import { AmberNotice, PageHeader, Pill, formatDate, previewText } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { APPROVAL_WORDS, approvalReasons } from '@/lib/editing';
import { openDrafts } from '@/lib/queries';

/** Every open draft. Approvers open one and publish it; the list itself changes nothing. */
export default async function PendingPage() {
  const viewer = await requireViewer();
  if (!viewer.canApprove) {
    return (
      <Shell viewer={viewer} active="/pending">
        <PageHeader title="Pending approval" />
        <AmberNotice title="For approvers">Only an approver can see and publish pending drafts.</AmberNotice>
      </Shell>
    );
  }
  const drafts = await openDrafts();
  return (
    <Shell viewer={viewer} active="/pending">
      <PageHeader title="Pending approval" sub={`${drafts.length} open draft${drafts.length === 1 ? '' : 's'}, oldest first.`} />
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
