# Sprint 0 — chatbot repo safety check for "AI Suggested Replies"

Read-only review of this repo at commit `c138f91` (branch `main`), 2026-10-27.
Nothing was run, no `.env` was opened, no database or network was touched.
Every finding cites `path:Lnn`. Anything not provable from this repo is marked
**UNVERIFIED**. Facts about the OS repo (the dropped index, the generated
`whapi_dedup_id` column) are taken from the brief and not checked here.

---

## Q1. Message saving (`wp_chat_messages`)

### Q1.1 Every insert this repo makes

All application writes to `wp_chat_messages` go through one helper,
`message._insert_ignoring_duplicates` (`app/services/message.py:L448-L455`),
which calls `db.insert(TABLE, payload)` (`app/db/supabase.py:L143-L146`), i.e.
`client.table("wp_chat_messages").insert(payload).execute()` run on a worker
thread (`app/db/supabase.py:L65-L67`, `L112`).

- **Plain `insert`.** No `upsert`, no `on_conflict`, no `ignore_duplicates`
  anywhere in `app/` (grep of `app/` for `upsert|on_conflict|ignore_duplicates`
  finds only a comment in `app/graph/checkpointer.py:L91`, about LangGraph's
  own tables).
- **Writes are never retried** (`app/db/supabase.py:L104`: one attempt unless
  the method is GET/HEAD).

| Caller | Where | Columns written |
|---|---|---|
| Inbound client message `store_incoming` | `message.py:L210-L237`, called `webhook.py:L748` | conversation_id, direction='inbound', from_number (digits), to_number, body, whapi_message_id, status='received', is_bot=False, media_path, media_type, media_mime, media_filename, media_caption |
| Bot reply `send_bot_reply` | `message.py:L463-L517` (insert `L503-L515`) | conversation_id, direction='outbound', from_number, to_number (digits), body, whapi_message_id (from the Whapi send response, `L500`), status='sent', sent_by='bot', is_bot=True |
| Human agent reply `store_agent_reply` | `message.py:L275-L299`, called `webhook.py:L895-L898` | as inbound plus sent_by='agent', is_bot=False, direction='outbound', media columns |
| WhatsApp Business auto-reply / broadcast `store_auto_reply` | `message.py:L302-L321`, called `webhook.py:L870` | conversation_id, direction='outbound', from_number, to_number (**not** digit-normalised, unlike the others), body, whapi_message_id, status='sent', sent_by='whatsapp_auto', is_bot=True |
| Second copy of our own reply `store_own_echo` | `message.py:L130-L150`, called `webhook.py:L858` | as the bot reply, sent_by='bot', is_bot=True, whapi_message_id of the echo |

`send_bot_reply` is reached from the normal turn (`webhook.py:L1358`), the
emergency/assault reply (`webhook.py:L337`) and the "are you there" reply during
a stand-down (`webhook.py:L413`).

Outside `app/`: `scripts/seed_reset_ui_testdata.py:L121` inserts a test row by
raw SQL (plain `insert`, test numbers only). No other insert found.

### Q1.2 Would any of these fail with 42P10 or 23505?

- **42P10: no.** 42P10 is raised only for `INSERT ... ON CONFLICT (target)` when
  no unique index matches the target. This repo never sends `on_conflict`
  (supabase-py adds `?on_conflict=` only for `upsert`), so dropping the
  `whapi_message_id` unique index cannot produce 42P10 from this code.
- **23505: yes, possible, and it is swallowed.** With the partial unique index
  on `whapi_dedup_id`, any insert whose id (minus its `-suffix`) is already on
  the table fails with 23505. `_insert_ignoring_duplicates` treats any message
  containing "duplicate key" or "23505" as already stored and returns `None`
  (`message.py:L451-L454`).
- **New compared with the old index:** two *different* full ids that share a
  prefix now collide. If Whapi's send-response id and the from_me echo id
  differ only in the suffix, whichever writer is second gets 23505. This is
  **UNVERIFIED** (no Whapi id samples in this repo), but it is the only reason
  a suffix-stripping dedup column would exist.

### Q1.3 How a failed insert is handled

