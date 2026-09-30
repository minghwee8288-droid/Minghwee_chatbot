'use client';

import { useFormState, useFormStatus } from 'react-dom';
import { login, type LoginState } from './actions';

export type Notice = { text: string; tone: 'info' | 'error' };

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className="btn btn-primary w-full">
      {pending ? 'Signing in…' : 'Sign in'}
    </button>
  );
}

export function LoginForm({ notice }: { notice?: Notice }) {
  const [state, action] = useFormState<LoginState, FormData>(login, { error: '' });
  // A failed sign-in always shows the same generic message (from actions.ts),
  // whether or not the email exists.
  const shown: Notice | undefined = state.error ? { text: state.error, tone: 'error' } : notice;
  return (
    <form action={action} className="space-y-4">
      <div>
        <label htmlFor="email" className="field-label mb-1.5 block">
          Email
        </label>
        <input id="email" name="email" type="email" autoComplete="username" required className="input h-10" />
      </div>
      <div>
        <label htmlFor="password" className="field-label mb-1.5 block">
          Password
        </label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          className="input h-10"
        />
      </div>
      {shown ? (
        shown.tone === 'error' ? (
          <div role="alert" className="notice notice-red">
            {shown.text}
          </div>
        ) : (
          <div role="status" className="notice notice-amber">
            <span className="notice-dot" aria-hidden="true" />
            <span>{shown.text}</span>
          </div>
        )
      ) : null}
      <Submit />
    </form>
  );
}
