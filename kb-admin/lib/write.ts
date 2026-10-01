import 'server-only';
import postgres from 'postgres';
import { isAllowedWrite, writeStatement, WRITE_FUNCTIONS, type WriteFunction } from './access';
import { env } from './env';

/**
 * The ONLY way kb-admin changes anything, and only through six functions.
 *
 * Three layers, each enough on its own:
 *   1. GRANTS: the kb_admin_editor login has no privilege on any table. It can
 *      EXECUTE the six kb_admin_* functions (scripts/sql, migrations 005, 008),
 *      each of which checks the caller's role in cb_kb_admin_users itself.
 *   2. This module exports `callWrite` and nothing else, and sends only the
 *      exact statement lib/access.ts writeStatement() builds for one of the
 *      six names - every value a bound parameter.
 *   3. scripts/selfcheck.mjs fails if any other kb-admin file names a write.
 *
 * A separate pool from the reader's (lib/db.ts): reads never run as the
 * editor, and the editor can read nothing directly.
 *
 * If the editor is not configured (lib/config.ts), there is no pool and every
 * call refuses: kb-admin is then the read-only site it was before.
 */

let client: postgres.Sql | null = null;

function sql(): postgres.Sql {
  const cfg = env();
  if (!cfg.editor) throw new Error('the editor is not configured');
  if (!client) {
    client = postgres({
      host: cfg.db.host,
      port: cfg.db.port,
      database: 'postgres',
      username: cfg.editor.db.user,
      password: cfg.editor.db.password,
      ssl: 'require',
      prepare: false,
      max: 2,
      idle_timeout: 20,
      connect_timeout: 10,
      connection: { application_name: 'kb-admin-editor' },
    });
  }
  return client;
}

export type WriteParam = string | number | boolean | null;

export async function callWrite<T>(fn: WriteFunction, params: WriteParam[]): Promise<T> {
  const statement = writeStatement(fn);
  if (!isAllowedWrite(statement) || params.length !== WRITE_FUNCTIONS[fn].length) {
    throw new Error('kb-admin calls the six kb_admin_* functions only');
  }
  const [row] = await sql().unsafe(statement, params);
  return (row as unknown as { result: T }).result;
}
