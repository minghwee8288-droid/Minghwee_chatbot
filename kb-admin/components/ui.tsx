/** Small presentation pieces shared by the pages. No data access here. */

export type Tone = 'good' | 'warn' | 'bad' | 'off' | 'lock';

export function Pill({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

export function PageHeader({ title, sub }: { title: string; sub?: React.ReactNode }) {
  return (
    <header>
      <h1 className="page-title">{title}</h1>
      {sub ? <p className="page-sub">{sub}</p> : null}
    </header>
  );
}

export function AmberNotice({ title, children }: { title?: string; children?: React.ReactNode }) {
  return (
    <div className="notice notice-amber" role="note">
      <span className="notice-dot" aria-hidden="true" />
      <div>
        {title ? <p className="notice-head">{title}</p> : null}
        {children ? <div>{children}</div> : null}
      </div>
    </div>
  );
}

export function RedNotice({ children }: { children: React.ReactNode }) {
  return (
    <div className="notice notice-red" role="alert">
      {children}
    </div>
  );
}

export function ActivePill({ active }: { active: boolean }) {
  return active ? <Pill tone="good">Active</Pill> : <Pill tone="off">Inactive</Pill>;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "2026-09-24 12:44:53.59+00" -> "24 Sep 2026, 12:44 UTC". Anything else is shown as given. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const m = value.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return value;
  const zone = /(\+00(:?00)?|Z)$/.test(value) ? ' UTC' : '';
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1] ?? m[2]} ${m[1]}, ${m[4]}:${m[5]}${zone}`;
}

/** Plain-English names for the entry types; unknown values fall back to the raw code. */
export const ENTRY_TYPES: Record<string, string> = {
  qa_pair: 'Question and answer',
  document_chunk: 'Document passage',
  table_unit: 'Table',
};

/** Who maintains an entry; unknown values fall back to the raw code. */
export const MAINTAINED_BY: Record<string, string> = {
  loader: 'Imported by the loader',
  ui: 'KB Admin',
  '(none)': 'Not set',
};
