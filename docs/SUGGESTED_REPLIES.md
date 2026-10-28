# Suggested replies (`POST /internal/suggest`)

Reply drafts for a human consultant in the Smart Inbox. Given a client's
message, the endpoint returns up to 4 suggestions, short drafts written in the
consultant's voice, from Claire's own prompt, knowledge-base retrieval and guards. A
consultant reads them and may send one; the endpoint itself sends nothing and
writes nothing.

Code: `app/suggest/`. Self-check: `scripts/selfcheck_suggest.py`. Eval:
`scripts/eval_suggest.py`. The only change to an existing file is one guarded
block in `app/main.py` that registers the router when `SUGGEST_SECRET` is set.
The Sprint 0 safety check that preceded this is `docs/SPRINT0_CHATBOT_REPORT.md`.

## Files

| File | What it does |
|---|---|
| `router.py` | The route, secret, tenant check, concurrency gate, timeout, error mapping; `run_suggest()` |
| `context.py` | Builds the input: conversation, messages up to the anchor, contact/ticket/case context, saved LangGraph state |
| `audience.py` | Group chats and internal numbers are skipped |
| `meaning.py` | Drops a draft that says the same as another in other words (embedding) |
| `retrieval.py` | Runs the bot's `rag_retriever` node; renders records with `[ref <id>]` |
| `prompt.py` | Claire's system prompt plus the consultant wrapper (`SUGGEST_PROMPT_VERSION`) |
| `llm.py` | Its own OpenRouter client: JSON mode, temperature 0.3, no retries, returns token usage |
| `validate.py` | Every check a draft must pass, then flags |
| `settings.py` | The four environment variables |

## Endpoint contract

`POST /internal/suggest`, server to server.

Header: `X-Suggest-Secret: <SUGGEST_SECRET>`. Body (at most 4,000 bytes):

```json
{"tenant_id": "<TENANT_ID>", "conversation_id": 1381, "anchor_message_id": 167426}
```

`anchor_message_id` is `wp_chat_messages.id` of the client's message to reply to.

### 200 response

```json
{
  "status": "ready",
  "reason": "",
  "anchor_message_id": 167426,
  "bot_status": "human_active",
  "suggestions": [
    {"label": "Answer", "text": "...", "kb_refs": ["6c143f15-..."],
     "confidence": "high", "flags": []}
  ],
  "kb_gap": false,
  "sensitive": false,
  "language": "English",
  "model": "openai/gpt-5.6-luna",
  "usage": {"prompt_tokens": 8594, "completion_tokens": 194},
  "latency_ms": 8188,
  "prompt_version": "suggest-v5"
}
```

| `status` | `reason` | Meaning |
|---|---|---|
| `ready` | `""` | 1 to 3 drafts in `suggestions` |
| `insufficient` | `no_valid_draft` | The model answered but every draft was dropped |
| `stale` | `not_newest_inbound` | A newer client message exists; draft for that one |
| `stale` | `already_answered` | Claire or staff replied after the anchor |
| `skipped` | `group` | A group chat (`is_group`, `group_jid`, or a `@g.us` number) |
| `skipped` | `internal` | One of our own numbers (`INTERNAL_NUMBERS`, `cb_internal_contacts`) |

`stale` and `skipped` make no retrieval and no model call (`usage` is 0).

Each suggestion:

- `label`: `Answer`, `Clarify`, `Next step`, `Hold` (we are checking and will confirm) or `Empathise` (acknowledges how they feel, then moves it forward).

How many: the model writes up to 5 candidates in one call, each a different
approach. Every candidate goes through the checks below; then near-duplicates
are dropped in two passes, keeping the high-confidence one, then the earlier
one: very similar wording (SequenceMatcher 0.75, or mostly the same content
words, Jaccard 0.6; `validate.py`), then the same meaning in other words
(embedding cosine 0.85, one batched request to the bot's embedding model that
bypasses its query cache; `meaning.py`; skipped if the request fails). The
threshold is measured: same-meaning pairs scored 0.85-0.95, different
approaches 0.30-0.83. Then up to 4 are returned: high confidence first, one per label before a
second draft of any label, and a low-confidence draft only when it brings a
label no other draft has. Nothing is padded: if 2 survive, 2 are returned.

