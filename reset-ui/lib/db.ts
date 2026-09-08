/**
 * Database access, split exactly the way `scripts/reset_conversation.py`
 * splits it, and for the same reason.
 *
 *  - PostgREST (supabase-js, service-role key) for the ordinary tables:
 *    wp_chat_conversations, wp_chat_messages, cb_tickets, cb_handovers,
 *    leads, leads_candidate, employers, candidates, placements. The chatbot
 *    reaches all of these this way, so they are certainly in PostgREST's
 *    schema cache.
 *
 *  - A direct Postgres connection for cb_checkpoints / cb_checkpoint_blobs /
 *    cb_checkpoint_writes. Those three are created by LangGraph over a raw
 *    psycopg connection (app/graph/checkpointer.py rewrites the library's
 *    hardcoded unprefixed names), so they are NOT necessarily in PostgREST's
 *    schema cache and `.from('cb_checkpoints')` is not a reliable way to
 *    reach them. The Python script says exactly this and connects to
 *    SUPABASE_DB_URL instead; so does this.
 *
 * The service-role key bypasses RLS. That is required — RLS exists to
 * constrain the portal's anon key, and this tool has to delete rows the
 * portal's key cannot. It is also why the key stays server-side.
 */

import { createClient, type SupabaseClient } from '@supabase/supabase-js';
import postgres from 'postgres';
import { env } from './env';

let client: SupabaseClient | null = null;

export function supabase(): SupabaseClient {
  if (!client) {
    if (!env.supabaseUrl || !env.serviceRoleKey) {
      throw new Error(
        'SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set before the database can be reached.',
      );
    }
    client = createClient(env.supabaseUrl, env.serviceRoleKey, {
      auth: { persistSession: false, autoRefreshToken: false },
    });
  }
  return client;
}

/**
 * Run one function against a direct Postgres connection, then close it.
 *
 * Deliberately not pooled across invocations: a Vercel function is frozen
 * between requests and a held connection is a connection Supabase's pooler
 * has to keep. This runs a handful of DELETEs a few times a day.
 */
export async function withPostgres<T>(
  fn: (sql: postgres.Sql) => Promise<T>,
): Promise<T> {
  if (!env.dbUrl) {
    throw new Error('SUPABASE_DB_URL is not set.');
  }
  const sql = postgres(env.dbUrl, {
    max: 1,
    idle_timeout: 5,
    connect_timeout: 15,
    prepare: false, // the transaction pooler does not support prepared statements
    onnotice: () => {},
  });
  try {
    return await fn(sql);
  } finally {
    await sql.end({ timeout: 5 });
  }
}

/**
 * An exact server-side count of the rows one conversation owns in a table.
 *
 * `count: 'exact', head: true` and not `rows.length`: a long-running test
 * number can hold more messages than any page limit returns, and reporting
 * "500 rows cleared" on a delete that removed 900 is the kind of thing you
 * only notice much later. The Python `_count()` makes the same point.
 */
export async function countForConversation(
  table: string,
  conversationId: number,
): Promise<number> {
  const { count, error } = await supabase()
    .from(table)
    .select('*', { count: 'exact', head: true })
    .eq('conversation_id', conversationId);
  if (error) throw new Error(`Counting ${table} failed: ${error.message}`);
  return count ?? 0;
}
