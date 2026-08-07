# Ming Hwee WhatsApp Chatbot

WhatsApp assistant for **Ming Hwee Employment Agency** (Singapore, MOM Licence 12C6072).
It sits on Thomas's existing WhatsApp Business number via Whapi, answers common
enquiries from the knowledge base, collects the basics for each service request, and
hands the thread to a human sales agent **silently** — same thread, no announcement,
no sign that a bot was ever involved.

This is **Project 2** of three. It shares one Supabase database with the RAG pipeline
(Project 1) and the Ming Hwee OS platform (Project 3). It **reads** `cb_knowledge_base`,
and **writes** only to `wp_chat_*`, `cb_tickets` and `cb_handovers`.

---

## Architecture

```
WhatsApp ──► Whapi ──► POST /webhook/whapi
                              │
                   verify signature, parse, ack 200
                              │
                    ┌─────────┴──────────┐
              from_me = true        from_me = false
                    │                     │
          agent detector          store message + identify contact
     (bot_status = human_active)          │
                                    debounce 3s
                                          │
                                LangGraph conversation engine
                                          │
        ┌─────────────────────────────────┼──────────────────────────┐
   intent_classifier ──► rag_retriever ──► response_generator ──► reply
        │                                              │
        ├──► info_collector ──► ticket_creator ──► handover_executor
        │                                              │
        └──► handover_executor (dispute_assault, immediate)
```

| Layer | Module |
|---|---|
| Webhook + pipeline | [app/api/webhook.py](app/api/webhook.py) |
| Whapi client / parser / debouncer | [app/whapi/](app/whapi/) |
| Conversation, contact, message, ticket, handover, assignment, RAG, transcription | [app/services/](app/services/) |
| LangGraph state, nodes, prompts | [app/graph/](app/graph/) |
| Supabase access | [app/db/supabase.py](app/db/supabase.py) |

---

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

copy .env.example .env          # then fill it in
python run.py
```

Start it with `run.py`, not `uvicorn app.main:app`. On Windows the default
ProactorEventLoop cannot run psycopg in async mode, so the Postgres checkpointer would
fall back to memory and conversation state would not persist. The policy has to be set
before the event loop exists, which is earlier than uvicorn imports the app.

Docker:

```bash
docker compose up --build
```

Check it is alive:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/ready     # per-dependency status
```

### Environment

See [.env.example](.env.example). Required for the bot to work end to end:
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `WHAPI_API_TOKEN`, `OPENROUTER_API_KEY`,
`OPENAI_API_KEY`, `TENANT_ID`. In `ENVIRONMENT=production` the app refuses to start
without them; in development it warns and continues.

`SUPABASE_DB_URL` is the direct Postgres connection string, used only by the LangGraph
checkpointer (it needs a real Postgres connection, not the REST API). Without it the
bot falls back to an in-memory checkpointer and multi-turn info collection resets on
restart — fine for local work, not for production. On first start LangGraph creates its
own `checkpoints*` tables in that database; no application table is touched.

### Whapi

Point the channel's webhook at `https://<your-host>/webhook/whapi`, subscribe to the
`messages` event with **both** directions (`post` for incoming and outgoing), and set a
shared secret matching `WHAPI_WEBHOOK_SECRET`. The secret is accepted as
`X-Whapi-Secret` / `X-Webhook-Secret`, as an HMAC-SHA256 body signature in
`X-Whapi-Signature`, or as a bearer token in `Authorization`.

Outgoing messages must be delivered too — that is how the agent detector notices Thomas
or a sales agent replying from the WhatsApp Business app.

### Retrieval contract

The query embedding must be produced exactly the way the RAG pipeline built the stored
vectors, or every similarity score comes back low and the bot hands over on every
message:

```
model      : text-embedding-3-small
dimensions : 1536          (EMBEDDING_DIMENSIONS, passed explicitly on every call)
provider   : EMBEDDING_BASE_URL — blank for OpenAI direct,
             https://openrouter.ai/api/v1 to match the pipeline
namespace  : RAG_NAMESPACE — blank searches all, or pin to document_chunks
```

`cb_knowledge_base.embedding` must be `vector(1536)` and `cb_match_knowledge_base` must
take a `vector(1536)` argument. A dimension mismatch is caught at the embedding call and
raised rather than silently returning nothing.

Verify and calibrate against the live knowledge base:

```bash
python scripts/check_retrieval.py
```

It reports namespace/row counts, runs ten representative client questions, prints the
similarity distribution, and suggests `RAG_MATCH_THRESHOLD` / `RAG_CONFIDENCE_FLOOR`.