Several messages in a row: the request answers every client message since our
last outbound message (staff or Claire), not only the newest. "Hi" / "What is
the transfer fee?" / "For a Filipino helper" is one turn: the knowledge-base
search runs on all three joined (`incoming_text`, read by the bot's
`rag_retriever`), and the prompt shows them numbered under "The client's
unanswered messages". A WhatsApp auto-reply, or an outbound row with no text (a
reaction, a delete notice), does not end the run.
- `kb_refs`: ids of the knowledge-base rows the draft relies on; every one was retrieved for this request.
- `confidence`: `high` or `low`. Any flag forces `low`.
- `flags`: a list, possibly empty:
  - `unverified_figures`: the draft has a number that no cited record and none of our own messages (Claire's or staff's) states. That covers a number only the client wrote, and a count nobody wrote.
  - `weak_support`: an `Answer` whose key claim words are mostly absent from the records it cites (a keyword check, threshold 50%).
  - `nationality_unconfirmed`: the draft cites a record labelled for one helper nationality (PH/ID/MM) while the conversation's nationality is unknown, ambiguous or another one. A draft that QUOTES such a fee is dropped instead, and if no Clarify survives a fixed one is added ("Which helper is this for, and what is her nationality?..."), carrying this flag.

  - `unverified_commitment`: a day or deadline ("tomorrow", "by Friday", "within 3 days") that nothing gives. A relative day counts only if the client's latest message says it (one in an earlier message may have passed); a deadline only if our messages or the cited records state it.

