# Clear Conversation UI

A web page that does what `scripts/reset_conversation.py` does, so the client can clear
one WhatsApp conversation without a terminal, a Python environment, or the database
credentials.

**Standalone by design.** This folder deploys to Vercel on its own. It does not import
from `app/`, it does not call the chatbot's API, and it changes nothing in `app/` or
`portal-ui/`. Deploying, breaking or deleting it cannot affect the running bot.

---

## What it does

1. **Enter the password and the number**, then press **Clear chat**.
2. **Confirm** — an itemised list of exactly what will go, then it runs.

It then reports each step: what was deleted, what was skipped, and what failed.

One form, one button. Earlier versions had a separate "Look up" step, a verification
panel and a tick box per lead; the client asked for all three gone, on the grounds that
they already know whose number they typed. **Every lead on the number is cleared
automatically.**

The lookup still happens — pressing Clear chat reads the number first — but it is
invisible, and its only job is to fill in the confirmation dialog so that dialog can say
what it actually found rather than asking "are you sure?" about nothing.

**The confirmation is deliberately kept.** Every delete here is irreversible and the only
thing identifying the target is a phone number typed by hand, so one click on a dialog
naming the contact is what stands between a mistyped digit and somebody else's
transcript.

### What gets deleted

Exactly what the Python script deletes, in the same child-first order:

| Step | Table | Scope |
|---|---|---|
| 1 | `cb_handovers` | `conversation_id` = the one conversation |
| 2 | `cb_tickets` | `conversation_id` = the one conversation |
| 3 | `wp_chat_messages` | `conversation_id` = the one conversation |
| 4 | `cb_checkpoints`, `cb_checkpoint_blobs`, `cb_checkpoint_writes` | `thread_id` = that thread |
| 5 | `leads` / `leads_candidate` | every lead on that number — each `DELETE` keyed on one resolved lead id |
| 6 | `wp_chat_conversations` | *updated*, not deleted — back to `bot_active`, fresh thread, identity cleared |

There is **no unscoped delete anywhere in this codebase**, and there must never be one:
`wp_chat_messages` is the portal's transcript table for every client.

`wp_chat_summaries` is deliberately left alone. Nothing in `app/` reads it, the Python
script does not touch it, and it belongs to the portal.

### What is different from the script

**It deletes leads, including employer leads.** `reset_conversation.py` refuses to touch
`leads`, deliberately — that table holds real sales pipeline. The client asked for the
lead to go with the conversation, so this tool clears every lead on the number. Two
protections remain:

- a lead **something else still references** is refused, with the reason, and left in
  place (see below) — the conversation is still cleared;
- each `DELETE` is keyed on **one resolved lead id**, never on the phone, so the loose
  last-four-digits phone match can never sweep up a lead belonging to somebody else.

Deleting the lead *and* the checkpoint together is also the procedure CLAUDE.md §10
already prescribes: deleting the lead alone is what broke conversation 36, where the
checkpoint kept pointing at the dead row and every ticket insert failed the foreign key,
silently, for twenty minutes.

**It can clear a lead with no conversation.** A lead outlives its conversation, and the
client still needs a way to remove it.

---

## Why a lead sometimes cannot be deleted

`leads` is referenced by three tables and **every one of those foreign keys is
`ON DELETE NO ACTION`** — confirmed against the live database, not assumed:

```
cb_tickets.created_lead_id                    (CLAUDE.md §9.5)
lead_activities.lead_id
employer_service_requests.converted_lead_id
```

So a delete that collides with any of them fails outright. The UI checks all three
**before** offering the lead as deletable, greys it out, and says which rows are in the
way. Without that check the client would get a raw Postgres foreign-key error.

This is not theoretical: of the four employer leads in the database when this was built,
two were blocked by `lead_activities` rows.

`leads_candidate` has no inbound foreign keys, so it is never blocked.

Once `scripts/ticket_lead_fk_set_null.sql` is applied (CLAUDE.md §9.5), the
`cb_tickets` blocker disappears on its own. The other two still stand and should.

---

## Deploying to Vercel

From the client's Vercel account:

1. **New Project** → import this repository.
2. Set **Root Directory** to `reset-ui`. This is the important one — without it Vercel
   tries to build the repository root, which is a Python service.
3. Framework preset: **Next.js** (detected automatically).
4. Add the environment variables from [`.env.example`](.env.example) under
   **Settings → Environment Variables**, for **Production** and **Preview**:

   | Variable | Required | Notes |
   |---|---|---|
   | `SUPABASE_URL` | yes | same project as the chatbot |
   | `SUPABASE_SERVICE_ROLE_KEY` | yes | service_role, **not** anon |
   | `TENANT_ID` | yes | must match the chatbot's, or no lead is found |
   | `RESET_UI_PASSWORD` | no | overrides the built-in password (see below) |
   | `SUPABASE_DB_URL` | recommended | pooler URI, port 6543 — clears the bot's memory |
   | `RESET_ALLOWED_NUMBERS` | optional | fail-closed safety list; empty = any number |

