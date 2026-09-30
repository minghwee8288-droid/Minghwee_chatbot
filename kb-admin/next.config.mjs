/** @type {import('next').NextConfig} */
const nextConfig = {
  // scripts/dev-test.mjs builds into .next-test, so a TEST-project dev server
  // never shares a cache with the normal one.
  distDir: process.env.KB_ADMIN_DIST_DIR || '.next',
  reactStrictMode: true,
  poweredByHeader: false,
  experimental: {
    // Runs instrumentation.ts at server start, which validates the environment
    // and refuses to start against the wrong project.
    instrumentationHook: true,
  },
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'Referrer-Policy', value: 'no-referrer' },
          { key: 'Cache-Control', value: 'no-store' },
          { key: 'X-Robots-Tag', value: 'noindex, nofollow' },
        ],
      },
    ];
  },
};

export default nextConfig;
