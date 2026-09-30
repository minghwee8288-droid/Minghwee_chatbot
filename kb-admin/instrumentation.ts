/**
 * Runs once when the server starts. Validates the configuration so a server
 * pointed at the wrong project, or holding the wrong kind of key, refuses to
 * start rather than failing on the first click. Every request re-checks it too.
 *
 * The import has to sit INSIDE this exact `if`: Next.js removes it from the
 * edge bundle only in this form, and lib/env.ts needs the filesystem.
 */
export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    const { env } = await import('./lib/env');
    try {
      const cfg = env();
      console.info(
        `[kb-admin] project ${cfg.ref}, database ${cfg.db.user} on port ${cfg.db.port} (read-only), ` +
          `preview ${cfg.previewSecret ? 'on' : 'off'}`,
      );
    } catch (error) {
      // Next.js would otherwise keep the process up and answer 500 to every
      // request. Exit instead, so a misconfigured server is plainly not running.
      console.error(String(error instanceof Error ? error.message : error));
      process.exit(1);
    }
  }
}
