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
   It can do one thing: EXECUTE 24 database functions (`lib/access.ts`
   `WRITE_FUNCTIONS`), all SECURITY DEFINER and owned by NOLOGIN roles:
   - Q&A entries (owner `kb_admin_fn_owner`, migrations 004 and 008): `kb_admin_save_draft`,
     `kb_admin_publish`, `kb_admin_discard_draft`, `kb_admin_restore`, `kb_admin_toggle`,
     `kb_admin_create_entry`;
   - documents (owner `kb_admin_doc_owner`, migrations 011 and 014): `kb_admin_doc_upload`,
     `kb_admin_doc_stage`, `kb_admin_doc_edit_chunk`, `kb_admin_doc_mark_checked`,
     `kb_admin_doc_publish`, `kb_admin_doc_restore`, `kb_admin_doc_discard`,
     `kb_admin_doc_retire`, and two read-only searches, `kb_admin_match_with_batch` and
     `kb_admin_match_live`;
   - the KB watcher (owner `kb_admin_fn_owner`, migration 015): `kb_admin_source_add`,
     `kb_admin_source_remove`, `kb_admin_source_record_fetch`, `kb_admin_watch_propose`,
     `kb_admin_watch_edit`, `kb_admin_watch_reject`, `kb_admin_watch_approve`,
     `kb_admin_watch_mark_published`; and once migration 016 is applied,
     `kb_admin_watch_propose_checked`, `kb_admin_source_set_suggestions`,
     `kb_admin_source_redraft`.
   They enforce every rule themselves: the caller's role, the character limits, no
   NRIC/FIN, which changes need an approver, stale drafts and impact checks, the canary,
   and an audit row for each write. `lib/write.ts` exports only `callWrite`, which sends
   one of those fixed statements.
4. The embedding key (only when the editor is on), to embed an entry's new text.
5. The bot's **preview secret**, for Test a question.

It holds no service-role key, and no LLM key unless the KB watcher is switched on (below). Nothing it does can touch the rules, the
access list, a Q&A entry outside the Q&A functions, or a document chunk outside the
document functions (which never touch a Q&A row, never delete, and switch rows on and off
only by publish, restore and retire) - the database refuses, whatever the app sends.

### Documents (Phase 3 scope B)

Upload a .docx, .md or .txt file (4 MB at most; never PDF) on **Knowledge base → Upload
document**. Preview chunks it (`lib/chunker.ts`) and shows warnings - facts the live rows
state that the upload drops, older facts it brings back (`lib/facts.ts`), staff wording,
Q&A entries left alone, an imported document being re-split - and saves nothing. Prepare
stores the file, embeds the ticked chunks as heading + newline + content, and stages them.
The impact check runs the 42 probe questions through the bot's search now and with the
batch in place, with the bot's own filtering and reranking (`lib/retrieval.ts`, proved
identical to `rag.py` by `scripts/check_rerank_parity.py`). An approver publishes once
the check is recorded. The first replacement of an imported document records its
imported chunks as an earlier version, so they can be restored.

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

## KB watcher (watch, review, publish) - off by default

Migration `scripts/sql/kb_admin_015_watcher.sql` (on production since 2026-10-15) plus
`KB_SOURCES_ENABLED=true` turn on two pages:

- **Watched pages** (`/sources`): add / label / remove a public page (category: government
  rule, process, embassy, other). **Fetch now** reads it politely (our user-agent, robots.txt
  obeyed, timeouts, retries with backoff, one request per host at least 10 s apart), keeps a
  snapshot of the main text, and - only if the normalised text changed (whitespace, curly
  quotes, date stamps and date-only lines ignored; a date inside a sentence still counts) -
  asks the drafting model for knowledge-base proposals. A failed fetch is recorded and shown
  against the page.
- **Page changes to review** (`/watch`): each proposal shows the page, the exact source
  passage, what changed on the page, and old vs proposed entry. **Approve** (approvers only)
  goes through the existing Q&A path: `kb_admin_save_draft` / `kb_admin_create_entry`, the
  canary-checked embedding, `kb_admin_publish`; provenance (source URL, fetched_at,
  approved_by, approved_at) stays on the proposal. **Edit** and **Reject** for editors.
- **The fee lock**: a proposal naming an agency fee, putting a money figure beside a helper's
  salary, or aimed at a row from the agency's fees document (23 questions, 7 corrections -
  generated from the loader by `scripts/gen_fee_locked.py`) is shown as **Needs agency
  confirmation** and cannot be approved; the database refuses it too (KB007). So is any money
  figure from a page that is not in the **government rule** category.
- **Government figures**: a money figure from a government rule page (levy, security bond, MOM
  fees, minimum income, insurance minimums) can be approved, under the banner "This changes a
  figure: check it against the source before approving" and only with the approver's tick; the
  database refuses it without the tick (KB008) and records it.

