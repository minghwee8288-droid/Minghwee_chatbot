import Link from 'next/link';
import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { sourceProblem } from '@/lib/documents';
import { editableServices, namespaces, sourcesOverview } from '@/lib/queries';
import { UploadFlow } from './UploadFlow';

// Preparing a large document embeds every chunk; give the action time.
export const maxDuration = 300;

export default async function UploadPage({ searchParams }: { searchParams: Record<string, string | undefined> }) {
  const viewer = await requireViewer();
  if (!viewer.canEdit) {
    return (
      <Shell viewer={viewer} active="/documents">
        <PageHeader title="Upload a document" />
        <AmberNotice title="For editors">Your account cannot upload documents. Ask an approver for editor access.</AmberNotice>
      </Shell>
    );
  }
  const [sources, services, spaces] = await Promise.all([sourcesOverview(), editableServices(), namespaces()]);
  // Sources a new version can be uploaded for: those with document rows (or a
  // kb-admin document), less the reserved and internal names the database refuses.
  const replaceable = sources
    .filter((s) => s.source_document && !sourceProblem(s.source_document) && (s.total > s.qa_pairs || s.document_id))
    .map((s) => s.source_document);
  const initial = searchParams.source && replaceable.includes(searchParams.source) ? searchParams.source : '';
  return (
    <Shell viewer={viewer} active="/documents">
      <PageHeader
        title="Upload a document"
        sub="A .docx, .md or .txt file is split into chunks you review first. Nothing reaches the chatbot until an approver publishes it."
      />
      <p className="text-[13px]">
        <Link href="/documents" className="link">
          ← Knowledge base
        </Link>
      </p>
      <UploadFlow sources={replaceable} initialSource={initial} services={services} namespaces={spaces} />
    </Shell>
  );
}
