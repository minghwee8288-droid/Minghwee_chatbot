# Ming Hwee WhatsApp Chatbot

WhatsApp assistant for **Ming Hwee Employment Agency** (Singapore, MOM Licence 12C6072).
It sits on Thomas's existing WhatsApp Business number via Whapi, answers common
enquiries from the knowledge base, and collects the basics for each service request.
When it hits something a human must handle, it raises a ticket for that one topic and
keeps talking to the client about everything else — silently, same thread, no
announcement that anyone or anything else is involved. The bot only stands the whole
conversation down when an agent actually replies on the thread, or when it fails
outright.

This is **Project 2** of three. It shares one Supabase database with the RAG pipeline
(Project 1) and the Ming Hwee OS platform (Project 3). It **reads** `cb_knowledge_base_updated`,
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
          agent detector          maybe_return_to_bot (pause lapsed?)
     (bot_status = human_active,   store message + identify contact
      auto-resumes after                    │
      AGENT_PAUSE_MINUTES)            debounce 3s
                                             │
                                   LangGraph conversation engine
                                             │
        ┌────────────────────────────────────┼─────────────────────────────┐
   intent_classifier ──► rag_retriever ──► response_generator ──► reply
        │        │                                        │
        │        └──► blocked_topic_responder (topic already ticketed — reply)
        │                                                  │
        ├──► info_collector ──► ticket_creator ──► handover_executor
        │                                                  │
        └──► handover_executor (dispute_assault, first report only)
```

`ticket_creator`/`handover_executor` log the escalation and finalise agent assignment,
but leave `bot_status` untouched — the ticket blocks only its own topic
(`blocked_topics`, read fresh from `cb_tickets` every turn), not the conversation.

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
namespace  : RAG_NAMESPACE — blank searches all, or pin to one namespace
```

Retrieval reads `cb_knowledge_base_updated` through
[`cb_match_knowledge_base_updated`](scripts/cb_match_knowledge_base_updated.sql), which
ships in this repo because the rebuilt schema defines the table but no match function.
`embedding` on that table is a bare `public.vector` with **no declared dimension**, so
the database will not reject a query vector of the wrong size — the guard is the
explicit dimension check at the embedding call in
[app/services/rag.py](app/services/rag.py). The model and dimension above must be the
ones the pipeline used at ingest.

Rows carry their own routing labels, so the filter runs *before* the vector search:

| column | filtered on | catch-all also matched |
| --- | --- | --- |
| `service_type` | the active service (`state.service_type`) | `general` |
| `contact_type` | `effective_contact_type()` — employer / candidate / supplier / partner | `all` |
| `nationality` | PH / ID / MM from `collected_info` | `all` |
| `namespace` | `RAG_NAMESPACE`, when set | — |

An unrecognised value drops that filter rather than applying it: a wrong label returns
only the catch-all bucket, which is worse than no filter at all.

`contact_type` and `nationality` are CHECK-constrained, so their vocabularies are known.
`service_type` is a free-form `varchar(60)` and nothing guarantees the pipeline labels
chunks with the same words the bot calls its services, so the filter is checked against
the values actually present in the table (read once per process, logged at startup). A
service the table has never heard of is logged once and searched without a service
filter. Run `check_retrieval.py` to see the vocabulary and decide whether a mapping is
needed.

`chunk_type` decides what is read off a row — `qa_pair` and `faq` are read as question +
answer, `document_chunk` and `table_unit` from `content`. A `table_unit` goes in whole or
not at all (`RAG_MAX_TABLE_CHARS`); half a fee table is worse than none. `style_example`
rows are tone and formatting guidance, not evidence, and are excluded both in the match
function and again in `rag.py`. `metadata.priority` (1 = emergency/safety) reranks
results as a boost over similarity, and a row's `rag_score_floor` overrides the global
threshold for that row.

Verify and calibrate against the live knowledge base:

```bash
python scripts/check_retrieval.py
```