**When a page gives no readable text** (2026-10-15), the page's record says why:
- **Blocked by site**: a bot-protection or challenge page (Cloudflare, Akamai, Incapsula, a
  captcha). It is never worked around; use the page's PDF or print version, or another
  official page with the same facts.
- **Under maintenance**: a maintenance page, or a redirect to `/maintenance/`. The page is
  read again on the next check.
- **Built by JavaScript**: an app shell whose text only appears in a browser. With
  `KB_RENDER_URL` and `KB_RENDER_SECRET` set, the page goes to the page renderer
  (`render-service/` on our server; see its README), through the same address, robots.txt
  and per-host gap checks. Without them it is reported as needing the renderer.

The extractor reads ASP.NET pages that wrap the whole page in one `<form>` (mom.gov.sg), and
reads a listing page with several `<article>`s whole.

**Better drafting (2026-10-16; migration 016 is on production):**
- **Low-value proposals are dropped.** A draft that only points elsewhere ("Where can I find
  information about...", "The MOM page provides information on...", "visit this page") or that
  states no fact a client could act on (an amount, a rule, a deadline, an eligibility condition,
  a step) is never proposed (`lib/watcher/quality.ts`, and the same rule in the prompt).
- **An index page drafts nothing.** A page that is mostly links to the pages below it (MOM's
  `.../foreign-domestic-worker-levy`) is recognised (`lib/watcher/indexPage.ts`); its useful
  sub-pages appear on its Watched pages row as **Suggested pages to watch**, each with a
  one-click **Watch this page** (category = the parent's).
- **Overlap check.** Every draft is searched against the live knowledge base with the bot's own
  search. A new draft that duplicates a live entry (similarity 0.72 or more) becomes a change to
  that entry, or nothing if the entry already says it. Live entries at 0.60 or more are listed on
  the proposal under **Similar existing entries** (link, similarity, figures that differ). Two
  figures that disagree (same kind, same period, said of the same thing - "$350 against $300 for
  the levy") put up a conflict banner and need the approver's tick; the database refuses the
  approval without it (KB009).
- **Re-draft from latest snapshot** (approvers only, audit-logged with a reason): supersedes the
  page's open proposals and drafts again from its last snapshot, without waiting for the page to
  change.
- **Quote matching ignores spacing only.** The source passage is found on the page with spacing
  of every kind set aside (tabs, line breaks, non-breaking spaces, "1 st" for "1st"); the words and
  figures must match exactly, and the page's own span is what is stored and shown.
- **Changes to existing entries are additive.** The model returns only the new facts to add (each
  with its page passage) and the sentences the page contradicts (with the contradicting passage);
  kb-admin builds the entry from the live text, so nothing else in it can change. The review page
  shows each sentence as added (green), replaced (old -> new, with the page line) or unchanged
  (grey). If there is nothing to add or replace, nothing is proposed.
- **Safety net: a change never silently loses a fact.** A live sentence the proposal no longer
  carries (after an Edit, say) is shown in red under **Points removed**; unless the source page
  contradicts it, Approve is blocked with "This update drops existing information".
- **No rewording-only changes.** A change that adds no new fact, figure, rule, deadline or step is
  not proposed.
On a database without 016 kb-admin reads and calls exactly what it did with 015 (no suggestions,
no panel, no re-draft); it checks once a minute.

`WATCHER_ENABLED=true` (+ `CRON_SECRET`, `KB_WATCHER_ACTOR_ID`) adds the schedule:
`vercel.json` calls `/api/watcher/run` daily and each page is re-checked every
`WATCHER_INTERVAL_DAYS` (default 7). Without them the route answers 404. Nothing the watcher
does reaches the chatbot without an approver.

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

`scripts/sql/kb_admin_001` … `014`, applied in order with
`python scripts/apply_sql.py --expect-ref <project ref> <file>`; 007 only checks. 001
records the Phase 1 read-only setup and changes nothing where it already exists. After
005, set the editor's password by hand (`ALTER ROLE kb_admin_editor PASSWORD '…' VALID
UNTIL 'infinity'`) - the file's placeholder is already expired. Then seed the canary.

## Self-check

`npm run selfcheck` fails if any source file names a write verb or a `kb_admin_*` function
outside the sixteen allowed functions and two action modules, the Supabase data API, a service-role key or
`NEXT_PUBLIC_`; if `lib/db.ts` exports anything but `select`; if a page, API route or
server action skips the auth or role check; if a form reads its state without a default
(it is `undefined` after a redirect); if `.env.local` is tracked or any secret in it
appears in another file; or if the config validator, access decision or editing rules
misbehave.
