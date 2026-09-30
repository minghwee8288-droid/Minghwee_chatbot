import 'server-only';
import postgres from 'postgres';
import { isSingleRead } from './access';
import { env } from './env';

/**
 * The only way kb-admin reaches the database, and it can only read.
 *
 * Three layers, each enough on its own:
 *   1. GRANTS: kb_admin_reader has SELECT on three tables and nothing else.
 *      This is the hard limit; the other two are belt and braces.
 *   2. Every query runs inside BEGIN READ ONLY.
 *   3. This module exports `select` and nothing else, and it refuses any text
 *      that is not a single SELECT (or WITH ... SELECT).
 *
 * Pooler transaction mode (6543) does not support named prepared statements,
 * hence prepare: false.
 */

let client: postgres.Sql | null = null;

function sql(): postgres.Sql {
  if (!client) {
    const { db } = env();
    client = postgres({
      host: db.host,
      port: db.port,
      database: 'postgres',
      username: db.user,
      password: db.password,
      ssl: 'require',
      prepare: false,
      max: 3,
      idle_timeout: 20,
      connect_timeout: 10,
      connection: { application_name: 'kb-admin' },
    });
  }
  return client;
}

export async function select<T>(query: string, params: (string | number | boolean | null)[] = []): Promise<T[]> {
  if (!isSingleRead(query)) {
    throw new Error('kb-admin runs single SELECT statements only');
  }
  const rows = await sql().begin('read only', (tx) => tx.unsafe(query, params));
  return rows as unknown as T[];
}
