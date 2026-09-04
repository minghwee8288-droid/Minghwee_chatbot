# CLAUDE.md — Ming Hwee Chatbot

**This file is the entry point. Read it before touching code, and update it after.**

---

## 0. Protocol (read first, every session)

1. **Start here, not in the code.** This file describes what the system does, why it is
   built the way it is, and which rules are enforced mechanically rather than by prompt.
   Grep the code to confirm details, but form your mental model here first.
2. **Then read `PENDING_CHANGES.md`** for work already agreed but not yet done.
3. **After any change that alters behaviour, update this file in the same commit.**
   Specifically: §4 (graph), §5 (invariants), §6 (data), §7 (config), §8 (persona),
   §9 (known issues), and always append to §11 (change log). A change that leaves this
   file wrong is an incomplete change.
4. **Do not delete the incident notes.** Long comments in this codebase cite real
   conversation IDs and real failures. They are the reason the code is shaped the way it
   is. If you change that code, update the note — never strip it.
5. **Never guess a schema.** The database is shared with two other products. Confirm
   column names and CHECK constraints against the live DB before writing to it.

---

## 1. What this is

A WhatsApp assistant for **Ming Hwee Employment Agency** (Singapore maid agency, MOM
Licence 12C6072). It answers client enquiries, qualifies leads, and hands work to human
agents. FastAPI + LangGraph behind the Whapi WhatsApp gateway, on Supabase Postgres.

It is **project 2 of 3 sharing one Supabase database**:

- `wp_*` — the WhatsApp portal (another product, pre-existing, CHECK-constrained)
- `cb_*` — this chatbot
- unprefixed (`employers`, `candidates`, `profiles`, `leads`, `branches`) — the platform

There is also `portal-ui/` — a React ticket dashboard that reads the same DB directly.

### The two ideas that shape everything

**Topic-scoped escalation.** When something needs a human, the bot raises a ticket for
*that one topic* and keeps answering everything else in the same thread. Only a human
actually replying, or the bot crashing, silences the whole conversation.

**Guards, not just prompts.** Prompt rules are not guarantees. Anything that must not
happen is enforced in `app/graph/guards.py` as well as in the prompt, because the same
prompt produced clean output on one run and rule-breaking output on the next.

---

## 2. Repo map

```
app/
  main.py            FastAPI app, lifespan, startup validation, CORS
  config.py          ALL settings (pydantic-settings, lru_cached — restart to reload)
  utils.py           phone normalisation, NRIC redaction
  api/
    webhook.py       (922) Whapi webhook, allowlist gate, debounce, agent detection
    health.py        /health (static) and /health/ready (always 200 — parse the body)
  whapi/
    client.py        Whapi HTTP client, retries; recipients must be bare digits
    parser.py        payload -> IncomingMessage; signature verification
    debouncer.py     per-phone buffer, timer restarts on each message
  graph/
    graph.py         (325) node wiring, routing, checkpointer, _TURN_RESET, run_turn
    state.py         ConversationState TypedDict + reducers + effective_contact_type
    guards.py        (468) mechanical output safety — see §5
    closure.py       "say nothing" path (acknowledgements, closings)
    llm.py           OpenRouter via langchain_openai; complete() / complete_json()
    checkpointer.py  rewrites LangGraph SQL to cb_-prefixed tables
    nodes/           intent_classifier, rag_retriever, response_generator,
                     info_collector, ticket_creator, handover_executor,
                     blocked_topic_responder
    prompts/
      system.py      IDENTITY + RULES + clock + assembly (build_system_prompt)
      style.py       voice guide harvested from real agent transcripts
      templates.py   (504) per-node task instructions, extraction, assault verify
  services/
    ticket.py        (1581) THE BIG ONE — field schema DSL, merge policy, ticket CRUD
    lead.py          (567) lead creation/update, numbering, phone matching
    rag.py           (495) embeddings + pgvector search + filtering
    message.py       store/send, auto-reply detection, history
    conversation.py  wp_chat_conversations access, bot_status
    handover.py      cb_handovers; escalation vs stand-down
    contact.py       identify a number against master records
    assignment.py    which agent gets it
    transcription.py voice notes -> text
  db/supabase.py     service-role client (bypasses RLS), read retries, insert_numbered
scripts/             preflight, retrieval check, reset, simulate, SQL migrations
portal-ui/           React + Vite ticket dashboard (separate deliverable)
```

---

## 3. Request lifecycle

```
Whapi POST -> _accept (verify, 200 immediately, work in BackgroundTasks)
  -> parse_webhook -> IncomingMessage[]
  -> from_me?  yes -> handle_outbound  (agent detection -> silence the bot)
               no  -> handle_inbound
       replay guard (in-memory, 900s TTL)
       transcription (voice -> text) BEFORE any gate, so safety sees the words
       may_engage()  -> allowlist -> bot_status -> agent grace window
       store, read receipt, identify contact
       debouncer.add()  (2.0s, timer restarts per message)
  -> process_batch -> per-phone asyncio lock -> _process_locked
       redact NRIC, build ~20-key payload, run_turn(thread_id, payload)
       repeat guard (90s window) -> send_bot_reply
```

**Everything is per-process and in-memory**: replay guard, per-phone locks, debouncer
buffers, sent-by-bot cache. **Run exactly one worker and one replica.** More than one
breaks duplicate suppression, the debouncer and agent detection.

---

## 4. The graph

```
START -> intent_classifier
  suppress_reply ................................. END
  blocked topic + KB-answerable intent ........... rag_retriever
  blocked topic (anything else) .................. blocked_topic_responder
  dispute_assault ................................ handover_executor
  default ........................................ rag_retriever

rag_retriever
  still parked ................................... blocked_topic_responder
  service/enquiry/dispute/candidate intent ....... info_collector
  fields_for(service_type) non-empty .............. info_collector
  default ........................................ response_generator

info_collector
  no service_type ................................ response_generator
  info_complete .................................. ticket_creator
  default ........................................ END  (question asked, wait)

response_generator
  needs_handover, no ticket ...................... ticket_creator
  needs_handover, has ticket ..................... handover_executor
  default ........................................ END

ticket_creator -> handover_executor -> END
blocked_topic_responder -> END
```

State lives in a LangGraph Postgres checkpointer on `cb_checkpoint*` tables, keyed by
`wp_chat_conversations.langgraph_thread_id`.

`_TURN_RESET` (graph.py) blanks 12 per-turn fields each invocation. `ticket_id` is in
that list; `created_lead_id` deliberately is **not** — it must survive across turns
because the lead is opened early and the ticket is created much later.

---

## 5. Invariants enforced in code (not just prompts)

