import Link from 'next/link';
import type { Viewer } from '@/lib/auth';

const NAV = [
  { href: '/documents', label: 'Knowledge base' },
  { href: '/rows', label: 'Browse entries' },
  { href: '/rules', label: 'Pricing rules' },
  { href: '/test', label: 'Test a question' },
  { href: '/activity', label: 'Activity' },
];

/**
 * The frame every signed-in page is drawn in: dark sidebar on the left, page on
 * the right. It only draws - the page has already called requireViewer() and
 * passes the viewer in.
 */
export function Shell({
  viewer,
  active,
  children,
}: {
  viewer: Viewer;
  active: string;
  children: React.ReactNode;
}) {
  const role = viewer.role.charAt(0).toUpperCase() + viewer.role.slice(1);
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
            {(viewer.canApprove ? [...NAV.slice(0, 2), { href: '/pending', label: 'Pending approval' }, ...NAV.slice(2)] : NAV).map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className="nav-link"
                aria-current={active === item.href ? 'page' : undefined}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="sidebar-foot">
            <p className="text-[11px] uppercase tracking-[0.06em]" style={{ color: 'var(--side-faint)' }}>
              Signed in as
            </p>
            <p className="mt-1 truncate text-[13px]" title={viewer.email}>
              {viewer.email}
            </p>
            <p className="text-[12px]" style={{ color: 'var(--side-muted)' }}>
              {role}
            </p>
            <form action="/logout" method="post" className="mt-3">
              <button type="submit" className="signout">
                Sign out
              </button>
            </form>
          </div>
        </div>
      </aside>
      <main className="main">
        <div className="page">{children}</div>
      </main>
    </div>
  );
}