| Failure | Inbound | Bot reply | Agent reply | Auto-reply / own echo |
|---|---|---|---|---|
| **23505** | `DEBUG "Message %s already stored"` (`message.py:L453`), returns None, turn continues | same DEBUG line; **reply already sent**; turn continues normally | same | same |
| **any other error** | re-raised (`L455`) → `handle_payload` logs `ERROR "Failed to handle message <id>"` with traceback (`webhook.py:L273-L274`). **The message is never debounced, so the client gets no reply.** | re-raised **after** the WhatsApp send (`L495` then `L503`). The client has the reply; the row is missing; `touch_outbound` (`L516`, stamps `last_bot_reply_at`) and everything after the send in `_process_locked` (typing stop, intent/service patch `webhook.py:L1362-L1375`) are skipped. Logged by the debouncer as `ERROR "Debounced handler failed for <phone>"` (`app/whapi/debouncer.py:L75-L76`). On the emergency path it is caught as `"Failed to handle message <id>"`. | caught: `ERROR "Could not store agent message on conversation %s — silencing the bot anyway"` (`webhook.py:L900-L904`); the stand-down still happens | re-raised → `"Failed to handle message <id>"`; for the auto-reply the broadcast recovery `_undo_broadcast_standdowns` (`webhook.py:L875`) is then skipped |

Default log level is INFO (`app/config.py:L134`), so **a 23505 is invisible in
production logs** — the DEBUG line is not emitted.

So: **the bot still sends its reply if its own insert fails** (the send comes
first). What is lost is the row, `last_bot_reply_at`, and the per-turn
conversation patch.

### Q1.4 Agent detection when the bot's own row is missing or mis-labelled

How detection works (`webhook.handle_outbound`, `webhook.py:L826-L909`):

1. `was_sent_by_bot(id)` (`message.py:L158-L163`): first an in-memory id cache
   filled right after the send (`L501`, TTL 600 s, `L36-L38`), then the DB row's
   `is_bot`, matched on the **exact** `whapi_message_id`.
2. If not, `echoes_our_own_send(phone, body)` (`message.py:L124-L127`): an
   in-memory body cache filled **before** the send (`L491`, TTL 300 s,
   `L80-L82`). A hit stores the copy as ours and returns with a WARNING
   `"outbound message %s repeats a reply we just sent"` (`webhook.py:L857-L866`).
3. Auto-reply / broadcast check, content check, then `store_agent_reply` and
   `handover.agent_took_over` → `bot_status = human_active`
   (`app/services/handover.py:L286-L293`), log
   `"Agent detected on conversation %s — bot silenced"`.

Cases:

- **Bot row never saved, process still up (within 5–10 min):** the echo is
  recognised by step 1 (same id) or step 2 (same body). No stand-down.
  `bot_status` unchanged.
- **Bot row never saved and the process restarted, or the echo arrives after
  the TTLs:** both caches are empty and the DB has no row, so the echo is
  treated as a **human agent**: stored as `sent_by='agent', is_bot=False`, and
  `bot_status → human_active`. The bot then stays silent until
  `maybe_return_to_bot` resumes it after `HUMAN_IDLE_MINUTES` (30) of no human
  message, measured from that fake agent row (`webhook.py:L497-L508`,
  `L530-L570`). This is the 2026-09-18 conversation 3766 failure shape.
- **Row saved by another system (the portal) first with `is_bot` false/null,
  so our insert gets 23505 and is silently dropped:**
  - Agent detection in *this* process is still fine while the caches hold
    (steps 1–2), so `bot_status` does not change.
  - But the stored transcript now says a human wrote the bot's words.
    `last_agent_message_at` (`message.py:L520-L546`) selects outbound
    `is_bot = False` rows, so **the bot's own reply counts as human activity**:
    - the 30-minute idle gate (`last_human_activity_at` →
      `maybe_return_to_bot`) can be extended or restarted by the bot's own
      messages whenever the chat is `human_active`;
    - the "are you there" reply during a stand-down is suppressed
      (`webhook.py:L394`);
    - for threads not `bot_active`, `may_engage`'s grace window treats it as an
      agent reply (`webhook.py:L454-L461`).
  - `format_history` renders it as `Agent:` instead of `You:`
    (`message.py:L603-L607`). Then `claire_has_spoken` is false, the prompt
    tells Claire to introduce herself again (`app/graph/prompts/system.py:L520-L532`),
    and the guards that compare with her last line (`near_duplicate`,
    `strip_repeated_opener`, `answering_our_question`) compare with the wrong
    line.
  - **Whether the portal actually inserts from_me echoes, and with which
    `is_bot` value, is UNVERIFIED** (OS repo).
