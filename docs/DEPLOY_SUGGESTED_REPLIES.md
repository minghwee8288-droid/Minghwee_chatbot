# Deploying AI Suggested Replies (`POST /internal/suggest`)

The steps for the bot's server. The endpoint is described in
`docs/SUGGESTED_REPLIES.md`. It writes nothing, and it does not exist until
`SUGGEST_SECRET` is set. Deploy in two stages: first ship the code with the
feature **off** and check that the bot is unchanged; then switch it on.

Server paths used below: repo `/home/deploy/Minghwee_chatbot`, compose service
`chatbot`, container `minghwee-chatbot`, port `127.0.0.1:8000`.

Rules for every step:

- Never `cat`, `grep` or `printenv` the value of any `.env` line. Check for a
  setting by **counting** its lines, or by measuring its length.
- `.env` is not in git. `git pull` never changes it.
- After any `.env` change, run `docker compose up -d`. Do **not** use
  `restart`: it does not re-read `env_file`.

## 0. Before you start

- The code must be on `main`. This feature is built on the
  `feature/suggested-replies` branch, so whoever owns the release merges it
  first. Merging to `main` also redeploys the two Vercel apps (kb-admin and
  reset-ui). They do not import anything from `app/`, so their behaviour does
  not change.
- On a development machine, all four suites must pass on the merged code:
  - `scripts/smoke_nodes.py`: 558
  - `scripts/selfcheck_flows.py`: 961
  - `scripts/selfcheck_golive.py`: 37
  - `scripts/selfcheck_suggest.py`: 127
- No new Python packages are needed. `requirements.txt` is unchanged, so
  `fastapi`, `pydantic` and `openai` are already pinned.

## 1. Record the current commit (for rollback)

```bash
cd /home/deploy/Minghwee_chatbot
git rev-parse HEAD | tee ~/suggest_rollback_commit.txt
git status --short            # must be empty apart from untracked files
```

## 2. Pull and rebuild, with the feature OFF

```bash
grep -c '^SUGGEST_SECRET=' .env      # must print 0 (feature off)
git pull
git log -1 --oneline                 # the merge commit you expect
docker compose up -d --build
```

The `Dockerfile` copies all of `app/` into the image, so `app/suggest/` comes
with it. No compose or Dockerfile change is needed.

## 3. Health checks (feature off)

```bash
docker compose ps                                    # chatbot "healthy" (allow ~30 s)
curl -s localhost:8000/health
curl -s localhost:8000/health/ready                  # always 200: read the body
docker compose logs --tail 50 chatbot | grep "Safety gate"   # same numbers as before
docker compose exec chatbot printenv | grep -c '^SUGGEST_SECRET='   # 0
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/internal/suggest
#   must print 404: the route is not registered
```

Then send one WhatsApp message from an allowlisted test number and check that
the bot answers as before. Leave it like this as long as you want; with the
feature off, the only change to the bot is the code that is now in the image.

## 4. Switch it on: add `SUGGEST_SECRET`

Generate a **new** secret on the server. Never reuse a local or test value.
This command writes it to `.env` without printing it:

```bash
cd /home/deploy/Minghwee_chatbot
cp .env ~/env_backup_$(date +%Y%m%d_%H%M%S)
[ -n "$(tail -c1 .env)" ] && echo >> .env            # make sure .env ends with a newline
grep -c '^SUGGEST_SECRET=' .env                      # must print 0 before appending
printf 'SUGGEST_SECRET=%s\n' "$(openssl rand -hex 32)" >> .env
grep -c '^SUGGEST_SECRET=' .env                      # must print 1
docker compose up -d
```

Give the value to the Smart Inbox backend through the agreed secure channel,
such as the password manager. Keep it out of chat, tickets, logs and screenshots.
Only that backend sends it, as the `X-Suggest-Secret` header. A browser never
sees it.

Optional settings, all with safe defaults. Add any of them only if you need to:

| Setting | Default | What it does |
|---|---|---|
| `SUGGEST_MODEL` | `openai/gpt-6-luna` | The model that writes the drafts |
| `SUGGEST_MAX_CONCURRENCY` | `2` | Requests at once; any more get 429 straight away |
| `SUGGEST_TIMEOUT_SECONDS` | `25` | Whole-request limit; past it the caller gets 504 |

## 5. Check that the secret is present (never print it)

```bash
docker compose exec chatbot printenv | grep -c '^SUGGEST_SECRET='              # 1
docker compose exec chatbot sh -c 'printf %s "$SUGGEST_SECRET" | wc -c'        # 64
docker compose ps                                                              # healthy
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/internal/suggest \
     -H 'Content-Type: application/json' -d '{}'
#   must print 401: the route exists and refuses a request without the header
```

## 6. nginx: expose only this path

Port 8000 is bound to `127.0.0.1`, so the Smart Inbox reaches the endpoint
through nginx. Find the HTTPS `server` block that already serves `/webhook/`:

```bash
sudo nginx -T 2>/dev/null | grep -nE 'server_name|listen|location|proxy_pass'
```

Back up that file, then add this block inside it:

```nginx
location = /internal/suggest {
    limit_except POST { deny all; }
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 35s;
    client_max_body_size 8k;
}
```

- `=` matches only this exact path, so nothing else under `/internal/` is
  exposed.
- `proxy_read_timeout 35s` is longer than the app's own 25 s limit. A slow
  request therefore gets the app's `504 {"error":"timeout"}`, not an nginx
  error page.
- The app refuses a body over 4,000 bytes; 8k leaves headroom.

Then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 7. Test from outside the server

Replace `<bot domain>` with the bot's public hostname (the one in the Whapi
webhook URL).

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<bot domain>/internal/suggest \
     -H 'Content-Type: application/json' -d '{}'
#   must print 401 (no header)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<bot domain>/internal/suggest \
     -H 'X-Suggest-Secret: wrong' -H 'Content-Type: application/json' -d '{}'
#   must print 401 (wrong secret)
curl -s -o /dev/null -w '%{http_code}\n' https://<bot domain>/internal/suggest
#   must print 403 (GET refused by nginx)
```

Do not test with the real secret from a shared shell. That end-to-end call is
the Smart Inbox team's test, made from their backend. Each request writes one
log line with ids, counts and timings, and no message text:

```bash
docker compose logs --since 10m chatbot | grep 'suggest:'
```

## 8. Rollback

**A. Switch the feature off (code stays).** This is the quickest option, and
it changes nothing else.

```bash
cd /home/deploy/Minghwee_chatbot
cp .env ~/env_backup_$(date +%Y%m%d_%H%M%S)
sed -i '/^SUGGEST_SECRET=/d' .env
grep -c '^SUGGEST_SECRET=' .env                     # 0
docker compose up -d
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/internal/suggest   # 404
```

Then remove the `location = /internal/suggest` block from nginx and run
`sudo nginx -t && sudo systemctl reload nginx`. Or leave the block: it only
leads to a 404.

**B. Roll the code back to the commit recorded in step 1.**

```bash
cd /home/deploy/Minghwee_chatbot
git checkout "$(cat ~/suggest_rollback_commit.txt)"   # detached HEAD on the old commit
docker compose up -d --build
docker compose ps                                     # healthy
curl -s localhost:8000/health
docker compose logs --tail 50 chatbot | grep "Safety gate"
```

Do A first if the secret is still set, because the old code ignores it anyway.
To return to the current code later, run `git checkout main && git pull` and
then `docker compose up -d --build`. Rollback never involves the database: the
endpoint writes nothing.
