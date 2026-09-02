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
| Max 2 sentences | `guards.clamp_reply` | Logs the discarded tail deliberately. |
| Never repeat an opener | `guards.strip_repeated_opener` | Checks several previous bot lines. |
| One phone, one lead, ever | `lead.create_if_absent` (§1B) | **Currently scoped per-table — see §9.** |
| Ticket survives a dead lead link | `ticket.create` retry | Drops `created_lead_id`, records `notes.lead_link_broken`. |
| NRIC never reaches the LLM | `utils.redact_nric` | Applied to the batch text; the raw body is still stored in the DB. |
| Assault: keyword override, no LLM confidence | `intent_classifier.ASSAULT_PATTERNS` | **English-only — see §9.** |

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
- `OPENROUTER_MODEL` — currently `moonshotai/kimi-k3`. `llm.py` is model-agnostic: it
  reads this setting, sends `temperature`/`frequency_penalty`, and never sends
  `budget_tokens`. Provider routing is deliberately unconstrained — OpenRouter drops
  parameters the target provider does not accept, and `require_parameters` made every
  call 404, so **do not reintroduce it**. (If you switch back to Sonnet 5: `off` maps to
  Anthropic thinking disabled, and `budget_tokens` 400s there — but `llm.py` sends none.)
- `LLM_REASONING=off` — Kimi K3 is a reasoning model; `off` turns the thinking down for
  ~3s replies at far lower cost.
- `RAG_SOFT_FLOOR` (0.40) is the real retrieval knob. `RAG_CONFIDENCE_FLOOR` is **dead
  config, read by nothing** — kept only so existing `.env` files still parse.
- `TENANT_ID` must be set or lead and ticket numbering break.

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
written for the *helper* (her permit expiry, her employer's consent, her availability). An
**employer** who says "transfer" wants to *hire* a transfer helper, so he is routed into
`new_hiring` with `hire_source` pre-seeded to `transfer` — he is never asked the helper's
own questions. Only a `candidate` keeps the helper-side `transfer` flow. `transfer` is
therefore no longer in `_CONTACT_BY_INTENT` (it was forcing every transfer to `employer`,
which is what put an employer into the helper's questionnaire); it is an employer
*fallback* instead, so the model can still tag a genuine job-seeker as a candidate.

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
10. **Unbounded in-memory caches** with no TTL: `_LOCKS` (webhook),
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
python scripts/preflight.py            # go-live gate: KB, agents, branch, portal bridge
python scripts/check_retrieval.py      # retrieval calibration; tunes RAG_SOFT_FLOOR
python scripts/watch_conversation.py --follow

# DESTRUCTIVE — test numbers only
python scripts/reset_conversation.py +6591234567
```

**Resetting test data.** `reset_conversation.py` deliberately never deletes from `leads`.
Because of §1B a reset number therefore keeps its lead and will not produce a new one —
this looks like a bug and is not. To test lead creation properly, use a number with no
lead, **or delete the lead row and that thread's checkpoint together**. Deleting the lead
alone is what broke conversation 36: the checkpoint kept pointing at the dead row and
every ticket insert failed the foreign key, silently, ten times in twenty minutes.

---

## 11. Change log

Append here, newest first. One entry per behavioural change.

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
