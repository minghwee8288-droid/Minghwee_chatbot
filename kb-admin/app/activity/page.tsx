import Link from 'next/link';
import { Shell } from '@/components/Shell';
import { documentLabel } from '@/components/labels';
import { PageHeader, Pill, formatDate, previewText, type Tone } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { plural } from '@/lib/plural';
import { activity, type AuditRow } from '@/lib/queries';

/** What each audit action means, in plain words. Unknown actions are shown as given. */
const WHAT: Record<string, { text: string; tone: Tone }> = {
  draft_saved: { text: 'saved a draft of', tone: 'off' },
  discarded: { text: 'discarded a draft of', tone: 'off' },
  published: { text: 'published a change to', tone: 'good' },
  approved: { text: 'approved a change to', tone: 'lock' },
  restored: { text: 'restored an earlier version of', tone: 'warn' },
  toggled: { text: 'switched', tone: 'warn' },
  external_insert: { text: 'added, outside KB Admin,', tone: 'bad' },
  external_update: { text: 'changed, outside KB Admin,', tone: 'bad' },
  external_delete: { text: 'removed, outside KB Admin,', tone: 'bad' },
  entry_created: { text: 'added a new Q&A entry,', tone: 'off' },
  doc_uploaded: { text: 'uploaded a file for', tone: 'off' },
  batch_prepared: { text: 'prepared chunks for review in', tone: 'off' },
  chunk_edited: { text: 'edited a chunk being prepared in', tone: 'off' },
  batch_discarded: { text: 'discarded a version being prepared of', tone: 'off' },
  batch_published: { text: 'published a new version of', tone: 'good' },
  batch_restored: { text: 'restored an earlier version of', tone: 'warn' },
  doc_retired: { text: 'retired (switched off) the whole document', tone: 'bad' },
};

/** The audit action names, in words, for the pill. */
const ACTION_NAME: Record<string, string> = {
  doc_uploaded: 'file uploaded',
  batch_prepared: 'chunks prepared',
  chunk_edited: 'chunk edited',
  batch_discarded: 'version discarded',
  batch_published: 'document published',
  batch_restored: 'version restored',
  doc_retired: 'document retired',
  entry_created: 'entry added',
};

const DOC_ACTIONS = new Set(['doc_uploaded', 'batch_prepared', 'chunk_edited', 'batch_discarded', 'batch_published', 'batch_restored', 'doc_retired']);

/** The numbers a document action reports, in words. */
function docDetail(a: AuditRow): string {
  const n = a.new_values ?? {};
  if (a.action === 'batch_published') {
    const self = n.self_published === true ? ' - published by the person who prepared it' : '';
    return ` (${plural(n.rows_inserted as number, 'chunk')} on, ${n.rows_retired ?? 0} off${n.baseline_batch_id ? '; the imported version kept as an earlier version' : ''}${self})`;
  }
  if (a.action === 'batch_restored') return ` (${plural(n.rows_restored as number, 'chunk')} back on, ${n.rows_retired ?? 0} off)`;
  if (a.action === 'doc_retired') return ` (${plural(n.rows_retired as number, 'chunk')} off)`;
  if (a.action === 'batch_prepared') return ` (${plural(n.chunks_added as number, 'chunk')})`;
  return '';
}

function sentence(a: AuditRow): string {
  const who = a.actor_email ?? `the database login "${a.actor_db_role}"`;
  const what = WHAT[a.action]?.text ?? a.action.replace(/_/g, ' ');
  if (a.action === 'toggled') {
    const on = a.new_values?.active === true;
    return `${who} switched ${on ? 'on' : 'off'}`;
  }
  if (a.action === 'approved' && a.self_approved) return `${who} approved their own change to`;
  if (a.action === 'restored' && typeof a.new_values?.restored_from_version === 'number') {
    return `${who} restored version ${a.new_values.restored_from_version} of`;
  }
  return `${who} ${what}`;
}

export default async function ActivityPage() {
  const [viewer, list] = await requireViewerWith(() => activity());
  return (
    <Shell viewer={viewer} active="/activity">
      <PageHeader title="Activity" sub="The latest 100 changes to the knowledge base, newest first. Times are Singapore time." />
      {list.length ? (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>When</th>
                <th>What happened</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {list.map((a) => (
                <tr key={a.id}>
                  <td className="mono whitespace-nowrap text-[12px]">{formatDate(a.created_at)}</td>
                  <td className="max-w-[520px]">
                    <Pill tone={WHAT[a.action]?.tone ?? 'off'}>{ACTION_NAME[a.action] ?? a.action.replace(/_/g, ' ')}</Pill>{' '}
                    <span>{sentence(a)} </span>
                    {DOC_ACTIONS.has(a.action) ? (
                      <>
                        {a.source_name ? (
                          <Link href={`/documents/history?source=${encodeURIComponent(a.source_name)}`} className="link" title={a.source_name}>
                            {documentLabel(a.source_name)}
                          </Link>
                        ) : (
                          'a document'
                        )}
                        <span className="text-[12px] text-muted">{docDetail(a)}</span>
                      </>
                    ) : a.entry_id ? (
                      <Link href={`/rows/${a.entry_id}?tab=history`} className="link">
                        {previewText(a.question || 'an entry', 90)}
                      </Link>
                    ) : (
                      'an entry that no longer exists'
                    )}
                    {a.action === 'published' && a.new_values?.reembedded === true ? (
                      <span className="text-[12px] text-muted"> (new text, re-indexed for search)</span>
                    ) : null}
                  </td>
                  <td className="max-w-[360px] break-words text-muted">{a.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="card card-pad text-[13px] text-muted">No changes have been recorded yet.</div>
      )}
    </Shell>
  );
}
