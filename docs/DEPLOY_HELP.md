# Deploying Help (staff answer service) — VPS steps

Help is a separate container, `chatbot-help` (`uvicorn app.help.main:app`, port
8001 on 127.0.0.1). It reads the knowledge base read-only and writes nothing.
These steps touch **only** Help: Claire's `chatbot` container is not rebuilt or
recreated.

Run everything from the repo folder on the server (`/home/deploy/Minghwee_chatbot`).

## 1. Pull the code

```bash
git pull
git log -1 --oneline        # the Help commit, or newer
```

`git pull` may also bring changes to Claire's code. They do nothing until the
next full deploy rebuilds `chatbot`; steps 3-4 below do not.

## 2. Add the secret to `.env`

Generate a value and append it without printing it:

```bash
echo "HELP_SECRET=$(openssl rand -hex 32)" >> .env
grep -c '^HELP_SECRET=.\+' .env     # must print 1
```

Give the same value to the OS Portal backend through its own secret store (read it
with `grep '^HELP_SECRET=' .env | cut -d= -f2-` when you set it there, and never paste
it into a chat, ticket or log). The other `HELP_*` keys are optional; empty means the
defaults (see `.env.example`).

## 3. Build and start Help only

```bash
docker compose up -d --build chatbot-help
```

Naming the service builds and recreates that service only.

## 4. Check Claire was not touched

```bash
docker compose ps
```

`minghwee-chatbot` must show the same "Up ..." time as before step 3 (it was not
recreated). `minghwee-chatbot-help` should reach `healthy` within about 30 s.

## 5. Health check, on the server

```bash
curl -s http://127.0.0.1:8001/internal/help/health      # {"ok":true}
```

## 6. nginx

Paste `docs/nginx_help_location.conf` into the live site: the `limit_req_zone` line in
the http context, the `location` block inside the HTTPS server block for
bot.minghwee.com, above any `location /`. Then:

```bash
sudo cp /etc/nginx/sites-enabled/<site> ~/<site>.bak-$(date +%F)   # back up first
sudo nginx -t && sudo systemctl reload nginx
```

## 7. One test from the server (the secret is read from `.env`, never printed)

```bash
curl -s -X POST https://bot.minghwee.com/internal/help/answer \
  -H "Content-Type: application/json" \
  -H "X-Help-Secret: $(grep '^HELP_SECRET=' .env | cut -d= -f2-)" \
  -d '{"request_id":"'"$(cat /proc/sys/kernel/random/uuid)"'","user_role":"admin","messages":[{"role":"user","content":"What is the transfer fee for a Filipino helper?"}]}'
```

Expect `"status":"ok"` with sources. Without the header the answer is 401; a GET is
403 from nginx. Logs: `docker compose logs --tail=50 chatbot-help` shows one
`help: request_id=... status=...` line per request (no question text).

## 8. Rollback

```bash
docker compose stop chatbot-help
```

Then remove the `location = /internal/help/answer` block (and the `bot_help` zone line)
from nginx, `sudo nginx -t && sudo systemctl reload nginx`. Claire is unaffected
either way. To remove the container entirely: `docker compose rm -f chatbot-help`.

## Note: the normal full deploy

The usual `docker compose up -d --build` builds and (re)starts **both** `chatbot` and
`chatbot-help`. That is fine: they share the image source and Help starts nothing of
Claire's.
