import Link from 'next/link';
import { Shell } from '@/components/Shell';
import { PageHeader, Pill, formatDate, previewText, type Tone } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
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
};

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
  const viewer = await requireViewer();
  const list = await activity();
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
                    <Pill tone={WHAT[a.action]?.tone ?? 'off'}>{a.action.replace(/_/g, ' ')}</Pill>{' '}
                    <span>{sentence(a)} </span>
                    {a.entry_id ? (
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
