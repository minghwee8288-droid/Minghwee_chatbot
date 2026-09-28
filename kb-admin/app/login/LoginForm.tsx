'use client';

import { useFormState, useFormStatus } from 'react-dom';
import { login, type LoginState } from './actions';

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="w-full rounded-md bg-brand px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
    >
      {pending ? 'Signing in…' : 'Sign in'}
    </button>
  );
}

export function LoginForm({ notice }: { notice: string }) {
  const [state, action] = useFormState<LoginState, FormData>(login, { error: '' });
  const message = state.error || notice;
  return (
    <form action={action} className="space-y-4">
      <label className="block text-sm">
        <span className="text-slate-600">Email</span>
        <input
          name="email"
          type="email"
          autoComplete="username"
          required
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
        />
      </label>
      <label className="block text-sm">
        <span className="text-slate-600">Password</span>
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          required
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
        />
      </label>
      {message ? (
        <p role="alert" className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {message}
        </p>
      ) : null}
      <Submit />
    </form>
  );
}
