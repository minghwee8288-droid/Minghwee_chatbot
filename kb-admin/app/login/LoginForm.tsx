'use client';

import { useEffect, useState } from 'react';
import { useFormState, useFormStatus } from 'react-dom';
import { login, type LoginState } from './actions';

export type Notice = { text: string; tone: 'info' | 'error' };

const ICON = {
  width: 18,
  height: 18,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
  focusable: false,
};

function EyeIcon() {
  return (
    <svg {...ICON}>
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg {...ICON}>
      <path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-2.9 3.9" />
      <path d="M6.6 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
      <path d="M2 2l20 20" />
    </svg>
  );
}

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className="btn btn-primary w-full">
      {pending ? 'Signing in…' : 'Sign in'}
    </button>
  );
}

export function LoginForm({ notice }: { notice?: Notice }) {
  const [raw, action] = useFormState<LoginState, FormData>(login, { error: '' });
  // Undefined after a redirect (Next 14), like every other form here.
  const state: LoginState = raw ?? { error: '' };
  // A failed sign-in always shows the same generic message (from actions.ts),
  // whether or not the email exists.
  const shown: Notice | undefined = state.error ? { text: state.error, tone: 'error' } : notice;
  // Hidden by default, and hidden again after every sign-in attempt: each
  // attempt hands useFormState a new result object.
  const [showPassword, setShowPassword] = useState(false);
  useEffect(() => setShowPassword(false), [raw]);
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
        <div className="relative">
          <input
            id="password"
            name="password"
            type={showPassword ? 'text' : 'password'}
            autoComplete="current-password"
            required
            className="input h-10 pr-10"
          />
          {/* type="button": the toggle never submits the form. */}
          <button
            type="button"
            onClick={() => setShowPassword((v) => !v)}
            aria-label={showPassword ? 'Hide password' : 'Show password'}
            aria-pressed={showPassword}
            aria-controls="password"
            className="absolute inset-y-0 right-0 flex w-10 items-center justify-center rounded-r-lg"
            style={{ color: 'var(--muted)' }}
          >
            {showPassword ? <EyeOffIcon /> : <EyeIcon />}
          </button>
        </div>
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
