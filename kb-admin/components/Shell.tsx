import Link from 'next/link';
import type { Viewer } from '@/lib/auth';
import { env } from '@/lib/env';

const NAV = [
  { href: '/documents', label: 'Documents' },
  { href: '/rows', label: 'Rows' },
  { href: '/rules', label: 'Rules' },
  { href: '/test', label: 'Test a question' },
];

export function Shell({
  viewer,
  active,
  children,
}: {
  viewer: Viewer;
  active: string;
  children: React.ReactNode;
}) {
  const { ref } = env();
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <span className="font-semibold text-brand">KB Admin</span>
          <nav className="flex flex-wrap gap-1 text-sm">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded-md px-3 py-1.5 ${
                  active === item.href ? 'bg-brand text-white' : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-xs text-slate-500">
            <span className="rounded bg-amber-100 px-2 py-0.5 font-medium text-amber-800">
              {ref} · read-only
            </span>
            <span>{viewer.email}</span>
            <form action="/logout" method="post">
              <button className="rounded-md border border-slate-300 px-2 py-1 hover:bg-slate-50">
                Sign out
              </button>
            </form>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
    </div>
  );
}

export function Pill({ children, tone = 'slate' }: { children: React.ReactNode; tone?: 'slate' | 'green' | 'red' | 'amber' }) {
  const tones = {
    slate: 'bg-slate-100 text-slate-700',
    green: 'bg-emerald-100 text-emerald-800',
    red: 'bg-rose-100 text-rose-800',
    amber: 'bg-amber-100 text-amber-800',
  };
  return <span className={`inline-block rounded px-1.5 py-0.5 text-xs ${tones[tone]}`}>{children}</span>;
}