Dropped as well (Thomas's review, 2026-10-28): a phone number in the draft (a number the client has just given becomes "on that number"); telling the client to contact Ming Hwee; a nationality, language, religion, age or skill nobody in the conversation mentioned; a case status or completed action ("received", "approved", "in order") that our messages or the cited records do not state, since the client's own words do not count; a promise to look again right after our last message said we do not have it. A Clarify that guesses a nationality is replaced by "Which helper are you referring to?". An unclear message gets one Clarify (prompt rule).

The nationality in play (`app/suggest/nationality.py`) is: the client's message naming exactly one; else the last 6 transcript lines naming exactly one (several named = unknown); else the saved state; else the helper records, if they agree; else our own earlier messages (staff or Claire) naming exactly one. The fixed Clarify is not added when our own messages already identified the helper: a name tagged "(MDW)", a helper we hold on record, or exactly one nationality.

`kb_gap`: the model said our records do not answer it, or nothing was retrieved, or the best match is under `RAG_SOFT_FLOOR`. `sensitive`: the model's judgement (complaint, harm, dispute, legal, medical).

### Errors

| HTTP | Body | When |
|---|---|---|
| 404 | | `SUGGEST_SECRET` unset (the route does not exist) |
| 401 | | Missing or wrong `X-Suggest-Secret` (checked before the body is read) |
| 413 / 422 | | Body too large / invalid (only after a valid secret) |
| 403 | `{"error": "wrong_tenant"}` | `tenant_id` is not this bot's `TENANT_ID` |
| 404 | `{"error": "conversation_not_found"}` | |
| 429 | `{"error": "busy"}` | `SUGGEST_MAX_CONCURRENCY` requests already running (never queued) |
| 504 | `{"error": "timeout"}` | Over `SUGGEST_TIMEOUT_SECONDS` |
| 500 | `{"error": "llm_error"}` | Model call failed or returned no JSON |
| 500 | `{"error": "read_only_violation"}` | Something on the path tried to write or reach WhatsApp (nothing was written) |
| 500 | `{"error": "internal"}` | Anything else; never a traceback |

## Environment variables

Read from `os.environ` in `app/suggest/settings.py` (not `config.py`). In
Docker, `env_file` puts every `.env` line into the environment. Locally,
`.env` is NOT read into `os.environ`, so export them in the shell.

| Variable | Default | |
|---|---|---|
| `SUGGEST_SECRET` | unset | Unset: the route is not registered (404) |
| `SUGGEST_MODEL` | `openai/gpt-6-luna` | Chosen on the 2026-10-28 comparison; not the bot's model |
| `SUGGEST_MAX_CONCURRENCY` | 2 | Minimum 1 |
| `SUGGEST_TIMEOUT_SECONDS` | 25 | Whole request |

Also read, unchanged, from the bot's settings: `TENANT_ID`,
`OPENROUTER_API_KEY` / base URL, `HISTORY_LIMIT`, `RAG_SOFT_FLOOR`,
`INTERNAL_NUMBERS`, `LLM_REASONING`.

## Safety design

- **Writes nothing.** No Whapi send, typing indicator or debouncer; no
  `_SENT_BY_BOT`, body-echo or replay-guard cache; no `bot_status`, thread id
  or checkpoint change (the saved state is read with `aget_state` only); no
  ticket, lead, handover, contact, conversation or message write;
  `kb_rules.refresh()` is never called (the live snapshot is only read).
- **Read-only mode as a second lock.** The whole request runs inside
  `app/readonly.read_only()`: a non-GET database call or any RPC but the KB
  search is refused and listed, and a Whapi request raises. Any refusal makes
  the request a 500 with no drafts. `read_only()` is a ContextVar, so a live
  turn running at the same moment is unaffected (proved in the self-check).
- **Skips non-clients.** Group chats and internal numbers get `skipped`.
  Internal numbers are read the way `may_engage` reads them, but the table is
  queried directly so the live bot's `internal_contacts` cache is not touched.
- **Consultant voice.** The wrapper overrides Claire's identity rules. Drafts
  naming Claire or claiming to be an AI/bot/assistant are dropped (checked on
  the model's own text and again on the final text). Drafts that hand over to
  "a live agent", including a draft the bot's `strip_handover_talk` rewrote to
  Claire's handover line, are dropped: the consultant is the live agent.
- **Grounding.** Every money amount and day/week/month/year count must appear
  in the cited records or the conversation, else the draft is dropped. Every
  `kb_ref` must be a row retrieved for this request (`ref <id>` and
  `[ref <id>]` are accepted as the id). The bot's own figure, fee and
  nationality guards run too.
- **Yes/no overreach.** The wrapper forbids a yes or no unless the records
  state it explicitly (offer Clarify / Next step instead), and `weak_support`
  marks an Answer whose claims are not in its records.
- **Privacy.** NRIC/FIN numbers are redacted before the model and again in the
  drafts. The suggest module logs ids, counts and timings only. Note: the
  bot's own guards (e.g. `strip_handover_talk`) log the text they change at
  WARNING, as they do for Claire's replies.
- **Bounded.** Secret before body, tenant check, concurrency gate, timeout,
  4 KB body cap.

## Copied blocks

Each carries a header in the code: "Copied from <file> L<start>-L<end> at
commit c138f91. Keep in sync." If the source changes, update the copy.

| Copy in | Source | What |
|---|---|---|
| `context.py` `build()` | `app/api/webhook.py` L1137-L1267 | The turn payload (contact, tickets, cases, helpers). Left out: the `langgraph_thread_id` write, the timing hooks, `last_human_activity_at` (the last human message comes from the transcript instead) |
| `context.py` `build()` | `app/graph/graph.py` L538-L559 | Saved state + `_TURN_RESET` + payload + `stale_intake_reset`, in `run_turn`'s order |
| `validate.py` `_node_guards()` | `app/graph/nodes/response_generator.py` L194-L211, L473-L612 | The node-level guards, with "replace with a holding line" turned into "drop the draft" |
| `validate.py` `_post_guards()` | `app/graph/graph.py` L561-L630 | The rewriting guards `run_turn` applies to every reply |

## Running

Self-check (no network, no database; fakes everything and refuses all HTTP):

```powershell
$env:PYTHONIOENCODING="utf-8"; python scripts/selfcheck_suggest.py
```

Local endpoint (read-only against the configured database and OpenRouter).
Use `run.py`, not plain uvicorn: on Windows uvicorn's event loop cannot
connect the Postgres checkpointer, so the saved state would be missing.

```powershell
$env:PYTHONIOENCODING="utf-8"; $env:SUGGEST_SECRET="<local value>"; $env:HOST="127.0.0.1"; $env:PORT="8765"
python run.py
```

Eval (calls the real model; one call per conversation, about 9k prompt tokens):

```powershell
$env:PYTHONIOENCODING="utf-8"
python scripts/eval_suggest.py --limit 10 [--model <openrouter model>] [--conversations 1381 2409]
```

It finds recent staff replies to a client message, keeps employer and
helper/candidate conversations only (group, internal, supplier, partner,
recruiter, unrecognised and tester numbers are left out and counted), drafts
as of the client's message (later messages and the saved state are not used),
and writes a masked Markdown report plus JSON to
`C:\Users\DELL\OneDrive\Desktop\suggest_eval\` by default (`--out-dir`).
Contact, ticket and case context in the eval is today's, not as it was then.

## Known limits

- The intent classifier is not run: retrieval uses the intent and service from
  the last saved turn (none in the eval).
- An anchor answered by Claire or staff is `stale`. WhatsApp Business
  auto-replies are left out of the stale check and the history: the two known
  texts (`context.KNOWN_AUTO_REPLIES`, plus `WHATSAPP_AUTO_REPLY_TEXTS`) and
  any outbound text sent within 10 s of a client message that was also sent in
  another conversation. A new greeting text sent more slowly is still read as
  staff.
- An invalid `kb_ref` drops the whole draft, not just the ref.
- The bot's `ungrounded_figures` guard also grounds on our own earlier
  messages here (Claire's and staff's), so a fee staff already quoted may be
  repeated.
- The fixed nationality Clarify is in English whatever the client's language.
- Word-numbers ("two weeks") are not grounding-checked.
- `weak_support` is a keyword heuristic, not a truth check.

## Change log

Newest first.

- **2026-10-28** - Up to 4 suggestions: 5 candidates in one model call
  (labels `Hold` and `Empathise` added), near-duplicates dropped, then the best
  4 by confidence and label variety, never padded. Near-duplicates by wording
  and, by embedding, by meaning (4641's three "I'll check with the team"
  drafts). Multi-message turns: all the
  client's unanswered messages are searched and shown together, numbered, and
  an outbound row with no text no longer ends the run. `suggest-v5`, max
  tokens 1400. Self-check 127.
- **2026-10-28** - From Thomas's review: no invented status (739), no
  unconfirmed day or deadline (194, flag `unverified_commitment`), no detail
  nobody mentioned, no promise to look again after staff said no (4615), no
  nationality guessed in a Clarify (4645), no phone number or "contact Ming
  Hwee" (4553), one Clarify for an unclear message (4614). `suggest-v4`.
  Self-check 97. The review file is now `thomas_review.xlsx`.
- **2026-10-28** - Model default `openai/gpt-6-luna` (`SUGGEST_MODEL` still
  overrides), timeout 25 s. Every draft must respond to the client's latest
  message (4641 offered an unrelated GST draft; `suggest-v3`). A helper our own
  messages already named counts as identified, so no fixed "which helper?"
  Clarify (4641). Messages stored in the same second are ordered by id: 4615
  read as stale on some runs and ready on others. Self-check 88.
- **2026-10-27** - A draft the guards shorten by more than 40%, or leave
  ending mid-thought, is dropped ("cut short by the guards") instead of
  offered in its cut form (4633). Self-check 83.

- **2026-10-27** - From the 10-conversation eval: WhatsApp Business
  auto-replies ignored (4633 was stale); our earlier messages ground the
  figure guard (4641's $1,388 was dropped); a fee belonging to another or an
  unconfirmed nationality is dropped, with a Clarify and the
  `nationality_unconfirmed` flag (4645 quoted Indonesian charges); the eval
  transcript is in `created_at` order. Self-check 82.

- **2026-10-27** - `skipped` status (group, internal) with a `reason` field on
  every response; stale reasons became codes. Per-suggestion `flags`
  (`unverified_figures`, `weak_support`), any flag forcing confidence low.
  Wrapper: no yes/no without an explicit record; client numbers are not
  confirmed facts (`suggest-v2`). Eval limited to employer and helper
  conversations. Self-check 78.
- **2026-10-27** - Stage 5 local test fixes: refs cited as `ref <id>` were
  rejected (every cited draft dropped); a draft rewritten to Claire's handover
  line was kept. Added `as_of` mode (scripts only) and `eval_suggest.py`.
  Self-check 63. Commit `24e9927`.
- **2026-10-27** - First version: the endpoint, read-only, consultant wrapper,
  validation and guards. Self-check 56; existing suites unchanged (558 / 961 /
  37). Commit `b7bcf17`.
