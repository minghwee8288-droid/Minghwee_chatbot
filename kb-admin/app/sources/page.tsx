import Link from 'next/link';
import { notFound } from 'next/navigation';
import { redraftSourceAction, removeSource } from '@/app/watch/actions';
import { ReasonAction } from '@/components/ReasonAction';
import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader, Pill, formatDate, type Tone } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { plural } from '@/lib/plural';
import { watchedSources, watcherSchema16 } from '@/lib/watchQueries';
import { CATEGORY_LABELS } from './categories';
import { AddSourceForm, FetchNowButton, WatchSuggestedButton } from './SourceForms';

// "Fetch now" and "Re-draft" fetch / draft in one server action each.
export const maxDuration = 300;

const STATUS: Record<string, { text: string; tone: Tone }> = {
  first: { text: 'Read for the first time', tone: 'good' },
  changed: { text: 'Changed', tone: 'warn' },
  unchanged: { text: 'No change', tone: 'good' },
  failed: { text: 'Fetch failed', tone: 'bad' },
};

const CHECKED: Record<string, string> = {
  first: 'The page was read for the first time.',
  changed: 'The page has changed.',
  unchanged: 'The page has not changed since it was last read, so nothing was drafted.',
  failed: 'The page could not be fetched; the reason is shown against it below.',
};

/**
 * Stage 1: the pages the knowledge-base watcher reads. Off (404) unless the
 * watcher is switched on. Viewers see the list; editors and approvers add,
 * remove and fetch. Nothing here changes what the chatbot answers.
 */
export default async function SourcesPage({ searchParams }: { searchParams: Record<string, string | undefined> }) {
  const [viewer, loaded] = await requireViewerWith(() =>
    Promise.all([watchedSources().catch(() => null), watcherSchema16().catch(() => false)]));
  if (!viewer.watcherOn) notFound();
  const [sources, has16] = loaded;
  const drafts = Number(searchParams.drafts ?? '0');
  const indexPages = Number(searchParams.index ?? '-1');
  const watchedUrls = new Set((sources ?? []).map((s) => s.url.replace(/\/+$/, '')));
  return (
    <Shell viewer={viewer} active="/sources">
      <PageHeader
        title="Watched pages"
        sub="Public pages the watcher reads. When a page's text really changes, it drafts the knowledge-base change for a person to review - nothing goes live without an approver."
      />
      {searchParams.checked && CHECKED[searchParams.checked] ? (
        <AmberNotice title="Fetched">
          {CHECKED[searchParams.checked]}{' '}
          {indexPages >= 0 ? (
            <>
              The page is mainly an index (a list of links to other pages), so nothing was drafted from it.
              {indexPages > 0 ? ` ${plural(indexPages, 'sub-page')} with the facts ${indexPages === 1 ? 'is' : 'are'} listed under it as suggested pages to watch.` : ''}
            </>
          ) : drafts > 0 ? (
            <>
              {plural(drafts, 'proposal')} drafted - <Link className="link" href="/watch">review them</Link>.
            </>
          ) : searchParams.checked === 'failed' || searchParams.checked === 'unchanged' ? null : (
            'Nothing in the change needed a knowledge-base proposal.'
          )}
        </AmberNotice>
      ) : null}
      {searchParams.redrafted ? (
        <AmberNotice title="Re-drafted">
          {plural(Number(searchParams.superseded ?? '0'), 'open proposal')} superseded.{' '}
          {searchParams.index === '1'
            ? 'The page is mainly an index, so nothing was drafted from it.'
            : drafts > 0
              ? <>{plural(drafts, 'proposal')} drafted from the latest snapshot - <Link className="link" href="/watch">review them</Link>.</>
              : 'Nothing on the latest snapshot needed a knowledge-base proposal.'}
        </AmberNotice>
      ) : null}
      {searchParams.added ? <AmberNotice title="Added">The page is now watched. Use Fetch now to read it the first time.</AmberNotice> : null}
      {searchParams.removed ? <AmberNotice title="Removed">The page is no longer watched. Its snapshots and proposals are kept.</AmberNotice> : null}
      {sources === null ? (
        <AmberNotice title="Not set up yet">The watcher's tables are not in this database yet (the watcher migration, 015, has not been applied).</AmberNotice>
      ) : sources.length ? (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Page</th>
                <th>Category</th>
                <th>Last checked</th>
                <th>Result</th>
                <th className="num">Open proposals</th>
                {viewer.canEdit ? <th>Actions</th> : null}
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.id} data-source={s.id}>
                  <td className="max-w-[340px]">
                    <p className="font-medium">{s.label}</p>
                    <a href={s.url} className="link break-all text-[12px]" target="_blank" rel="noreferrer noopener">{s.url}</a>
                  </td>
                  <td>{CATEGORY_LABELS[s.category] ?? s.category}</td>
                  <td className="mono whitespace-nowrap text-[12px]">{s.last_checked_at ? formatDate(s.last_checked_at) : 'never'}</td>
                  <td>
                    {s.last_status ? <Pill tone={STATUS[s.last_status]?.tone ?? 'off'}>{STATUS[s.last_status]?.text ?? s.last_status}</Pill> : <span className="text-muted">-</span>}
                    {s.last_status === 'failed' && s.last_error ? <p className="mt-1 text-[12px]" style={{ color: 'var(--bad)' }}>{s.last_error}</p> : null}
                    {s.suggested_pages?.length ? (
                      <div className="mt-2 space-y-1.5" data-suggested={s.id}>
                        <p className="text-[12px] font-medium">Suggested pages to watch</p>
                        <p className="text-[12px] text-muted">This page is mainly an index, so nothing is drafted from it. The facts are on these pages:</p>
                        <ul className="space-y-1.5">
                          {s.suggested_pages.map((p) => (
                            <li key={p.url} className="text-[12px]">
                              <a href={p.url} className="link break-all" target="_blank" rel="noreferrer noopener">{p.label}</a>
                              {watchedUrls.has(p.url.replace(/\/+$/, '')) ? (
                                <span className="ml-2 text-muted">already watched</span>
                              ) : viewer.canEdit ? (
                                <span className="ml-2 inline-block align-middle"><WatchSuggestedButton sourceId={s.id} url={p.url} /></span>
                              ) : null}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </td>
                  <td className="num mono">{s.open_drafts}</td>
                  {viewer.canEdit ? (
                    <td className="space-y-2">
                      <FetchNowButton sourceId={s.id} />
                      {viewer.canApprove && has16 && s.last_checked_at ? (
                        <ReasonAction
                          action={redraftSourceAction}
                          hidden={{ source_id: s.id }}
                          openLabel="Re-draft from latest snapshot"
                          submitLabel="Re-draft"
                          busyLabel="Drafting…"
                          help="The page's open proposals are superseded and drafted again from its latest snapshot with the current drafting rules. Nothing is fetched and nothing goes live. Recorded in the activity log with your reason."
                        />
                      ) : null}
                      <ReasonAction
                        action={removeSource}
                        hidden={{ source_id: s.id }}
                        openLabel="Stop watching"
                        submitLabel="Stop watching"
                        help="The page stops being fetched. Its snapshots and proposals are kept."
                      />
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="card card-pad text-[13px] text-muted">No page is watched yet.</div>
      )}
      {viewer.canEdit && sources !== null ? <AddSourceForm /> : null}
    </Shell>
  );
}
