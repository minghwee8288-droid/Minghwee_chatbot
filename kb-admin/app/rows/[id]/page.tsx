import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Shell } from '@/components/Shell';
import { documentLabel, label } from '@/components/labels';
import { restoreVersion, toggleEntry } from '@/app/editor/actions';
import { ReasonAction } from '@/components/ReasonAction';
import { ActivePill, AmberNotice, Pill, formatDate, type Tone } from '@/components/ui';
import { requireViewerWith } from '@/lib/auth';
import { FILTERS, openDraft, row, versions, type Version } from '@/lib/queries';

type Params = Record<string, string | string[] | undefined>;

/** The list's filters, carried through so "Back to entries" returns to the same view. */
const KEPT = [...Object.keys(FILTERS), 'active', 'q', 'page'];

const STATUS_PILL: Record<Version['status'], { tone: Tone; text: string }> = {
  published: { tone: 'good', text: 'Live' },
  superseded: { tone: 'off', text: 'Replaced' },
  draft: { tone: 'warn', text: 'Draft' },
  discarded: { tone: 'off', text: 'Discarded' },
};

const FLASH: Record<string, string> = {
  created: 'Entry created. It is switched off and the chatbot cannot see it yet; its text is the draft below. Review the draft and publish it (an approver is needed to switch it on).',
  published: 'Published. The chatbot now reads the new version of this entry.',
  discarded: 'The draft was discarded. Nothing the chatbot reads changed.',
  on: 'Switched on. The chatbot can use this entry again.',
  off: 'Switched off. The chatbot no longer uses this entry.',
};

