import { Shell } from '@/components/Shell';
import { label } from '@/components/labels';
import { AmberNotice, PageHeader, Pill, PolicyPill, formatDate } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { rules, type Rule } from '@/lib/queries';

type Group = {
  title: string;
  explain: string;
  subjectLabel: string;
  valueLabel: string;
  /** What the rule is about: a service, a nationality or a contact item. */
  subject: (r: Rule) => React.ReactNode;
  value: (r: Rule) => React.ReactNode;
};

const CONTACT_ITEMS: Record<string, string> = {
  office_address: 'Office address',
  whatsapp_number: 'WhatsApp number',
};

/** Any rule value as plain text - never as JSON. */
function plain(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (Array.isArray(value)) return value.map(plain).join(' · ');
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => `${k}: ${plain(v)}`)
      .join(' · ');
  }
  return String(value);
}

const Mono = ({ children }: { children: React.ReactNode }) => <span className="mono font-semibold">{children}</span>;
const Code = ({ children }: { children: React.ReactNode }) => <span className="mono text-[12px]">{children}</span>;
const Service = ({ code }: { code: string }) => <span title={code}>{label('service', code)}</span>;

const POLICY: Record<string, string> = {
  stated: 'The chatbot quotes the fee',
  withheld: 'The chatbot passes fee questions to an agent',
};

const GROUPS: Record<string, Group> = {
  price_policy: {
    title: 'Pricing policy',
    explain: 'Whether the chatbot may quote the fee for each service, or must leave it to an agent.',
    subjectLabel: 'Service',
    valueLabel: 'Policy',
    subject: (r) => <Service code={r.service_type} />,
    value: (r) =>
      typeof r.value === 'string' ? (
        <>
          <PolicyPill value={r.value} />
          {POLICY[r.value] ? <span className="ml-2 text-[12px] text-muted">{POLICY[r.value]}</span> : null}
        </>
      ) : (
        <Mono>{plain(r.value)}</Mono>
      ),
  },
  price_nationality: {
    title: 'Fees by nationality',
    explain: 'For services priced by nationality, the nationalities we hold a fee for.',
    subjectLabel: 'Service',
    valueLabel: 'Nationalities with a fee',
    subject: (r) => <Service code={r.service_type} />,
    value: (r) =>
      Array.isArray(r.value) ? (
        <span title={r.value.map(String).join(', ')}>{r.value.map((c) => label('nationality', String(c))).join(', ')}</span>
      ) : (
        <Mono>{plain(r.value)}</Mono>
      ),
  },
  salary_floor: {
    title: 'Minimum salary',
    explain: 'The lowest monthly salary a helper of each nationality can be placed at.',
    subjectLabel: 'Nationality',
    valueLabel: 'Minimum monthly salary',
    subject: (r) => <span title={r.nationality}>{label('nationality', r.nationality)}</span>,
    value: (r) => <Mono>{typeof r.value === 'number' ? `S$${r.value.toLocaleString('en-SG')}` : plain(r.value)}</Mono>,
  },
  contact: {
    title: 'Contact details',
    explain: 'The contact details the agency publishes.',
    subjectLabel: 'Item',
    valueLabel: 'Value',
    subject: (r) => (CONTACT_ITEMS[r.service_type] ? <span>{CONTACT_ITEMS[r.service_type]}</span> : <Code>{r.service_type}</Code>),
    value: (r) => <Mono>{plain(r.value)}</Mono>,
  },
};

const ORDER = ['price_policy', 'price_nationality', 'salary_floor', 'contact'];

function fallback(type: string): Group {
  return {
    title: type,
    explain: '',
    subjectLabel: 'Applies to',
    valueLabel: 'Value',
    subject: (r) => (
      <>
        <Service code={r.service_type} />
        {r.nationality !== 'all' ? <span className="text-muted"> · {label('nationality', r.nationality)}</span> : null}
      </>
    ),
    value: (r) => <Mono>{plain(r.value)}</Mono>,
  };
}

export default async function RulesPage() {
  const viewer = await requireViewer();
  const list = await rules();
  const locked = list.filter((r) => r.locked).length;
  const types = [...new Set(list.map((r) => r.rule_type))].sort(
    (a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b),
  );

  return (
    <Shell viewer={viewer} active="/rules">
      <PageHeader title="Pricing rules" sub={`${list.length} ${list.length === 1 ? 'rule' : 'rules'} · ${locked} locked`} />

      {locked > 0 ? (
        <AmberNotice title="Some rules are locked">
          Locked rules control fee amounts the chatbot is allowed to quote. They can&apos;t be changed here. Contact the
          Growwstacks team to change them.
        </AmberNotice>
      ) : null}

      {types.map((type) => {
        const g = GROUPS[type] ?? fallback(type);
        const items = list.filter((r) => r.rule_type === type);
        return (
          <section key={type} className="card tbl-wrap" aria-labelledby={`g-${type}`}>
            <div className="card-head">
              <h2 id={`g-${type}`} className="text-[13px] font-semibold">
                {g.title}
              </h2>
              <span className="mono text-[12px] text-muted">
                {items.length} {items.length === 1 ? 'rule' : 'rules'}
              </span>
            </div>
            {g.explain ? <p className="px-4 pb-1 pt-3 text-[12px] text-muted">{g.explain}</p> : null}
            <table className="tbl" style={{ tableLayout: 'fixed' }}>
              <colgroup>
                <col style={{ width: '26%' }} />
                <col />
                <col style={{ width: 110 }} />
                <col style={{ width: 215 }} />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">{g.subjectLabel}</th>
                  <th scope="col">{g.valueLabel}</th>
                  <th scope="col">Status</th>
                  <th scope="col">Last changed</th>
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <tr key={r.id}>
                    <td className="break-words">{g.subject(r)}</td>
                    <td className="break-words">{g.value(r)}</td>
                    <td>{r.locked ? <Pill tone="lock">Locked</Pill> : <span className="text-faint">—</span>}</td>
                    <td className="mono whitespace-nowrap text-[12px] text-muted">{formatDate(r.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        );
      })}
    </Shell>
  );
}
