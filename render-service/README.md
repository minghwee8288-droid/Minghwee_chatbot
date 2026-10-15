# render-service

Renders JavaScript-built pages for kb-admin's KB watcher. Some official pages
(the Indonesian embassy's site, `kemlu.go.id/singapore`) send an empty app shell
and build their text in the browser, so a plain fetch reads nothing. Vercel
cannot run a browser reliably, so this small service runs headless Chromium on
our own server, beside the bot, in its **own container**. It imports nothing from
the bot, and the bot never calls it.

kb-admin uses it **only** when a plain fetch of a watched page has no readable
text and the page is not a bot-protection or maintenance page. Before calling it,
kb-admin checks the address, robots.txt and its per-host gap
(`kb-admin/lib/watcher/fetcher.ts`, `renderPage`).

## What it does and refuses

- `POST /render` with `Authorization: Bearer <RENDER_SECRET>` and `{"url": "https://..."}`
  returns `{ok: true, status, finalUrl, html}` or `{ok: false, error}`. Without the key,
  every path answers 404. `GET /health` returns `{"ok": true}`.
- It uses the watcher's user-agent (`MingHweeKBWatcher/1.0 ...`), renders one page at a
  time, and waits at least 10 s between two renders of one host.
- It never tries to get past a bot-protection check. kb-admin reports such a page as
  "Blocked by site".
- It renders https pages on public hosts only. Every request the page makes is checked
  too, and anything resolving to a private, loopback, link-local or metadata address
  (the bot, the database, `169.254.169.254`) is aborted.
- It loads no images, media or fonts, allows no downloads, and blocks service workers.
- Pages are capped at 3 MB, with a 30 s timeout plus up to 20 s for the text to settle.

## Deploy (on the bot's server)

```bash
cd ~/Minghwee_chatbot && git pull
cd render-service
cp .env.example .env
#   set RENDER_SECRET (openssl rand -hex 32)
docker compose up -d --build
curl -s localhost:8100/health          # {"ok":true}
```

The port is bound to `127.0.0.1` only. To let kb-admin on Vercel reach it, add one location
to the bot's existing HTTPS server block in nginx, then `sudo nginx -t && sudo systemctl reload nginx`:

```nginx
location /kb-render/ {
    proxy_pass http://127.0.0.1:8100/;
    proxy_read_timeout 90s;
    client_max_body_size 8k;
}
```

Then on Vercel (kb-admin project, Production), set the following and redeploy:

- `KB_RENDER_URL` = `https://<the bot's domain>/kb-render/render`
- `KB_RENDER_SECRET` = the same value as `RENDER_SECRET`

To switch it off, unset those two variables. To remove the service:
`docker compose down` in this folder. The bot is unaffected either way.

Memory: Chromium is capped at 1 GB by `docker-compose.yml`. It runs only on "Fetch now",
or on the daily check for a page that needs it.
