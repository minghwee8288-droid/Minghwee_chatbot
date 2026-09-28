import Link from 'next/link';
import { Pill, Shell } from '@/components/Shell';
import { requireViewer } from '@/lib/auth';
import { FILTERS, PAGE_SIZE, filterOptions, rows, type FilterKey } from '@/lib/queries';

type Params = Record<string, string | string[] | undefined>;

const LABELS: Record<FilterKey, string> = {
  source_document: 'Document',
  service_type: 'Service',
  contact_type: 'Audience',
  nationality: 'Nationality',
  chunk_type: 'Type',
  managed_by: 'Managed by',
};

function one(v: string | string[] | undefined): string {
  return (Array.isArray(v) ? v[0] : v) ?? '';
}

export default async function RowsPage({ searchParams }: { searchParams: Params }) {
  const viewer = await requireViewer();
  const options = await filterOptions();

  // Only values that actually exist are used as filters; anything else is ignored.
  const filters: Partial<Record<FilterKey, string>> = {};
  for (const key of Object.keys(FILTERS) as FilterKey[]) {
    const value = one(searchParams[key]);
    if (value && options[key].includes(value)) filters[key] = value;
  }
  const activeParam = one(searchParams.active);
  const active = activeParam === 'active' || activeParam === 'inactive' ? activeParam : 'all';
  const search = one(searchParams.q).slice(0, 200);
  const page = Math.max(1, Math.min(1000, Number.parseInt(one(searchParams.page) || '1', 10) || 1));

  const list = await rows({ filters, active, search, page });
  const total = list[0]?.total_count ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const link = (p: number) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) if (v) qs.set(k, v);
    if (active !== 'all') qs.set('active', active);
    if (search) qs.set('q', search);
    qs.set('page', String(p));
    return `/rows?${qs.toString()}`;
  };

  return (
    <Shell viewer={viewer} active="/rows">
      <h1 className="mb-3 text-xl font-semibold">Rows</h1>
      <form method="get" className="mb-4 grid gap-2 rounded-lg border border-slate-200 bg-white p-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <label className="flex flex-col gap-1 sm:col-span-2 lg:col-span-4">
          <span className="text-xs text-slate-500">Search question, answer, content and heading</span>
          <input name="q" defaultValue={search} className="rounded-md border border-slate-300 px-2 py-1.5" />
        </label>
        {(Object.keys(FILTERS) as FilterKey[]).map((key) => (
          <label key={key} className="flex flex-col gap-1">
            <span className="text-xs text-slate-500">{LABELS[key]}</span>
            <select name={key} defaultValue={filters[key] ?? ''} className="rounded-md border border-slate-300 px-2 py-1.5">
              <option value="">All</option>
              {options[key].map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label className="flex flex-col gap-1">
          <span className="text-xs text-slate-500">Status</span>
          <select name="active" defaultValue={active} className="rounded-md border border-slate-300 px-2 py-1.5">
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="inactive">Inactive</option>
          </select>
        </label>
        <div className="flex items-end gap-2">
          <button className="rounded-md bg-brand px-3 py-1.5 text-white">Apply</button>
          <Link href="/rows" className="rounded-md border border-slate-300 px-3 py-1.5">
            Clear
          </Link>
        </div>
      </form>

      <p className="mb-2 text-sm text-slate-500">
        {total} matching row{total === 1 ? '' : 's'}
        {total > PAGE_SIZE ? ` · page ${page} of ${pages}` : ''}
      </p>

      <ul className="space-y-2">
        {list.map((r) => (
          <li key={r.id} className="rounded-lg border border-slate-200 bg-white p-3">
            <div className="flex flex-wrap items-start gap-2">
              <Link href={`/rows/${r.id}`} className="font-medium text-brand underline-offset-2 hover:underline">
                {r.question || r.section_heading || '(untitled chunk)'}
              </Link>
              {!r.is_active ? <Pill tone="amber">inactive</Pill> : null}
            </div>
            <p className="mt-1 line-clamp-2 text-sm text-slate-600">{r.snippet}</p>
            <div className="mt-2 flex flex-wrap gap-1">
              <Pill>{r.service_type}</Pill>
              <Pill>{r.contact_type}</Pill>
              <Pill>{r.nationality}</Pill>
              <Pill>{r.chunk_type}</Pill>
              <Pill>{r.source_document ?? '—'}</Pill>
            </div>
          </li>
        ))}
      </ul>

      {pages > 1 ? (
        <div className="mt-4 flex gap-2 text-sm">
          {page > 1 ? (
            <Link href={link(page - 1)} className="rounded-md border border-slate-300 px-3 py-1.5">
              ← Previous
            </Link>
          ) : null}
          {page < pages ? (
            <Link href={link(page + 1)} className="rounded-md border border-slate-300 px-3 py-1.5">
              Next →
            </Link>
          ) : null}
        </div>
      ) : null}
    </Shell>
  );
}
