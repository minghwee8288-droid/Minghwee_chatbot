import Link from 'next/link';
import { UnverifiedBadge } from '@/app/sources/UnverifiedBadge';
import { detectLanguage } from '@/lib/watcher/language';
import { notFound } from 'next/navigation';
import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader, Pill, formatDate, previewText } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { plural } from '@/lib/plural';
import { watchQueue, type WatchDraftRow } from '@/lib/watchQueries';
import { DRAFT_STATUS } from './status';

function Rows({ drafts }: { drafts: WatchDraftRow[] }) {
  return (
    <div className="card tbl-wrap">
      <table className="tbl">
        <thead>
          <tr>
            <th>Proposal</th>
            <th>Page</th>
            <th>Status</th>
            <th>Drafted</th>
          </tr>
        </thead>
        <tbody>
          {drafts.map((d) => (
            <tr key={d.id} data-draft={d.id}>
              <td className="max-w-[380px]">
                <Link href={`/watch/${d.id}`} className="link font-medium">{previewText(d.proposed_question, 120)}</Link>
                <p className="text-[12px] text-muted">
                  {d.kind === 'new' ? 'New entry' : 'Change to an existing entry'}
                  {d.conflict_check && (d.status === 'pending' || d.status === 'blocked') ? <span style={{ color: 'var(--bad)' }}> - disagrees with an existing entry</span> : null}
                </p>
              </td>
              <td className="max-w-[220px] text-[13px]">
                {d.source_label}
                {d.source_unverified ? <div className="mt-1"><UnverifiedBadge /></div> : null}
                {!detectLanguage(d.source_passage).english ? <p className="text-[12px] text-muted">Translated from {detectLanguage(d.source_passage).name}</p> : null}
              </td>
              <td><Pill tone={DRAFT_STATUS[d.status]?.tone ?? 'off'}>{DRAFT_STATUS[d.status]?.text ?? d.status}</Pill></td>
              <td className="mono whitespace-nowrap text-[12px]">{formatDate(d.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Stage 3: the review queue. Listing changes nothing; each proposal opens on its own screen. */
export default async function WatchQueuePage() {
  const [viewer, queue] = await requireViewerWith(() => watchQueue().catch(() => null));
  if (!viewer.watcherOn) notFound();
  if (queue === null) {
    return (
      <Shell viewer={viewer} active="/watch">
        <PageHeader title="Page changes to review" />
        <AmberNotice title="Not set up yet">The watcher's tables are not in this database yet (the watcher migration, 015, has not been applied).</AmberNotice>
      </Shell>
    );
  }
  return (
    <Shell viewer={viewer} active="/watch">
      <PageHeader
        title="Page changes to review"
        sub={`${plural(queue.open.length, 'open proposal')}. Each shows what changed on the page and the knowledge-base change it implies. Only an approver can approve; a proposal touching an agency fee can never be approved here.`}
      />
      {queue.open.length ? <Rows drafts={queue.open} /> : <div className="card card-pad text-[13px] text-muted">Nothing is waiting for review.</div>}
      {queue.recent.length ? (
        <>
          <h2 className="text-[15px] font-semibold">Recently decided</h2>
          <Rows drafts={queue.recent} />
        </>
      ) : null}
    </Shell>
  );
}
