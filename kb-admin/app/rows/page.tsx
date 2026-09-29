import Link from 'next/link';
import { Shell } from '@/components/Shell';
import { label, type LabelKind } from '@/components/labels';
import { ActivePill, PageHeader, previewText } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { FILTERS, PAGE_SIZE, filterOptions, rows, type FilterKey } from '@/lib/queries';

type Params = Record<string, string | string[] | undefined>;

const LABELS: Record<FilterKey, string> = {
  source_document: 'Document',
  service_type: 'Service',
  contact_type: 'Audience',
  nationality: 'Nationality',
  chunk_type: 'Entry type',
  managed_by: 'Maintained by',
};

/** Which label set each dropdown reads. The value sent is always the raw code. */
const OPTION_KIND: Partial<Record<FilterKey, LabelKind>> = {
  service_type: 'service',
  contact_type: 'audience',
  nationality: 'nationality',
  chunk_type: 'entryType',
  managed_by: 'maintainedBy',
};

function optionName(key: FilterKey, value: string): string {
  const kind = OPTION_KIND[key];
  return kind ? label(kind, value) : value;
}

/** The preview the query returns is cut at this many characters (lib/queries.ts). */
const SNIPPET_CHARS = 220;

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

  const query = (p: number | null) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) if (v) qs.set(k, v);
    if (active !== 'all') qs.set('active', active);
    if (search) qs.set('q', search);
    if (p !== null) qs.set('page', String(p));
    return qs.toString();
  };
  const link = (p: number) => `/rows?${query(p)}`;
  const here = query(page);
  const filtered = Object.keys(filters).length > 0 || active !== 'all' || Boolean(search);
  const entries = (n: number) => `${n.toLocaleString('en-SG')} ${n === 1 ? 'entry' : 'entries'}`;

  return (
    <Shell viewer={viewer} active="/rows">
      <PageHeader
        title="Browse entries"
        sub={filtered ? `${entries(total)} match your filters` : `${entries(total)} in the knowledge base`}
      />

      <form method="get" className="card card-pad" aria-label="Filter entries">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[260px] flex-[2_1_300px]">
            <label htmlFor="f-q" className="field-label mb-1.5 block">
              Search
            </label>
            <input
              id="f-q"
              name="q"
              type="search"
              defaultValue={search}
              placeholder="Search the text…"
              className="input"
            />
          </div>
          {(Object.keys(FILTERS) as FilterKey[]).map((key) => (
            <div key={key} className="min-w-[140px] flex-[1_1_150px]">
              <label htmlFor={`f-${key}`} className="field-label mb-1.5 block">
                {LABELS[key]}
              </label>
              <select id={`f-${key}`} name={key} defaultValue={filters[key] ?? ''} className="select">
                <option value="">All</option>
                {options[key].map((v) => (
                  <option key={v} value={v}>
                    {optionName(key, v)}
                  </option>
                ))}
              </select>
            </div>
          ))}
          <div className="min-w-[120px] flex-[1_1_130px]">
            <label htmlFor="f-active" className="field-label mb-1.5 block">
              Status
            </label>
            <select id="f-active" name="active" defaultValue={active} className="select">
              <option value="all">All</option>
              <option value="active">Active</option>
              <option value="inactive">Inactive</option>
            </select>
          </div>
          <div className="flex gap-2">
            <button type="submit" className="btn btn-primary">
              Apply
            </button>
            {filtered ? (
              <Link href="/rows" className="btn btn-secondary">
                Clear
              </Link>
            ) : null}
          </div>
        </div>
      </form>

      {list.length === 0 ? (
        <div className="card card-pad text-center">
          <p className="font-medium">No entries match these filters.</p>
          <p className="mt-1 text-[13px] text-muted">
            Try a shorter search or fewer filters, or{' '}
            <Link href="/rows" className="link">
              clear all filters
            </Link>
            .
          </p>
        </div>
      ) : (
        <div className="card tbl-wrap">
          <table className="tbl" style={{ tableLayout: 'fixed' }}>
            <colgroup>
              <col style={{ width: 58 }} />
              <col />
              <col style={{ width: 200 }} />
              <col style={{ width: 140 }} />
              <col style={{ width: 100 }} />
              <col style={{ width: 96 }} />
            </colgroup>
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Entry</th>
                <th scope="col">Document</th>
                <th scope="col">Service</th>
                <th scope="col">Audience</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {list.map((r, i) => {
                const heading = r.question || r.section_heading;
                const preview = previewText(r.snippet, 180, r.snippet.length >= SNIPPET_CHARS);
                return (
                  <tr key={r.id}>
                    <td className="mono text-[12px] text-muted">{(page - 1) * PAGE_SIZE + i + 1}</td>
                    <td className="truncate-cell">
                      <Link href={`/rows/${r.id}${here ? `?${here}` : ''}`} className="block">
                        <span className="link block truncate font-semibold" title={heading ?? undefined}>
                          {heading || 'Untitled passage'}
                        </span>
                        {preview ? (
                          <span className="mt-0.5 line-clamp-2 text-[12px] leading-[1.5] text-muted">{preview}</span>
                        ) : null}
                      </Link>
                    </td>
                    <td className="truncate-cell">
                      <span className="block truncate text-muted" title={r.source_document ?? undefined}>
                        {r.source_document || '—'}
                      </span>
                    </td>
                    <td className="truncate-cell">
                      <span className="block truncate" title={r.service_type}>
                        {label('service', r.service_type)}
                      </span>
                    </td>
                    <td>
                      <span title={r.contact_type}>{label('audience', r.contact_type)}</span>
                    </td>
                    <td>
                      <ActivePill active={r.is_active} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <nav
            className="flex items-center justify-between gap-3 border-t px-4 py-3"
            style={{ borderColor: 'var(--divider)' }}
            aria-label="Pages"
          >
            <p className="text-[13px] text-muted">
              Page <span className="mono">{page}</span> of <span className="mono">{pages}</span> · {entries(total)}
            </p>
            <div className="flex gap-2">
              {page > 1 ? (
                <Link href={link(page - 1)} className="btn btn-secondary">
                  ← Previous
                </Link>
              ) : (
                <span className="btn btn-disabled" aria-disabled="true">
                  ← Previous
                </span>
              )}
              {page < pages ? (
                <Link href={link(page + 1)} className="btn btn-secondary">
                  Next →
                </Link>
              ) : (
                <span className="btn btn-disabled" aria-disabled="true">
                  Next →
                </span>
              )}
            </div>
          </nav>
        </div>
      )}
    </Shell>
  );
}
