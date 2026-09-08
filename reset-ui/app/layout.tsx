import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Clear Conversation — Ming Hwee',
  description:
    'Hand one WhatsApp conversation back to the bot and clear its lead, without the terminal.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      {/* The page is one short form, so it is centred in the viewport rather
          than pinned to the top with the rest of the screen left empty. */}
      <body className="min-h-full bg-slate-100 text-slate-800 antialiased">{children}</body>
    </html>
  );
}
