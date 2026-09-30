import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { charCount } from '@/lib/editing';
import { editableServices, openDraft, row } from '@/lib/queries';
import { EditForm } from './EditForm';

/** Edit one question-and-answer entry. Saving makes a draft; nothing goes live here. */
export default async function EditPage({ params }: { params: { id: string } }) {
  const viewer = await requireViewer();
  const r = await row(params.id);
  if (!r) notFound();

  const back = (
    <Link href={`/rows/${r.id}`} className="link text-[13px]">
      ← Back to the entry
    </Link>
  );
  if (!viewer.canEdit || r.chunk_type !== 'qa_pair') {
    return (
      <Shell viewer={viewer} active="/rows">
        {back}
        <AmberNotice title="This entry cannot be edited here">
          {r.chunk_type !== 'qa_pair'
            ? 'Only question-and-answer entries can be edited.'
            : 'Your account can view the knowledge base but not change it.'}
        </AmberNotice>
      </Shell>
    );
  }

  const [draft, services] = await Promise.all([openDraft(r.id), editableServices()]);
  const othersDraft = draft && draft.created_by !== viewer.userId ? draft : null;
  const mine = draft && draft.created_by === viewer.userId ? draft : null;
  const base = mine
    ? {
        question: mine.question ?? '',
        answer: mine.answer ?? '',
        section_heading: mine.section_heading ?? '',
        service: mine.service,
        audience: mine.audience,
        nationality: mine.nationality,
      }
    : {
        question: r.question ?? '',
        answer: r.answer ?? '',
        section_heading: r.section_heading ?? '',
        service: r.service_type,
        audience: r.contact_type,
        nationality: r.nationality,
      };

  return (
    <Shell viewer={viewer} active="/rows">
      <div>
        {back}
        <div className="mt-3">
          <PageHeader title="Edit entry" sub="Your change is saved as a draft. You then review it and publish it." />
        </div>
      </div>
      {othersDraft ? (
        <AmberNotice title="Someone else has a draft of this entry open">
          {othersDraft.created_by_email ?? 'A colleague'} started a draft. It must be published or discarded before
          you can save one. <Link className="link" href={`/drafts/${othersDraft.id}`}>Open their draft</Link>
        </AmberNotice>
      ) : null}
      {mine ? (
        <AmberNotice title="You are continuing your open draft">
          Saving replaces it. <Link className="link" href={`/drafts/${mine.id}`}>Review it without changes</Link>
        </AmberNotice>
      ) : null}
      <EditForm entryId={r.id} initial={base} services={services} liveLength={charCount(r.content ?? '')} />
    </Shell>
  );
}