Calibrated against the live knowledge base (791 chunks): answerable client questions
score **0.533–0.771**, off-topic messages **0.147–0.344**. The shipped values of 0.35 /
0.45 sit between those bands. Re-run this after any change to the RAG pipeline.

Known content issues in the knowledge base, with fix-up SQL, are documented in
[scripts/kb_hygiene.sql](scripts/kb_hygiene.sql) — a stale MOM insurance figure that
outranks the correct one, and internal delivery documents that are client-retrievable.

### Preflight

```bash
python scripts/preflight.py
```

Read-only check of everything the bot needs at runtime: knowledge base, voice
transcription, tenant, agent rotation, the admin escalation target, the portal user
bridge and the tables it writes to. Anything reported as a BLOCKER means handovers will fail. Seeding
SQL for the data gaps is in [scripts/seed_assignment.sql](scripts/seed_assignment.sql).

### Testing against real WhatsApp, from a laptop

Terminal 1 — the tunnel (leave running):

```bash
cloudflared tunnel --url http://localhost:8000
```

Terminal 2 — the bot:

```bash
python run.py
```

Then paste the printed `https://….trycloudflare.com/webhook/whapi/<secret>` into the
Whapi channel's webhook settings, **adding** it alongside the portal's own webhook.
Free tunnels get a new hostname on every start, so this has to be repeated at the
start of each session.

### Testing without WhatsApp

```bash
python scripts/simulate_message.py "how much for a filipino helper"
python scripts/simulate_message.py "noted, will call you" --from-me   # agent takes over
```

---

## How the conversation works

**Gate.** Nothing is generated unless `wp_chat_conversations.bot_status = 'bot_active'`.
The status is re-checked after the debounce window, so an agent who replies mid-typing
always wins.

**Debouncing.** Inbound messages are stored immediately (the portal shows them at once)
but buffered for `DEBOUNCE_SECONDS`. Three messages in three seconds become one input
and one reply.

**Contact identification** ([app/services/contact.py](app/services/contact.py)) runs on
the first message: employer → supplier → partner → candidate → unknown. Employers also
get their active case looked up through `placements`. Numbers are matched against every
spelling found in the platform tables (`+6591234567`, `6591234567`, `91234567`).

**Intents.** `greeting`, `general_question`, `process_question`, `document_question`,
`case_enquiry`, `fee_enquiry`, `salary_enquiry`, the seven services, `dispute_salary`,
`dispute_assault`, `other`. Assault is additionally caught by a keyword guard that
overrides the classifier — safety never depends on model confidence.

**Information collected per service** — see `SERVICE_FIELDS` in
[app/services/ticket.py](app/services/ticket.py). The collector asks one question per
message, in the agency's voice, and never re-asks something already answered.

**Handover triggers**

| Trigger | Reason logged | Reply sent first |
|---|---|---|
| All fields collected → ticket | `ticket_raised` | short "let me pull this together" |
| Fee / salary enquiry complete | `fee_enquiry` / `salary_enquiry` | no figures, ever |
| Assault / violence | `dispute_escalation` | one empathetic message + 999 advice |
| Salary or leave dispute | `dispute_escalation` | acknowledgement |
| Nothing relevant in the KB | `bot_confused` | "let me check with the team" |
| Client asks for a person | `client_requested` | none |
| Graph or Whapi failure | `bot_confused` | none |

**Assignment** ([app/services/assignment.py](app/services/assignment.py)), in priority
order: admin escalation for assault → the employer's existing `salesperson_profile_id`
→ `cb_get_next_agent()` round robin. The chosen `profiles.id` is mapped to
`wp_chat_users.id` and written to `wp_chat_conversations.assigned_user_id` so the portal
shows the assignment.

**Safety overrides silence.** The bot staying quiet on a human-handled thread is the
rule that keeps handovers invisible — but it cannot apply to someone reporting harm.
Otherwise a client messaging at 2am about an assault, on a thread an agent touched
yesterday, gets no reply, no ticket and nobody paged until morning. When a stood-down
thread receives a message matching the harm signals **and** a separate verification
confirms it, the bot sends one safety message, raises an urgent `dispute_assault` ticket
and escalates to admin — then stops. `bot_status` is left as it was, so the thread still
belongs to the agent and there is no fight over it when they arrive.

The only stand-down an emergency cannot override is `BOT_ALLOWED_NUMBERS`: a number that
is not being tested is never messaged, for any reason.

**Coming back to the bot.** A handover is not permanent — otherwise a client whose
enquiry closed in March would get silence when they message again in June. On the next
inbound message the bot takes the thread back if either the agent marked it resolved in
the portal (`agent_resolved`), or nobody has spoken for `HUMAN_ACTIVE_TIMEOUT_HOURS`
(`agent_idle`, default 72h). Both are logged to `cb_handovers` with
`direction = 'human_to_bot'`. Set the timeout to 0 to make handovers permanent.

