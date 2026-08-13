import React, { useMemo } from 'react';
import { createClient } from '@supabase/supabase-js';
import TicketSystemModule from '../ticket-system';

/**
 * Local test harness. NOT part of the deliverable.
 *
 * The portal creates its own Supabase client and passes it in; this file
 * stands in for that so the module can be exercised against real data before
 * hand-off. Only `ticket-system/` ships.
 */

const url = import.meta.env.VITE_SUPABASE_URL;
const key = import.meta.env.VITE_SUPABASE_KEY;

function Setup() {
  return (
    <div className="mx-auto max-w-2xl p-8">
      <h1 className="text-xl font-black text-[#003E60]">Harness not configured</h1>
      <p className="mt-2 text-sm text-gray-600">
        Create <code className="rounded bg-gray-100 px-1">portal-ui/.env.local</code> with:
      </p>
      <pre className="mt-3 overflow-x-auto rounded-lg bg-gray-900 p-4 text-xs text-gray-100">
{`VITE_SUPABASE_URL=https://<project>.supabase.co
VITE_SUPABASE_KEY=<anon or service_role key>`}
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

  // A service_role key bypasses RLS and must never reach a deployed page. It is
  // tolerable in a local-only harness; the warning is here so it stays that way.
  const looksLikeServiceRole = key.includes('service_role') || key.length > 300;

  return (
    <div className="min-h-screen bg-gray-50 p-6">
      <div className="mx-auto max-w-7xl space-y-4">
        <div className="rounded-lg border border-dashed border-gray-300 bg-white px-4 py-2 text-xs text-gray-500">
          Local harness — this wrapper is not part of the deliverable. Only{' '}
          <code className="rounded bg-gray-100 px-1">ticket-system/</code> ships to the portal.
          {looksLikeServiceRole ? (
            <span className="ml-1 font-bold text-[#DF0000]">
              This key may be a service_role key. Keep it local; never deploy it.
            </span>
          ) : null}
        </div>
        <TicketSystemModule supabaseClient={supabase} />
      </div>
    </div>
  );
}