function History({ list, canRestore, entryId }: { list: Version[]; canRestore: boolean; entryId: string }) {
  if (!list.length) {
    return (
      <div className="card card-pad text-[13px] text-muted">
        No history yet. The first edit saves the entry as it is now as version 1.
      </div>
    );
  }
  return (
    <div className="card tbl-wrap">
      <table className="tbl">
        <thead>
          <tr>
            <th className="num">#</th>
            <th>Who</th>
            <th>When</th>
            <th>Reason</th>
            <th>Status</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {list.map((v) => (
            <tr key={v.id}>
              <td className="num mono">{v.version_number}</td>
              <td>{v.created_by_email ?? <span className="text-muted">Before the editor</span>}</td>
              <td className="mono whitespace-nowrap text-[12px]">{formatDate(v.created_at)}</td>
              <td className="max-w-[420px] break-words">{v.reason}</td>
              <td>
                <Pill tone={STATUS_PILL[v.status].tone}>{STATUS_PILL[v.status].text}</Pill>
              </td>
              <td className="whitespace-nowrap">
                <Link href={`/drafts/${v.id}`} className="link text-[13px]">
                  View
                </Link>
                {canRestore && v.status === 'superseded' ? (
                  <div className="mt-2">
                    <ReasonAction
                      action={restoreVersion}
                      hidden={{ entry_id: entryId, version_id: v.id }}
                      openLabel={`Restore version ${v.version_number}`}
                      submitLabel="Restore as a draft"
                      busyLabel="Restoring…"
                      help="This makes a new draft holding that version. Nothing goes live until the draft is published."
                    />
                  </div>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

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
  // The entry, its open draft and (on the history tab) its versions, all at once
  // with the sign-in check. The draft and versions only exist for a Q&A entry;
  // for any other row they come back empty and are not used.
  const wantsHistory = searchParams.tab === 'history';
  const [viewer, [r, draftFound, historyFound]] = await requireViewerWith(() =>
    Promise.all([row(params.id), openDraft(params.id), wantsHistory ? versions(params.id) : Promise.resolve([] as Version[])]),
  );
  if (!r) notFound();

  const back = backQuery(searchParams);
  // A new entry (migration 008) holds no text until its first publish: the
  // text is in its draft, read below once we know there is one.
  const neverPublished = r.chunk_type === 'qa_pair' && r.question === null && r.answer === null;
  const text = r.answer || r.content || '';
  // Shown only when the stored passage says more than the answer: on most
  // question-and-answer entries it is just the question followed by the answer.
  const passage = r.answer && r.content && !r.content.includes(r.answer.trim()) ? r.content : '';
  const managedRaw = typeof r.metadata?.managed_by === 'string' ? r.metadata.managed_by : '(none)';
  const editable = r.chunk_type === 'qa_pair';
  const tab = searchParams.tab === 'history' && editable ? 'history' : 'details';
  const draft = editable ? draftFound : null;
  const history = editable && tab === 'history' ? historyFound : [];
  const heading = r.question || (neverPublished ? draft?.question : null) || r.section_heading || 'Untitled passage';
  const flashKey = ['created', 'published', 'discarded'].find((k) => searchParams[k]) ?? (typeof searchParams.switched === 'string' ? searchParams.switched : '');
  const flash = FLASH[flashKey];
  // The list's filters ride along, so "Back to entries" still returns to the same view.
  const tabHref = (t: 'details' | 'history') => {
    const qs = new URLSearchParams(back);
    if (t === 'history') qs.set('tab', 'history');
    const q = qs.toString();
    return `/rows/${r.id}${q ? `?${q}` : ''}`;
  };

  return (
    <Shell viewer={viewer} active="/rows">
      <div>
        <Link href={`/rows${back ? `?${back}` : ''}`} className="link text-[13px]">
          ← Back to entries
        </Link>
        <h1 className="page-title mt-3">{heading}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[13px] text-muted">
          <ActivePill active={r.is_active} />
          <span title={r.source_document ?? undefined}>
            {r.source_document ? documentLabel(r.source_document) : 'Untitled document'}
          </span>
        </div>
        {editable && viewer.canEdit ? (
          <div className="mt-4 flex flex-wrap items-start gap-2">
            {draft ? (
              <Link href={`/drafts/${draft.id}`} className="btn btn-primary">
                Review open draft
              </Link>
            ) : null}
            {!draft || draft.created_by === viewer.userId ? (
              <Link href={`/rows/${r.id}/edit`} className={`btn ${draft ? 'btn-secondary' : 'btn-primary'}`}>
                {draft ? 'Continue editing' : 'Edit'}
              </Link>
            ) : null}
            {/* Switching on an entry that has never been published would switch on an
                empty entry with no search fingerprint; publishing its draft does that job. */}
            {viewer.canApprove && !neverPublished ? (
              <ReasonAction
                key={r.is_active ? 'on' : 'off'}
                action={toggleEntry}
                hidden={{ entry_id: r.id, active: r.is_active ? 'off' : 'on' }}
                openLabel={r.is_active ? 'Switch off' : 'Switch on'}
                submitLabel={r.is_active ? 'Switch off now' : 'Switch on now'}
                busyLabel="Switching…"
                help={
                  r.is_active
                    ? 'The chatbot stops using this entry at once. You can switch it back on.'
                    : 'The chatbot starts using this entry again at once.'
                }
              />
            ) : null}
          </div>
        ) : null}
      </div>

      {flash ? <AmberNotice title={flash} /> : null}
      {neverPublished ? (
        <AmberNotice title="New entry, not yet published">
          This entry was added in kb-admin. It stays switched off, and the chatbot cannot find it, until its draft is
          published. Publishing switches it on, so an approver is needed.
        </AmberNotice>
      ) : draft ? (
        <AmberNotice title="This entry has an open draft">
          Started by {draft.created_by_email ?? 'a colleague'} on {formatDate(draft.created_at)}. The chatbot still reads the
          live version below until the draft is published.
        </AmberNotice>
      ) : null}

      {editable ? (
        <nav className="tabs" aria-label="Entry views">
          <Link href={tabHref('details')} className="tab" aria-current={tab === 'details' ? 'page' : undefined}>
            Details
          </Link>
          <Link href={tabHref('history')} className="tab" aria-current={tab === 'history' ? 'page' : undefined}>
            History
          </Link>
        </nav>
      ) : null}

      {tab === 'history' ? <History list={history} canRestore={viewer.canEdit} entryId={r.id} /> : (

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
            <p className="break-words" title={r.source_document ?? undefined}>
              {documentLabel(r.source_document)}
            </p>
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
      )}
    </Shell>
  );
}
