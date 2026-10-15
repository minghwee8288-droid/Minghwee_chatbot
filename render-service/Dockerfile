# The Playwright image carries Chromium and its system libraries; its tag must
# match the playwright version in package.json.
FROM mcr.microsoft.com/playwright:v1.48.2-jammy
ENV NODE_ENV=production PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY guard.mjs server.mjs ./
USER pwuser
EXPOSE 8100
CMD ["node", "server.mjs"]
