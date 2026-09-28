import Link from 'next/link';
import { Pill, Shell } from '@/components/Shell';
import { requireViewer } from '@/lib/auth';
import { documents, totals } from '@/lib/queries';

export default async function DocumentsPage() {
  const viewer = await requireViewer();
  const [docs, sum] = await Promise.all([documents(), totals()]);
  return (
    <Shell viewer={viewer} active="/documents">
      <div className="mb-4 flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Documents</h1>
        <span className="text-sm text-slate-500">
          {sum.total} rows · {sum.active} active · {sum.inactive} inactive
        </span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
            <tr>
              <th className="px-3 py-2">Source document</th>
              <th className="px-3 py-2 text-right">Rows</th>
              <th className="px-3 py-2 text-right">Active</th>
              <th className="px-3 py-2 text-right">Inactive</th>
              <th className="px-3 py-2">Services</th>
              <th className="px-3 py-2">Last changed</th>
            </tr>
          </thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.source_document} className="border-t border-slate-100 align-top">
                <td className="px-3 py-2">
                  <Link
                    href={`/rows?source_document=${encodeURIComponent(d.source_document)}`}
                    className="text-brand underline-offset-2 hover:underline"
                  >
                    {d.source_document}
                  </Link>
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{d.total}</td>
                <td className="px-3 py-2 text-right tabular-nums">{d.active}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {d.inactive ? <Pill tone="amber">{d.inactive}</Pill> : 0}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {d.services.map((s) => (
                      <Pill key={s}>{s}</Pill>
                    ))}
                  </div>
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-slate-500">{d.last_changed.slice(0, 16)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Shell>
  );
}
