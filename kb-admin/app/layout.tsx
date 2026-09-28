import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'KB Admin — Ming Hwee',
  description: 'Read-only view of the chatbot knowledge base and pricing rules.',
  robots: { index: false, follow: false },
};

// Every page reads the session cookie and the database per request.
export const dynamic = 'force-dynamic';

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-800 antialiased">{children}</body>
    </html>
  );
}
