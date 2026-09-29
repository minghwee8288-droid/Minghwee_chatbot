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

const SGT = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Singapore',
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  hourCycle: 'h23',
});

/**
 * A database timestamp in Singapore time: "2026-09-24 12:44:53.59+00" ->
 * "24 Sep 2026, 8:44 pm SGT". Anything that is not a timestamp is shown as given.
 */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const iso = value
    .trim()
    .replace(' ', 'T')
    .replace(/([+-]\d{2})$/, '$1:00');
  const when = new Date(iso);
  if (!/^\d{4}-\d{2}-\d{2}/.test(value) || Number.isNaN(when.getTime())) return value;
  const part: Record<string, number> = {};
  for (const p of SGT.formatToParts(when)) if (p.type !== 'literal') part[p.type] = Number(p.value);
  const hour12 = part.hour % 12 || 12;
  const minute = String(part.minute).padStart(2, '0');
  return `${part.day} ${MONTHS[part.month - 1]} ${part.year}, ${hour12}:${minute} ${part.hour < 12 ? 'am' : 'pm'} SGT`;
}

/**
 * Entry text for a one-line preview: markdown marks (** * # | and a leading "- ")
 * removed, then cut at a whole word with "…". The full text is untouched elsewhere.
 */
export function previewText(text: string | null | undefined, max = 180, alreadyCut = false): string {
  if (!text) return '';
  const lines = text.split(/\r?\n/).map((line) =>
    line
      .replace(/^\s*#{1,6}\s+/, '')
      .replace(/^\s*[-*•]\s+/, '')
      .replace(/\*+/g, '')
      .replace(/\|/g, ' ')
      .replace(/(^|\s):?-{3,}:?(?=\s|$)/g, ' '),
  );
  const flat = lines.join(' ').replace(/\s+/g, ' ').trim();
  if (flat.length <= max && !alreadyCut) return flat;
  const head = flat.slice(0, max + 1);
  const space = head.lastIndexOf(' ');
  const cut = (space > max * 0.6 ? head.slice(0, space) : flat.slice(0, max)).replace(/[\s.,;:·–—-]+$/, '');
  return `${cut}…`;
}

/** The pricing policy for a service, as a pill. Unknown values are shown as given. */
export function PolicyPill({ value }: { value: string }) {
  if (value === 'stated') return <Pill tone="good">Quotes the fee</Pill>;
  if (value === 'withheld') return <Pill tone="off">Passes to agent</Pill>;
  return <span className="mono font-semibold">{value}</span>;
}