- **Separate performance note:** `exists`, `was_sent_by_bot` and
  `quoted_message_origin` filter on `whapi_message_id` (`message.py:L154`,
  `L162`, `L176`). If the dropped unique index was the only index on that
  column, these become sequential scans on every outbound echo and quoted
  message. **UNVERIFIED** whether a non-unique index remains.

### Q1.5 Log text to search for on the server

```
docker compose logs chatbot | grep -E \
 'Failed to handle message|Debounced handler failed|Could not store agent message|repeats a reply we just sent|Agent detected on conversation|42P10|23505|duplicate key|no unique or exclusion constraint'
```

- `Failed to handle message` — inbound row (or auto-reply/echo row) insert
  failed; the client was not answered.
- `Debounced handler failed for` — a bot-reply insert (or anything else in the
  turn after the graph) failed; the reply WAS sent. The traceback names the
  PostgREST error code.
- `Could not store agent message on conversation` — agent row lost.
- `repeats a reply we just sent` — the body cache caught an echo whose id did
  not match; frequent occurrences suggest send-id and echo-id differ (the
  suffix question above).
- `Agent detected on conversation` with no matching staff message in the
  portal — a bot echo mistaken for an agent.
- A 23505 on our own inserts is **not logged at INFO**. To detect the
  "portal saved it first" case, query the DB instead: outbound rows with
  `is_bot` false whose body equals a bot reply sent seconds earlier on the same
  conversation, or bot replies with no `is_bot = true` row. (Read-only SQL; not
  run here.)

---

## Q2. Reuse map for the suggestion endpoint

"Safe" = no DB write, no Whapi call, no checkpoint write, and no in-memory side
effect beyond benign caches.

### a. History text

- `message.get_history(conversation_id, limit=None, exclude_ids=None) -> list[dict]`
  (`app/services/message.py:L549-L572`). Reads `wp_chat_messages` (direction,
  body, is_bot, sent_by, whapi_message_id, created_at), newest
  `HISTORY_LIMIT` rows (default 40, `app/config.py:L232`; the server `.env`
  value wins), returned oldest first, NRIC-redacted. **SAFE** (read only).
- `message.format_history(rows) -> str` (`message.py:L575-L608`). Pure.
  Labels: inbound → `Client:`, outbound `is_bot` true → `You:`, outbound
  `is_bot` false → `Agent:`; `sent_by='whatsapp_auto'` rows dropped.
  **SAFE**. Note Q1.4: mis-labelled rows change what the model sees.

### b. Contact identification / enrichment (read-only)

All of these only SELECT (no writes found in `app/services/contact.py`; the
`result.update(...)` calls at `L598-L645` are dict updates). In-memory side
effect: `contact._MISSING_COLUMNS` cache only.

- `contact.get_record_name(employer_id) -> str | None` (`contact.py:L371`)
- `contact.count_prior_hires(employer_id) -> int` (`L396`)
- `contact.get_placed_helper(employer_id) -> dict | None` (`L453`)
- `contact.get_placement_helpers(employer_id) -> list[dict]` (`L538`)
- `contact.get_candidate_record(candidate_id) -> dict | None` (`L516`)
- `contact.get_cases(employer_id, phone) -> list[dict]` (`L308`)
- `contact.get_case_summary(case_id) -> dict | None` (`L661`)
- `contact.identify(phone) -> dict` (`L573`) — read only itself, **but do not
  use `webhook.identify_contact`**, which writes the result onto the
  conversation row.
- `lead_service.find_by_phone(phone, kind=None)` (`app/services/lead.py:L245`) — read.
- `webhook.merge_known_helpers(helpers, limit=3)` (`app/api/webhook.py:L1031`) — pure.

The turn payload itself is assembled **inline** in `_process_locked`
(`webhook.py:L1129-L1265`), not in a reusable function, and it contains one
write: `conversation_service.update(..., langgraph_thread_id=...)` when the
conversation has no thread id (`webhook.py:L1136-L1139`). A suggestion endpoint
must copy the `asyncio.gather` block (`L1155-L1205`) and the `payload` dict
(`L1230-L1265`) without that write — or Sprint 1 extracts a read-only
`build_turn_payload()` and has `_process_locked` call it (a refactor of the
live path, so it needs the smoke suite).

