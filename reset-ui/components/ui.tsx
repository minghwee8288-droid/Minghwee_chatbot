'use client';

/** Small shared pieces, kept out of page.tsx so the flow there stays readable. */

import { useId, useState, type ReactNode } from 'react';

export function Card({
  children,
  tone = 'plain',
}: {
  children: ReactNode;
  tone?: 'plain' | 'danger' | 'warning' | 'success';
}) {
  const border = {
    plain: 'border-slate-200',
    danger: 'border-danger/30',
    warning: 'border-amber-300',
    success: 'border-emerald-300',
  }[tone];

  return (
    <section className={`rounded-2xl border bg-white p-8 shadow-lg shadow-slate-900/5 ${border}`}>
      {children}
    </section>
  );
}

export function Pill({
  children,
  tone = 'slate',
}: {
  children: ReactNode;
  tone?: 'slate' | 'green' | 'amber' | 'red' | 'blue';
}) {
  const classes = {
    slate: 'bg-slate-100 text-slate-700',
    green: 'bg-emerald-100 text-emerald-800',
    amber: 'bg-amber-100 text-amber-800',
    red: 'bg-red-100 text-red-800',
    blue: 'bg-sky-100 text-sky-800',
  }[tone];
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold ${classes}`}
    >
      {children}
    </span>
  );
}

export function Banner({
  tone,
  title,
  children,
}: {
  tone: 'error' | 'success' | 'warning' | 'info';
  title: string;
  children?: ReactNode;
}) {
  const style = {
    error: 'border-danger/40 bg-red-50 text-red-900',
    success: 'border-emerald-300 bg-emerald-50 text-emerald-900',
    warning: 'border-amber-300 bg-amber-50 text-amber-900',
    info: 'border-sky-300 bg-sky-50 text-sky-900',
  }[tone];
  const icon = { error: '✕', success: '✓', warning: '!', info: 'i' }[tone];
  const iconStyle = {
    error: 'bg-danger text-white',
    success: 'bg-emerald-600 text-white',
    warning: 'bg-amber-500 text-white',
    info: 'bg-sky-600 text-white',
  }[tone];

  return (
    <div className={`flex gap-4 rounded-xl border p-5 ${style}`}>
      <span
        className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-sm font-black ${iconStyle}`}
        aria-hidden
      >
        {icon}
      </span>
      <div className="min-w-0">
        <p className="text-base font-bold">{title}</p>
        {children ? <div className="mt-1 text-sm leading-relaxed">{children}</div> : null}
      </div>
    </div>
  );
}

export function Spinner({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-2.5">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
      {label}
    </span>
  );
}

/** Shared by both inputs, so the password field cannot drift from the other. */
const FIELD_CLASS =
  'w-full rounded-xl border border-slate-300 bg-white px-4 py-3.5 text-base outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/20';

const LABEL_CLASS = 'text-xs font-bold uppercase tracking-widest text-slate-500';

/** A labelled text input, sized for a page that is mostly this one field. */
export function TextInput({
  label,
  hint,
  ...props
}: {
  label: string;
  hint?: string;
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="block">
      <span className={LABEL_CLASS}>{label}</span>
      <input {...props} className={`mt-2 ${FIELD_CLASS}`} />
      {hint ? <span className="mt-2 block text-sm text-slate-500">{hint}</span> : null}
    </label>
  );
}

function EyeIcon({ off }: { off: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-5 w-5"
      aria-hidden
    >
      {off ? (
        <>
          <path d="M10.6 6.2A10 10 0 0 1 12 6c6.5 0 10 6 10 6a17.8 17.8 0 0 1-3.1 3.8" />
          <path d="M6.7 6.7A17.8 17.8 0 0 0 2 12s3.5 6 10 6a9.9 9.9 0 0 0 4.2-.9" />
          <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
          <path d="m3 3 18 18" />
        </>
      ) : (
        <>
          <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z" />
          <circle cx="12" cy="12" r="3" />
        </>
      )}
    </svg>
  );
}

/**
 * A password field with a show/hide toggle.
 *
 * The button is NOT inside the <label> — a label forwards clicks to its
 * control, so a button nested in one fights the thing it sits on. The label is
 * bound by `htmlFor` instead, which is also what a screen reader expects.
 */
export function PasswordInput({
  label,
  hint,
  ...props
}: {
  label: string;
  hint?: string;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type'>) {
  const [visible, setVisible] = useState(false);
  const id = useId();

  return (
    <div>
      <label htmlFor={id} className={LABEL_CLASS}>
        {label}
      </label>
      <div className="relative mt-2">
        <input
          {...props}
          id={id}
          type={visible ? 'text' : 'password'}
          // Room for the button, so a long password does not run under it.
          className={`${FIELD_CLASS} pr-12`}
        />
        <button
          type="button"
          onClick={() => setVisible((shown) => !shown)}
          // Not a submit button: inside a form, the default type would clear
          // the field by submitting it.
          aria-label={visible ? 'Hide password' : 'Show password'}
          aria-pressed={visible}
          title={visible ? 'Hide password' : 'Show password'}
          className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-lg p-2 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
        >
          <EyeIcon off={visible} />
        </button>
      </div>
      {hint ? <span className="mt-2 block text-sm text-slate-500">{hint}</span> : null}
    </div>
  );
}

/** Local time, or the raw string if it will not parse. */
export function when(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}
