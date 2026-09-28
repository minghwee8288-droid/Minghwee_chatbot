import 'server-only';
import { readFileSync } from 'node:fs';
import { validateConfig, type KbAdminConfig } from './config';

let cached: KbAdminConfig | null = null;

/** The validated configuration. Throws (and so refuses every request) if any
 *  setting names the wrong project or the wrong kind of credential. */
export function env(): KbAdminConfig {
  if (!cached) {
    cached = validateConfig(process.env, (path) => readFileSync(path, 'utf8'));
  }
  return cached;
}