### c. Reading LangGraph state without running the graph

- `graph.get_graph()` (`app/graph/graph.py:L413`) returns the compiled graph
  with the Postgres checkpointer (pool `min_size=1, max_size=10`,
  `L373-L385`).
- `await graph.aget_state({"configurable": {"thread_id": thread_id}})` — used
  read-only by `run_turn` itself (`graph.py:L543-L547`). `snapshot.values` is
  the last turn's full `ConversationState` (collected_info, service_type,
  intent, asked counts, briefed_services, plus the previous turn's reply and
  rag_matches); `snapshot.created_at` is the last turn's time. **SAFE**: a
  checkpoint read through `CbAsyncPostgresSaver` (with its one-retry on a dead
  connection), no write. Thread id comes from
  `wp_chat_conversations.langgraph_thread_id`; if it is null there is no
  state — do not mint one.
- To make the `state` the prompt builder wants: `{**snapshot.values,
  **_TURN_RESET, **payload}` mirrors `run_turn` (`graph.py:L538-L541`,
  `_TURN_RESET` at `L444`). `stale_intake_reset(values, created_at,
  human_since=...)` (`L487`) is pure if you want the same stale-question rule.
- **Never call `graph.ainvoke` / `run_turn` on the real thread id**: it writes
  the checkpoint and the nodes write leads, tickets and handovers.

### d. Ticket topic helpers (all read-only SELECTs on `cb_tickets`)

- `ticket.open_topics_for_conversation(conversation_id) -> dict[str, dict]` (`app/services/ticket.py:L3072`)
- `ticket.completed_topics_for_conversation(conversation_id)` (`L2989`)
- `ticket.recent_for_conversation(conversation_id, limit=3)` (`L2907`)
- `ticket.helpers_on_conversation(conversation_id, limit=10)` (`L3031`)
- `ticket.latest_closed_at(conversation_id)` (`L2927`)
- `ticket.picked_up_recently(conversation_id, hours)` (`ticket.py:~L2954`)

### e. RAG

- `rag.search(query, *, match_count=None, match_threshold=None, namespace=None,
  service_type=None, contact_type=None, nationality=None) -> list[dict]`
  (`app/services/rag.py:L403-L475`). Embeds via `embed_query` (`L145`,
  OpenRouter/OpenAI `AsyncOpenAI` client `L80-L81`), calls RPC
  `cb_match_knowledge_base_updated`, post-filters and reranks; returns `[]` on
  any failure. **SAFE**: the RPC is a read and is on the read-only allow-list
  (`app/readonly.py:L41`). Side effects: in-memory embedding cache and prefetch
  map (`rag.py:L108-L115`), vocabulary cache (`L327-L343`). Logs one INFO line
  `"KB search returned ..."`.
- `rag.format_context(matches) -> str` (`rag.py:L537`) and
  `rag.best_similarity(matches)` (`L477`) — pure.
- Filters from state live in the retriever node, not in `rag`:
  `_search_query(state)` (`app/graph/nodes/rag_retriever.py:L335`),
  `_retrieval_audience(state)` (`L517`), `_service_filter(state)` (`L572`),
  `_nationality(state)` (`L638`) — private but pure.
- Simplest faithful reuse: call the node `rag_retriever(state)`
  (`rag_retriever.py:L781`) on the built state. It only reads (KB search, and
  `get_case_summary` on a `case_enquiry`) and returns `rag_matches`,
  `rag_context`, `rag_best_score` and so on. **SAFE**, but UNVERIFIED that no
  branch further down the node writes (none found by grep; read the full node
  in Sprint 1).

### f. `build_system_prompt`

- Signature: `build_system_prompt(state=None, *, rag_context="",
  extra_instructions="") -> str` (`app/graph/prompts/system.py:L509-L610`).
  Pure (reads the `kb_rules` snapshot and the clock).
- Minimum state keys: `history_text`, `incoming_text`, `contact_type`. Strongly
  recommended: `record_name`, `recent_tickets`, `matched_cases`,
  `matched_case_id`, `matched_lead_number` (`_contact_block`, `L332-L405`),
  `case_summary` (`L584`), `service_type` (replacement cost block, `L478`),
  `collected_info` (`L595`). `customer_name` is deliberately never printed.
