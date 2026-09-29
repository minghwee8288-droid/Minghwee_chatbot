import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Shell } from '@/components/Shell';
import { label } from '@/components/labels';
import { ActivePill, formatDate } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { FILTERS, row } from '@/lib/queries';

type Params = Record<string, string | string[] | undefined>;

/** The list's filters, carried through so "Back to entries" returns to the same view. */
const KEPT = [...Object.keys(FILTERS), 'active', 'q', 'page'];

function backQuery(searchParams: Params): string {
  const qs = new URLSearchParams();
  for (const key of KEPT) {
    const v = searchParams[key];
    const value = Array.isArray(v) ? v[0] : v;
    if (value) qs.set(key, value.slice(0, 200));
  }
  return qs.toString();
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="border-t px-5 py-4 first:border-t-0" style={{ borderColor: 'var(--divider)' }}>
      <h2 className="field-label mb-1.5">{label}</h2>
      <div className="min-w-0 text-[14px]">{children}</div>
    </div>
  );
}

function Long({ value }: { value: string }) {
  return <p className="whitespace-pre-wrap break-words leading-[1.6]">{value}</p>;
}

function Code({ value }: { value: string }) {
  return <span className="mono text-[13px]">{value}</span>;
}

export default async function RowPage({ params, searchParams }: { params: { id: string }; searchParams: Params }) {
  const viewer = await requireViewer();
  const r = await row(params.id);
  if (!r) notFound();

  const back = backQuery(searchParams);
  const heading = r.question || r.section_heading || 'Untitled passage';
  const text = r.answer || r.content || '';
  // Shown only when the stored passage says more than the answer: on most
  // question-and-answer entries it is just the question followed by the answer.
  const passage = r.answer && r.content && !r.content.includes(r.answer.trim()) ? r.content : '';
  const managedRaw = typeof r.metadata?.managed_by === 'string' ? r.metadata.managed_by : '(none)';

  return (
    <Shell viewer={viewer} active="/rows">
      <div>
        <Link href={`/rows${back ? `?${back}` : ''}`} className="link text-[13px]">
          ← Back to entries
        </Link>
        <h1 className="page-title mt-3">{heading}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[13px] text-muted">
          <ActivePill active={r.is_active} />
          <span>{r.source_document || 'Untitled document'}</span>
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_300px] items-start gap-5">
        <article className="card">
          <Section label="Question / heading">
            <p className="font-semibold leading-[1.6]">{r.question || r.section_heading || '—'}</p>
            {r.question && r.section_heading ? (
              <p className="mt-1 text-[13px] text-muted">Section: {r.section_heading}</p>
            ) : null}
          </Section>
          <Section label="Answer text">
            {text ? <Long value={text} /> : <span className="text-muted">No text on this entry.</span>}
          </Section>
          {passage ? (
            <Section label="Full passage">
              <Long value={passage} />
            </Section>
          ) : null}
        </article>

        <aside className="card" aria-label="Details">
          <Section label="Document">
            <p className="break-words">{r.source_document || '—'}</p>
          </Section>
          <Section label="Service">
            <span title={r.service_type}>{label('service', r.service_type)}</span>
          </Section>
          <Section label="Audience">
            <span title={r.contact_type}>{label('audience', r.contact_type)}</span>
          </Section>
          <Section label="Nationality">
            <span title={r.nationality}>{label('nationality', r.nationality)}</span>
          </Section>
          <Section label="Status">
            <ActivePill active={r.is_active} />
            <p className="mt-1.5 text-[12px] text-muted">
              {r.is_active ? 'The chatbot can use this entry.' : 'The chatbot does not use this entry.'}
            </p>
          </Section>
          <Section label="Entry type">
            {label('entryType', r.chunk_type) !== r.chunk_type ? label('entryType', r.chunk_type) : <Code value={r.chunk_type} />}
          </Section>
          <Section label="Maintained by">
            {label('maintainedBy', managedRaw) !== managedRaw ? label('maintainedBy', managedRaw) : <Code value={managedRaw} />}
          </Section>
          <Section label="Last updated">
            <span className="mono text-[13px]">{formatDate(r.updated_at)}</span>
          </Section>
          <Section label="Added">
            <span className="mono text-[13px]">{formatDate(r.created_at)}</span>
          </Section>
          <Section label="Reference">
            <span className="mono break-all text-[12px] text-muted">{r.id}</span>
          </Section>
        </aside>
      </div>
    </Shell>
  );
}