5. **Deploy**, then open the URL and enter the password.

### The password

The page ships with a built-in password — **`Minghwee@123`** — defined as
`DEFAULT_PASSWORD` in [`lib/env.ts`](lib/env.ts). It works with no configuration, which
is why `RESET_UI_PASSWORD` is not in the required list.

Be clear-eyed about what that means: the password is **in this repository**, so anyone
who can read the repo has it, and changing it means editing that line and redeploying.

Setting `RESET_UI_PASSWORD` in the Vercel project overrides it — the environment always
wins — which keeps the real password out of git and lets it be rotated from the Vercel
dashboard without a code change. Worth doing the day this is pointed at live client
numbers rather than test ones. Nothing else has to change to switch over.

There is no user table and nothing to migrate either way.

### Use the pooler URL for `SUPABASE_DB_URL`

Transaction mode, port 6543. A serverless function opens a connection per request; the
direct connection (5432) will run into Supabase's connection limit. The code already sets
`prepare: false`, which the transaction pooler requires.

If `SUPABASE_DB_URL` is left blank the tool still works — the LangGraph checkpoint is
simply left behind, and both the page and the result report say so rather than claiming
success. That matches the Python script, which prints the same warning and carries on.

---

## Security

- The **service-role key never reaches the browser.** No variable is prefixed
  `NEXT_PUBLIC_`, and every database call happens inside a serverless function.
- The password is compared **timing-safely**, with a best-effort per-instance throttle
  after repeated failures. That throttle is a speed bump, not a lockout — a serverless
  deployment cannot hold reliable shared state, and it is documented as such rather than
  relied upon.
- The API accepts **only a phone number**. The conversation and every lead are resolved
  from it server-side, so no request can name a row belonging to somebody else, and table
  names are never taken from the request.
- Lead blockers are **re-checked at the moment of deletion**, not trusted from the lookup.

If you want the tool restricted to test numbers, set `RESET_ALLOWED_NUMBERS`. It fails
closed the same way `BOT_ALLOWED_NUMBERS` does: a list of entirely malformed numbers
locks everything out rather than silently allowing everyone.

---

## Running locally

```bash
cd reset-ui
npm install
cp .env.example .env.local     # then fill it in
npm run dev                    # http://localhost:5175
```

The password is `Minghwee@123` unless you set `RESET_UI_PASSWORD` in `.env.local`.

`.env.local` is gitignored.

> Do not run `npm run build` while `npm run dev` is running — they share `.next`, and the
> build will pull the directory out from under the dev server, which then fails with
> `Cannot find module './chunks/vendor-chunks/next.js'`. Stop the dev server first.

---

## How this was verified

Not by reading it. Against the live database, with a synthetic conversation seeded for
the purpose and deleted afterwards:

- a conversation with 4 messages, 1 ticket, 1 handover, 2 checkpoint rows and a candidate
  lead — every row confirmed gone afterwards by a direct SQL query, the conversation
  confirmed back at `bot_active` with a new thread and identity cleared, and the other
  55,802 messages in the table confirmed untouched;
- a conversation with a deletable **employer** lead — both cleared in one operation;
- an employer lead with a `lead_activities` row — **refused**, with the reason, while the
  conversation still cleared: a partial outcome reported honestly rather than a
  foreign-key crash;
- a number with nothing on it — refused;
- a wrong password, a near-miss password and a missing password — all 401;
- format-tolerant matching against real rows stored as `+6592466313`, `+918989898989`
  and bare `9713339155`.

The phone-matching rules in `lib/phone.ts` are a deliberate line-for-line port of
`app/utils.py`, and each function names its Python original. If those rules change on the
Python side, change them here too — a mismatch means this tool reports "no conversation"
for a client who has one.

---

## Files

```
app/
  page.tsx              the whole flow: look up, then clear
  layout.tsx            shell
  api/lookup/route.ts   read-only preview  (POST)
  api/reset/route.ts    the destructive one (POST)
lib/
  reset.ts              the reset itself — mirrors reset_conversation.py
  lookup.ts             conversation / contact / lead lookup
  blockers.ts           why an employer lead cannot be deleted (one copy, two callers)
  db.ts                 supabase-js + the direct Postgres connection
  phone.ts              port of app/utils.py
  env.ts                configuration and the safety gate
  auth.ts               the shared-password check
  types.ts              shapes shared by the API and the page
components/
  ConfirmDialog.tsx     the itemised confirmation
  ui.tsx                small shared pieces
```