- `extra_instructions` is appended last as `--- This message ---`
  (`L607-L608`), which is where the nodes put their per-turn instruction. It is
  the right place for the "you are drafting 2–3 options for a human consultant,
  return JSON" wrapper. Caution: the system prompt's stage line (`L519-L558`)
  and rules are written in Claire's voice addressing the client; the wrapper
  must say the drafts are still written *as the message the consultant will
  send to the client*, and that the "Agent:" lines are the consultant's own.

### g. `llm.complete_json`

- `complete_json(system_prompt, user_prompt, *, temperature=0.0,
  max_tokens=800, default=None) -> dict` (`app/graph/llm.py:L160-L200`);
  `complete(...) -> str` (`L72-L99`).
- **The model cannot be overridden per call.** `get_llm` always uses
  `settings.openrouter_model` (`llm.py:L51`). A cheaper suggestion model needs
  an additive `model=` parameter threaded through `complete`/`get_llm` (default
  unchanged), or a separate small helper.
- **Token usage/cost is not returned.** `complete` returns `response.content`
  only (`L96-L99`); `response.usage_metadata` / `response_metadata` are
  discarded.
- Timeouts: per purpose from the caller's function name (`LLM_TIMEOUTS`,
  `L110-L117`; `timing.llm_step_name`, `app/timing.py:L175`); an unknown
  purpose gets 30 s, plus one retry. `complete_json` can make up to 3 calls on
  bad JSON (`L169-L200`).
- Log hazard: on unparseable JSON it logs the first 300 chars of the model
  output at WARNING (`llm.py:L208`) — that would put draft text (client
  content) in the server log.

### h. Guards

How the reply path uses them: node-level in `response_generator`
(`app/graph/nodes/response_generator.py:L473-L660`) — `is_degenerate`,
`leaks_internal_reasoning`, `quotes_hiring_package_cost`, `clamp_reply`,
`ungrounded_figures`, `guard_nationality_fee`, `looks_like_document`,
weak-retrieval handover, `strip_repeated_opener`, `near_duplicate` — then
post-graph in `run_turn` (`app/graph/graph.py:L554-L631`). The post-graph chain
is **inline** (and copied line for line in `app/api/admin.py:L224-L274`); there
is no single "apply all guards to a text" function.

All of the following are pure string functions in `app/graph/guards.py` (some
read the in-memory `kb_rules` snapshot), so all can be applied to an arbitrary
draft:

| Function | Line | Needs |
|---|---|---|
| `is_degenerate(text)` | L670 | text |
| `leaks_internal_reasoning(reply)` | L2138 | text |
| `speaks_of_us_as_a_third_party(reply)` | L2106 | text |
| `strip_meta_commentary(reply)` | L630 | text |
| `strip_handover_talk(reply)` | L2175 | text |
| `looks_like_document(text, allow_steps=False)` | L709 | text |
| `clamp_reply(text, max_sentences=2)` | L2045 | text |
| `ungrounded_figures(reply, *contexts) -> list[str]` | L484 | rag context, history, incoming text |
| `quotes_hiring_package_cost(reply)` | L166 | text; gate on `service_type in kb_rules.cost_withheld_services()` |
| `guard_nationality_fee(reply, service, nationality, *sources, matches=())` | L408 | service_type, nationality (`lead.nationality_for_fee` / `nationality_in_play`), rag text, matches |
| `strip_repeated_opener(reply, *previous)` / `near_duplicate(reply, previous)` | L1693 / L873 | `recent_bot_lines(history_text)` |
| `without_tag_before_possessive(reply)` | L957 | text |
| `without_stray_script(reply, *client_text)` | L1028 | `graph.stray_script_context(payload)` (`graph.py:L653`) |
| `without_assumed_first_helper(reply, *client_text)` | L1086 | same |
| `without_figureless_approximate(reply)` | L1154 | text |
| `state_fixed_fee_plainly(reply, records)` | L1635 | `graph.records_text(result)` (`graph.py:L644`) |
| `with_after_mom_approval(reply, nationality)` | L344 | direct hire only |
| `price_probe_reply(reply, history_text, probe)` | L1570 | history |
| `tidy_helper_names_in_reply(reply, names)` | L1254 | `graph.helper_names` (`graph.py:L634`) |
| `without_reintroduction(reply, history_text, message)` | L1373 | history |
| `without_unasked_conditional_fee(reply, *client_text)` | L1429 | home leave only |
| `without_case_numbers(reply, case_numbers)` / `case_numbers_held(*states)` | L2352 / L2373 | payload |
| `references_as_code(reply)` | L2331 | text |
| `without_names_addressed(reply, names)` | L2291 | names |
| `holding_reply(history_text)` | L558 | fallback text |

