import Link from 'next/link';
import { Shell } from '@/components/Shell';
import { documentLabel } from '@/components/labels';
import { PageHeader, Pill } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { documents, totals } from '@/lib/queries';

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
  const [docs, sum] = await Promise.all([documents(), totals()]);
  const docCount = docs.length;
  return (
    <Shell viewer={viewer} active="/documents">
      <PageHeader
        title="Knowledge base"
        sub={`${sum.active.toLocaleString('en-SG')} active ${sum.active === 1 ? 'entry' : 'entries'} across ${docCount} ${
          docCount === 1 ? 'document' : 'documents'
        } · the chatbot reads these live.`}
      />

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
            </colgroup>
            <thead>
              <tr>
                <th scope="col">Document name</th>
                <th scope="col" className="num">Entries</th>
                <th scope="col" className="num">Active</th>
                <th scope="col" className="num">Inactive</th>
                <th scope="col">Status</th>
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