Two `cb_handovers.reason` values exist beyond the brief's list: `agent_takeover` (an
agent replied, which is neither the client asking for a human nor the agent resolving
anything) and `agent_idle`. Labelling agent takeovers `client_requested`, as the brief's
vocabulary would force, made the handover log unreadable.

---

## Safety rules enforced in code

- **NRIC guard.** `[STFGM]\d{7}[A-Z]` is redacted in
  [app/utils.py](app/utils.py) before anything reaches the LLM, the embedding call, the
  transcript replay or `captured_info`, and a warning is logged.
- **No invented facts.** If the best `cb_match_knowledge_base` similarity is below
  `RAG_CONFIDENCE_FLOOR`, the bot says it will check with the team and hands over
  instead of answering.
- **Grounded figures only.** Every number of 100 or more in a generated reply must
  appear in the retrieved records, compared after normalising `$60,000` / `S$60000` /
  `60000` to the same form. A reply quoting an unsupported levy, fee, insurance limit or
  permit duration is discarded and replaced with a handover
  ([response_generator.py:31](app/graph/nodes/response_generator.py#L31)). The prompt
  alone was not enough — the model quoted a figure that was not in context.
- **No prices from the bot.** Fee and salary questions only collect nationality and care
  type; the figures come from the agent.
- **No bot disclosure, no handover announcements.** Both are hard rules in the system
  prompt ([app/graph/prompts/system.py](app/graph/prompts/system.py)), and no code path
  sends a transfer message.
- **Group chats ignored.** Only 1:1 client threads are processed.

---

## Notes on the build

- Modules exist beyond the structure in the brief:
  [app/graph/llm.py](app/graph/llm.py) (the shared OpenRouter client),
  [app/services/transcription.py](app/services/transcription.py) (speech-to-text for
  voice notes) and [app/graph/nodes/ticket_creator.py](app/graph/nodes/ticket_creator.py),
  which the graph in the brief requires as a node.
- `cb_style_config` is no longer read. The agency's voice is Part B of the system
  prompt — a structured guide whose sections and ordering are part of the prompt — so it
  lives in [app/graph/prompts/style.py](app/graph/prompts/style.py) rather than in a
  table of one-line settings.
- Voice notes are transcribed before they reach the graph, so the model answers what the
  client said. This goes through OpenRouter (`openai/gpt-4o-mini-transcribe`), so it
  needs no key beyond `OPENROUTER_API_KEY`. Its `/audio/transcriptions` takes base64
  JSON rather than the multipart upload OpenAI and Groq expect, so pointing
  `TRANSCRIPTION_BASE_URL` elsewhere also needs a code change. With transcription off, a
  voice note falls back to the attachment path: acknowledged, ticketed, handed over.
- `SUPABASE_DB_URL` was added to the environment: the brief specifies a PostgreSQL
  checkpointer, and that needs a Postgres DSN, which none of the listed variables carry.
- Contact and case lookups `select("*")` rather than named columns, so a column named
  differently in the platform schema degrades to a missing display name rather than a
  failed identification. The name is read from `display_name` / `full_name` / `name`,
  whichever the table has.
- Phone matching is two-stage, because the platform tables store numbers
  inconsistently — `profiles.phone_e164` is clean (`+6591234567`) but `employers.phone`
  is spaced (`+65 9887 6655`). Exact variants are tried first, then a narrow search on
  the last four digits with the comparison done on digits only
  ([contact.py:47](app/services/contact.py#L47)).
- **The `candidates` table has no phone column**, so the brief's candidate lookup cannot
  work against the current schema. The lookup is attempted, the missing column is logged
  once, and helpers fall through to `contact_type = 'unknown'` and are handled as a new
  enquiry. Identifying helpers by phone needs a phone column on `candidates`, or a link
  table pointing at `profiles`.
- Employer identification reads the `employers` table, as specified. Note that some
  employers also exist as `profiles` rows with `archetype_key = 'employer'`; those are
  not matched, because `matched_employer_id` is a foreign key to `employers(id)`.
- No schema changes are made. The only DDL in the system is LangGraph's own checkpoint
  tables.

## Deployment

Any host that can run a long-lived ASGI process works (Railway, Fly, a VPS). Note the
debouncer and the recently-sent message cache are **in-process**, so run a single
instance per WhatsApp number; scaling out would need both moved to Redis. Serverless
platforms that freeze the process between requests (e.g. Vercel functions) will drop
debounced batches and are not suitable without that change.