Not suitable for drafts: `with_caring_line` (adds text), `strip_repeated_gratitude`
style rewrites are fine but cosmetic. The node-level guards that *replace* a
reply with a holding line or hand over should, for drafts, **discard** the draft
instead.

### i. NRIC and language

- `utils.redact_nric(text, *, context="") -> str` (`app/utils.py:L21`). Pure;
  logs a WARNING with the context string (never the NRIC) when it redacts.
  Note `get_history` already redacts; the incoming text must be redacted by the
  caller as `_process_locked` does (`webhook.py:L1111`).
- `language.mainly_non_english(text)` (`app/graph/language.py:L90`),
  `language.reply_language(text) -> str` (`L103`),
  `language.client_writes_indonesian(incoming, history_text="")` (`L159`),
  `language.to_indonesian(reply)` (`L169`). All pure, no model call. Rule 12
  itself ("reply in the client's language") is a prompt rule inside
  `system.RULES`, so it comes for free with `build_system_prompt`.

### j. `app/readonly.py` and the admin preview

- `readonly.read_only()` (`app/readonly.py:L50-L57`) sets a ContextVar for the
  current task (inherited by tasks it creates). While active,
  `Database.execute` answers any non-GET/HEAD query with an empty result and
  records it (`app/db/supabase.py:L95-L103`), `Database.rpc` refuses every RPC
  except `cb_match_knowledge_base_updated` (`supabase.py:L197-L199`), and the
  Whapi client refuses to connect (per `readonly.py:L15-L16`; the client-side
  check itself is in `app/whapi/client.py`, not re-read line by line here).
  **Reusable as a belt-and-braces wrapper** for the suggestion endpoint: wrap
  the whole handler in it and fail (or alert) if `refused` is non-empty.
  Limits: it does not cover the LangGraph psycopg pool (checkpoint writes go
  around `Database`), nor in-memory caches.
- Preview protection (`app/api/admin.py`):
  - key in header `X-Admin-Preview-Key`, compared with `hmac.compare_digest`
    in a route dependency (`L95-L102`, `L151`);
  - body read and validated by hand **after** the key, so no-key is always 401
    and never 422 (`L116-L135`, `L156`); 32 KB cap (`L72`, `L117`);
  - in-process sliding one-minute rate limit `ADMIN_PREVIEW_PER_MINUTE`
    (default 10, `app/config.py:L366`) → 429 (`admin.py:L105-L113`);
  - one at a time via an `asyncio.Lock`, a second request is 429 not queued
    (`L75`, `L157-L159`);
  - logs lengths only, never text (`L161-L162`).
- Registration: `create_app()` includes the admin router only when
  `ADMIN_PREVIEW_SECRET` is set (`app/main.py:L106-L107`), so with no secret the
  path is 404 and absent from the schema. The new route should follow the same
  pattern with its **own** secret and router.

---

## Q3. Concurrency and safety

### Shared resources a suggestion call would compete for

- **The event loop / single process.** One uvicorn process, no workers
  (`run.py:L25`; `CLAUDE.md` §3). Everything is in-process asyncio.
- **The default thread pool.** Every Supabase REST call runs through
  `asyncio.to_thread` (`app/db/supabase.py:L65-L67`), sharing the default
  executor (`min(32, cpu+4)` threads) and supabase-py's single shared HTTP
  client (`supabase.py:L36-L46`, `L55-L62`). A suggestion builds ~12 reads
  (the same gather as a turn) — a burst of suggestions can queue the webhook's
  reads behind them.
- **The psycopg pool** (`max_size=10`, `app/graph/graph.py:L373-L385`) if
  `aget_state` is used — shared with every turn's checkpoint read/write.
