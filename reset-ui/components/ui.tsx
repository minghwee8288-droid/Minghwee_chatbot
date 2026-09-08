'use client';

/** Small shared pieces, kept out of page.tsx so the flow there stays readable. */

import type { ReactNode } from 'react';

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
      <span className="text-xs font-bold uppercase tracking-widest text-slate-500">{label}</span>
      <input
        {...props}
        className="mt-2 w-full rounded-xl border border-slate-300 bg-white px-4 py-3.5 text-base outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/20"
      />
      {hint ? <span className="mt-2 block text-sm text-slate-500">{hint}</span> : null}
    </label>
  );
}

/** Local time, or the raw string if it will not parse. */
export function when(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}
