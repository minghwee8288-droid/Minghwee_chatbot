import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Clear Conversation — Ming Hwee',
  description:
    'Hand one WhatsApp conversation back to the bot and clear its lead, without the terminal.',
  icons: {
    icon: [
      { url: '/favicon.ico', sizes: '16x16 32x32' },
      { url: '/brand/favicon-32x32.png', sizes: '32x32', type: 'image/png' },
      { url: '/brand/icon-192.png', sizes: '192x192', type: 'image/png' },
      { url: '/brand/icon-512.png', sizes: '512x512', type: 'image/png' },
    ],
    apple: { url: '/brand/apple-touch-icon.png', sizes: '180x180' },
  },
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
