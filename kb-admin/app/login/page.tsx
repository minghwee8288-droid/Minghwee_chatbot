import { env } from '@/lib/env';
import { LoginForm } from './LoginForm';

const NOTICES: Record<string, string> = {
  'no-session': 'Please sign in.',
  'bad-session': 'Your session has ended. Please sign in again.',
  'no-access': 'This account does not have KB Admin access.',
  inactive: 'KB Admin access for this account has been switched off.',
  'signed-out': 'You have signed out.',
};

export default function LoginPage({ searchParams }: { searchParams: { e?: string } }) {
  const { ref } = env();
  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-lg font-semibold text-brand">KB Admin</h1>
        <p className="mb-5 mt-1 text-sm text-slate-500">
          Ming Hwee chatbot knowledge base · read-only
        </p>
        <LoginForm notice={NOTICES[searchParams.e ?? ''] ?? ''} />
        <p className="mt-5 text-xs text-slate-400">Project {ref}</p>
      </div>
    </main>
  );
}
