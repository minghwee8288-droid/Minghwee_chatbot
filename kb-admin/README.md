# kb-admin

The agency's own tool for the chatbot's knowledge base and pricing rules.

- **Knowledge base** (`/documents`) - each source document with its entry counts (active /
  inactive) and whether it is live.
- **Browse entries** (`/rows`) - every knowledge-base entry, with filters, text search and a
  detail page. On a Q&A entry: **Edit**, a **History** tab (every published version, with
  Restore) and, for an approver, **Switch off / on**.
- **Draft review** (`/drafts/<id>`) - the change side by side with what is live, then
  Publish or Discard.
- **Pending approval** (`/pending`) - drafts waiting for an approver.
- **Activity** (`/activity`) - the audit log: every change, by whom and why, including
  writes to the knowledge base that did not come through kb-admin.
- **Pricing rules** (`/rules`) - the pricing and contact rules the bot reads (`cb_kb_rules`),
  locked rules marked. Read-only.
- **Test a question** (`/test`) - runs the real bot on a question (optionally with follow-ups
  and a chosen audience), read-only, and shows the reply and the entries it used.

Standalone Next.js app. It imports nothing from `app/`, and deploys as its own Vercel
project with Root Directory `kb-admin`.

## Two logins, and what each can do

Every credential is server-side (nothing is `NEXT_PUBLIC_`).

1. The Supabase **anon** key, for sign-in only - a service-role or secret key is refused.
2. **`kb_admin_reader`** - every page read. SELECT on the knowledge base (not the
   embeddings), the rules, the access list, the version history, the audit log and the
   canary. Every query runs inside `BEGIN READ ONLY`; `lib/db.ts` exports `select` only.
3. **`kb_admin_editor`** (only when the editor is on) - holds **no table privilege at all**.
   It can do one thing: EXECUTE five database functions, `kb_admin_save_draft`,
   `kb_admin_publish`, `kb_admin_discard_draft`, `kb_admin_restore` and `kb_admin_toggle`.
   Those are SECURITY DEFINER, owned by the NOLOGIN role `kb_admin_fn_owner`, and enforce
   every rule themselves: the caller's role, the 1200-character limit, no NRIC/FIN, which
   changes need an approver, stale drafts, the canary, and an audit row for each write.
   `lib/write.ts` exports only `callWrite`, which sends one of those five fixed statements.
4. The embedding key (only when the editor is on), to embed an entry's new text.
5. The bot's **preview secret**, for Test a question.

It holds no service-role key and no LLM key. Nothing it does can touch a document
chunk, the rules, the access list or anything outside Q&A entries - the database
refuses, whatever the app sends.

### The editor is off unless all of its settings are set

`KB_ADMIN_DB_PASSWORD_EDITOR` (or `_FILE`) and `KB_ADMIN_EMBEDDING_API_KEY` (plus
`KB_ADMIN_EMBEDDING_BASE_URL` for OpenRouter). With **none** set kb-admin is exactly the
read-only viewer: no edit control is shown and every write action refuses. With only
**some** set it is still read-only and the start-up log names what is missing. The editor
password must differ from the reader's.

### Embeddings must match the bot's

Before storing a vector kb-admin embeds a fixed sentence and compares it with
`cb_kb_canary`, which the **bot's own** embedding function produced
(`scripts/seed_kb_canary.py`). No match, no publish. The model name is read from the
canary row, so kb-admin cannot drift onto a different model. The bot embeds through
OpenRouter (`openai/text-embedding-3-small`, 1536 dims), so kb-admin needs an OpenRouter
key and `KB_ADMIN_EMBEDDING_BASE_URL=https://openrouter.ai/api/v1`.

## Who can sign in, and what they can do

A Supabase Auth account on the same project **plus** an active row in
`cb_kb_admin_users`. Both are checked server-side on every page and action; no row, or
`active = false`, means no access from the next click.

| Role | Can |
|---|---|
| `viewer` | read everything |
| `editor` | + save drafts, publish **wording-only** changes, restore, discard their own drafts |
| `approver` | + publish any change, approve an editor's pending change, switch entries on/off |

A change needs an approver when it touches a **number**, the **service**, the
**audience**, the **nationality** or **on/off**. An editor's such change is saved as a
pending draft and appears under Pending approval.

kb-admin cannot add, change or remove anyone - access rows are managed by a separate,
reviewed SQL change. Sessions are httpOnly, SameSite=Strict cookies and end 12 hours after
sign-in.

## Edits the loader will not undo

A published edit tags the entry `metadata.managed_by = 'ui'`, and
`scripts/load_service_notes.py` skips and reports every such entry on all four of its
write paths. Nothing else in the repo writes to Q&A entries.

## Running locally

```bash
npm install
cp .env.example .env.local     # fill in; see the comments. Never commit it.
npm run selfcheck              # static + behaviour checks, no network
npm run build && npm start     # http://127.0.0.1:5176
```

kb-admin refuses to start unless every setting names the same Supabase project
(`KB_ADMIN_EXPECTED_REF`) and the database user is `kb_admin_reader`.

**Against the TEST project:** put its values in `.env.test.local` (git-ignored, same keys)
and run `npm run dev:test` - port 5177, build folder `.next-test`. Every key is pinned from
that file (a missing one is blank, never taken from `.env.local`), and the launcher refuses
a file that names the production project.

"Test a question" needs the bot's `POST /admin/preview` reachable at `BOT_PREVIEW_URL`,
with the same `ADMIN_PREVIEW_SECRET` set on the bot. Leave the secret empty to switch
the page off.

## The database side

`scripts/sql/kb_admin_001` … `007`, applied in order with
`python scripts/apply_sql.py --expect-ref <project ref> <file>`; 007 only checks. 001
records the Phase 1 read-only setup and changes nothing where it already exists. After
005, set the editor's password by hand (`ALTER ROLE kb_admin_editor PASSWORD '…' VALID
UNTIL 'infinity'`) - the file's placeholder is already expired. Then seed the canary.

## Self-check

`npm run selfcheck` fails if any source file names a write verb or a `kb_admin_*` function
outside the five allowed call sites, the Supabase data API, a service-role key or
`NEXT_PUBLIC_`; if `lib/db.ts` exports anything but `select`; if a page, API route or
server action skips the auth or role check; if a form reads its state without a default
(it is `undefined` after a redirect); if `.env.local` is tracked or any secret in it
appears in another file; or if the config validator, access decision or editing rules
misbehave.