- **OpenRouter.** `ChatOpenAI` instances are `lru_cache`d per parameter set
  (`app/graph/llm.py:L23`), so suggestions share connection pools and the
  provider rate limit with live turns. A 429 from OpenRouter hits the bot too.
- **Embeddings client** (`rag.py:L80-L81`) and its in-memory caches.
- Whapi client: not used by suggestions; must stay unused.

### Recommended cap and timeout

- At most **2 concurrent** suggestion requests (an `asyncio.Semaphore`); over
  the cap → 429, not queued (the preview's pattern).
- A per-minute limit (e.g. 20) in process.
- Whole-request timeout **20 s** (`asyncio.wait_for`), LLM call timeout ~15 s
  with no extra retry beyond the existing one, and a single attempt at JSON
  (do not let `complete_json` make three calls).
- Request body cap like the preview's 32 KB.

### Things a turn does that the endpoint must NOT trigger

- `run_turn` / `graph.ainvoke` on the real thread (checkpoint write; nodes
  open leads `_open_lead_early`, create tickets, log handovers, advance the
  round robin via `cb_get_next_agent`).
- `info_collector` (writes a lead), `ticket_creator`, `handover_executor`,
  `blocked_topic_responder` (notes on tickets) — call only `rag_retriever` and
  pure helpers.
- `webhook.identify_contact` and `conversation_service.update / get_or_create /
  touch_* / set_bot_status` (all write `wp_chat_conversations`).
- The `langgraph_thread_id` write in payload building (`webhook.py:L1136-L1139`).
- `message.send_bot_reply`, `mark_sent_by_bot`, `mark_body_sent_by_bot`,
  `remember_bot_reply` — marking a **draft** as "sent by bot" would make the
  consultant's later send of that text look like the bot's echo
  (`echoes_our_own_send`), and the bot would **not** stand down for a human.
- `typing_indicator.start/stop`, `debouncer.add/flush_now/drain`, the
  per-phone `_conversation_lock` (would block live turns), `_schedule_read_receipt`.
- `timing.turn()` (would emit a fake `TURN_TIMING` line).
- `graph.init_graph()` / building a new checkpointer pool.

One residual interaction to know about: if the consultant sends a suggestion
that is word-for-word the bot's own reply from the last 5 minutes on the same
number, `echoes_our_own_send` will treat it as the bot's echo and the bot will
not stand down (`message.py:L124-L127`, `webhook.py:L857`). Pre-existing, rare,
but a suggestion feature makes it likelier if drafts copy the bot's last line.

---

## Q4. Deployment facts

- Container `minghwee-chatbot`, service `chatbot`, port 8000 bound to
  **127.0.0.1 only** (`docker-compose.yml:L2-L8`), started by `python run.py`
  (`Dockerfile:L24`).
- nginx: no config in this repo. `CLAUDE.md` §9.34 says nginx should forward
  only `/webhook/` and `/health` (**UNVERIFIED**; the live config was never
  checked from here). kb-admin reaches `/admin/preview` via `BOT_PREVIEW_URL`
  (`kb-admin/.env.example:L27`, default `http://127.0.0.1:8000`); how the
  production Vercel kb-admin reaches it is **UNVERIFIED**. A new route will
  need either an explicit nginx `location` for its path (proxying to
  `127.0.0.1:8000`) or server-side access from the Smart Inbox's backend; the
  public hostname is `bot.minghwee.com` per the Whapi webhook listing in
  `CLAUDE.md` §9.46 (**UNVERIFIED** as the nginx `server_name`).
- Env vars: `env_file: .env` (`docker-compose.yml:L5-L6`), i.e. `.env` next to
  the compose file on the server. Settings are `lru_cache`d, so a new variable
  needs **`docker compose up -d`** (re-reads `env_file`), not `restart`
  (`CLAUDE.md` §7). `git pull` never changes `.env`.

---

## GO / NO-GO for Sprint 1

**GO, with two conditions.**

Reasons:

- No `on_conflict` anywhere, so the dropped unique index cannot cause 42P10 in
  this repo; 23505 is already caught.
- Every piece the endpoint needs exists and can be called read-only: history,
  contact reads, ticket topics, checkpoint read, `rag_retriever`,
  `build_system_prompt`, `complete_json`, and pure guards.
- A proven protection pattern exists (preview: own secret, router registered
  only when set, key before body, rate limit, one-at-a-time, `read_only()`).

Conditions:

1. **Confirm the message-row question on the server first** (Q1.5 log grep plus
   the read-only SQL). If bot replies are being stored by the portal as
   `is_bot=false` (or dropped as duplicates), fix that before Sprint 1 ships —
   suggestions would read a history with Claire's lines labelled `Agent:`.
2. Sprint 1 needs two small **additive** changes outside the new route: a
   `model=` override in `llm.complete`/`get_llm` (default unchanged), and
   either a read-only payload builder or a careful copy of the gather block.
   Both need `smoke_nodes.py` and `selfcheck_flows.py` green.

## Risks found

- **Silent 23505 on bot replies** (DEBUG only) — with a suffix-stripped dedup
  key, a portal-written echo row can win and our `is_bot=true` row is dropped
  with no log at INFO. Effects: Claire's lines become `Agent:` in history
  (re-introductions, guards off), and bot messages count as human activity for
  the 30-minute idle gate and the stand-down "are you there" reply.
- **Bot-reply insert failure after sending** skips `touch_outbound` and the
  per-turn patch; logged only as `Debounced handler failed for <phone>`.
- **Inbound insert failure drops the client's message** with no reply
  (`Failed to handle message`).
- **After a restart, an echo of the bot's message whose row is missing is read
  as a human agent** and silences the bot for 30 minutes.
- Possible missing index on `whapi_message_id` (UNVERIFIED) → slow echo
  lookups.
- `complete_json` logs up to 300 chars of model output at WARNING on bad
  JSON — client content in logs.
- No per-call model override and no token usage returned.
- Post-graph guard chain is duplicated inline in `run_turn` and the preview;
  a third copy for suggestions would drift.
- Shared thread pool, psycopg pool and OpenRouter quota with live turns.
- Marking a draft as bot-sent would stop agent detection for the
  consultant's send.
- `store_auto_reply` writes un-normalised `from_number`/`to_number`, unlike
  every other writer (cosmetic, pre-existing).

## Recommended reuse list

- `app.services.message.get_history` — last N messages, redacted.
- `app.services.message.format_history` — `Client:` / `You:` / `Agent:` transcript.
- `app.services.conversation.get_by_id` / `get_by_phone` — read the conversation row.
- `app.services.contact.get_record_name`, `count_prior_hires`, `get_placed_helper`, `get_placement_helpers`, `get_candidate_record`, `get_cases` — read-only enrichment.
- `app.services.ticket.open_topics_for_conversation`, `completed_topics_for_conversation`, `recent_for_conversation`, `helpers_on_conversation` — ticket context.
- `app.services.lead.find_by_phone` — open lead, read-only.
- `app.api.webhook.merge_known_helpers` — pure.
- `app.graph.graph.get_graph` + `aget_state`, `_TURN_RESET`, `stale_intake_reset` — state without running the graph.
- `app.graph.nodes.rag_retriever.rag_retriever` — retrieval exactly as a turn does it.
- `app.services.rag.format_context`, `best_similarity` — pure.
- `app.graph.prompts.system.build_system_prompt` — with the consultant-draft wrapper in `extra_instructions`.
- `app.graph.llm.complete_json` — plus an additive `model=` override.
- `app.graph.guards`: `is_degenerate`, `leaks_internal_reasoning`, `speaks_of_us_as_a_third_party`, `strip_meta_commentary`, `ungrounded_figures`, `quotes_hiring_package_cost`, `guard_nationality_fee`, `looks_like_document`, `without_stray_script`, `without_figureless_approximate`, `state_fixed_fee_plainly`, `without_reintroduction`, `without_case_numbers`, `references_as_code` — applied per draft; drop a draft instead of replacing it.
- `app.graph.graph.stray_script_context`, `records_text`, `helper_names` — inputs for those guards.
- `app.utils.redact_nric` — on the incoming text.
- `app.graph.language.reply_language` / `client_writes_indonesian` / `to_indonesian` — language handling.
- `app.readonly.read_only` — wrap the whole handler; treat any refusal as a bug.
- `app.api.admin.require_preview_key` / `_take_rate_slot` pattern — copy for a separate secret and router, registered in `create_app` only when that secret is set.