It reports namespace / routing / chunk-type counts, runs ten representative client
questions, prints the similarity distribution, and suggests `RAG_MATCH_THRESHOLD` /
`RAG_CONFIDENCE_FLOOR`.

The shipped thresholds (0.35 / 0.45) were calibrated against the **old**
`cb_knowledge_base`, where answerable client questions scored 0.533–0.771 and off-topic
messages 0.147–0.344. The rebuilt pipeline chunks and embeds differently, so re-run
`check_retrieval.py` against `cb_knowledge_base_updated` and reset
`RAG_MATCH_THRESHOLD` / `RAG_CONFIDENCE_FLOOR` from those numbers before going live.

[scripts/kb_hygiene.sql](scripts/kb_hygiene.sql) documents content issues found in the
**old** table (a stale MOM insurance figure that outranked the correct one, and internal
delivery documents that were client-retrievable). It is kept as a record of what to
re-check in the new table; its SQL targets `cb_knowledge_base` and no longer applies.

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

**The employer qualification set** (`new_hiring`) is the long one: 20 fields ordered by
topic — who they are, what they need, their household, their preferences, timing,
anything else, how they found us. Everything from `hire_source` down is `optional`
(asked once, dropped without complaint), so a client in a hurry is never blocked.

Two mechanisms keep it from reading as a form:

- **Gates.** A `Field` may carry a `Gate` on an earlier answer, so the detail questions
  only appear for the households they apply to — `children_detail` behind childcare,
  `elderly_detail` behind eldercare (both open on "all of the above"), `pet_detail`
  behind a yes, `referrer_name` behind an actual referral. A gate has three states, not
  two: `undecided` (the answer it keys off is not in yet) keeps the field in the
  *extractor's* list while keeping it out of the *asker's*, which is what lets one
  opening message answer four questions at once and never be asked them again.
  `applicable_fields(..., include_undecided=True)` is the extractor's view;
  `missing_fields()` is the asker's.
- **Options and topics.** `Field.options` carries the answers the office works with, and
  `Field.group` the topic. The collector feeds both to the model
  (`_field_guidance()`): options steer the wording and are offered as examples, never
  read out as a menu, and a change of `group` lets the model put a seam in rather than
  jumping from pets to salary. Whatever the client actually says is the recorded answer,
  listed or not — `EXTRACTION_SYSTEM` maps free text onto the closest bucket only when
  nothing is lost by it.

**The application paperwork is deliberately not collected over chat.** NRIC/FIN, date of
birth, citizenship, passport, spouse identity, residential address, occupation, income
bracket and the IC numbers of everyone at the address are all Singpass fields on the work
permit application. Rules 4 and 4a forbid asking for any of them, and `redact_nric()`
would strip an NRIC before it reached `captured_info` regardless. `pending_documentation()`
puts the list on the ticket as `captured_info.notes.still_to_collect` instead, so the
agent picking up a fully qualified enquiry knows exactly what is left to do.

**Escalation is topic-scoped, not conversation-wide.** Every trigger below raises a
`cb_tickets` row and logs a `cb_handovers` entry, but leaves `bot_status` alone — the bot
keeps answering everything else on the conversation. The ticket's `captured_info.topic_key`
(mirrored by `ticket_service.topic_key_for()`) is what a follow-up on that exact topic is
matched against: while the ticket is `open`, a message that resolves to the same topic is
routed to `blocked_topic_responder` instead of being answered, re-collected, or escalated
again — a short, varied acknowledgement, with anything new the client says appended to
the ticket (`ticket_service.add_follow_up`) so the agent opening it sees the whole
picture. The topic unblocks the moment the ticket leaves `status = 'open'`, with no sync
step: `open_topics_for_conversation()` is read fresh from `cb_tickets` every turn.

| Trigger | Reason logged | Reply sent first |
|---|---|---|
| All fields collected → ticket | `ticket_raised` | short "let me pull this together" |
| Fee / salary enquiry complete | `fee_enquiry` / `salary_enquiry` | no figures, ever |
| Assault / violence | `dispute_escalation` | one empathetic message + 999 advice |
| Salary or leave dispute | `dispute_escalation` | acknowledgement |
| Nothing relevant in the KB | `bot_confused` | "let me check with the team" |
| Client asks for a person | `client_requested` | none — that topic goes quiet, the rest of the conversation does not |
| Media / candidate registration | `media_received` / `candidate_registration` | brief acknowledgement |

