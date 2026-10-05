import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Shell } from '@/components/Shell';
import { documentLabel } from '@/components/labels';
import { AmberNotice, PageHeader } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { batch } from '@/lib/queries';
import { ImpactRunner } from './ImpactRunner';

// 42 questions, two searches each, plus embedding the questions once.
export const maxDuration = 300;

export default async function ImpactPage({ params }: { params: { id: string } }) {
  const viewer = await requireViewer();
  const b = await batch(params.id);
  if (!b) notFound();
  return (
    <Shell viewer={viewer} active="/documents">
      <PageHeader
        title={`Impact check: ${documentLabel(b.source_name)}`}
        sub="Every probe question, searched as the chatbot searches today and as it would with this version published."
      />
      <p className="text-[13px]">
        <Link href={`/documents/batches/${b.id}`} className="link">
          ← Back to the version
        </Link>
      </p>
      {b.status !== 'staged' ? (
        <AmberNotice title="Nothing to check">Only a version being prepared can be checked; this one is {b.status}.</AmberNotice>
      ) : !viewer.canEdit ? (
        <AmberNotice title="For editors">Running the impact check needs editor access.</AmberNotice>
      ) : (
        <ImpactRunner batchId={b.id} source={b.source_name} canMark={viewer.canEdit} />
      )}
    </Shell>
  );
}
