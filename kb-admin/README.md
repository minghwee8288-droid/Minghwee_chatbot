# kb-admin

A **read-only** viewer for the chatbot's knowledge base and pricing rules, for the
agency's own staff. Phase 1: it can show, never change.

- **Knowledge base** (`/documents`) - each source document with its entry counts (active /
  inactive) and whether it is live.
- **Browse entries** (`/rows`) - every knowledge-base entry, with filters, text search and a
  detail page.
- **Pricing rules** (`/rules`) - the pricing and contact rules the bot reads (`cb_kb_rules`),
  locked rules marked.
- **Test a question** (`/test`) - runs the real bot on a question (optionally with follow-ups
  and a chosen audience), read-only, and shows the reply and the entries it used.

Standalone Next.js app. It imports nothing from `app/`, and deploys (later) as its own
Vercel project with Root Directory `kb-admin`.

## What it holds, and what it cannot do

It holds exactly three credentials, all server-side (nothing is `NEXT_PUBLIC_`):

1. the Supabase **anon** key, for sign-in only - a service-role or secret key is refused;
2. the **`kb_admin_reader`** database login - SELECT on the knowledge base (not the
   embeddings), the rules and the access list, and nothing else;
3. the bot's **preview secret**.

It holds no service-role key, no LLM key and no embedding key. Every query runs inside
`BEGIN READ ONLY`, `lib/db.ts` exports `select` only, and the grants on
`kb_admin_reader` are the hard limit underneath both.

## Who can sign in

A Supabase Auth account on the same project **plus** an active row in
`cb_kb_admin_users` with role `viewer`. Both are checked server-side on every page and
API request; no row, or `active = false`, means no access from the next click. kb-admin
cannot add, change or remove anyone - access rows are managed by a separate, reviewed
SQL change.

Sessions are httpOnly, SameSite=Strict cookies and end 12 hours after sign-in.

## Running locally

```bash
npm install
cp .env.example .env.local     # fill in; see the comments. Never commit it.
npm run selfcheck              # static + behaviour checks, no network
npm run build && npm start     # http://127.0.0.1:5176
```

kb-admin refuses to start unless every setting names the same Supabase project
(`KB_ADMIN_EXPECTED_REF`) and the database user is `kb_admin_reader`.

"Test a question" needs the bot's `POST /admin/preview` reachable at `BOT_PREVIEW_URL`,
with the same `ADMIN_PREVIEW_SECRET` set on the bot. Leave the secret empty to switch
the page off.

## Self-check

`npm run selfcheck` fails if any source file names a write verb, the Supabase data API,
a service-role key or `NEXT_PUBLIC_`; if `lib/db.ts` exports anything but `select`; if a
page or API route skips the auth check; if `.env.local` is tracked or any secret in it
appears in another file; or if the config validator or access decision misbehaves.
