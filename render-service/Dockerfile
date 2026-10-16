# The Playwright image carries the system libraries a browser needs; its tag
# must match the playwright version in package.json.
FROM mcr.microsoft.com/playwright:v1.48.2-jammy
ENV NODE_ENV=production PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
# System Chrome (google-chrome-stable), launched with channel "chrome"
# (2026-10-16): Playwright's own Chromium failed to render on the test machine,
# Chrome rendered kemlu.go.id. Installed as root, before dropping to pwuser.
RUN npx playwright install --with-deps chrome
ENV RENDER_CHROME_CHANNEL=chrome
COPY guard.mjs server.mjs ./
USER pwuser
EXPOSE 8100
CMD ["node", "server.mjs"]