A repeat message on an already-open topic is never re-escalated: `route_after_intent`
checks `blocked_topics` before anything else, so whatever the classifier makes of it, a
parked topic stays parked.

**Assignment** ([app/services/assignment.py](app/services/assignment.py)), in priority
order: admin escalation for assault → the employer's existing `salesperson_profile_id`
→ `cb_get_next_agent()` round robin. The chosen `profiles.id` is mapped to
`wp_chat_users.id` and written to `wp_chat_conversations.assigned_user_id` so the portal
shows the assignment.

**Only two things stand the bot down conversation-wide**, since every ticket-raising
trigger above is topic-scoped: a human agent actually replying on the thread, and the bot
failing outright (an unhandled graph exception, or Whapi refusing the send).

**A human agent replying pauses the bot, not permanently.** Whapi echoes every outbound
message on the number back as a webhook; whichever ones are not the bot's own send (id
cache + `is_bot`) and not a WhatsApp Business auto-reply (matched by config, or
structurally — the same text sent verbatim to several different clients) are a real
agent, and `agent_took_over()` sets `bot_status = human_active`. The bot resumes on its
own once `AGENT_PAUSE_MINUTES` (default 10) pass with no further agent message — the
clock restarts on every one — logged as `agent_pause_expired`. It also resumes
immediately if the agent marks the thread resolved in the portal (`agent_resolved`).
Resuming never mints a fresh `langgraph_thread_id`: the agent may only have said a couple
of words before going quiet mid-collection, and there is no reason to discard that.
Resumption is checked only when a new inbound message arrives — nothing fires on a timer,
so the bot never replies proactively to something that was already said during the
pause; it answers only the next genuinely new message. Set `AGENT_PAUSE_MINUTES=0` to
make an agent reply a permanent handover, the old behaviour.

**The bot failing outright** (`_fail_over_to_human` in
[app/api/webhook.py](app/api/webhook.py)) is the one case with no agent message behind
it, so it falls back to the much longer `HUMAN_ACTIVE_TIMEOUT_HOURS` (default 72h,
reason `agent_idle`) measured from the bot's own last reply, since there is no agent
timestamp to measure from instead. Set it to 0 to make that standdown permanent.

**Safety overrides silence.** The above two standdowns are the rule that keeps handovers
invisible — but that cannot apply to someone reporting harm on a thread the bot has been
told to leave alone entirely (blocked by `BOT_ALLOWED_NUMBERS` or `AGENT_GRACE_HOURS`).
Otherwise a client messaging at 2am about an assault, on a thread an agent touched
yesterday, gets no reply, no ticket and nobody paged until morning. When a stood-down
thread receives a message matching the harm signals **and** a separate verification
confirms it, the bot sends one safety message, raises an urgent `dispute_assault` ticket
and escalates to admin — then stops. `bot_status` is left as it was, so the thread still
belongs to the agent and there is no fight over it when they arrive.

The only stand-down an emergency cannot override is `BOT_ALLOWED_NUMBERS`: a number that
is not being tested is never messaged, for any reason.

Three `cb_handovers.reason` values exist beyond the brief's list: `agent_takeover` (an
agent replied, which is neither the client asking for a human nor the agent resolving
anything), `agent_idle` and `agent_pause_expired`. Labelling agent takeovers
`client_requested`, as the brief's vocabulary would force, made the handover log
unreadable.

---

## Safety rules enforced in code

- **NRIC guard.** `[STFGM]\d{7}[A-Z]` is redacted in
  [app/utils.py](app/utils.py) before anything reaches the LLM, the embedding call, the
  transcript replay or `captured_info`, and a warning is logged.
- **No invented facts.** If the best `cb_match_knowledge_base_updated` similarity is below
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
