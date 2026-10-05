import Link from 'next/link';
import { NAV } from './Shell';

/**
 * Shown by a route's loading.tsx while the page reads the session and the
 * database, so a click answers at once instead of looking frozen. The same
 * frame as Shell, but it knows nothing about who is signed in: no email, no
 * role, no approver-only link - those arrive with the page itself.
 */
export function LoadingPage({ title, active, blocks = 3 }: { title: string; active: string; blocks?: number }) {
  return (
    <div className="layout">
      <aside className="sidebar" aria-label="Main">
        <div className="sidebar-inner">
          <div className="sidebar-brand">
            <p className="text-[15px] font-semibold">Ming Hwee</p>
            <p className="mt-0.5 text-[12px]" style={{ color: 'var(--side-muted)' }}>
              Knowledge Base Admin
            </p>
          </div>
          <nav className="sidebar-nav">
            {NAV.map((item) => (
              <Link key={item.href} href={item.href} className="nav-link" aria-current={active === item.href ? 'page' : undefined}>
                {item.label}
              </Link>
            ))}
          </nav>
        </div>
      </aside>
      <main className="main">
        <div className="page" role="status" aria-live="polite" aria-busy="true">
          <header>
            <h1 className="page-title">{title}</h1>
            <p className="page-sub">Loading…</p>
          </header>
          {Array.from({ length: blocks }, (_, i) => (
            <div key={i} className="card card-pad space-y-2.5" aria-hidden="true">
              <div className="h-3 w-2/5 rounded motion-safe:animate-pulse" style={{ background: 'var(--border)' }} />
              <div className="h-3 w-4/5 rounded motion-safe:animate-pulse" style={{ background: 'var(--border)' }} />
              <div className="h-3 w-3/5 rounded motion-safe:animate-pulse" style={{ background: 'var(--border)' }} />
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}
