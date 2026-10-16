import 'server-only';
import { request } from 'node:https';
import type { FetchLike } from '@/lib/watcher/fetcher';

/**
 * One https GET WITHOUT a certificate check (2026-10-16, migration 017).
 *
 * PoliteFetcher hands this ONLY the host of a source an approver marked "allow
 * unverified connection" - the Myanmar embassy, whose certificate is
 * self-signed and issued for another site - and only for that host's own
 * addresses; any other host, and any redirect off it, goes through the normal,
 * verified fetch. Nothing else in kb-admin uses it. Redirects are returned,
 * not followed (the fetcher follows them itself); the body is capped at 4 MB.
 */
const MAX_BODY = 4 * 1024 * 1024;

export const insecureFetch: FetchLike = (url, init) =>
  new Promise((resolve, reject) => {
    const u = new URL(url);
    if (u.protocol !== 'https:') {
      reject(new Error('insecureFetch reads https:// only'));
      return;
    }
    const req = request(
      {
        protocol: 'https:',
        hostname: u.hostname,
        port: u.port || 443,
        path: `${u.pathname}${u.search}`,
        method: 'GET',
        headers: init.headers,
        rejectUnauthorized: false,
        servername: u.hostname,
      },
      (res) => {
        const chunks: Buffer[] = [];
        let size = 0;
        res.on('data', (c: Buffer) => {
          size += c.length;
          if (size > MAX_BODY) {
            req.destroy(new Error('the page is larger than 4 MB'));
            return;
          }
          chunks.push(c);
        });
        res.on('end', () => {
          const body = Buffer.concat(chunks).toString('utf8');
          resolve({
            status: res.statusCode ?? 0,
            headers: { get: (name: string) => { const v = res.headers[name.toLowerCase()]; return Array.isArray(v) ? v.join(', ') : v ?? null; } },
            text: async () => body,
          });
        });
        res.on('error', reject);
      },
    );
    const onAbort = () => req.destroy(Object.assign(new Error('timed out'), { name: 'TimeoutError' }));
    if (init.signal.aborted) onAbort();
    else init.signal.addEventListener('abort', onAbort, { once: true });
    req.on('error', reject);
    req.end();
  });
