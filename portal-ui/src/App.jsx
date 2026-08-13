import React, { useMemo } from 'react';
import { createClient } from '@supabase/supabase-js';
import TicketSystemModule from '../ticket-system';

/**
 * Standalone demo deployment.
 *
 * Temporary: shows the ticket-system module on its own Vercel URL so the
 * client can see it before it is copied into the main portal's source tree.
 * Must run on the `anon` key plus RLS, never `service_role` -- this build can
 * be reached from the public internet.
 *
 * No login: anyone with the URL can view (not edit) live ticket data. That is
 * a deliberate, temporary tradeoff for a quick client demo -- login comes
 * back when this is integrated into the main portal.
 */

const url = import.meta.env.VITE_SUPABASE_URL;
const key = import.meta.env.VITE_SUPABASE_KEY;

function Setup() {
  return (
    <div className="mx-auto max-w-2xl p-8">
      <h1 className="text-xl font-black text-[#003E60]">Not configured</h1>
      <p className="mt-2 text-sm text-gray-600">
        Create <code className="rounded bg-gray-100 px-1">portal-ui/.env.local</code> with:
      </p>
      <pre className="mt-3 overflow-x-auto rounded-lg bg-gray-900 p-4 text-xs text-gray-100">
{`VITE_SUPABASE_URL=https://<project>.supabase.co
VITE_SUPABASE_KEY=<anon public key>`}
      </pre>
      <p className="mt-3 text-sm text-gray-600">
        Then restart <code className="rounded bg-gray-100 px-1">npm run dev</code>. The file is
        gitignored.
      </p>
    </div>
  );
}

export default function App() {
  // Created once: a client rebuilt on every render would restart the module's
  // fetches in a loop.
  const supabase = useMemo(() => (url && key ? createClient(url, key) : null), []);

  if (!supabase) return <Setup />;

  // A service_role key bypasses RLS and must never reach a deployed page.
  const looksLikeServiceRole = key.includes('service_role') || key.length > 300;
  if (looksLikeServiceRole) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <h1 className="text-xl font-black text-[#DF0000]">Refusing to start</h1>
        <p className="mt-2 text-sm text-gray-600">
          VITE_SUPABASE_KEY looks like a service_role key. Replace it with the anon public key
          before running this build.
        </p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 p-6">
      <div className="mx-auto max-w-7xl space-y-4">
        <TicketSystemModule supabaseClient={supabase} />
      </div>
    </div>
  );
}