| Rule | Where | Notes |
|---|---|---|
| No invented figures | `guards.ungrounded_figures` | Only catches numbers >= 100. Digits must be Western — prompt rule 12b enforces that in every language. |
| No named colleague / no promised time | `guards.strip_handover_talk` | Handover *announcements* are now required (§8). This catches only "Grace will call at 3pm". The name half is case-SENSITIVE via `(?-i:...)`. |
| Canned strings are pre-vetted | import-time assert in `blocked_topic_responder.py` | Strings sent verbatim bypass the guards, so they are checked at import. |
| No prompt echo / degenerate output | `guards.is_degenerate` | Weak on unspaced scripts (Chinese, Burmese). |
| Max 2 sentences (3 on a greeting, a handover close, or an answered question) | `guards.clamp_reply` | Logs the discarded tail deliberately. Two is one short of what those three turns structurally need — see §11. |
| Claire never speaks of Ming Hwee as a third party | `guards.speaks_of_us_as_a_third_party` | "we sent it to the agency, they have passed it up" and prompt-leaked "as instructed". Falls back to the vetted holding line. |
| No internal monologue reaches the client | `guards.leaks_internal_reasoning` | Unbracketed process-narration `strip_meta_commentary` misses: "no relevant records found", "the client asked...", "should I ask them or handle this", "never quote". Discards the whole reply for the holding line. The empty-records `rag.format_context` block was reworded so it is no longer a line to echo. |
| A vague or mistyped answer does not close a field | `info_collector._unfinished` | Value is kept; the field returns to the front of the queue for one more ask. |
| Never repeat an opener | `guards.strip_repeated_opener` | Checks several previous bot lines. |
| One phone, one lead, ever | `lead.create_if_absent` (§1B) | **Currently scoped per-table — see §9.** |
| Ticket survives a dead lead link | `ticket.create` retry | Drops `created_lead_id`, records `notes.lead_link_broken`. |
| NRIC never reaches the LLM | `utils.redact_nric` | Applied to the batch text; the raw body is still stored in the DB. |
| Assault: keyword override, no LLM confidence | `intent_classifier.ASSAULT_PATTERNS` | **English-only — see §9.** |
| A transfer is never asked the first-time-hire question | `intent_classifier._TRANSFER_PATTERN` | "transfer" beside maid/helper/employer/permit/service, or "change employer", forces `transfer`. Does not fire while another service is mid-collection. English-only (§9.2). |
| A client is TOLD what we recognised, not just silently spared the question | `info_collector` (`recognised_note`) | Fires once, off `placed_helper`. Suppresses `returning_note`, which says the opposite. |
| The channel is chosen before the address is asked for | `_WANTS_EMAIL` gate | `update_channel` first; `email` only if they picked email. |
| The AI introduction survives a first-turn answer | `info_collector.COLLECTOR_INTRO_NOTE` | Repeated in the instruction that wins over the system prompt on a collector turn. |
| No flow anywhere asks for a case ID | `SERVICE_FIELDS` | All four removed. The case is found from the phone by `contact.find_active_case()`. Asserted by `scripts/selfcheck_flows.py`. |
| A run of questions always says why it is asking | `info_collector._COLLECTION_PURPOSE` | Once, on the opening turn. Every flow with more than 4 fields has an entry, and that is asserted. |
| Claire uses the client's name when she knows it | prompt rule 1c | The positive half of 1b, which only ever said which name NOT to use. |
| An employer looking FOR a transfer helper is never asked her name | `_TAKING_ON_TRANSFER` / `_RELEASING_HELPER` gates | Everything after the direction question is gated on it. |
| A helper asking to be transferred keeps the helper flow | `intent_classifier._HELPER_SPEAKING` | Beats the employer fallback for `transfer`. "transfer my helper" excluded by lookahead. |
| The employer flow asks what the candidate form profiles | `SERVICE_FIELDS["new_hiring"]` | `helper_room`, `helper_profile`, `special_duties` come straight from `candidates.biodata.commitments`. |
| A new hire's cost is never quoted before a salesperson speaks to them | `guards.quotes_hiring_package_cost` + `COST_WITHHELD_SERVICES` | Salary, levy and the $5,000 bond deliberately still go out. |
| A small-ticket service explains itself before it collects | `info_collector._SMALL_TICKET_SERVICES` | `renewal`, `passport_renewal`. Opening turn only; strictly grounded in `rag_context`. |
| A renewal never asks for a case ID | `SERVICE_FIELDS["passport_renewal"]`, `["renewal"]` | `_case_id()` deliberately absent. Client instruction, 2026-09-04. |
| Nobody is asked whether they have hired with us before | `info_collector._known_fields` | Filled from `prior_hires` either way now — zero reads as "first time with us". |
| An existing client is not asked for a helper we placed | `contact.get_placed_helper` | Only when there is exactly ONE live placement naming a candidate. |
| A volunteered requirement is acknowledged, not silently filed | `info_collector._VOLUNTEERED_REQUIREMENT` | Adds a note to the collector instruction. Narrow on purpose — "can't"/"don't" excluded. |
| A service filter never starves an answerable question | `rag_retriever` (widening retry) | Below the soft floor, retries with no service filter. Nationality is kept. |
| A returning client is never asked if they have hired before | `info_collector._known_fields` | Filled from non-archived `placements`. Only a POSITIVE count is evidence — 0 also means "unknown number". |
| An answer to our own question cannot be dragged onto a parked topic | `intent_classifier` (live-collection rule) | Unless the client names that service or chases its status. |
| A job-seeker is never met with a holding line | `intent_classifier._JOBSEEKER_PATTERN` | "need a job", "provide work to us", "someone's home … work", "I am a maid" force `candidate_registration`. Skipped for a known employer, so "someone to work at **my** home" stays `new_hiring`. |

`closure.py` is the other half: `needs_no_reply()` decides when to say nothing. It never
silences the first message of a conversation, and never silences a bare yes/no when our
own last line contained a question mark.

---

## 6. Data model (the parts that bite)

**`cb_tickets.service_type` is `text[]`** with a containment CHECK over 11 values. The
bot's vocabulary is wider (13 flows), so `_storable_service()` substitutes an allowed
value and preserves the truth in `captured_info.topic_key` / `enquiry_type`. Never insert
a value outside the 11.

Live constraints, confirmed against the DB:

```
cb_tkt_service_check    service_type <@ ARRAY[new_hiring, direct_hiring, replacement,
                        transfer, renewal, home_leave, passport_renewal, dispute_salary,
                        dispute_assault, fee_enquiry, salary_enquiry]
                        AND cardinality(service_type) > 0
cb_tkt_priority_check   high | medium | low
cb_tkt_status_check     open | in_progress | resolved | closed
cb_tickets_created_lead_id_fkey -> leads(id)   [no ON DELETE SET NULL yet — §9]
```

**`placements` is the only record of "they hired through us."** One row per helper
placed with an employer (`employer_id`, `candidate_id`, `archived_at`, confirmed live
2026-09-03; 6 rows). An `employers` row means the portal holds their details and a
`leads` row means they once enquired — neither is a hire. `contact.count_prior_hires()`
counts the non-archived rows and the webhook puts the number on every turn as
`prior_hires`, which is what stops the hiring flow asking a returning client whether they
have hired before. `cases.case_type` also carries a label like `First-time hire`, but its
full vocabulary is unconfirmed (one row exists), so nothing reads it.

**Passport data lives inside `candidates.biodata`, not in a column.** There is no
passport column on any table — which is exactly why an audit by column name concluded
the data did not exist at all, and was wrong. `biodata` is a jsonb blob the portal
writes, holding `passportExpiry` (ISO, e.g. `2033-09-27`) and `passportNo`, alongside
health, skills, family, education and languages. All 6 candidate rows carry both,
confirmed 2026-09-04. **When you need a field that is not a column, look in `biodata`
before concluding it is missing.**

`contact._passport_expiry()` reads **only** the expiry. `passportNo` is deliberately
never read: anything returned there lands in `collected_info`, which goes into the
model's prompt, and Rule 4a keeps the Singpass block off WhatsApp. The office already
has the number; the client does not need to be told it.

**`placements.candidate_id` is null on most rows** — 2 of 6, live 2026-09-04 — and one
employer holds 4 placements with a single candidate among them. That is why
`get_placed_helper()` returns a helper only when there is exactly one live placement AND
it names a candidate: naming the wrong helper on a ticket is worse than asking.

**`candidates.biodata` IS the matching form.** Its `commitments` block holds the 19
things every helper is profiled on — `share_room`, `no_offday`, `window_clean`,
`wash_car`, `gardening`, `go_marketing`, `hand_wash`, `handle_pork`, `handle_beef`,
`care_newborn`/`children`/`elderly`/`disabled`/`bedridden`, `cook_family`, `long_hours`,
`pet_care`, `use_appliances`, `general_house` — plus `skills` in three groups
(housework, infant_child, elderly_disabled) and `languages` with proficiency. The
`candidates` columns add `age`, `experience_years`, `english_level`, `religion`,
`off_days_per_month`, `asking_salary_cents` and `candidate_type`. **When asked what the
employer flow should collect, this is the form to read** — it is what the office actually
filters on.

