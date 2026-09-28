import { Shell } from '@/components/Shell';
import { requireViewer } from '@/lib/auth';
import { env } from '@/lib/env';
import { TestForm } from './TestForm';

export default async function TestPage() {
  const viewer = await requireViewer();
  const enabled = Boolean(env().previewSecret);
  return (
    <Shell viewer={viewer} active="/test">
      <h1 className="mb-1 text-xl font-semibold">Test a question</h1>
      <p className="mb-4 max-w-3xl text-sm text-slate-500">
        Runs the real bot on your question, read-only: nothing is saved, nothing is sent on WhatsApp,
        and no lead or ticket is created. Shows the reply and the knowledge-base rows it used.
        Each test calls the AI model, so there is a small cost per test.
      </p>
      {enabled ? (
        <TestForm />
      ) : (
        <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
          Switched off on this server (no preview secret configured).
        </p>
      )}
    </Shell>
  );
}
