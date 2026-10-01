import Link from 'next/link';
import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { editableServices } from '@/lib/queries';
import { EditForm } from '../[id]/edit/EditForm';

/**
 * Add a brand-new question-and-answer entry (migration 008).
 * Saving creates the entry switched off and unsearchable, with the text as its
 * first draft; the normal review -> publish flow then takes over.
 */
export default async function NewEntryPage() {
  const viewer = await requireViewer();
  const back = (
    <Link href="/documents" className="link text-[13px]">
      ← Back to the knowledge base
    </Link>
  );
  if (!viewer.canEdit) {
    return (
      <Shell viewer={viewer} active="/rows">
        {back}
        <AmberNotice title="You cannot add entries">Your account can view the knowledge base but not change it.</AmberNotice>
      </Shell>
    );
  }

  const services = await editableServices();
  const initial = {
    question: '',
    answer: '',
    section_heading: '',
    service: services.includes('general') ? 'general' : services[0] ?? 'general',
    audience: 'all',
    nationality: 'all',
  };

  return (
    <Shell viewer={viewer} active="/rows">
      <div>
        {back}
        <div className="mt-3">
          <PageHeader
            title="Add a Q&A entry"
            sub="The new entry is saved switched off, with your text as a draft. It goes live only when an approver publishes it."
          />
        </div>
      </div>
      <EditForm mode="create" initial={initial} services={services} liveLength={0} />
    </Shell>
  );
}