**The bot never closes a ticket.** No code path writes `cb_tickets.status` after insert.
The portal owns resolution.

**`wp_chat_conversations.customer_number` is bare digits and UNIQUE.** Writing E.164
there creates a duplicate conversation — that bug happened, and
`scripts/fix_split_conversations.py` repairs it.

**`leads` / `leads_candidate`** — `branch_id` is NOT NULL with no default, so
`resolve_branch_id()` must succeed or no lead is created. `leads_candidate` has no
`interest_type`, `requirement`, `budget` or `summary` column.

**Ticket and lead numbers** are read-max-and-increment, retried on duplicate key by
`db.insert_numbered`. Ticket numbers order by `ticket_number`; lead numbers order by
`created_at` — that asymmetry matters (§9).

**RLS does not apply to the bot.** It uses the service-role key and bypasses RLS
entirely. RLS exists only to constrain the portal's anon/authenticated key.

---

## 7. Configuration

All settings live in `app/config.py`, loaded from `.env`, **`lru_cache`d — a restart is
required for any change.** `.env` is gitignored, so `git pull` never updates it: edit it
on the server and run `docker compose up -d` (**not** `restart` — restart does not
re-read `env_file`).

Easy to get wrong:

- `BOT_ALLOWED_NUMBERS` — the safety gate. Fails **closed** if malformed. The startup log
  prints the resolved list; trust that, not the file.
- `OPENROUTER_MODEL` — currently `openai/gpt-5.6-luna` (switched from `moonshotai/kimi-k3`
  on 2026-09-03; rollback = set it back and `docker compose up -d`). `llm.py` is
  model-agnostic: it reads this setting, sends `temperature`/`frequency_penalty` and a
  `reasoning` field via `extra_body`, and never sends `budget_tokens`. Provider routing is
  deliberately unconstrained — OpenRouter drops parameters the target provider does not
  accept, and `require_parameters` made every call 404, so **do not reintroduce it**.
  **Use plain `luna`, not `luna-pro`** — the pro slug bakes in `reasoning.mode=pro` (heavy
  thinking billed as output, 7–55s latency), the opposite of a quick-reply bot. (If you
  switch back to Sonnet 5: `off` maps to Anthropic thinking disabled, and `budget_tokens`
  400s there — but `llm.py` sends none.) **All guards were tuned against Kimi's failure
  modes; a new model fails differently — a model change needs a full scenario pass, not a
  spot check.**
- `LLM_REASONING=off` — Luna (like Kimi) is a reasoning model; `off` turns the thinking
  down for fast, far cheaper replies. Whether `off` actually overrides a `-pro` slug's
  baked-in reasoning is untested — another reason to stay on plain `luna`.
- `RAG_SOFT_FLOOR` (0.40) is the real retrieval knob. `RAG_CONFIDENCE_FLOOR` is **dead
  config, read by nothing** — kept only so existing `.env` files still parse.
- `TENANT_ID` must be set or lead and ticket numbering break.
- `HISTORY_LIMIT` (40) — past messages loaded into each prompt. The `.env` value
  **overrides** the code default, so bumping the default alone changes nothing on a box
  whose `.env` pins it. Collected fields persist in the checkpoint independently; this is
  only the free-text window.

---

## 8. Persona and language (current behaviour)

