import { Shell } from '@/components/Shell';
import { AmberNotice, PageHeader, RedNotice } from '@/components/ui';
import { requireViewer } from '@/lib/auth';
import { env } from '@/lib/env';
import { TestForm } from './TestForm';

export default async function TestPage() {
  const viewer = await requireViewer();
  const enabled = Boolean(env().previewSecret);
  return (
    <Shell viewer={viewer} active="/test">
      <PageHeader
        title="Test a question"
        sub="Ask a question exactly as a client would type it on WhatsApp. The chatbot answers from the live knowledge base in test mode. Nothing is sent to anyone."
      />
      {enabled ? (
        <>
          <AmberNotice title="Each test costs a small amount (AI model calls). Use it to spot-check answers." />
          <TestForm />
        </>
      ) : (
        <RedNotice>Testing is switched off on this server. Ask the Growwstacks team to turn it on.</RedNotice>
      )}
    </Shell>
  );
}
