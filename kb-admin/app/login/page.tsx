import { LoginForm, type Notice } from './LoginForm';

const NOTICES: Record<string, Notice> = {
  'no-session': { text: 'Please sign in.', tone: 'info' },
  'bad-session': { text: 'Your session has ended. Please sign in again.', tone: 'info' },
  'no-access': { text: 'This account does not have KB Admin access.', tone: 'error' },
  inactive: { text: 'KB Admin access for this account has been switched off.', tone: 'error' },
  'signed-out': { text: 'You have signed out.', tone: 'info' },
};

export default function LoginPage({ searchParams }: { searchParams: { e?: string } }) {
  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-[400px]">
        <div className="mb-6 flex flex-col items-center text-center">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/brand/minghwee-logo.png" alt="Ming Hwee" width={64} height={64} className="mb-3 h-16 w-16" />
          <p className="text-[17px] font-semibold">Ming Hwee</p>
          <p className="mt-0.5 text-[13px] text-muted">Knowledge Base Administration</p>
        </div>

        <div className="card p-6">
          <h1 className="mb-5 text-[18px] font-semibold">Sign in to continue</h1>
          <LoginForm notice={NOTICES[searchParams.e ?? '']} />
        </div>

        <p className="mt-5 text-center text-[12px] text-muted">Read-only access to the Ming Hwee knowledge base.</p>
      </div>
    </main>
  );
}