**Claire, Ming Hwee's AI assistant.** She introduces herself on the first message and
answers honestly when asked whether she is an AI. She must not hide behind it ("I'm only
an AI so I can't help" is banned) and must not raise it again unprompted.

**Handovers are announced**, not hidden: *"I've passed this to our team, a live agent
will connect with you shortly"*, followed by an offer to help with anything else. The one
exception is a harm report — no "anything else?" after someone reports violence (rule 2b;
`ASSAULT_FALLBACK_REPLY` is verified not to contain it).

**Any language in, same language out** (rule 12). Numbers always in Western digits (12b),
and `Claire` / `Ming Hwee` always in the Latin alphabet in every script. A **short or
ambiguous message never switches the language** (rule 12d): a name, a number, "yes",
"hiring" keeps the conversation in whatever language it was in. This exists because the
model flipped a running English chat to Mandarin on a one-word reply ("hiring") — and the
strongest foreign-language signal in the whole context was **our own prompt**. The
system prompt therefore now contains **no non-Latin script at all**: the anti-calque and
Latin-alphabet rules describe the wrong output ("never in Chinese characters") instead of
printing an example of it. Keep it that way — do not paste CJK/Devanagari/Burmese literals
back into `system.py`.

**Claire is the bot's name, never the client's** (rule 1b). With the persona change, when
`full_name` is unknown the only name in the prompt is Claire's, and the model addressed a
client as "Claire". The rule forbids using its own name where the client's belongs; if it
does not know the name, it uses none.

**Agent-facing data stays English** even when the conversation is not: extraction values
and lead summaries are written in English because Singapore staff read them off a ticket.

**"Transfer" is split by contact type** (`resolve_service`). The `transfer` field list is
written for the *helper* (her permit expiry, her employer's consent, her availability), so
only a `candidate` keeps it. An **employer** who says "transfer" resolves to
**`transfer_employer`** — its own service with its own short, employer-answerable field
list, whose first question disambiguates the two opposite things an employer can mean
(taking a transfer helper *on* vs *releasing* their own). `transfer` is not in
`_CONTACT_BY_INTENT` (forcing every transfer to `employer` is what put an employer into
the helper's questionnaire); it is an employer *fallback* instead, so a genuine job-seeker
can still be tagged a candidate.

> **Why `transfer_employer` is a separate service and not a remap onto `new_hiring`:**
> the blocked-topic key *is* the service key. The first version of this fix mapped an
> employer's transfer onto `new_hiring`, so once a hiring ticket existed, "I also want a
> transfer" computed the **same** topic key — the graph read a brand-new request as a
> follow-up on the parked hiring topic and replied "a live agent will connect with you
> shortly" indefinitely, collecting nothing (live, 2026-09-02). Any future "route service
> A into service B's questions" must keep its own key, or it inherits B's parked topic.
> `transfer_employer` is not one of the 11 values `cb_tkt_service_check` allows, so
> `TICKET_SERVICE_FALLBACK` files it under `transfer` with the true key in
> `captured_info.topic_key`; it is in `EMPLOYER_LEAD_SERVICES` so it still opens a lead.

---

## 9. Known issues (verified, not yet fixed)

Ordered by what will hurt first.

1. **Assignment is unseeded.** `cb_round_robin_state` is empty and all 6 `wp_chat_users`
   rows have `profile_id = NULL`. Every ticket is created unassigned and the portal cannot
   show an owner. `scripts/seed_assignment.sql` exists but needs two business decisions
   first: which consultants receive leads, and the portal-to-profile mapping.
2. **English-only safety nets, now that Claire replies in any language.**
   `ASSAULT_PATTERNS` — and therefore `emergency_override` in `webhook.py`, the
   out-of-hours path — will not fire on a harm report in Hindi or Burmese. The LLM
   classifier still catches it, but the deterministic net is gone. Same for
   `HUMAN_REQUEST_PATTERNS`, `closure.py`, and the `blocked_topic_responder` regexes.
3. **§1B is enforced per-table.** `create_if_absent` calls `find_by_phone(phone, kind)`
   (one table) while the webhook looks up unscoped (both). One phone can end up with both
   an employer and a candidate lead, and the two paths disagree about whether a lead
   exists.
4. **A second ticket overwrites the first enquiry's lead data.** `created_lead_id`
   persists for the life of the thread, so every later completed collection re-runs
   `_finish_lead` and rewrites `interest_type`, `requirement` and `summary` from the new
   topic.
5. **`cb_tickets_created_lead_id_fkey` has no `ON DELETE SET NULL`.** Migration written
   (`scripts/ticket_lead_fk_set_null.sql`), not applied. Until then, deleting a lead a
   ticket references fails with an FK error.
6. **`next_lead_number` orders by `created_at`, not by number.** If a lead's timestamp is
   out of order with its number, `insert_numbered`'s retry re-proposes the same taken
   number five times and then raises.
7. **Webhook secret committed in plaintext** in `.claude/settings.json` (git-tracked).
   Needs rotating and scrubbing from history.
8. **Duplicated, divergent constants.** `EMPLOYER_LEAD_SERVICES` /
   `CANDIDATE_LEAD_SERVICES` are defined in both `lead.py` and `ticket_creator.py` and
   **disagree**; the `ticket_creator.py` copies are dead. `_lead_name` duplicates
   `lead.best_name`.
9. **`scripts/TEST_SCRIPT.md` §E is inverted** — it still treats "admits it is a bot" as
   a failure, which is now the required behaviour.
10. **Re-raising a parked topic mid-collection is answered with the wrong question.**
    With a transfer ticket parked and `new_hiring` collecting, "I want to transfer my
    helper" resolves through plain stickiness to `new_hiring`, so the client gets the
    next hiring question rather than "your transfer is with an agent". The named-service
    correction does not fire (the named service IS parked) and the live-collection rule
    does not fire (we did not land on a parked topic). Not fixed deliberately: forcing
    `service_type` to the parked service for one turn would overwrite the in-flight
    `service_type` on the checkpoint and strand the live collection.
11. **Unbounded in-memory caches** with no TTL: `_LOCKS` (webhook),
    `_AUTO_REPLY_VERDICTS` (message), `_MISSING_COLUMNS` (contact), `_vocabularies` (rag).

---

## 10. Operations

```bash
# deploy
git pull && docker compose up -d --build     # code change
docker compose up -d                         # .env change only (NOT `restart`)

# verify a deploy actually took
docker compose exec chatbot grep -c created_lead_kind /app/app/graph/state.py
docker compose logs chatbot | grep "Safety gate"

# diagnostics (all read-only)
python scripts/smoke_nodes.py          # RUNS each node with the LLM and DB stubbed. Run this FIRST.
python scripts/selfcheck_flows.py      # behavioural assertions; also runs IN the container:
                                       #   docker compose exec chatbot python /app/scripts/selfcheck_flows.py
                                       # Verifies BEHAVIOUR, not grep counts — see the note below.
python scripts/preflight.py            # go-live gate: KB, agents, branch, portal bridge
python scripts/check_retrieval.py      # retrieval calibration; tunes RAG_SOFT_FLOOR
python scripts/watch_conversation.py --follow

# DESTRUCTIVE — test numbers only
python scripts/reset_conversation.py +6591234567
```

**`selfcheck_flows.py` reads data; `smoke_nodes.py` runs code. You need both.**
2026-09-04: `selfcheck_flows.py` passed all 18 assertions inside the running container
while `info_collector` raised `UnboundLocalError` on **every single turn** — the intro
note read `first_contact` eighty lines above the line that assigned it. Nothing caught
it: it compiles, no linter is installed, and the graph swallowed it as `bot_confused` and
handed each message to a human, so the symptom the client saw was the bot silently not
replying. `smoke_nodes.py` executes each node against seven stub states with the LLM and
`_open_lead_early` patched out, and fails all seven on that bug. **Run it before every
deploy.**

**Verify a deploy by behaviour, not by grep counts.** `grep -c` counts matching LINES,
and a predicted count is a guess about how many times a token was written — it goes wrong
often enough to be worse than useless, because a wrong prediction looks like a failed
deploy. `scripts/selfcheck_flows.py` asserts the things that actually matter (an employer
taking on a transfer is not asked the helper's name; a helper-initiated transfer reads as
a candidate; the hiring total is blocked while salary is not; neither renewal flow asks
for a case ID) and exits non-zero on any failure. Note `scripts/` is NOT in the image, so
it needs `docker compose cp scripts chatbot:/app/scripts` first — and that copy lives
only in the running container's writable layer, so it is lost on the next recreate.

**Resetting test data.** `reset_conversation.py` deliberately never deletes from `leads`.
Because of §1B a reset number therefore keeps its lead and will not produce a new one —
this looks like a bug and is not. To test lead creation properly, use a number with no
lead, **or delete the lead row and that thread's checkpoint together**. Deleting the lead
alone is what broke conversation 36: the checkpoint kept pointing at the dead row and
every ticket insert failed the foreign key, silently, ten times in twenty minutes.

---

## 11. Change log

Append here, newest first. One entry per behavioural change.

- **2026-09-04** — **HOTFIX: the bot stopped replying entirely.** `intro_note = ... if
  first_contact else ""` was placed eighty lines ABOVE `first_contact = ...`, so
  `info_collector` raised `UnboundLocalError` on every turn from the 14:02 deploy onward.
  The graph caught it as `bot_confused` and handed each message to a human, which is why
  the symptom was silence rather than an error — a client sent the same message twice and
  got nothing. `first_contact` is now `_is_first_contact(state)`, one function used by
  both sites, so the two can neither drift nor be ordered wrongly. **The lesson is the
  test, not the typo:** `selfcheck_flows.py` passed all 18 of its assertions in the
  running container while this was live, because it reads data structures and never
  executes a node. New `scripts/smoke_nodes.py` runs `info_collector` against seven states
  with the LLM and `_open_lead_early` stubbed — no network, no database — and fails all
  seven on this bug (verified by reintroducing it). Run it before every deploy.

- **2026-09-04** — **Three fixes from the agency's own testing.** (A) **"or swimming
  ability?"** went out to a client. It came from `additional_notes`' label, where
  *"must be able to swim"* had been written on 2026-09-03 as an illustration of the SHAPE
  of a volunteered requirement — and an example in a label is read as something to offer.
  Replaced with "phone use during work", which the office actually hears. **An example in
  a field label is a suggestion the client will be read; only put real ones there.**
  (B) **The bot picked the update channel for the client and then asked for the one detail
  that channel needs.** *"Do you have an email I can note down for updates?"* → *"please
  update through this phone number whatsapp"*. New `update_channel` field asks which they
  want, and `email` is gated behind choosing email — so a WhatsApp client is never asked
  for one. Agency's own suggested wording. (C) **Claire skipped her own introduction.**
  Live 19:39: a first message ("which nationality is the cheapest to hire") was answered
  with the salary range and the next question, no introduction at all. Rule 1 and
  `build_system_prompt`'s stage line both call for it, but on a collector turn they lose to
  `COLLECTOR_INSTRUCTION`'s "ask for that one detail and nothing else".
  `COLLECTOR_INTRO_NOTE` repeats it in the instruction that is actually winning, and the
  sentence budget goes to **4** for the one case that needs all three — introduction,
  answer, question. `response_generator` also clamped a first message to 2, which pushed
  the introduction off the end there; it gets 3 on first contact now. `new_hiring` is 24
  fields. `scripts/selfcheck_flows.py` is 18 assertions and **caught the field-count change
  itself** on the first run after this edit, which is what it is for.

- **2026-09-04** — **Stop interrogating people.** Three changes from the client's
  strongest piece of feedback. (A) **The case ID question is gone from every flow.**
  `passport_renewal` and `renewal` lost it earlier today; `home_leave` and `replacement`
  lose it now. Their objection is general — clients do not remember a reference from a
  case opened months or years ago — and `contact.find_active_case()` already finds it from
  the phone number, which is what they asked for. `_case_id()` is kept but uncalled: the
  objection is to *asking*, not to the concept, and a volunteered one is still recorded.
  `scripts/selfcheck_flows.py` asserts no flow ever gains one back. (B) **Rule 1c: use
  their name.** 1b was entirely negative — never call them Claire, use no name if you do
  not know theirs — with nothing saying to use the name when we DO have it, which we
  usually do (`employers.display_name`, the lead, or the WhatsApp push name). Once, at the
  top of the conversation, not sprinkled through every reply. (C) **`_COLLECTION_PURPOSE`
  — every qualification now says why before it starts.** Their words: *"question, answer,
  question, answer... feels like an interrogation and will increase the drop-off rate"*,
  and `new_hiring` is 23 fields deep. The note fires once, on the opening turn, gated on
  the same "nothing asked yet" test as the small-ticket briefing (the two sets are
  disjoint, so a flow gets one or the other). **The purpose is supplied, never the
  wording** — a fixed opening sentence is exactly the formula the no-repeat rules exist to
  prevent, so the model is told the reason and left to say it in its own voice, once,
  never again in that conversation. All six qualification flows are covered including
  `transfer` (the helper's own, six questions, and she has less patience than an employer);
  the self-check fails if any flow over four fields is left without one.

- **2026-09-04** — **Transfer: an employer looking FOR a helper stops being asked her
  name, and a helper asking for herself stops being run as an employer.** The client's
  definition: a transfer helper is one already in Singapore, available because the
  previous employer or recruitment arrangement ended — and **either side can start it**.
  (A) `transfer_employer` asked `helper_name` and `reason` unconditionally, so *"I'm
  looking for a transfer helper"* was answered *"May I know the helper's name?"* — of
  someone who has not met her. (Their analogy: you would not ask the developer's name of
  somebody looking to hire a developer.) Everything after the direction question is now
  gated: **`_RELEASING_HELPER`** opens the helper's name and the reason,
  **`_TAKING_ON_TRANSFER`** opens a short requirement set instead — what they need help
  with, preferred nationality, household, budget. All four keys are in
  `_PORTABLE_ACROSS_SERVICES`, so an employer who answered them in a hiring flow is not
  asked twice, and a first-time employer gets requirement questions rather than questions
  about a helper they do not have. The gate is *undecided* until the direction is
  answered, which means the name can never be asked before we know which of the two they
  mean. (B) **`_HELPER_SPEAKING`** overrides `_CONTACT_FALLBACK_BY_INTENT["transfer"] =
  "employer"` when the message is written in the first person about herself — "transfer
  me", "my employer", "find me a new employer". *"I want to transfer my helper"* is
  excluded by a lookahead, so the employer half is untouched. Verified against 6 helper
  and 6 employer phrasings. The KB already defines the term (retrieval 0.64-0.77 on
  "what is a transfer helper"), so no content was needed.

- **2026-09-04** — **The employer flow now asks what the candidate form profiles.** The
  client's instruction was to stop asking arbitrary questions and take the requirement set
  from the existing forms. The form is `candidates.biodata`: every helper answers 19
  `commitments` and 22 `skills`, and the `candidates` columns add age, experience_years,
  english_level and religion. Mapping the employer flow against it found **nine matching
  attributes with no employer-side question at all** — `share_room`, `window_clean`,
  `wash_car`, `gardening`, `go_marketing`, `hand_wash`, `handle_pork`/`handle_beef`,
  `age`, `experience_years`. Closed in **three questions and one rewording**, using the
  form's own wording so the extractor maps cleanly: **`helper_room`** (own room vs
  sharing — a hard filter, plenty of helpers will not share, and it was simply absent);
  **`helper_profile`** (age and experience in one question, since they are one thought);
  **`special_duties`** (the five extra-duty commitments in one question rather than five);
  and **`cooking`** widened to ask whether she would need to handle pork or beef. 20 → 23
  fields, two of the three optional. Still unresolved and NOT a code problem: D also
  requires that "a salesperson should engage with the customer first", and §9.1 means
  every ticket is still created **unassigned**.

- **2026-09-04** — **Insurance becomes a service; a new hire's cost stops being quoted.**
  (A) **`insurance` did not exist** — no field list, no intent, nothing — yet the client
  names it a small-ticket service beside permit and passport renewal. It now has its own
  service key (three questions, two optional), sits in `_SMALL_TICKET_SERVICES` and
  `EMPLOYER_LEAD_SERVICES`, and is filed under `renewal` by `TICKET_SERVICE_FALLBACK`
  since `cb_tkt_service_check` does not allow the value. Its own key, not a remap onto
  `renewal`, for the TRANSFER_EMPLOYER reason: the blocked-topic key IS the service key.
  In both alias tables `insuran` is matched **before** `renew`, so "renew my helper's
  insurance" is insurance. The knowledge base already answers it — *"A combined 14-month
  policy costs S$280-350; a 26-month policy costs S$400-520"*, retrieval 0.63-0.73 — so
  the cost requirement is met with no new content.
  (B) **The hiring cost is now withheld.** `ungrounded_figures` was passing the total
  happily, because it is genuinely in the records: *"approximately S$14,000-17,500"* and
  Form A's **$1,568** service fee. Grounded is not the same as wanted — the client's rule
  is that a new hire's price never reaches anyone before a salesperson has spoken to them.
  `quotes_hiring_package_cost` catches a total/package/placement-or-service-fee framing
  next to a figure and swaps in `COST_DEFERRAL_REPLY`, wired into both write paths.
  **Salary ($600-800), the levy ($300) and the $5,000 security bond are deliberately NOT
  caught** — they are not what a client means by "the cost", and withholding them would
  make the bot evasive about facts they are entitled to. Scoped to `new_hiring`,
  `direct_hiring` and `fee_enquiry`; small-ticket services quote freely.
  **KB contradictions found and NOT fixed** (we do not know which is right): one row says
  medical insurance minimum **S$15,000**/year and another says **S$60,000**/year (MOM's
  actual figure is $15,000, so the second looks wrong); and Form A prices insurance at
  **$590** while the FAQ says **$280-350 / $400-520**. The bot may quote either.

- **2026-09-04** — **Work permit renewal: no case ID, and it explains itself.** Thomas
  groups WPR with passport renewal and insurance as a *small-ticket* service — one we do
  end to end rather than qualify and hand off. (A) **`renewal` lost `_case_id()`**, same
  reasoning as passport_renewal, and `helper_name` now fills from `placed_helper`, so a
  recognised client answers **one** question (permit expiry) instead of three.
  (B) **`_SMALL_TICKET_SERVICES`** makes the opening collector turn lead with what the
  job involves before asking anything, and widens that turn's sentence budget to 3. The
  records already carry it: retrieval on "I want to renew my helper's work permit" scores
  **0.82** and returns *"you need an updated employment contract, current insurance
  coverage, a recent medical examination, and a renewal application submitted through
  MOM's e-Service portal. Ming Hwee handles the entire process. Total time on your end:
  less than 1 hour over 4 weeks."* — process, documents and duration in one row. The note
  is strictly grounded ("ONLY what the records above actually state... if the records say
  nothing, just ask") because there is **no agency fee for either service in the KB**
  (checked 2026-09-04): instructing it to quote a cost would be instructing it to invent
  one, and `ungrounded_figures` would bin the reply. Load the fee and it quotes it with no
  code change. Also confirmed while checking: 84 of 275 KB rows carry their text in
  `question`/`answer` with `content` NULL, and `rag._format_match` handles that correctly
  — not a bug, recorded so the next audit does not re-raise it.

- **2026-09-04** — **The bot says what it recognises.** Filling a field from the client's
  own file and saying nothing is indistinguishable, from their side, from never having
  asked: they see a bot that opened on question three. `recognised_note` now makes the
  collector open by naming the helper and the passport expiry we hold, on the one turn
  the records fill them — the same say-once mechanism `returning_note` uses (`known`
  carries only what was filled *this* turn). It **suppresses** `returning_note`, which
  instructs the opposite ("no details of who, when or how many, we are not showing them
  their file"); the specific note wins. The model is explicitly told to state the date and
  **not** to call it urgent or expiring soon — Liza Fernandez's passport runs to 2033, and
  the spec's own example phrasing ("expiring next May") is exactly the invention to avoid.

- **2026-09-04** — **Ask the database, not the client.** Six changes from one written
  spec. (A) **`passport_renewal` no longer asks for a case ID** — `_case_id()` removed
  from that flow entirely. Live, it was the *first* question a client got and the honest
  answer was "I don't have any case ID"; the number identifies them, a reference does
  not. (B) **The urgency question is gone** ("How soon does she need the new passport?").
  An expiring passport IS the urgency, the answer is always "as soon as possible", and
  the expiry date we already collect says it more precisely. (C) **"Have you hired with
  us before?" is never asked**, of anyone. `_known_fields` now fills `first_time_hire`
  from the `placements` count in **both** directions — a positive count reads "hired
  through us before - N placements on record", zero reads "first time with us - no
  placement on record". This reverses yesterday's "only a POSITIVE count is evidence" on
  the client's explicit instruction: not being in the database *is* the answer. The
  accepted cost is that a client who hired with us under a different number is filed as a
  first-timer; the wording keeps that legible to the agent, since "no placement on
  record" is a statement about our records rather than about the client. `returning_note`
  had to be re-keyed onto the count, since the field is now always present.
  (D) **`get_placed_helper()`** fills the helper's name and nationality from
  `placements` → `candidates`, so an existing client is not asked for someone already on
  their own file. Measured: an employer with one linked placement now skips two of five
  passport-renewal questions. Deliberately conservative — it returns nothing unless there
  is exactly ONE live placement AND it names a candidate, because `candidate_id` is null
  on 4 of 6 rows and one employer has 4 placements with a single candidate among them.
  (E) **The languages question stopped hiding four of its options.** The field already
  offered seven, but the question was a bare "What languages are spoken at home?" and the
  model picked three to show ("such as English, Mandarin or Tamil?"). The question now
  names the list and invites more than one, and `other` was added to the options.
  (F) **The passport expiry IS read from the database** — from
  `candidates.biodata.passportExpiry`, a jsonb blob, not a column. An earlier pass on
  this same commit audited the schema by COLUMN NAME, found no passport column anywhere,
  and reported the requirement as impossible; the client corrected it. All 6 candidate
  rows carry the expiry. Only the expiry is read — `passportNo` sits in the same blob and
  is deliberately left there, because anything returned lands in `collected_info` and so
  in the prompt, and Rule 4a keeps the Singpass block off WhatsApp. The stored ISO date
  is reformatted to "27 September 2033" since the model reads it back to the client and
  09/27 vs 27/09 is a real ambiguity here. Measured: an existing client with a linked
  placement now answers **2** passport-renewal questions instead of 7 — name, country and
  expiry come off their file, and the case ID and urgency questions are gone entirely.

- **2026-09-03** — **Replacement collects five more things; a parked service no longer
  starves retrieval; a volunteered requirement is acknowledged.** (A) **`replacement`
  went from 4 fields to 8**, at the client's request: how long the current helper has
  been with them, whether we placed her, what happens to her (home vs transferred out —
  the difference between a repatriation and a transfer, and it decides who picks the case
  up), a two-sided timeline, and what they want in the replacement. `helper_from_us` is
  deliberately a question and not a `prior_hires` read: that count says whether we have
  placed *anyone* with them, not whether we placed *this* helper, and
  `placements.candidate_id` is null on most rows so there is nothing to match her
  against. (B) **A service filter buried an answerable question.** Live: with a
  `passport_renewal` ticket parked, *"How much time it takes in renewal"* was filtered to
  the four `passport_renewal` rows, scored **0.385** and fell under the 0.40 soft floor,
  so the client got the holding line — while the rows that answer it (work permit
  renewal, filed under `renewal`) score **0.464** and were excluded by the filter, not by
  the question. The next message happened to contain the word "passport", scored 0.506
  and was answered, so from the client's side we ignored a question and then answered it
  a message late. `rag_retriever` now retries with **no service filter** whenever the
  filtered search comes back under the floor, keeping the better of the two. Handled here
  rather than as another `_service_filter` keyword because the trigger is not the wording
  of the question — it is the filtered search coming back empty-handed. **Nationality is
  deliberately kept** on the retry: the KB holds per-nationality passport timings and a
  confident answer about the wrong country is worse than a holding line. Measured after:
  0.385 → 0.443 (answers), and *"how much do you charge"* still correctly falls to the
  holding line, so the widening does not turn everything into a confident answer.
  (C) **A stated requirement was silently dropped.** *"She shouldn't do smoke and drinks
  not allowed in my home please"* got the next question with no reaction; the client
  asked *"Did you read this?"* and the model replied *"Yes, I read it"* while paraphrasing
  a **different** message, then admitted the skip only when pushed a second time. Three
  parts: `additional_notes`' label was widened from "anything else we should know" — which
  is not something an extractor reads a house rule into — to name requirements, house
  rules and preferences outright; `COLLECTOR_INSTRUCTION`'s "never repeat their sentence
  back" rule was **carved out**, since it was written about echoing the answer to the
  question just asked and was being applied to volunteered requirements too (another
  prompt instructing the bad behaviour); and `_VOLUNTEERED_REQUIREMENT` adds the
  acknowledgement note mechanically so it does not depend on the model noticing. The
  pattern is narrow on purpose — `can't` and `don't` are excluded because they are far
  more often about the client ("I can't say yet", "I don't have a case ID"). Verified: all
  6 requirement shapes fire, all 19 ordinary answers from that transcript stay quiet.

- **2026-09-03** — **The hiring flow asks the database before it asks the client, and an
  answer to our own question can no longer be swallowed by a parked topic.** Both from
  one live thread. (A) Mid-transfer, "Meanwhile I want to hire new helper" was answered
  **"Is this your first time hiring a domestic helper?"** — asked of a man who was in the
  middle of telling us about the helper he currently employs, and asked about hiring in
  general when what the agency needs to know is whether they are *our* client. Two
  changes: the question is now scoped to us (*"Have you hired a helper through our agency
  before, or will this be your first time hiring with us?"*), and it is only asked when
  the database cannot answer it. `contact.count_prior_hires()` counts an employer's
  non-archived `placements` rows — the only table that records an actual placement, as
  opposed to `employers` (the portal has their details) or `leads` (they once enquired) —
  and the webhook puts the count on every turn as `prior_hires`. A positive count fills
  `first_time_hire` in `_known_fields`, so the question is never put and the collector is
  told once to welcome them back before asking its next question. **Only a positive count
  is evidence:** zero covers "hired elsewhere", "no placement on file yet" and "we have
  never seen this number", so those still get the question — which is exactly why the
  wording had to change too. (B) On the next message the client answered **"First time"**
  and got **"Noted, a live agent is handling the transfer"** — the collection opened one
  message earlier was abandoned mid-question. Two words carry no topic, so the model read
  them against a thread that was mostly about the transfer, labelled them `transfer`,
  which was parked, and `blocked_topic_responder` took the turn. New rule in
  `intent_classifier`, the mirror image of the 2026-09-03 named-service correction: when a
  live, **unparked** collection exists and the turn lands on a **parked** topic, keep the
  live collection — unless the client **names** that service (the older correction's job)
  or **chases** it (*"any update on my transfer?"*), which is what parking a topic is for.
  Verified against six shapes. `_CHASING_STATUS` moved from `blocked_topic_responder` to
  `intent_classifier` rather than being duplicated — that module already imports
  `_named_service` from this one, and the reverse direction would be a cycle.
  `transfer_employer` deliberately did **not** gain a history question: it is documented
  as short on purpose, and the agent can read the placement history off the portal.

- **2026-09-03** — **Prompt audit: six contradictions removed.** Confirmed the live model
  is `openai/gpt-5.6-luna`, so the recent misclassifications were Luna's — but the audit
  found the prompts were instructing much of the bad behaviour outright, which no model
  change would fix. (1) **IDENTITY claimed to be human.** *"You are a real person on the
  team"* sat one sentence before *"you are Ming Hwee's AI assistant. You do not pretend
  to be human."* A leftover from the pre-Claire persona, and a standing instruction to
  lie about being an AI. Removed. (2) **Two contradictory sentence caps.** Rule 6 and
  `style.py` both said *"never three"*, while rule 2 requires a handover line **plus**
  the "anything else?" offer, first contact needs greeting + question, and
  `ANSWER_THEN_ASK` says "no more than three" — the guard was widened to 3 for exactly
  these on 2026-09-02 but the prompt still forbade it. Rule 6 now names the three cases
  where a third sentence is *expected*, and caps at four. (3) **`style.py` banned the
  offer rule 2 requires** — *"Never offer further help as a closing line"* vs rule 2's
  mandatory *"In the meantime, is there anything else I can help you with?"*. Now carved
  out. (4) **`style.py` banned the "I'll confirm the exact amount" that rule 5 requires**
  for money. Now exempted for fees/salaries/levies. (5) **`style.py` still said
  "hand over silently"** — the reverse of the announced-handover policy since 2026-09-01,
  and it also skipped rule 2b. Rewritten. (6) **`style.py` said agents "ask 2-3 things"**
  against rule 8's one-question-per-message; reconciled (one question may cover related
  details). Also: the unknown-contact block said only *"default to employer tone"*, which
  helped bury the job-seeker — it now says to read the message first. **Code:**
  `response_generator` never checked `near_duplicate`, so it sent *"I'll check with the
  team and come back to you shortly."* twice word for word to a job seeker (live), who
  replied *"what is something to check with your team"*. It now varies the wording and
  hands over — repeating ourselves means we are stuck.

- **2026-09-03** — **Transfer and job-seeker forced deterministically; the compound
  "transfer out + hire new → new_hiring" rule is REVERSED.** Two live failures, both
  from the client's own screenshots. (A) *"Hello"* → *"I need to transfer my maid to
  someone else because I need to find a new maid"* → **"Is this your first time hiring a
  domestic helper?"**. That was not a model slip: the classifier prompt explicitly
  instructed it, telling the model to classify a compound release-plus-hire as the
  collectible half, `new_hiring`. Per the client's written spec (transfer = an existing
  helper on a valid permit moving between employers; *"the word transfer near
  maid/helper/employer means TRANSFER — never New Hiring"*, and never ask a transfer
  client the first-time question), **transfer now wins whenever the word is used about a
  helper**, even in a compound message; the onward hire becomes an agent step. The
  new_hiring preference survives only for a compound with **no** mention of a transfer
  ("my helper is leaving, I want an Indonesian one"). (B) A job-seeker — *"I heard you
  provide some work to us so that we can earn money"*, *"I can go to someone's home and
  do some work"*, *"I said I need a job"* — was never read as `candidate_registration`
  and got the same holding line twice, prompting *"what is something to check with your
  team"*. Both are now keyword overrides beside `ASSAULT_PATTERNS`, because the prompt
  alone has now failed on each of them live. Neither fires while a different service is
  mid-collection, and the job-seeker net is skipped for a known employer so *"someone to
  work at **my** home"* stays `new_hiring` — verified against six employer phrasings.
  Both are English-only, so they inherit §9.2.

- **2026-09-03** — **Model switched to `openai/gpt-5.6-luna`** (from `moonshotai/kimi-k3`).
  Non-pro Luna deliberately — the `luna-pro` slug bakes in `reasoning.mode=pro` (heavy
  thinking billed as output, 7–55s latency), wrong for quick WhatsApp replies. On the
  benchmarks Luna is well above Kimi on intelligence/coding/agentic, has a 1M context, and
  a lower sticker price ($0.20/$1.20 vs $0.52/$2.45 per 1M) — though a reasoning model's
  real cost depends on how much it thinks. `llm.py` needed no change (model-agnostic,
  sends `reasoning` via `extra_body`, no `budget_tokens`). `.env.example` and
  `config.py` default updated; the **live server `.env` must be edited too** (it overrides
  the code default) — set `OPENROUTER_MODEL=openai/gpt-5.6-luna` and `docker compose up -d`
  (not `restart`). Rollback = set it back to `moonshotai/kimi-k3`. NOT YET VALIDATED: the
  guards (`leaks_internal_reasoning`, `ungrounded_figures`, the language rules) were all
  tuned against Kimi's failure modes; Luna fails differently and needs a full scenario
  pass. The Kimi-specific notes are kept as a switch-back hint.

- **2026-09-03** — **"Of course — which service can I help you with?" asked for a
  service the client had just named.** After a hiring + salary handover, "I am looking
  for transfer helper" read to the model as `other`; the stickiness rule (intent
  classifier, "an in-flight service survives an ambiguous follow-up") glued the parked
  service back on, so `_blocked_topic` matched and `blocked_topic_responder` sent
  `NEW_SERVICE_REPLY` — asking which service, for the one they had named. The client had
  to repeat "I want transfer service I have tell you that" before collection started.
  Root cause: the keyword map (`_INTENT_ALIASES`) only ever runs against the model's
  returned *intent string*, never the client's own words, so a plainly-named service was
  invisible to the deterministic layer. Fix: `_named_service()` matches the standalone,
  unambiguous service words (transfer, renew, replace, passport, home leave, direct
  hire) against the *message*, and the stickiness branch now overrides the parked service
  when the client both **names** such work and **asks for** it (`_WANTS_SERVICE`), gated
  on `blocked_topics` being non-empty. So the new request routes to its own collection
  instead of being parked. The two gates keep it from hijacking an ordinary
  mid-collection answer: "Filipino" and "her passport is expiring" (no want-verb) still
  stick; "I also want to renew my helper's permit" overrides. Verified against all seven
  shapes. The broad `new_hiring` net and the money words are deliberately excluded.

- **2026-09-03** — **Model leaked its own reasoning to a client on a salary-range
  question.** When retrieval came back empty, `rag.format_context` injected a block
  containing the literal string `(no relevant records found)` and the instruction
  *"You do not have information to answer this. Tell the client you will check..."*.
  Kimi copied it almost verbatim to the client: *"The client asked about salary range.
  Records say 'no relevant records found', so I can't give a figure ... never quote a
  figure that isn't there. Should I ask them the range, or handle this together?"* —
  the whole reply was internal monologue. It slipped every guard: fluent (not
  degenerate), unbracketed (`strip_meta_commentary` only cuts brackets), no figure, no
  named colleague. Two-part fix, both halves: (1) the empty-records block was reworded
  to carry no quotable status line and no copyable instruction; (2) new guard
  `leaks_internal_reasoning` discards any reply carrying process-narration markers
  ("no relevant records found", "you do not have information to answer", "the client
  asked/wants/said", "should I ... handle this", "never quote", "figure that isn't
  there") — every one is language that can only appear when the model is describing its
  own reasoning, so a match drops the whole reply for the holding line. Wired into both
  `response_generator._clean`'s discard path and `info_collector._write` (the collector
  answers mid-flow money questions, so it hits the same turn). Verified: all four leak
  shapes caught, the grounded `$1,568` fee answer and the honest "I'll check with our
  team" deflection both pass untouched. The separate question of whether fix E is
  actually surfacing salary *figures* for this turn is unresolved pending the RAG log
  line (`KB search returned N matches ... filters: service=...`) for that conversation.

- **2026-09-02** — Fifth live round. Five defects, each reproduced from the log or a
  screenshot before it was touched.
  (A) **The model never saw a field's own question.** `COLLECTOR_INSTRUCTION` is given
  `field.label` only, so `elderly_detail` — written as *"their age, how mobile they are,
  and any medical conditions"* — went out as *"May I know who the care is for?"*.
  "For my grandmother" filled it, the flow moved on, and the medical condition was never
  asked about at all; the client spent four messages saying so ("you are ignoring that").
  `_field_guidance` now carries the hand-written question as the ground the question must
  cover. **The label says which field; only the question says what it has to get.**
  (B) **A field could hold a value and still not be answered.** `_unfinished()` catches two
  shapes that both reached a client: an answer asserting something exists without saying
  what ("Yes grandmother has medical condition") and a mistyped address (`Vd@gmail.con`,
  which went onto the lead as written — email is the only channel the office has for
  sending profiles, so it fails silently and forever). The value is **kept**; the field
  goes back to the front of the queue for one more ask, bounded by `max_asks`.
  (C) **`clamp_reply(2)` was deleting required sentences**, and the log proves it both
  ways: *dropped "In the meantime, is there anything else I can help you with?"* after a
  handover (§8 requires that offer), and on first contact *"Good Evening! I'm Claire, Ming
  Hwee's AI assistant."* went out with no question, so the client had to repeat "I need
  helper". It was a coin flip on punctuation — a comma after "Good Evening" makes it one
  sentence and the question survives; an exclamation mark makes it two and the question is
  cut. Now 3 on first contact, on the handover close, and when answering a client's
  question.
  (D) **A question inside an answer lost both halves.** "In 2 weeks can you provide" has no
  question mark, so `_ASKS_SOMETHING` never fired and the question was ignored — while
  `_VALUE_IS_QUESTION`, far broader, *did* match and discarded the "in 2 weeks" answer too.
  The two patterns now agree on what reads as a question.
  (E) **Money retrieval widened to the turn before the question.** `ungrounded_figures`
  binned a whole reply — logged: *quoted unstated figure(s) ['700', '500', '600']* — because
  the turn that asks the budget question retrieved under `service=new_hiring` while the
  figures sit under `salary_enquiry`. `_service_filter` now also drops the filter when a
  money field is one of the next two, or when our own last line asked about money. Only the
  next two: `budget` is outstanding from turn 1, so testing the whole list would disable
  the service filter for the entire conversation.
  Also: `speaks_of_us_as_a_third_party` — Claire is Ming Hwee, so *"we sent it all over to
  the agency as instructed, they have passed them up to a live agent"* (live, after a
  client asked three times why a question had been skipped) is a different speaker, not a
  paraphrase. Nothing else caught it: fluent, not degenerate, no named colleague, no
  promised time, not in brackets.

- **2026-09-02** — Multi-topic round: four fixes for a thread that piled hiring + salary +
  transfer + passport into one conversation. (A) **Employer transfer given its own service
  `transfer_employer`**, fixing a regression from earlier the same day: remapping it onto
  `new_hiring` made its blocked-topic key identical to an open hiring ticket's, so a new
  transfer request was answered as a follow-up ("a live agent will connect with you
  shortly", forever) and never collected. Its field list is employer-answerable and opens
  by disambiguating take-on vs release. (B) **A service switch no longer wipes everything**
  — `_PORTABLE_ACROSS_SERVICES` carries client facts (name, nationality, household,
  budget, email) across, while helper/case/document fields still reset. This is why a
  salary question after a hiring flow re-asked "which nationality are you looking at?".
  (C) **A money question searches the whole KB** instead of being filtered to the in-flight
  service — measured: filtered retrieval surfaced **0** salary figures, unfiltered surfaced
  `$200/$300/$450/$950`, which is why the bot said "I don't have a specific range" twice
  before answering on the third ask. (D) **`passport_renewal` collects** helper location,
  permit expiry and urgency (Rule 4a still bars the passport number itself).

- **2026-09-02** — Second round of live-tester fixes. (1) **A client's question is
  never recorded as a field answer**: the extractor was filing "is there a salary budget
  in mind?" as the `budget` value, so the field looked answered and the salary question
  went unanswered until re-asked. `_VALUE_IS_QUESTION` in `info_collector` drops any
  interrogative extracted value, leaving the field open and letting ANSWER_THEN_ASK
  answer the question. (2) **`HISTORY_LIMIT` 20 → 40** (code default *and* both `.env`
  files — the `.env` value overrides the code default, so the live server `.env` must be
  edited too): a full `new_hiring` qualification runs 20+ turns and the earliest exchanges
  were scrolling out of the model's view mid-flow. Collected fields already persist in the
  checkpoint, so this only widens the free-text window. (3) **"workers"/"domestic worker"
  now read as `new_hiring`**, and a first-contact timeline-feasibility question ("can they
  start before October?") engages and collects instead of handing straight to an agent.
  (4) **Compound "release my helper + hire a new one" classifies as `new_hiring`** (the
  collectible need), with the release left as an agent step — it was being run as a bare
  `replacement` that collected nothing about the new hire.
- **2026-09-02** — Model reverted to `moonshotai/kimi-k3` (from `anthropic/claude-sonnet-5`).
  `.env` and `.env.example` updated; `llm.py` needed no change (model-agnostic, sends no
  `budget_tokens`). The Sonnet-specific parameter notes are kept as a switch-back hint.

- **2026-09-02** — Live multi-tester fixes. (1) **Transfer split by contact type**: an
  employer asking for a transfer helper was being run through the helper's own
  questionnaire (name, permit expiry, current employer's consent, which number to reach
  him on) — all four complaints from one tester. `resolve_service` now routes an
  employer's `transfer` into `new_hiring` with `hire_source` seeded to `transfer`;
  `transfer` removed from `_CONTACT_BY_INTENT` and made an employer *fallback*.
  (2) **Language flip fixed**: rule 12d — a short/ambiguous message ("hiring", "yes", a
  name) no longer switches the reply language; and **all non-Latin script purged from the
  system prompt** (it was the strongest foreign-language signal for a one-word English
  input). (3) **Rule 1b**: Claire never addresses the client by its own name; uses no name
  when the client's is unknown.
- **2026-09-01** — Claire persona: AI disclosure on first contact, handovers announced to
  the client ("a live agent will connect with you shortly" + offer further help).
  Inverted `_HANDOVER_TALK` from "no handover talk" to "no named colleague / no promised
  time"; rewrote 14 canned strings and 7 instruction templates. Assault replies
  deliberately omit the closing offer.
- **2026-09-01** — Reply in the client's language (rules 12/12a/12b/12c). Extraction
  values and lead summaries forced to English. Clock-block greeting made language-aware
  after it emitted "好的下午".
- **2026-09-01** — Email moved from question 2 to last in `new_hiring` and
  `candidate_new_hiring`; given `group="staying in touch"`.
- **2026-09-01** — Model switched to `anthropic/claude-sonnet-5` (from Kimi K2.6).
- **2026-09-01** — Survive a deleted lead row: ticket insert retries without
  `created_lead_id`; `update_from_collected` no longer logs success on a zero-row update;
  added `created_lead_kind` so `lead_kind` being reset per turn cannot misroute a
  candidate lead update to the `leads` table.
- **2026-09-01** — Use the WhatsApp push name for `full_name` when it is plainly a
  person's name, so the bot stops asking for a name it just used in its greeting.
