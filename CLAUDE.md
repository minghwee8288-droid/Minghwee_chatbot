# CLAUDE.md — Ming Hwee Chatbot

**This file is the entry point. Read it before touching code, and update it after.**

---

## 0. Protocol (read first, every session)

1. **Start here, not in the code.** This file describes what the system does, why it is
   built the way it is, and which rules are enforced mechanically rather than by prompt.
   Grep the code to confirm details, but form your mental model here first.
2. **After any change that alters behaviour, update this file in the same commit.**
   Specifically: §4 (graph), §5 (invariants), §6 (data), §7 (config), §8 (persona),
   §9 (known issues), and always append to §11 (change log). A change that leaves this
   file wrong is an incomplete change. **§9 is the queue** — open work lives there, not
   in a second file (see the 2026-09-10 entry for why there is no longer one).
3. **Do not delete the incident notes.** Long comments in this codebase cite real
   conversation IDs and real failures. They are the reason the code is shaped the way it
   is. If you change that code, update the note — never strip it.
4. **Never guess a schema.** The database is shared with two other products. Confirm
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
reset-ui/            Next.js "clear a conversation" page (separate deliverable, own
                     Vercel project). Standalone: imports nothing from app/, calls no
                     chatbot endpoint, talks to Supabase directly with the service-role
                     key from its own serverless functions. See reset-ui/README.md.
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
| A small-ticket service explains itself before it collects | `info_collector.briefs_on_this_turn` | `renewal` and `insurance`, on the turn after the first question. NOT `passport_renewal` — it briefs at the END instead, and an opening overview there reintroduced the embassy. Asserted across a turn SEQUENCE, because the condition this replaced could never be true. |
| The overview turn searches for the SERVICE, not the client's answer | `rag_retriever.OVERVIEW_QUERY` | That turn's message is usually a name, so the query built from it retrieved nothing — `renewal` measured 0.000. Same trick `BRIEFING_QUERY` already used. |
| A renewal never asks for a case ID | `SERVICE_FIELDS["passport_renewal"]`, `["renewal"]` | `_case_id()` deliberately absent. Client instruction, 2026-09-04. |
| Nobody is asked whether they have hired with us before | `info_collector._known_fields` | Filled from `prior_hires` either way now — zero reads as "first time with us". |
| An existing client is not asked for a helper we placed | `contact.get_placed_helper` | Only when there is exactly ONE live placement naming a candidate. |
| A volunteered requirement is acknowledged, not silently filed | `info_collector._VOLUNTEERED_REQUIREMENT` | Adds a note to the collector instruction. Narrow on purpose — "can't"/"don't" excluded. |
| A service filter never starves an answerable question | `rag_retriever` (widening retry) | Below the soft floor, retries with no service filter. Nationality is kept. |
| A returning client is never asked if they have hired before | `info_collector._known_fields` | Filled from non-archived `placements`. Only a POSITIVE count is evidence — 0 also means "unknown number". |
| An answer to our own question cannot be dragged onto a parked topic | `intent_classifier` (live-collection rule) | Unless the client names that service or chases its status. |
| A job-seeker is never met with a holding line | `intent_classifier._JOBSEEKER_PATTERN` | "need a job", "provide work to us", "someone's home … work", "I am a maid" force `candidate_registration`. Skipped for a known employer, so "someone to work at **my** home" stays `new_hiring`. |
| A generic question mid-collection is searched with the service as its subject | `rag_retriever._search_query` | `intent=other` used to send the message bare. "what is the process" then scored **0.000 filtered and unfiltered**. |
| A direct hire collects something | `SERVICE_FIELDS["direct_hiring"]` | Was an empty list, so the ticket said only "wants us to process a helper they have already chosen". Ten fields now; asserted. |
| The notice-period question is only put about a helper who is still employed | `_STILL_EMPLOYED` gate | `excludes` first, so "free to take a new job" and "between jobs" do not match on the word they contain. |
| A passport-renewal answer never names a route we have not established | `info_collector._NATIONALITY_DEPENDENT` + `_known_nationality` | The paperwork differs by nationality and the retrieval filter is DROPPED when the nationality is unknown, so all three routes compete. |
| A cost question never starts a second intake | `graph.route_after_rag` + `_other_service_established` | `fee_enquiry`/`salary_enquiry` collect ONLY when nothing else is in hand. |
| A question's SHAPE is not its subject | `rag_retriever._SUBJECTLESS_INTENTS` | `process_question`, `document_question`, `general_question` search under the in-flight service, as `other` already did. |
| A care type is never inferred from a bare enquiry | `info_collector` (`_states_a_care_type` on the MESSAGE, not just the value) | "I want to hire a helper" fills nothing. A volunteered one still lands. |
| A live collection survives a turn that resolves to no service | `intent_classifier` (no-service rule) | A turn with no topic in it cannot be a new topic. Guarded on the topic not being parked. |
| The (MDW) tag is used once, not every time | `style.py` | First mention only. |
| An option list the question spells out is read in full | `info_collector._field_guidance` | Applies when the written question already names 3+ of its own options. |
| A process or documents question may answer in steps | `guards.asks_for_process` + `PROCESS_INSTRUCTION` | Both halves required: the detector AND retrieved records. Without records it stays on the two-sentence path. |
| A numbered list is not counted as double the sentences | `guards.clamp_reply` (`_LIST_MARKER`) | `1.` used to end a sentence, so a six-step answer scored twelve and half was cut. Slicing now, so line breaks survive a clamp. |
| A numbered list is a document everywhere except where it was asked for | `guards.looks_like_document(allow_steps=)` | Headings, bold and bullets stay banned on every path. |
| A direct-hire answer never commits to a route we have not established | `info_collector._LOCATION_DEPENDENT` + `_known_helper_location` | A helper already in Singapore skips the embassy and the flight (2-3 weeks); one overseas does not (4-6). Both routes are filed under `direct_hiring`, so the filter does not separate them. |
| A step both hiring flows share is filed once, not copied | `service_type = 'general'` + `load_service_notes._SHARED_WITH_DIRECT_HIRE` | The match function passes `service_type in (filter, 'general')`. Sourcing, matching and interviews stay on `new_hiring` — they are the only difference. |
| An answer that opens no gate has not answered the question | `info_collector._undecidable_gate_keys` | `Gate.state()` closes on an unrecognised value, so two opposing gates on one field both close and every gated field goes with them. Blanked and re-asked, bounded by `max_asks`. |
| A transfer client is never asked when they want it sorted | `SERVICE_FIELDS["transfer_employer"]` | `timeline` deliberately absent. Asking someone in a hurry produces "ASAP" every time. Agency instruction, 2026-09-07. |
| The take-on transfer questions ARE new_hiring's questions | `ticket._hiring_field` | Reused via `dataclasses.replace`, not copied, so a reworded question lands in both flows (§9.8). |
| An answer to our own question never switches the SERVICE | `intent_classifier` (answer rule) | The stickiness rules only rescue `other`/no-service/money. A different hard service simply wins, and a transfer drifted into `new_hiring` on "3 year experienced maid". |
| Whether they are a returning client is never asked, nor offered as an answer | `referral_source` options + `_known_fields` | "returning client" as an option was read into the question, asking a man whose placements we count every turn. |
| A returning client is welcomed back on every flow | `info_collector.returning_note` | Fired off `first_time_hire`, which only `new_hiring` defines, so transfer/renewal/passport got nothing. Now also fires on the opening turn. |
| An answer to our own question is never routed away from the collection that asked it | `guards.answering_our_question` + `route_after_rag` | The classifier's stickiness fixes `service_type` but not `intent`, and the money branch reads `intent`. |
| Every employer flow asks the client's name | `SERVICE_FIELDS[...]["full_name"]` | `transfer_employer` had none, so rule 1c had nothing to use and the lead carried only a phone number. |
| ...and that is now checked as a SET, not one flow at a time | `selfcheck_flows.py` | The row above was written for `transfer_employer` in 2026-09-08 and was simply false everywhere else: renewal, home leave, replacement and insurance never asked at all. Money enquiries are excluded — they are a question, not an intake. |
| A renewal, a home leave and a passport renewal ask the name rather than read it off WhatsApp | `ticket.NAME_FROM_RECORD_ONLY` | Adding the field alone changes nothing: `_with_push_name` fills it from the profile and the question is skipped before it is asked. |
| A home leave never quotes a route we have not established | `info_collector._ROUTE_BY_NATIONALITY` | The nationality changes the documents, the lead time **and** the price (PH original passport + itinerary, 4 weeks, $400; ID copies, 2 weeks, $250). |
| The home-leave caveat catches money and timing; the passport one deliberately does not | `_HOME_LEAVE_ROUTE_DEPENDENT` vs `_NATIONALITY_DEPENDENT` | A passport renewal is $450 either way, so suppressing that answer would help nobody. |
| Home leave asks which country she is from | `SERVICE_FIELDS["home_leave"]` | It asked her name and the travel dates only, so there was nothing to route on. The agency's own step 1 is "confirm nationality and intended travel dates". |
| A replacement question is answered from the flow, not from the contract | `load_service_notes` replacement rows | Clause 3.1 of the Client Service Agreement was the top match for seven different questions, five of them **above** the floor, so the widening retry never fired. |
| A `general` row is vetted against the cost guard too | `selfcheck_flows.py` | `general` is retrieved from inside `new_hiring` and `direct_hiring`, where `quotes_hiring_package_cost` runs on the reply. |
| A fee is quoted only where the agency gave us one | `guards.FEE_STATED_SERVICES` vs `COST_WITHHELD_SERVICES` | Exact opposites, asserted disjoint. Renewal/passport/home leave state it; hiring, direct hire, replacement and both transfers defer to a consultant. |
| A price question is a question about the SERVICE | `rag_retriever._SUBJECTLESS_INTENTS` (+`fee_enquiry`) | "How much does it cost" inside a renewal returned Form A's **hiring** schedule. `salary_enquiry` stays out — what a helper earns is about the helper. |
| A service that states its own fee is not widened | `_service_filter` (`FEE_STATED_SERVICES` first) | Widening returned the **work permit** fee ($695) inside a **passport** renewal ($450). A wrong price is worse than a vague one. |
| An undecidable gate only re-asks where the gates cover the whole answer | `info_collector._gates_are_exhaustive` | `requirement`'s gates are childcare/eldercare; "general housework" is a real option opening neither, and was blanked and re-asked three times. |
| A trailing "are there" is a statement | `info_collector._ASKS_SOMETHING` (anchored) | "6 bedroom and 6 bathrooms are there" read as a question and drew a promise to come back with an answer. |
| A bare yes/no does not close a question that is not yes/no | `_BARE_YES_NO` + `_YES_NO_QUESTION` | "Yes" closed "Any preference on her age or experience?". `<=` on the ask count, since both affected fields are `max_asks=1`. |
| A parked topic still answers a bare price question | `blocked_topic_responder._GENERAL_INFO` | "Ok what is cost" needed "what is **the** cost" to match, so a $450 answer was handed to a human. |
| The widening retry cannot undo the fee filter | `rag_retriever._PRICE_QUESTION` + `FEE_STATED_SERVICES` | Below the floor it dropped the filter and reached $695 inside a $450 service. Timing questions still widen. |
| An elongated acknowledgement is an acknowledgement | `closure._unstretched` | "okayyyyyy" drew the handover line a third time. Both collapsed readings are tried — one for "okayyyy", two for "goooood". |
| A service explains itself once, after the field it depends on | `ticket.BRIEFING_AFTER` + `briefing_due()` | `passport_renewal` → `nationality`. Never on the opening turn: before the nationality the only honest answer is "it depends", which is the 2026-09-04 defect. |
| The briefing is remembered across turns | `state.briefed_services` (`_merge_unique`) | Deliberately absent from `_TURN_RESET`, like `created_lead_id`. |
| A briefing never happens on a parked topic, nor over a client's question | `rag_retriever._briefing_turn` | Only the collector briefs; `blocked_topic_responder` never sets `briefed_services`, so without this every later turn would retrieve the briefing set instead of its own answer. |
| A cross-question may not be answered against the records | prompt `ANSWER_THEN_ASK_INSTRUCTION` | "Can she go to the embassy by herself" was answered "Yes" for an **Indonesian** helper, whom our runner collects from the home. |
| The WhatsApp push name is not the client's name | `ticket.NAME_FROM_RECORD_ONLY` + `contact.get_record_name` | `passport_renewal` asks unless our own records hold it. `customer_name` is a profile label; `record_name` is the file. |
| The briefing leads with the cost and the timing | `SERVICE_BRIEFING_NOTE` | "This is straightforward and we'll handle it for you" opens with nothing the client can act on. |
| The briefing is the CLOSING message, after every question | `info_collector` (completion branch) | Agency, 2026-09-08: "After all the questions it should reply with that process message." Heading, then cost, then timing, then the process, then the handover. |
| A discarded briefing is never recorded as given | `briefing_lost` | It was marked given even when a guard threw it away, so it was never retried — live, the client got `passport_expiry`'s question verbatim and no briefing, ever. |
| A price we hold for two nationalities is not the third's price | `ticket.FEE_BY_NATIONALITY` + `fee_is_known_for()` | **$450** was quoted for a **Myanmar** helper. It is in the records (as PH/ID's price), so `ungrounded_figures` passed it. |
| The passport briefing is timeline, then cost, then documents | `SERVICE_BRIEFING_NOTE` | Shirley, 2026-09-09: "nationality → timeline → requirements/documents". It never explains the embassy, the appointment or the runner — that is our processing. |
| Passport renewal does not ask where she is, nor about the work permit | `SERVICE_FIELDS["passport_renewal"]` | Both called out at the 2026-09-09 meeting: location is irrelevant, and WP renewal is "a completely separate process". |
| The intrusive questions say why they are asked | `info_collector._WHY_WE_ASK` | Pets, rest days, house rules. The plain ones (home type, household) stay plain — Thomas asked for exactly that split. |
| A field's own options are grounded figures | `_write(grounded_options=)` | `budget`'s options ARE salary bands, `_field_guidance` tells the model to offer two or three, and `ungrounded_figures` then binned every budget reply. |
| An auxiliary after a comma still makes a yes/no question | `info_collector._yes_no_question` | "Beyond the usual cleaning and cooking, **would** she need to…" — a bare "no" was being re-asked. |
| A returning client is told what we last spoke about | `info_collector.RETURNING_NOTE` | One enquiry, what it was about, and "follow-up or something new?" — never dates, counts, or their history read back. |
| A numbered list is never handed over without a sentence saying what it is | `SERVICE_BRIEFING_NOTE` | "The cost is approximately $450." straight into "1. Copy of your NRIC" — the agency asked how the client is meant to know that is the document list. |
| The briefing ends with the client's own steps, not ours | `SERVICE_BRIEFING_NOTE` (`THE STEPS ARE THEIRS`) + the `what happens next` KB row | Confirm, pay, send documents, sign forms, hear back. The embassy/runner rows stay out of it — that is the same processing the 2026-09-09 meeting excluded. |
| The briefing has ONE ending | `SERVICE_BRIEFING_NOTE` (`CLOSE IT ONCE`) | It asked "Would you like to go ahead?" **and** said it had already passed everything on. The ticket is raised on that same turn, so the question is the half that goes. |
| The bot reads a case and never writes one | `contact.get_cases` + `selfcheck_flows.py` | All eleven `case_*` tables. Asserted mechanically: no insert/update/delete anywhere in `app/`, and no case table so much as NAMED outside `contact.py`. |
| A case is reachable three ways, not one | `contact.get_cases` | `leads.converted_case_id`, `employer_service_requests.converted_case_id`, then `cases.placement_id`. `cases` has no `employer_id` column at all, and `placement_id` is NOT NULL. |
| A case is never excluded by its status | `contact.get_cases` | The old `status = 'active'` filter hid `completed`, `cancelled` and `on_hold` — and the client whose case is on hold is the likeliest of all of them to be chasing us. Statuses only ORDER the list now. |
| A case number is context, never a line to read out | `system._known_cases_block` | The same rule as `RETURNING_NOTE`: referring to what is under way is warmth, reading their file back at them is not. |
| A name we already hold is USED, not just filed | `info_collector.RECORD_NAME_NOTE` | Skipping the question is right; skipping the greeting too makes it look like the name was never handled. Fires on the FIRST turn the name is known, whichever way we learned it — off our records, or because they just typed it. NOT gated behind `returning_note`/`recognised_note`: those decide what the message opens WITH, this decides that it carries the name. |
| A numbered list is never sent without a sentence saying what it is | `templates.PROCESS_INSTRUCTION` + `PROCESS_ADDENDUM` | The same rule the passport briefing got on 2026-09-09, now on both answering paths, so it holds for every service and for a parked topic. Both also close on a sentence rather than on step 8. |
| A question never reads its own bracket options out | `household`, `helper_profile` (no `options`) | "1-2, 3-4, 5-6, or 7 or more" and "30-40, at least 2 years" were `_field_guidance` dropping the field's options in as examples. Removed, not reworded — a question with no options takes any answer. `languages` keeps its options, which ARE the answer. **Now checked as a SET across all seven services**, not on the two fields the agency happened to name — the same correction §9 forced on the client's-name row. |
| A broadcast is never a human agent | `message.is_auto_reply` (+ in-process count) + `webhook._undo_broadcast_standdowns` | The detector is retrospective, so the first copies of a NEW broadcast are indistinguishable from an agent. Two conversations in one run is now enough, and earlier stand-downs are reversed. |
| A negative auto-reply verdict is never cached | `_AUTO_REPLY_VERDICTS` | Caching the first "no" on a fresh broadcast pinned it, so every later copy short-circuited to "no" and silenced the bot estate-wide. |
| A mass announcement is caught on its FIRST copy | `message._BROADCAST_MARKERS` | "Dear Valued Customer" and its kin. `operating hours` is deliberately absent — an agent answering "what time do you open" says it. |
| A transfer's document checklist is filed where the EMPLOYER can see it | `service_type = 'general'` + `selfcheck_flows.py` | `transfer_employer` is not a `service_type` any row uses, so retrieval narrows to `general` (§9.15) — a checklist written for the employer and filed under `transfer` is in the one bucket the person it is for cannot read. |
| A `general` row does not displace the service-specific row beside it | `selfcheck_flows.py` (the forms-row wording) | The cost of the catch-all bucket. "What **documents** does Ming Hwee prepare for a transfer?" was top for `new_hiring`'s own question at 0.754 against 0.726. "forms" separates them; the controls are measured, not assumed. |
| A document list says WHOSE documents they are | `SERVICE_FIELDS["transfer_employer"]`'s two directions + the row text | Retrieval cannot know whether the client is taking a helper on or releasing one, so the row answers both. A releasing employer asked for the new employer's income proof has been asked for a document that is not theirs. |
| A row written for one side of the desk is labelled for that side | `contact_type` on `cb_knowledge_base_updated` + `selfcheck_flows.py` | `service_type='transfer'` survives `resolve_service` only for a CANDIDATE, so an employer-facing checklist filed `contact_type='all'` is served to the HELPER. `contact_type` narrows to this audience plus `all`; the default stays `all`. |
| A service the KB has never been labelled with searches under the label it HAS | `rag_retriever._RETRIEVAL_ALIASES` | `transfer_employer` is not a `service_type` any row uses, so the filter narrowed it to `general` forever. Retrieval only — the ticket, the lead, the field list and the **blocked-topic key** all still see `transfer_employer`, which is what keeps a new transfer off a parked hiring topic. |

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

**`cases` is the portal's, and every one of its interesting columns is
CHECK-constrained.** Derived by attempting inserts against the live constraints on
2026-09-09 (`scripts/seed_case_testdata.py` re-derives them on demand, so this can be
checked rather than trusted):

```
cases_status_check      active | completed | cancelled | on_hold
                        rejects open, closed, in_progress, new, pending, draft
cases_country_check     PH | ID | MM          rejects "Philippines", "SG", "PHL"
cases_case_type_check   First-time hire | Home leave | Transfer | Renewal |
                        Direct hire | NULL    (see section 9 — two services missing)
current_stage_key       free text, no constraint
```

`cases.placement_id` is **NOT NULL**, so every case hangs off a `placements` row and
there is no direct employer link — which is why `get_cases` reads the two
`converted_case_id` columns as well. The table was **empty on every check made against
it**; the whole layer is written for data that has not arrived yet, which is why the
seeder exists.

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
11. **The platform has no case type for a passport renewal or a replacement.**
    `cases_case_type_check` accepts `First-time hire`, `Home leave`, `Transfer`,
    `Renewal`, `Direct hire` and NULL — measured against the live constraint,
    2026-09-09. Passport renewal is the service the agency has been testing all
    week and the one with the most flow work in it; replacement collects eight
    fields. Neither can be opened as a typed case on the portal, so the bot can
    read a case for five of the seven services and never for those two. Not
    fixable here: the constraint is the portal's, and widening it is their call.
    `process_templates` says the same thing from the other side — 5 rows, only
    two case types (First-time hire for PH/ID/MM, Home leave for PH).
12. **`transfer_direction` is still extracted as the bare word "transfer", and the
    2026-09-08 mitigation only bounds the damage.** Reproduced 3 runs out of 3 on
    2026-09-09: "Hi I am looking for a transfer helper" -> "Vaidik Dubey" -> "I am
    looking to take on a transfer helper" and the stored value is `'transfer'`,
    which matches NEITHER `_TAKING_ON_TRANSFER` nor `_RELEASING_HELPER`. Both
    gates close, `applicable_fields` drops to 2, and once `max_asks` (2) is spent
    the collection completes on `full_name` + `transfer_direction` alone - a
    4-turn conversation and a 2-field ticket, which is CB-2026-0004 exactly.
    `_undecidable_gate_keys` does its job (it blanks and re-asks) but cannot make
    the extractor return a decidable value. The field HAS options -
    `('taking on a transfer helper', 'releasing my current helper')` - and the
    extractor is not constrained to them, which is the likely fix; the other
    candidate is resolving the direction from the client's own opening message,
    which almost always says it. It is intermittent: a separate full run collected
    22 fields over 21 turns. Not fixed here because it is gating logic and a rushed
    change to it can strand a live collection.
13. **Unbounded in-memory caches** with no TTL: `_LOCKS` (webhook),
    `_AUTO_REPLY_VERDICTS` (message), `_MISSING_COLUMNS` (contact), `_vocabularies` (rag).
14. **The KB states three different medical-insurance minimums, and the note that
    used to sit here was wrong.** Two rows say medical insurance must cover at least
    **S$60,000/yr** (one of them sourced from Ming Hwee's own Client Service Agreement)
    and one says **S$15,000/yr**; the direct-hire flow the agency sent on 2026-09-08 also
    says $15,000. The earlier entry claimed "MOM's actual figure is $15,000, so the second
    looks wrong" — that is the pre-October-2023 minimum. MOM raised the medical minimum to
    $60,000/yr, and $15,000 survives as the **co-payment threshold** (claims above it are
    co-paid 75/25), which is almost certainly where the confusion started. Personal
    accident insurance is separately $60,000/yr, so a row naming both $15,000 and $60,000
    is not necessarily self-contradictory — it depends which policy each refers to.
    **Not fixed in code, because it is a legal minimum and Ming Hwee has to confirm it.**
    The 2026-09-08 direct-hire rows deliberately carry **no insurance figure at all** and
    defer it to a consultant; `scripts/load_service_notes.py` asserts that mechanically.
    The older rows are untouched and the bot may still quote either.

---

15. **Stale transfer timelines survive in the raw bulk-import chunks.**
    The routing half of this entry is FIXED — see the 2026-09-10 change log:
    `transfer_employer` is aliased onto `transfer` for retrieval, and the three
    marketing rows that stated a competing figure were corrected, so ten
    timing phrasings now reach only the agency's own numbers. What remains is
    the un-Q&A'd residue: the big `document_chunk` rows from
    `minghwee FAQs and Overview.md` still carry **2-3 weeks**, **3-4 weeks**
    and **6-8 weeks** for a transfer, and one is filed under `home_leave`.
    They did not surface in the top 5 for any of ten timing phrasings, and
    `UPDATES` cannot target them — it keys on `question`, and these have none —
    so they are recorded rather than edited. If one ever does surface, it needs
    a chunk-level correction path, not another `UPDATES` entry.
    Separately and **not** a contradiction: `27-helper-rights-simple-english.md`
    tells a helper a transfer takes **2-4 weeks**. That measures from her asking
    us to transfer, which includes finding an employer; the agency's 1-2 weeks
    is measured from the interview. Different clock, different audience, and the
    row is correctly `contact_type='candidate'` so an employer never sees it.
    Worth having Ming Hwee confirm rather than assuming.

**Waiting on Ming Hwee, not on code.** None of these is a defect; each is a decision or
a figure only the agency can give, and the bot quotes or does the right thing the day it
arrives. Gathered here so they are asked in one conversation instead of rediscovered one
at a time.

- **Which consultants receive new leads**, and how the 6 `wp_chat_users` rows map to
  profiles (§9.1). 18 of the 20 `sales` profiles are `@growwstacks.com` development
  accounts. **And: Shirley is in the portal's *sales* department but her platform
  archetype is `admin`, which is the assault-escalation target — should she also take
  ordinary leads?**
- **The medical insurance minimum** (§9.14). Three figures in the knowledge base and the
  bot may quote any of them.
- **The Settling-In Programme window.** Their flow says seven days; MOM's requirement for
  a first-time helper is tighter, and a missed registration is a penalty on the employer,
  so the rows say "within the window MOM allows" until this is confirmed.
- **Myanmar passport renewal**: no fee, no timeline and no confirmation that the
  three-form route still stands as the 2026-09-07 document described it. Nothing is
  quoted for Myanmar because nothing was given.
- **A grounded salary band**, so the budget question can explain itself. Today the model
  supplies a range from its own knowledge and `ungrounded_figures` bins the whole reply,
  leaving the client the bare question (2026-09-09 C).
- **Payment.** Their stated end state is a payment link in the flow, then forms, then
  processing. **Nothing in this bot takes money**; the knowledge base only says who will
  raise it and when.
- **Whether "a live agent will reach out within 24 hours" is a commitment they want to
  make.** `strip_handover_talk` removes promised times deliberately, and a promise the
  agency cannot keep is worse than none — so this needs to be a carve-out they ask for,
  not a prompt tweak.
- **Widening `cases_case_type_check`** so a passport renewal or a replacement can be
  opened as a typed case at all (§9.11). The portal's constraint, and their call.
- **A helper-facing transfer document list.** The checklist they sent on 2026-09-10 is
  the employer's — their NRIC, their income proof, the forms the new employer signs — so
  it is filed `contact_type='employer'` and a HELPER asking what *she* needs gets a
  holding line. If she should be answered rather than handed to a human, that content has
  to come from them.
- **Whether "1 to 2 weeks from the interview" is the number they want quoted as the
  transfer timeline overall.** Their 2026-09-08 table gives it, and every employer-facing
  row now states it (2026-09-10) — but a client hears "how long does a transfer take" as
  end to end, and the figure excludes finding and shortlisting the helper. If they mean a
  longer number for the whole thing, it is one row to change.
- **Whether the helper-facing "2-4 weeks" is the same span or a different one** (§9.15).

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
python scripts/selfcheck_reset_ui.py   # reset-ui/ still clears what reset_conversation.py clears
python scripts/selfcheck_flows.py      # behavioural assertions; also runs IN the container:
                                       #   docker compose cp scripts/. chatbot:/app/scripts
                                       #   docker compose exec chatbot python /app/scripts/selfcheck_flows.py
                                       # Note the `/.` — see "the second copy" below.
                                       # Verifies BEHAVIOUR, not grep counts — see the note below.
python scripts/preflight.py            # go-live gate: KB, agents, branch, portal bridge
python scripts/check_retrieval.py      # retrieval calibration; tunes RAG_SOFT_FLOOR
python scripts/e2e_services.py         # walks all 7 services end to end against the
                                       #   real model and grades the transcripts.
                                       #   ~25 min, ~270 LLM calls. Writes nothing.
python scripts/watch_conversation.py --follow

python scripts/unsilence_conversation.py --list       # stood-down threads the bot should own
python scripts/unsilence_conversation.py +6591234567  # put the bot back, thread KEPT

# DESTRUCTIVE — test numbers only
python scripts/reset_conversation.py +6591234567
python scripts/purge_test_contact.py +6591234567         # show what holds the number
python scripts/purge_test_contact.py +6591234567 --yes   # ...and delete it
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
it has to be copied in first — and that copy lives only in the running container's
writable layer, so it is lost on the next recreate.

**And the second copy goes to the wrong place.** `docker cp` into a destination
directory that **already exists** copies the source *into* it, so
`docker compose cp scripts chatbot:/app/scripts` creates `/app/scripts` correctly on a
freshly recreated container and then writes `/app/scripts/scripts/` on every run after
that. It prints `✔ Copied` either way. Live on 2026-09-10: a self-check was run against
a copy several commits old, reported **ALL PASS**, and was believed — the only thing that
gave it away was that two assertions known to be in the file did not appear in the
output. **A self-check that silently runs an old copy of itself is worse than no
self-check**, because it is evidence pointing the wrong way. Use `scripts/.` (copy the
contents, overwrite in place) or `rm -rf /app/scripts` first. Proven by the fix rather
than by reading the docs: the same command produced a stale script with the directory
present and the current one without it.

**A reset does NOT make a converted number look new again.** `reset_conversation.py`
clears the chat — messages, tickets, handovers, the checkpoint, the identity on the
conversation row — and stops at the platform's own tables, which is correct for a real
client. But the moment the portal converts a lead it creates an `employers` row **and** a
synthetic `profiles` row (`employer+<hex>@no-email.local`), and `identify()` matches a
phone against `employers` FIRST. So a converted test number stays recognised through any
number of resets: live on 2026-09-09 the agency reset conversation 3766, deleted its
lead, and still got *"Hi tunaktun"*. `scripts/purge_test_contact.py` closes that gap —
it prints everything first, deletes nothing without `--yes`, **refuses a contact that has
any placement** (a placement means a real client, not a test number), and keys every
delete on a resolved row id rather than the phone. Note the order it has to use:
`wp_chat_conversations.matched_employer_id` is a foreign key to `employers(id)`, so the
conversation's identity is blanked BEFORE the employer row goes — the other way round
fails on the constraint, after the leads and tickets have already been deleted.

**Resetting test data.** `reset_conversation.py` deliberately never deletes from `leads`.
Because of §1B a reset number therefore keeps its lead and will not produce a new one —
this looks like a bug and is not. To test lead creation properly, use a number with no
lead, **or delete the lead row and that thread's checkpoint together**. Deleting the lead
alone is what broke conversation 36: the checkpoint kept pointing at the dead row and
every ticket insert failed the foreign key, silently, ten times in twenty minutes.

**Commit the files you changed, not `git add -A`.** This repo is edited from an IDE and
from scripts at the same time, so the working tree routinely holds changes the commit in
front of you did not author. On 2026-09-10 a `git add -A` swept an unrelated one-line
edit into a documentation commit and put a **false claim** on `main` — the 2026-09-01
entry rewritten to say the model switched to Sonnet 5 *"from gpt 5.6 luna"*, a model that
did not arrive until 2026-09-03 and is contradicted two entries above it. The change log
is the only record of why the code looks the way it does, so a wrong line in it is worse
than a wrong line in a comment. Run `git status` first and commit by name.

---

## 11. Change log

Append here, newest first. One entry per behavioural change.

- **2026-09-10** — **§9.15 closed: an employer transfer can finally read its own
  knowledge base, and the four competing timelines are one.** Asked why this was still
  broken the day after the checklist landed. The answer was four causes stacked, and
  measuring them is what showed that the objection holding the fix open no longer stood.
  (A) **Why it happened at all.** `service_type` on the KB is free-form `varchar(60)`
  with no CHECK, and the rows were labelled by a pipeline that never heard of
  `transfer_employer`. `_labelled_filter` handles an unknown service by narrowing to
  `general` rather than widening — correct, and bought with a real incident (widening once
  quoted the **$1,568** new-hire package as a passport renewal fee) — so this one service
  was *permanently* restricted to the generic bucket. And it cannot simply BE `transfer`,
  because the blocked-topic key is the service key: that mapping is what answered a
  brand-new request with "a live agent will connect with you shortly" indefinitely (live,
  2026-09-02, §8).
  (B) **Why nobody noticed.** The widening retry fires only BELOW 0.40. Measured as an
  employer saw it: timing **0.472**, steps **0.650**, cost **0.453** — every one *above*
  the floor, so `_answerable()` read True and nothing widened. A wrong answer that scores
  well is indistinguishable from a working one, which is the same silent-success signature
  as the dead briefing condition and the budget guard.
  (C) **The objection that kept it open does not survive counting.** §9.15 argued an alias
  was risky because `transfer` also holds helper-facing rows an employer should not be
  answered from. Counted: **18 rows — 12 `employer`, 5 `all`, 1 `candidate`** — and the one
  helper-facing row is already `contact_type='candidate'`, which `contact_type` filtering
  excludes from an employer's search. The audience column was already doing the separating
  the service key was doing badly. So `_RETRIEVAL_ALIASES = {"transfer_employer":
  "transfer"}`, applied in `_service_filter` and **nowhere else** — the ticket, the lead,
  the field list and the topic key all still see `transfer_employer`, asserted four ways.
  (D) **The alias alone would have shipped a coin flip.** With it, the agency's corrected
  row (**1 to 2 weeks**) and a row saying **2-4 weeks** arrived in the SAME retrieved set,
  0.587 against 0.558, and the model could quote either. The 2026-09-08 correction had gone
  through `UPDATES`, which keys on question + service_type — so it corrected the one row it
  named and left ten others alone. The KB stated **four** transfer timelines across eleven
  rows: 2-3 weeks in six, 2-4 in three, 3-4 in one, against the agency's 1-2 in one, almost
  all from a single bulk import. Three rows corrected, each with its reason. **The
  comparison rows now name both SPANS**, not just two numbers — "1 to 2 weeks from the
  interview" against "4 to 6 weeks from signing" — because they are not the same clock, and
  a bare pair invites exactly the reordering that produced "approximately weeks or months"
  on 2026-09-09. Their 6-8 weeks half was also corrected to the agency's own 4-6.
  (E) **Verified live, and the before/after is the whole point.** *"How long does a
  transfer take"* was **"Around 2 to 3 weeks"**; it is now **"Around 1 to 2 weeks from the
  interview to her starting work with the new employer. MOM approval usually takes 1 to 3
  working days."** *"What are the steps"* returned nothing usable and now returns the real
  eight-step list with a lead-in and a closing sentence. Cost still defers to a consultant,
  which is `COST_WITHHELD_SERVICES` doing its job. The helper side is unchanged and still
  never sees an employer row. **Ten timing phrasings swept: no stale figure reaches an
  employer.**
  (F) **What is left, and it is recorded rather than half-fixed.** The raw
  `document_chunk` rows still carry 2-3 / 3-4 / 6-8 weeks; `UPDATES` keys on `question`
  and they have none, so they need a chunk-level path if one ever surfaces — none did, in
  any of the ten probes. And the helper-rights "2-4 weeks" is probably a different span
  rather than a contradiction (§9.15).
  `selfcheck_flows.py` is **271 assertions**; `smoke_nodes.py` is 32 states. The ten new
  assertions were proved by injecting two faults — the alias removed, and an extra service
  aliased — and an existing assertion caught the second one independently.

- **2026-09-10** — **The transfer document checklist, and the bucket it had to go in
  for anyone to read it.** The agency sent the two halves — what they ask the client
  for, and what Ming Hwee prepares — and asked that a documents question inside a
  transfer be answered from them.
  (A) **Transfer was the only service with no document rows at all.** Every other one
  carries the same pair, "what I provide" and "what we prepare"; a transfer carried
  neither, so the most practical question about it had nothing to retrieve. Three rows
  now: the employer's four items (her Work Permit number and expiry, the current
  employer's release, the new employer's NRIC or IC, and their income proof with the
  foreign-employer variants), the seven forms we prepare, and a third for the employer
  who is **releasing** rather than taking on.
  (B) **Filed as `general` and NOT as `transfer`, which is the whole decision.** An
  employer asking about a transfer runs under `transfer_employer`, which is not a
  `service_type` any row uses, so `_labelled_filter` narrows to `general` and every
  `transfer` row is invisible to them (§9.15). This checklist is written from the
  employer's side of the desk — their NRIC, their income proof, the forms the new
  employer signs — so filing it under `transfer` would have put it in the one bucket the
  person it is for cannot read, and the change would have looked done and done nothing.
  Measured before the load, under `transfer_employer`: *"what documents do i need for the
  transfer"* **0.359**, below the floor. Worse, *"what documents does ming hwee prepare"*
  scored **0.570 — above** the floor, topped by the **PDPA privacy notice**, and *"what
  do i need to give you to release my helper"* **0.497**, topped by *"What if my helper
  goes missing?"*. Three of six probes were confidently answerable from the wrong record,
  which is worse than a holding line and is the 2026-09-08 direct-hire shape again.
  After: **0.679, 0.772, 0.788**, every one on the right row. `general` is also
  forward-compatible with the *other* fix §9.15 names — if `transfer_employer` is later
  aliased onto `transfer`, a `general` row is still reachable, so none of this has to
  move again.
  (C) **The cost of the general bucket showed up immediately, and the control caught
  it.** Worded *"What **documents** does Ming Hwee prepare for a transfer?"* the row was
  top for **new_hiring's own** question — 0.754 against its 0.726 — so a new-hiring
  client asking what we prepare would have been read a transfer form list. "documents" is
  the colliding word; **"forms"** separates them (0.708 vs 0.717, and 0.535 vs 0.551 on
  the other phrasing) and loses nothing on the transfer side, and it is what `replacement`
  and `passport_renewal` already call their own version of this row. **13 controls across
  all seven services, every one keeping its own document row on top; 14 transfer probes,
  every one on a new row above the floor.** The row was reworded in place and re-embedded
  rather than left, and the loader is a no-op on two consecutive runs.
  **The margin on one control is thin and is recorded rather than rounded up:** on
  *"what documents does ming hwee prepare (new hiring)"* the new_hiring row leads at
  **0.717 against this row's 0.708**, so the transfer row is still rank 2 in the set the
  model receives. That is the accepted state — the same shape as the 2026-09-08 note that
  the older requirements row outranks the five-stages row, and both are in the top 5 — but
  it is 0.009, so a future reworking of either row can flip it. The wording that widens
  the gap properly (*"What forms do I sign to transfer a helper to a new employer?"*,
  0.494 on that control) was measured and rejected: it drops the transfer side from 0.772
  to 0.578, which trades a real answer for a comfortable margin.
  (D) **The rows say whose documents they are**, because `transfer_employer` serves both
  directions and retrieval cannot know which. A releasing employer told to produce the
  *new* employer's income proof has been asked for a document that is not theirs to give.
  (E) **The first live run found a defect the retrieval numbers could not show, and it
  was one I had just introduced.** Every probe above measures whether the right ROW comes
  back; none of them reads the reply. Run against the real model, the employer paths were
  right — lead-in sentence, the four items, the seven forms, the release correctly
  attributed to the *current* employer — but the **helper** path was not. `resolve_service`
  leaves `service_type='transfer'` only for a CANDIDATE (an employer always becomes
  `transfer_employer`), so `transfer` is the HELPER's key, and with `contact_type='all'`
  she retrieved an employer's checklist. Live: *"Your NRIC or IC and proof of income..."*
  addressed to the helper, in the same reply as *"The new employer provides their own
  identification"* — the message contradicted itself about who was being spoken to. Fixed
  with the column that exists for it: the three rows are `contact_type='employer'`, which
  narrows a search to that audience plus `all`, so a candidate's search never returns
  them. The loader's `contact_type` was hardcoded `"all"` and is now per-row, defaulting to
  `"all"` so nothing else changed audience — asserted both ways. After: the employer paths
  are unchanged (0.669 / 0.617 / 0.713, all three rows in the set) and the helper gets
  *"I'll confirm the documents needed for the transfer and get back to you"* — a holding
  line, which is the honest outcome for content we do not have, and strictly better than a
  confident answer aimed at somebody else. **The gap that leaves is worth naming: there is
  no helper-facing transfer document list.** The agency sent the employer's; if a helper
  asking what SHE needs should get an answer rather than a human, that content has to come
  from them.
  (F) **Found while reading the existing rows, NOT fixed: the transfer timeline now has
  THREE contradicting figures.** §9.15 recorded two — the FAQ's *"2-3 weeks"* against the
  agency's corrected *"1 to 2 weeks"*. The release FAQ adds a third: *"The transfer
  process typically takes **2-4 weeks**"*. Left alone deliberately — which one is right is
  the agency's to say, and §9.15 already carries the question.
  Verified: **8 new assertions, 261 in total, and `smoke_nodes.py` 32 states, ALL PASS**.
  The assertions were proved by injecting four faults rather than by reading them — the
  colliding wording, the checklist filed under `transfer`, one item dropped from the list,
  and one row back to `contact_type='all'` — and each went red naming the problem. The
  replies above are quoted from a run against the real model, not from a stub.

- **2026-09-10** — **The same four fixes, checked against all seven services
  instead of the one the agency tested.** Their question on reading the round below:
  *"have you done these things for all services whichever services needed these
  things"*. No behavioural change came out of it; what came out of it was one measured
  defect (§9.15) and two deliberate non-changes, all three of which are worth having
  written down.
  (A) **The name and the list rule are structural, and that was verified rather than
  reasoned.** `full_name` is **question 1 on every one of the seven**; `RECORD_NAME_NOTE`
  fires off the turn the name becomes known and reads no service key; both
  `PROCESS_INSTRUCTION` and `PROCESS_ADDENDUM` carry the lead-in and the closing rule,
  and `asks_for_process` never looks at `service_type`. Run live against the model on the
  six flows yesterday's round never touched: work permit renewal, home leave and the
  employer transfer greet a client on file by name in the opening line (*"Hi Ratna,
  welcome back to Ming Hwee"*), and passport renewal, replacement and direct hire ask a
  new client for their name and greet them with it on the very next message (*"Thanks,
  Sarah — may I know your current helper's name?"*). Five more list paths — passport
  documents, home leave process, replacement documents, transfer process, and home leave
  **while parked with an agent** — every one with a lead-in sentence and a closing
  sentence.
  (B) **The bracket fix was only ever two fields, and three others were left alone on
  purpose.** Every option set on the seven services was dumped and read. Three still
  carry digits: `budget` (`$500-600`, `$600-700`…), `home_type` (`HDB 1-3 room`,
  `HDB 4-5 room`) and `start_timeline` (`within 2 weeks`, `within 1 month`). None is the
  shape the agency objected to — a salary band is a real thing to pick from, *"HDB 4-5
  room"* is what the flat is **called** rather than a bracket someone invented, and a
  timeframe is not a count. `budget`'s options are also load-bearing in the other
  direction: they are the grounding that stops `ungrounded_figures` binning the whole
  reply (2026-09-09 D), so removing them would silently reintroduce that defect. Flagged
  for the agency rather than changed. The §5 row is now asserted **as a set across all
  seven**, which is the correction §9 already forced once on the client's-name row — a
  rule written about the one flow that was reported is a rule that is false everywhere
  else until somebody checks.
  (C) **And the check found what checking is for: `transfer_employer` cannot see one of
  its own knowledge-base rows.** Recorded in full as **§9.15**. Short version: it is not
  a `service_type` any row uses, so the filter narrows to `general`; on *"how long does a
  transfer take"* the filtered set is five generic FAQ rows at **0.472**, which is
  **above** the 0.40 floor, so the widening retry never fires and the client is told
  *"Around 2 to 3 weeks"* from an old FAQ — against the agency's own corrected row, filed
  under `transfer`, which says **1 to 2 weeks**. Same shape as the 2026-09-08 direct-hire
  defect. Not fixed here: it is a live routing change, and the `transfer` bucket also
  holds helper-facing rows, so pointing an employer at it needs a decision rather than an
  alias.
  (D) **A false line was put on `main` and taken off again.** The 2026-09-01 entry was
  rewritten to read *"Model switched to `anthropic/claude-sonnet-5` (from gpt 5.6
  luna)"*, which the log disproves two entries further up — luna arrived on 2026-09-03,
  from `moonshotai/kimi-k3`. It reached `main` inside a documentation commit that staged
  with `git add -A`. Restored to `(from Kimi K2.6)`, and the rule is now in §10: commit
  by name, because this file is edited from an IDE and from scripts at once.
  (E) **The set-wide check is real, not a claim.** Saying in §5 that the rule is checked
  across all seven was false when it was written — the assertion named `household` and
  `helper_profile` on two flows. It now sweeps every field on all seven and fails on any
  numeric range in an option list **or** in a written question, with exactly three keys
  allowed through by name and a comment saying what each one earns. Verified the way this
  file requires rather than by reading it: a bracket question was injected onto
  `replacement.helper_tenure` — a flow neither of the old assertions looked at — and the
  self-check went red on it and exited non-zero.
  Deployed and verified in the container: **32 smoke states and every assertion, ALL
  PASS**, safety gate unchanged at 17 numbers. The count is now **253**; the entry below
  says 252 and the same count on that commit measures **251**, so that figure was one
  out, and it is corrected here rather than carried forward — these counts are a
  tripwire (2026-09-04 caught a field-count change by exactly this), and a tripwire
  nobody trusts is not one.
  (F) **`PENDING_CHANGES.md` is gone, and §9 is the queue.** The agency asked whether
  it was still needed. It was not: every item on it except one was already a §9 entry
  said twice, and the copy had **drifted into contradicting the original** — it still
  listed the passport-renewal and work-permit fees as the outstanding content gap when
  both landed on 2026-09-08 and are asserted in `selfcheck_flows.py`, and it still
  repeated the claim that *"MOM's own figure is $15,000"*, which §9.14 corrects at
  length (the $60,000 minimum has stood since October 2023). That is §9.8's duplication
  hazard applied to prose, and prose has no self-check to catch it. The one thing on it
  that lived nowhere else — whether Shirley should take ordinary leads, given she is in
  the portal's sales department but carries the `admin` archetype the assault escalation
  targets — is preserved in the new **"Waiting on Ming Hwee, not on code"** block at the
  end of §9, together with the agency decisions that were scattered across the change
  log. `reset-ui/README.md` and §0 both pointed at the deleted file and now point at
  §9.5 and at §9. Nothing is lost: the file is in git history.
  (G) **And the deploy check was verifying an old copy of itself.** `scripts/` is not in
  the image and has to be copied into the container, and `docker cp` into a directory
  that already exists copies the source *inside* it — so the first copy after a recreate
  lands correctly and every later one writes `/app/scripts/scripts/`, printing
  `✔ Copied` all the same. The run reported **ALL PASS** against a script several commits
  old. Nothing about the bot was affected — `app/` is in the image and had been rebuilt —
  but the check that exists to catch a bad deploy was itself the thing quietly out of
  date, which is the same silent-success signature as the dead briefing condition and the
  budget guard. Caught only because two assertions known to be in the file were missing
  from the output. §10 now carries the `scripts/.` form and the reason.

- **2026-09-10** — **Four things from the agency's new-hiring test, and the name one is
  the same defect from both ends.** Their words: *"the flow runs good but with some
  issues ... first thing they didnt ask user name at starting and then after taking the
  name it didnt greet user by name so we want these thing also in every flow."*
  (A) **The greeting was gated behind the two notes that fire most often.**
  `RECORD_NAME_NOTE` was added on 2026-09-09 with `not returning_note and not
  recognised_note`, reasoned as "one opener, never two". That silenced it for every
  RETURNING client — which is most of them — so an existing client got *"Hi, I'm Claire
  ... Welcome back — may I know how many children you have"* with the name on their file
  never used, and asked outright: *"You didn't ask me for my name. What is the reason
  behind it?"* The bot then had to explain *"We already have your name recorded as Project
  Manager Growwstacks"*, which is the worst possible way for them to find that out. It is
  **not** a competing opener: `returning_note` and `recognised_note` decide what the
  message opens WITH, and this decides that whatever it opens with carries their name.
  (B) **And the other end of it: a NEW client got no greeting at all.** *"Vaidik Dubey"*
  → *"How many people live in your household?"*. The note only ever considered
  `record_name`, so a name the client had just typed did not count. The test is now "the
  name became known on THIS turn", which is true on the opening turn for a client whose
  name is on file **and** on the turn after a new client types it — the agency's rule
  covering both halves in one sentence: *"if user is existing then it should greet by name
  at starting then move forward to our flow, if user is new then ask th user name then in
  next message greet the user with our followup question."*
  (C) **A bare name with a comma is not a greeting.** First attempt produced *"Vaidik
  Dubey, how many people live in your household?"* — correct by the letter and a form
  calling out a row by the sound of it. The note now requires the name inside a greeting
  or acknowledgement (*"Thanks, Vaidik."*), says the first name alone is the friendlier
  address when they gave a full one, and still forbids changing the SPELLING of what they
  wrote. Live after: *"Hi Project Manager Growwstacks, welcome back..."* and *"Thanks,
  Vaidik. How many people live in your household?"*
  (D) **The bracket questions are gone.** *"do not ask for no. like 1-2, 3-4, 5-6 which is
  looking wierd so only how many family members are there"*, and *"dont now write these
  numbers 30-40 just normaly ask age and experiece in good manner"*. Both were
  `_field_guidance` reading the field's own `options` into the question, so the fix is to
  remove the OPTIONS, not to reword the question — a question with no options takes any
  answer, including the household of seven that the brackets were added to accommodate on
  2026-09-08. That earlier assertion is inverted rather than deleted, and named, because it
  was right for its day. `languages` keeps its options: there they ARE the answer.
  (E) **Every numbered list now says what it is first, on every service.** *"the bot is
  directly listing the documents and process like 1 2 3 so on so it should firstly write
  the heading in same message."* Exactly the 2026-09-09 briefing rule, applied to the two
  general answering paths instead of one service — `PROCESS_INSTRUCTION` for an ordinary
  answer and `PROCESS_ADDENDUM` for a question asked while a topic sits with an agent.
  Both also **close on a sentence rather than on step 8**, which is the *"ending should be
  satisfied for user"* half; the parked one is explicitly told that closing line does not
  reopen the topic. Verified live on new hiring, work permit renewal, direct hire and the
  parked path: lead-in present, list intact, closing sentence present.
  `selfcheck_flows.py` is 252 assertions; `smoke_nodes.py` is 32 states.

- **2026-09-09** — **"approximately weeks or months", and three checks that were wrong
  about a bot that was right.** Follow-up to the end-to-end run above.
  (A) **The Myanmar timeline said nothing.** From the agency's screenshot: *"It can take
  approximately weeks or months for an appointment slot to become available."*
  Reproduced twice. The rows were not wrong — they read *"approximately a day in person,
  though the wait for a slot can run to weeks or months"* — but putting "approximately"
  next to a range with **no number in it** invited exactly that reordering, and it was
  the first line of the briefing a Myanmar client reads. Both timing rows are reworded so
  "approximately" can only attach to the part we can be approximate about, and the
  unpredictable half is stated as unpredictable. The word **"appointment" stays**: unlike
  the process rows the 2026-09-09 meeting stripped, here the wait for a slot IS the
  timeline, and removing it leaves a sentence that cannot explain itself. After:
  *"The waiting time for an appointment can be several weeks or sometimes months."*
  (B) **Two `UPDATES` entries were fighting over the same row.** Adding a new entry for
  a question that already had one meant the later of the two won on every run — so the
  fix silently did not take, **and the loader stopped being idempotent**: two consecutive
  runs each "corrected" 4 rows, flip-flopping the text between two wordings forever. That
  is §9.8 in a new place, and the documented rule that a script claiming idempotency must
  be RUN twice is what caught it. The existing entries are now edited in place, and two
  consecutive runs correct 0.
  (C) **Three of the end-to-end checks were wrong, and the bot was right in all three.**
  Worth recording because each was a plausible-looking check that would have sent someone
  chasing a defect that does not exist. *"Says why it is about to ask a lot"* tested the
  first message's LENGTH and failed a reply that says exactly what it should — 136
  characters against a threshold of 140. *"Disambiguates take-on vs release"* asserted
  `transfer_direction` was collected, but a client who opens with *"I am looking for a
  transfer helper"* has already answered it, the extractor takes it straight off that
  message, and putting the question anyway would be asking them something they just said;
  it now checks that the take-on branch ran and that the direction is never re-asked.
  And the small-ticket overview was graded on whether the MODEL used what it was given,
  which is a coin flip — it is now graded on the half that is deterministic, that the
  turn fires and has records, with the model's use of it reported rather than failed.
  **A check that fails half the time on correct code is noise, and noise is how a real
  failure gets ignored.**
  (D) **Confirmed, not assumed: the embassy step is gone from the client's steps.** The
  screenshot's *"4. Holabola attends the embassy appointment"* does not reproduce — three
  runs across Myanmar and Indonesian, no runner, no accompaniment, no embassy appointment
  anywhere in the steps the client is given. The Filipino route is clean too.
  (E) **Checked and NOT a defect: the helper's name being re-asked.** *"Holabola"* is not
  extracted on the first try and the flow asks once more. Measured across eight names,
  it is specific to that invented word — `Ana`, `Siti`, `Nyein Nyein`, `Tara rara` and
  `Liza Fernandez` all extract first time, as do *"her name is Holabola"* and *"Holabola
  is her name"*. Recorded so it is not chased as a systematic bug.

- **2026-09-09** — **All seven services walked end to end, and the small-ticket
  briefing had never once happened.** The agency asked for a full check that every
  service talks the way they want. `scripts/e2e_services.py` is that check: it runs each
  service from the opening message to the ticket through the real nodes and the real
  model, with a scripted client that answers whatever it is actually **asked** (by field
  key, so a reworded question still gets a sensible answer and a NEW field shows up as
  unscripted rather than quietly derailing the run), and grades the transcript against
  the rules this file records.
  (A) **The harness found its own bug first, which is the reason to trust the rest.**
  `collected_info`, `asked_field_counts` and `briefed_services` are LangGraph reducer
  fields — the graph merges them, it does not overwrite them. A plain `dict.update()`
  threw away everything collected on earlier turns, so the collector re-asked the
  helper's name and the run graded a conversation that cannot happen in production. The
  harness now applies the real reducers.
  (B) **`_SMALL_TICKET_SERVICES` has been dead since 2026-09-04.** The condition was
  `brief_on_turn = 1 if _is_first_contact(state) else 0` tested against
  `sum(asked.values()) == brief_on_turn` — and `_is_first_contact` is only true while the
  history is empty, which is only true while nothing has been asked. So `brief_on_turn`
  was 1 exactly when the sum was 0, and 0 exactly when the sum was 1 or more: **the two
  sides could never be equal.** Neither `renewal` nor `passport_renewal` has explained
  itself to a client since the day the "wait a turn" fix landed. Nothing caught it
  because a briefing that never happens looks exactly like one working quietly — the
  client gets a perfectly reasonable question either way. Same signature as the budget
  guard. Now a named predicate, `briefs_on_this_turn`, asserted across a whole turn
  sequence rather than as one call, which is the only shape that would have caught it.
  (C) **Fixing the condition was not enough: the turn had no records.** That turn's
  incoming message is the answer to the first question — a NAME, usually — so the query
  built from it matched nothing at all: `renewal` measured **0.000**. The note is
  strictly grounded, so it correctly stayed silent. `OVERVIEW_QUERY` searches for the
  SERVICE instead, exactly as `BRIEFING_QUERY` does for the closing briefing, and
  deliberately avoids the word "process" for the same reason. Measured after: 0.000 →
  **0.536**.
  (D) **And that still was not enough: a general instruction beats a specific one.**
  `COLLECTOR_INSTRUCTION` says "ask for that one detail and nothing else", which is a
  flat contradiction of "tell them what the job involves first", and it won every time —
  the 2026-09-04 introduction defect exactly. The note now names the rule it is
  overriding, the way `COLLECTOR_INTRO_NOTE` does. **Partly fixed, and it is worth being
  honest about the number: it produces the overview on roughly two runs in four.** When
  it stays quiet the client simply gets the plain question, which is what they got every
  time before, so the failure mode is unchanged and strictly better than never.
  (E) **`passport_renewal` was REMOVED from the opening overview, and that was a
  regression caught the moment (B) started working.** Its first attempt read *"We handle
  the passport renewal from the embassy appointment through to the renewed passport being
  returned to you"* — the embassy and the appointment, which the 2026-09-09 meeting
  removed from this flow by name, arriving before the client had even given the helper's
  name. The overview query retrieves the process rows and those rows describe OUR
  processing. It also had nothing to add: `BRIEFING_AFTER` already gives that flow a full
  closing briefing. So no service briefs at both ends, and that is asserted over
  `BRIEFING_AFTER` as a set rather than by naming passport renewal.
  **Also found, NOT fixed, and needing a decision: `insurance` is unreachable.** It is a
  defined service — 4 fields, small-ticket, its own lead type — but the classifier does
  not produce it: *"i need insurance for my helper"* lands on `other` with no service,
  and *"renew my helper insurance"* lands on **`renewal`**. The §5 row claiming `insuran`
  is matched before `renew` is about the alias table, which only ever runs against the
  model's returned intent STRING, so it never gets the chance. Insurance is not one of
  the agency's seven services, which is why this is recorded rather than fixed.

- **2026-09-09** — **"it still didn't ask name" — it knew the name and would not say
  it.** The agency tested a passport renewal and got *"Hi, I'm Claire, Ming Hwee's AI
  assistant. May I know your helper's name?"* — straight past them to the helper, exactly
  as on 2026-09-08, and reported in the same words.
  (A) **Nothing was broken about the skip.** Read from the live row rather than assumed:
  conversation 3766, number 917970027379, matched to employer **"tunaktun"** —
  `get_record_name` returns that, `_known_fields` fills `full_name` from it, and the
  question is correctly not asked. That IS the rule the agency gave on 2026-09-08: *"ask
  for the name first if the name is not in the database. If the name is in the database,
  greet them before moving forward."* The first half worked and the second half never
  happened.
  (B) **Why the greeting needed its own note.** `full_name` reaches the prompt inside
  *"Already confirmed by the client (do not ask again)"* — an instruction NOT to ask.
  Nothing there says to USE it, and prompt rule 1c loses to `COLLECTOR_INSTRUCTION`'s
  "ask for that one detail and nothing else" — the same mechanism that was dropping the
  AI introduction on 2026-09-04, fixed the same way: in the instruction that actually
  wins on a collector turn.
  (C) **The two existing notes could not cover it.** `recognised_note` requires a
  `placed_helper` and `returning_note` requires a positive `prior_hires`. This client is
  an employer on file with **no placement**, so both were silent — and that is the
  commonest shape there is, because the portal creates an `employers` row the moment a
  lead is converted, long before anyone is placed. `RECORD_NAME_NOTE` is gated behind
  both, so there is one opener and never two.
  Verified live across all six shapes: a new number still gets *"May I know your name?"*
  on both renewals; a name on file is greeted (*"Hi tunaktun, I'm Claire…"*,
  *"Hi Ratna Choukade…"*, *"Hi Manish M…"*); a returning client still gets the single
  *"Hi Ratna, welcome back"*; and the WhatsApp push name leaked into none of them.
  **Also found while reading the row, and NOT a bot bug:** `reset_conversation.py` had
  been run and the lead deleted, yet the number was still recognised — because the
  **portal** had converted that lead at 12:16 and created an `employers` row *and* a
  synthetic `profiles` row (`employer+…@no-email.local`). Neither is chatbot data and
  neither reset touches them, by design. A reset therefore does NOT make a converted
  number look new again; the master records have to go too, and that is a portal-side
  decision, not one this repo should take on its own.

- **2026-09-09** — **Case IDs: the bot now knows which cases a client has, resolved
  silently and read-only.** The agency's brief: resolve the case in the backend rather
  than asking for it, hold it for the conversation, make it available across all seven
  services, and change nothing that currently works.
  (A) **Only ONE of the eleven `case_*` tables was ever used, and only barely.** An audit
  of the screenshot they sent found `cases` referenced twice — both SELECTs in
  `contact.py` — and the other ten (`case_stages`, `case_tasks`, `case_requirements`,
  `case_candidate_suggestions`, the four task tables, the two salary-schedule tables) not
  referenced anywhere at all. What the resolved id produced was **one line** in the
  prompt: *"They have an active case with us."* No number, no stage, no helper. The
  plumbing was half-built rather than missing.
  (B) **`cases` has no `employer_id` column**, and `placement_id` is NOT NULL — so the
  only structural route is `employers → placements → cases`, which is what
  `find_active_case` walked. But the agency's own lifecycle is *lead → employer → case*,
  and that route does not mention a placement. **`leads.converted_case_id` exists** (with
  `converted_employer_id` and `converted_at`), and it is exactly their lifecycle; so does
  `employer_service_requests.converted_case_id`, whose own column comment describes the
  returning-employer path — "actioned by creating a CASE directly, rather than first
  spinning it into a lead". The bot already creates and owns the `leads` row. `get_cases`
  reads all three and dedupes, so a case created by either conversion is visible without
  waiting for a placement row to appear.
  (C) **The `status = 'active'` filter was a guess that had never run.** `cases` has been
  empty on every check, so it had never once been evaluated against a real row. Probed
  against the live CHECK constraint, the column takes **active | completed | cancelled |
  on_hold** and rejects open, closed, in_progress, new, pending and draft — so 'active'
  was real, but filtering on it hid three quarters of the vocabulary, including the
  `on_hold` client who is the likeliest of the four to be chasing us. Status now only
  ORDERS the list. `cases.country` turned out to be **PH | ID | MM** and rejected
  "Philippines" outright, which is how the first seeding run failed.
  (D) **Read-only is enforced, not intended.** `selfcheck_flows.py` asserts there is no
  insert, update, delete or upsert against any of the eleven tables anywhere in `app/`,
  and that none of them is so much as **named** outside `contact.py`. The first version of
  that assertion failed for the right reason — a `case_[a-z_]+` pattern also matches
  `case_enquiry`, `case_id` and `case_summary`, which are an intent, a field key and a
  state key — so the check names the eleven tables explicitly.
  (E) **No user-facing change, which is the constraint that shaped the prompt block.**
  The case detail rides on every turn, for every service, via `_contact_block`, and is
  worded to be USED and not recited: know what is already under way, do not ask about a
  service the office is plainly handling, answer if they ask — and never read a case
  number, stage or status out unprompted. That is `RETURNING_NOTE`'s rule applied to the
  most file-like thing we hold. Verified live: the case context is in the system prompt
  and neither an ordinary answer nor an intake turn leaked a number, a stage or a status.
  The old single line is still emitted when an id resolved but the row did not, so nothing
  that used to be in the prompt can go missing.
  (F) **The ticket carries the case number.** `cb_tickets` has no `case_id` column, so it
  goes in `captured_info` (jsonb, no migration) with a `_DETAIL_LABELS` entry, and the
  agent picking the ticket up sees the reference the client will quote at them. The ban
  has always been on ASKING a client for a case ID — `_case_id()` is still uncalled by
  every flow, still asserted — not on using the one we already hold.
  (G) **Deliberately NOT done: the case does not fill any field.** A case names its
  placement, so it could fill the helper's name and skip a question — but that changes the
  shape of a conversation, which the brief ruled out. `get_placed_helper` stays the only
  thing that fills a helper from records.
  (H) **`scripts/seed_case_testdata.py`** creates a throwaway employer with three cases,
  one reachable down each path, runs the real lookup, and deletes everything. The two
  conversion-path cases are given placements belonging to a **decoy** employer, so the
  structural path cannot reach them — which is what proves the two `converted_case_id`
  columns are doing the work rather than coincidence. Run twice, both runs identical and
  the database back to `cases`=0 with every other count unchanged.
  **Found and NOT fixable here (section 9.11): there is no case type for a passport
  renewal or a replacement.** Two of the seven services cannot be opened as a typed case
  at all. That is the portal's constraint to widen.
  `selfcheck_flows.py` is 227 assertions; `smoke_nodes.py` is 32 states.

- **2026-09-09** — **`reset-ui/` cut down to one button, and two claims in the
  2026-09-08 entry below are now WRONG — read this instead.** The client used the page,
  then asked for most of it removed: *"we don't want the check this right person part and
  3 lead to clear part, we just want clear chat button and don't want tick check for lead
  clearance — we directly clear the lead from particular number"*, and then *"make that
  button clear chat, by clicking that button chat and lead should be cleared, nothing to
  lookup, directly clear"*.
  (A) **Leads are no longer ticked. Every lead on the number goes automatically.** This
  supersedes (A) of the entry below, which described the protection as "only a lead
  explicitly ticked by id, only after its number, name, status and age have been shown".
  The client declined that after using it. What remains is the protection that is not a
  matter of taste: a lead something else references is **refused with its reason and left
  in place** while the conversation still clears (the three `ON DELETE NO ACTION` foreign
  keys are unchanged and still checked twice), and every lead `DELETE` is keyed on **one
  resolved lead id, never on the phone** — which is what stops the loose last-four-digits
  `ilike` fallback in `find_by_phone` sweeping up a stranger's lead off the same four
  digits. `selfcheck_reset_ui.py` asserts that keying mechanically.
  (B) **The API now accepts a phone number and nothing else**, which supersedes (E)
  below. The conversation and every lead are resolved server-side, so no request can name
  a row belonging to somebody else — strictly narrower than the old shape, which took a
  conversation id and a list of lead ids from the browser. The stale-tab guard went with
  it and is no longer needed: there is no id from the page to disagree with.
  (C) **Pressing Clear chat on an already-cleared number says so**, rather than walking
  the client through a confirmation reading "0 messages, 0 tickets" and then reporting
  success — which reads as though the first clear had not worked. The conversation row
  **survives** a reset (it is updated, not deleted), so a second press finds a real
  conversation with nothing in it; that is the case this distinguishes, and it is
  deliberately worded differently from "Nothing found", which is a number with no
  conversation at all.
  (D) **The per-step result list is gone** — a clean run reports one line. A step that
  did NOT complete is still named, because "Cleared" printed over a lead the database
  refused would simply be untrue, and two of the four real employer leads are in exactly
  that state.
  (E) **The confirmation dialog was kept, against the direction of every other change
  here.** Every delete is irreversible, there is no dry run, and the only thing
  identifying the target is a phone number typed by hand — so the dialog naming the
  contact is what catches a mistyped digit. The lookup still runs; it is invisible, and
  its only job is to fill that dialog in.
  (F) **The password is now built into `lib/env.ts`** (`DEFAULT_PASSWORD`) at the client's
  request, so the page works with no configuration. **The repository is public**, so that
  value is readable on GitHub: `RESET_UI_PASSWORD` in the Vercel project overrides it and
  should be set before this is pointed at live client numbers. Deployed as its own Vercel
  project with **Root Directory `reset-ui`** — the repo root is a Python service and
  building it there fails.
  Verified against the live database on seeded contacts covering all three shapes: a
  candidate lead, a deletable employer lead, and one blocked by a `lead_activities` row.

- **2026-09-09** — **"Why did you remove that?" — it was never there.** The agency tested
  a **work permit renewal** and got *"May I know your helper's name?"* as the opening
  question: *"the chatbot is asking directly name of helper, not saying that before, may I
  know your name."* Nothing had been removed. `renewal` was two fields, `helper_name` and
  `permit_expiry`, and had never held the client's own name. Only `passport_renewal` asks
  it, because that was added on 2026-09-08 when they raised it against that one flow.
  (A) **The invariant in §5 was false in four places.** *"Every employer flow asks the
  client's name"* has been in this file since 2026-09-08 and was written about
  `transfer_employer` alone — nothing checked the others. `renewal`, `home_leave`,
  `replacement` and `insurance` all opened without it, so rule 1c had nothing to greet
  anyone with and each lead reached sales carrying a phone number and a **helper's** name.
  It is now asserted over `lead.EMPLOYER_LEAD_SERVICES` as a set, so a new flow cannot be
  added without one.
  (B) **Adding the field alone would have changed nothing, and that is the whole trap.**
  `_with_push_name` fills `full_name` from the WhatsApp profile whenever it looks like a
  person's name, so the question is skipped before it is ever asked — which is exactly the
  defect the agency reported against passport renewal on 2026-09-08, reported the same way
  both times. `renewal` and `home_leave` join `NAME_FROM_RECORD_ONLY`: asked when our
  records do not hold it, greeted when they do. Both put the client's name on official
  paperwork, which is the same reason passport renewal is in there.
  (C) **`replacement` and `insurance` got the field but NOT that rule.** They were the same
  gap and are fixed with it — but nobody has objected to the push name on those two, and
  filling it from the profile is itself the 2026-09-01 fix. There the question is only a
  fallback for when there is no usable push name, so the lead always carries a name.
  (D) **`fee_enquiry` and `salary_enquiry` are deliberately left alone**, and the assertion
  excludes them by name. They are a money QUESTION, not an intake — two fields, and
  `route_after_rag` only lets them collect when nothing else is in hand. Asking a name
  there turns a price question into a form, which is the 2026-09-07 defect that produced
  *"But I come here for passport renewal not for care"*.
  Verified live: a new number gets *"I'm Claire, Ming Hwee's AI assistant. May I know your
  name?"*, a client on file gets *"Hi Vaidik, welcome back — may I know your helper's
  name?"*, and home leave behaves the same. `renewal` is 3 fields, `home_leave` 4,
  `insurance` 4, `replacement` 8.

- **2026-09-09** — **The first briefing to reach a real client, and three things wrong
  with it.** The agency's own reading of the transcript, in their words.
  (A) **A numbered list arrived with nothing said in front of it.** The reply went from
  *"The cost is approximately $450."* straight into *"1. Copy of your NRIC"* — no sentence
  naming what the list was. Their question is the whole argument: *"it didn't acknowledge
  that these are the documents, so how will the user know these are the documents?"* Every
  list now gets a lead-in sentence, written by the model rather than fixed, because a
  fixed one repeated twice in the same message is the formula `strip_repeated_opener`
  exists to stop.
  (B) **The process is back — but only the client's half of it.** *"After the document,
  tell the user, this is the further process you have to follow."* This is NOT the process
  removed at the 2026-09-09 meeting: that was the embassy appointment and the runner, our
  own processing, and it stays out. What they are asking for is the other half — confirm,
  pay, send the documents, sign the forms, hear back — and **nothing in the knowledge base
  said it.** Every row that answers "what is the process for renewing a passport" describes
  the embassy visit, so widening the instruction alone would have been an invitation to
  improvise a process, which is the single worst thing this bot can do. One row was written
  (*"What happens next once I confirm my helper's passport renewal?"*) and it names no
  appointment, no runner and no embassy — asserted mechanically. `BRIEFING_QUERY` asks for
  it and still deliberately never says **"process"**, which is the word that pulls the
  embassy rows to the top of the set. The payment step is the agency's own, from the same
  meeting: confirm → payment → forms → processing. **Nothing in the bot takes money**; the
  row only tells the client who will raise it and when.
  (C) **`BRIEFING_MATCH_COUNT` 8 → 10, and this was the trap.** With the query asking for
  next steps as well, the **cost** row fell off the end of the retrieved set — measured at
  rank 9 (0.461) for all three nationalities. The briefing would then have said the price
  was not in our records, which is precisely the 2026-09-08 defect. Verified at 10: the
  next-steps, timing, cost, documents and nationality rows all survive for PH, ID and MM.
  (D) **It asked for a decision it had already acted on.** *"Would you like to go ahead? I
  have passed everything to our team, and a live agent will connect with you shortly. In
  the meantime, is there anything else I can help you with?"* — three endings, two of them
  contradictory. The agency: *"If it is asking, would you like to go ahead, then why is it
  telling, I have passed everything to our team?"* The ticket **is** raised on that turn, so
  the handover line is the true half and the question is the one that goes. The go-ahead
  question was Thomas's ask on 2026-09-09 and lasted one live conversation; it was right in
  a flow where the briefing came before the handover, and this briefing does not.
  (E) **The clamp had to grow with the message.** 14 → 20 sentences on a briefing turn.
  `clamp_reply` masks a list MARKER's full stop but not the one ending the step, so two
  lists of five spend ten of the budget before a word of prose.
  **Checked, and already correct: the ticket is raised.** Their last line was *"We have to
  raise the ticket after all the information we have collected"*, so the conversation in the
  screenshot was read from the database rather than assumed — **CB-2026-0003**, opened
  10:45:44, four seconds before the briefing was sent, carrying all four collected fields, a
  description and a lead. It reached the portal **unassigned**, which is §9.1 and needs two
  business decisions, not code.
  Verified live for all three nationalities: heading, timing, cost, an introduced document
  list, an introduced list of the client's own steps, and one closing line. No runner, no
  appointment being booked, nobody accompanying her.

- **2026-09-09** — **Warmth, from Thomas's note: *"she currently feels quite
  transactional — like a form, not a conversation. Since she has persistent memory, we'd
  like to use that to make her warmer and smarter."*** Three asks; two done as asked, one
  refused with evidence, and a latent defect found underneath.
  (A) **A returning client is greeted by what we last talked about.** `returning_note` had
  forbidden ALL detail — *"no details of who, when or how many, we are not showing them
  their file"* — written to stop the bot reciting somebody's record at them. Thomas asked
  for the opposite of the half that matters: *"Recognise them by name if known, reference
  their last enquiry ... This alone will make repeat customers feel remembered rather than
  processed."* It now refers to **one** enquiry, the most recent, and only what it was
  about, then asks whether this follows on or is new. The rest of the ban stands: no
  dates, no counts, no history read back. Verified live on three states — with a previous
  enquiry (*"Welcome back — is this for the childcare arrangement you previously enquired
  about, or something new?"*), with nothing on file (welcomes back, invents nothing), and
  a first-timer (no welcome back at all).
  (B) **The intrusive questions say why.** `_WHY_WE_ASK` supplies the REASON and not the
  wording, the same rule `_COLLECTION_PURPOSE` follows, because a fixed lead-in repeated
  four times in one conversation is the formula `strip_repeated_opener` exists to stop.
  Live: *"Got it — do you have any pets at home, such as dogs or cats? We ask so we only
  recommend helpers who are comfortable around animals."* and *"so we can set clear
  expectations with her beforehand, how would you prefer to arrange her rest days…"*.
  **The plain ones are untouched**, which he was equally explicit about: home type and
  household still go out short and direct. Keyed on the field key, so the three carry into
  `transfer_employer` through `_hiring_field` with no second copy (§9.8).
  (C) **`budget` is deliberately NOT in that set, and Thomas named it.** Measured four
  times: told to explain why it wants a budget, the model supplies a helpful salary range
  from its own knowledge, `ungrounded_figures` discards the entire reply, and the client
  receives the bare question **with no reason at all** — worse than not explaining, plus a
  wasted call. Rewording the reason to contain no money word did not help. The way to
  close it is data: a grounded salary band in the knowledge base would survive the guard
  and carry the explanation with it. That is Ming Hwee's to supply.
  (D) **And that testing found a live defect that predates all of it.** Every `budget`
  turn was already being discarded. `budget`'s own **options are the salary bands**
  (`below $500, $500-600, $600-700, $700-800, above $800`) and `_field_guidance` tells the
  model to offer two or three as examples — but `ungrounded_figures` checks the message,
  the history, the records and the collected values, and **never the field list the
  question came from**. So the bot was instructed to say a figure and punished for saying
  it, on every hiring conversation, silently, because a guard falling back to a correct
  question looks like nothing going wrong. `_write` now takes the asked field's options as
  grounding. After: *"Do you have a monthly salary budget in mind, such as $500–600 or
  $600–700?"* — and an invented *"$1,200"* is still caught.
  (E) **A bare "no" to the extra-duties question was being re-asked.** `_YES_NO_QUESTION`
  only recognised an auxiliary **opening** the question, and `special_duties` is written
  *"Beyond the usual cleaning and cooking, **would** she need to…"* — a yes/no question
  wearing a subordinate clause. Exactly the anchoring mistake `_ASKS_SOMETHING` made in
  the other direction on 2026-09-08. `_yes_no_question()` now also tests the clause after
  a comma; `helper_profile` has no auxiliary anywhere and still re-asks, which is the case
  the rule was written for.
  **Not done, and it needs a decision.** Thomas suggested a closing line with timing —
  *"A live agent will reach out within 24 hours"*. `strip_handover_talk` removes promised
  times on purpose, and a promise the agency cannot keep is worse than none. If 24 hours
  is a commitment Ming Hwee wants to make, say so and it becomes a deliberate carve-out;
  it should not be smuggled in as a prompt tweak. The rest of his closing ask — reassuring
  next steps — is already the handover line.
  **Also observed working:** the acknowledgement he asked for more of. Every verified turn
  opened *"Got it"* / *"Got it, no smoking"* before the question.

- **2026-09-09** — **Passport renewal, rebuilt around what the client actually needs to
  know.** From the client meeting: *"the bot should not leave the client confused. By the
  end of the conversation, the client should know what the service costs, how long it
  takes, what documents are required, what they need to do next."*
  (A) **Two questions removed.** *"Is she currently in Singapore, overseas, or on home
  leave?"* was called **irrelevant** to a passport renewal outright, and the work permit
  question goes with it because *"passport renewal and work permit renewal are completely
  separate processes"* — asking about one inside the other invites a client to think we
  are handling both, when a renewal is its own enquiry with its own $695. Both were added
  on 2026-09-02 on reasoning the client has now corrected. Removed from the LIST, not the
  codebase, exactly as `_case_id()` was: a volunteered permit expiry is still extracted
  and still reaches the ticket. 6 fields → **4**.
  (B) **The briefing is now timeline → cost → documents**, Shirley's own summary, and it
  **ends by asking whether they want to go ahead** — Thomas's point that a passport
  renewal is straightforward enough to price up front and ask for a decision. The cost
  came first as of 2026-09-08; the meeting reordered it.
  (C) **It no longer explains how we do the work.** Their exclusion list is explicit: no
  appointment being booked, no description of how it is arranged or attended, no runner,
  nobody accompanying or collecting her. It is internal processing and it was arriving
  before the client had even said yes.
  (D) **The rows were edited, not just the instruction.** The briefing quotes them, and an
  instruction not to mention a runner sitting beside a record that describes one is a
  fight the record usually wins — the cost row literally read *"the runner who takes her
  through the appointment"*. Five corrections: `roughly` → **`approximately`** (asked for
  by name) on all three timelines, and the embassy/printing/shipping/operating-hours
  detail out of each. **The nationality-specific process and embassy rows are deliberately
  LEFT** so a client who asks outright still gets a straight answer — the exclusion is
  about what we volunteer. Flagged for the agency in case they want those gone too.
  (E) **`BRIEFING_QUERY` stopped asking for "the process".** It was putting the process
  rows at the top of the retrieved set, which is the model's strongest hint about what to
  write. It now asks for the timing, the cost and the documents.
  Verified live for all three nationalities: heading, timeline, cost, documents, then
  *"Would you like to go ahead?"* — with no embassy, appointment or runner anywhere, and
  Myanmar still correctly deferring the price to a consultant.
  **Not built, and it needs the agency:** payment. Their stated end state is a payment
  link in the flow, then forms, then internal processing. Nothing here takes money.

- **2026-09-08** — **`reset-ui/`: clearing a conversation from a web page instead of a
  terminal, and the foreign keys that made "also delete the lead" a real question.**
  A separate Next.js app in its own folder, deployed to its own Vercel project. It
  imports nothing from `app/`, calls no chatbot endpoint, and changes nothing in `app/`
  or `portal-ui/` — deleting the whole folder cannot affect the running bot. The delete
  path mirrors `scripts/reset_conversation.py` step for step and in the same child-first
  order, including the LangGraph checkpoint, which it reaches over a direct Postgres
  connection for the reason the script gives: those three tables are created outside
  PostgREST's schema cache and the REST client is not a reliable way to reach them.
  (A) **It can delete an employer lead, which the script refuses to do.** That refusal is
  deliberate and documented — `leads` holds real sales pipeline — so the client's
  instruction was honoured with conditions rather than by removing the protection: only a
  lead **explicitly ticked by id**, never a phone sweep; only after the lead's number,
  name, status and age have been shown; and only when nothing else references it.
  Deleting the lead **and** the checkpoint together is also the procedure §10 already
  prescribes, since deleting the lead alone is what broke conversation 36.
  (B) **`leads` has THREE inbound foreign keys and every one is `ON DELETE NO ACTION`** —
  `cb_tickets.created_lead_id` (§9.5), `lead_activities.lead_id` and
  `employer_service_requests.converted_lead_id`, confirmed against the live database
  rather than assumed. So a lead delete fails outright on any of them. All three are
  checked **before** the lead is offered as deletable and **again** at the moment of
  deletion, because the page may have been open a while. Not theoretical: of the four
  employer leads in the database, **two were blocked by `lead_activities`**, and without
  the check the client would have been shown a raw Postgres foreign-key error. Tickets on
  the conversation being cleared are deliberately NOT blockers — they are deleted first,
  in the same operation, which is the whole reason the two happen together.
  `leads_candidate` has no inbound foreign keys and is never blocked.
  (C) **A lead can be cleared with no conversation.** Found while testing: a number with a
  lead but no thread showed the lead and offered no way to remove it.
  (D) **`lib/phone.ts` is a line-for-line port of `app/utils.py`** and each function names
  its Python original. The two must stay in step — a mismatch means the tool reports "no
  conversation" for a client who has one, or offers up somebody else's row. This is the
  §9.8 duplication risk accepted knowingly, because the alternative was making the
  chatbot service a dependency of the page. For the same reason the lead-blocker rule is
  **one module with two callers**, not two copies.
  (E) **Safety.** The service-role key never reaches the browser (no `NEXT_PUBLIC_`
  variable exists); one shared password, compared timing-safely; the API accepts only a
  phone number, one conversation id and lead ids, and table names are never taken from
  the request; the reset re-resolves the conversation from the phone and refuses if it no
  longer matches what the page was showing; and an optional `RESET_ALLOWED_NUMBERS` gate
  fails closed exactly as `BOT_ALLOWED_NUMBERS` does. There is no unscoped delete in the
  codebase. `wp_chat_summaries` is left alone — nothing in `app/` reads it and the script
  does not touch it.
  (F) **The drift is caught mechanically, not by intention.**
  `scripts/selfcheck_reset_ui.py` parses both implementations and asserts they clear the
  same tables and blank the same 13 columns, and **compiles `lib/phone.ts` and runs it**
  against `app/utils.py` on 12 inputs rather than reading it. Add a table to the Python's
  `WIPE_TABLES` and forget the TypeScript and it fails, naming the table — verified by
  injecting exactly that fault plus a dropped `contact_type`, and confirming both were
  caught. `scripts/seed_reset_ui_testdata.py` creates and removes a disposable
  conversation on a reserved number for testing the UI by hand.
  Verified against the live database with a seeded conversation (4 messages, 1 ticket,
  1 handover, 2 checkpoint rows, 1 lead), deleted afterwards: every row confirmed gone by
  direct SQL, the conversation back at `bot_active` with a fresh thread and identity
  cleared, and the other 55,802 messages untouched. Also verified: the lead-only path, a
  blocked employer lead refused with its reason, a stale conversation id refused with
  nothing deleted, and both wrong and missing passwords rejected.

- **2026-09-08** — **A broadcast silenced the bot on a live conversation, and the
  client's next message got nothing back.** Not a flow bug — the agent detector.
  (A) **What happened.** The agency broadcast a number-migration notice to about fifty
  clients. `handle_outbound` treats any outbound message we did not send as a human agent
  taking over, and stands the bot down. `is_auto_reply()` exists to stop exactly that, and
  it worked for most of the estate — the log is full of *"Ignoring WhatsApp Business
  auto-reply on conversation N"* — but conversation 3766 was read as an agent, went to
  `bot_status=human_active`, and the client's *"hi i want to renew my helper passport"*
  was answered with silence. Confirmed by reading the row, not inferred.
  (B) **Why the detector cannot catch the start of a broadcast.** It is retrospective: it
  counts how many conversations ALREADY hold this exact body, and needs three. So the
  first copies of a brand-new broadcast are, by construction, indistinguishable from an
  agent typing — every broadcast the agency ever sends takes the bot down on the first
  conversations it reaches.
  (C) **And a negative verdict was cached forever.** `_AUTO_REPLY_VERDICTS[normalised] =
  verdict` stored the "no" as well as the "yes". If the very first copy of a broadcast is
  evaluated before three rows exist — which is the normal case — that "no" is pinned, and
  every one of the remaining forty-nine copies short-circuits to it. That is the whole
  estate silenced by one broadcast, and it is luck rather than design that it did not
  happen this time. Only positive verdicts are remembered now.
  (D) **Two fixes, because one is not enough.** The same body seen on **two** different
  conversations within one process run is a broadcast — a human does not send sixty
  identical characters to two clients in the same breath — so recognition no longer waits
  on database writes. And `_undo_broadcast_standdowns` reverses the stand-downs the
  earlier copies already caused, via a new `handover.undo_agent_takeover` that restores
  `bot_active` **without** minting a fresh thread: `back_to_bot` does mint one, which
  would throw away a collection the client is four questions into. Verified: copy 1 is
  still missed (nothing exists to distinguish it), copy 2 onward is caught, copy 1 is then
  un-silenced, and a genuine agent reply still stands the bot down.
  (E) **`scripts/unsilence_conversation.py`.** `--list` shows stood-down conversations on
  numbers the bot actually serves — 208 threads sit in `human_active` and almost all of
  them are the portal doing its job, so the allowlist is the only filter that means
  anything. Naming a number puts the bot back on it, thread and checkpoint kept.
  Deliberately not `reset_conversation.py`, which deletes.
  **Checked after the fix: zero allowlisted conversations are stood down** — but that was
  run against a local `.env` with 11 numbers while the server's gate has 17, so run
  `--list` in the container to confirm the other six.
  (F) **And the first copy, once the agency showed us the text.** The counting rules
  above catch a broadcast from its second conversation; the first has nothing to compare
  against. `_BROADCAST_MARKERS` closes that with the one thing a mass announcement cannot
  hide — who it is addressed to. Nobody writes *"Dear Valued Customer"* to a client they
  are already talking to about their helper's passport. Kept strictly to that test:
  **"operating hours" is deliberately NOT a marker**, because an agent answering *"what
  time do you open"* would say it, and a rule that swallows a real agent is worse than the
  bug it fixes — verified against four genuine one-to-one agent replies, one of which
  quotes the opening hours in full. A one-off announcement carrying none of these phrases
  can still be matched outright by putting its exact text in `WHATSAPP_AUTO_REPLY_TEXTS`
  in `.env` (`||` separated).

- **2026-09-08** — **The passport-renewal briefing moves to the end of the collection,
  gets a heading, and can no longer be lost or priced from another nationality.**
  (A) **It never reached the client at all, and the transcript says why.** After the
  client answered *"myanmar"* the reply was *"When does her current passport expire?"* —
  which is `passport_expiry`'s hand-written question **word for word**, i.e. the
  fallback `_write` returns when a guard discards the generated reply. So the briefing
  WAS written and then thrown away (most likely on formatting: `looks_like_document`
  still bans bullets and bold even with `allow_steps`). The turn then recorded
  `briefed_services = ['passport_renewal']` **regardless**, so it was never tried again
  and the client went from nationality straight to handover having been told nothing.
  `briefing_lost` compares the outgoing reply against the fallback and only records the
  briefing as given when it actually survived; a discarded one is logged as an error and
  retried on the next turn.
  (B) **It now happens after ALL the questions, not after the nationality.** Agency's
  instruction on seeing the above: *"After all the questions it should reply with that
  process message ... so that the user would be able to understand what is happening
  next."* It is now part of the closing message — everything collected, then the
  explanation, then the handover line. That also removes the mid-flow interruption
  entirely.
  (C) **Heading, then cost, then timing, then process**, their order: *"the cost should
  be first, then the estimated time, and then the process ... Also, add the heading of
  the message."* The heading was optional in the first version and appeared in one reply
  out of three, so it is now required outright; and the steps are explicitly told to take
  their own lines, because one run-through came back as a single paragraph with
  *"1. ... 2. ... 3. ..."* inline, which is unreadable on a phone.
  (D) **$450 was quoted for a MYANMAR helper.** The fee row reads *"$450 for a Filipino
  helper and $450 for an Indonesian helper ... if your helper is of another nationality,
  tell us and a consultant will confirm the cost for her embassy"* — so the figure is
  genuinely in the retrieved records and `ungrounded_figures` passed it happily.
  **Grounded is not the same as true:** it is the other two nationalities' price, quoted
  to a client whose price we do not have. `FEE_BY_NATIONALITY` records which
  nationalities each service actually has a price for (`passport_renewal` and
  `home_leave`: PH and ID only), and where hers is not among them the briefing is told
  outright not to quote, adapt or range the one beside it, and to say a consultant will
  confirm. Verified live: Myanmar now opens *"A consultant will confirm the cost for her
  embassy"* and gives the timing and the three-form route in full.
  Verified end to end for all three nationalities: heading present, cost then timing then
  process, line breaks intact, and no route crossing — Indonesian gets the runner
  collecting her from home and a passport copy, Filipino gets attending in person with
  her ORIGINAL passport and the five signed forms, Myanmar gets the Undertaking of
  Employer Form, Standard Employment Contract and Information Sheet.
  `selfcheck_flows.py` is 177 assertions; `smoke_nodes.py` is 32 states.

- **2026-09-08** — **The passport-renewal name comes from our records or from the
  client, never from WhatsApp; and the briefing leads with the money and the time.**
  Both from the agency's test of the new flow.
  (A) **"Hi Vaidik, I'm Claire ... May I know your helper's name?"** went to a number we
  had never spoken to, skipping the employer's own name entirely — because
  `_with_push_name` filled `full_name` from the WhatsApp profile (2026-09-01, to stop the
  bot asking for a name it had just used in its greeting). Their rule now: *"ask for the
  name first if the name is not in the database. If the name is in the database ... greet
  them before moving forward."* `NAME_FROM_RECORD_ONLY` makes the push name no evidence
  at all on this flow — it is a label the client set on their own profile, and this flow
  collects the name that goes on embassy paperwork.
  (B) **"In the database" needed a source, and `customer_name` is not one.** It is
  written from the push name when the conversation row is created, and the identity patch
  only fills it *when empty* — so for a known employer it is still the push name, not the
  name on their file. New `contact.get_record_name()` reads `employers` directly and the
  webhook puts it on every turn as `record_name`; `_known_fields` fills `full_name` from
  it before anything else. Verified live: a new number gets *"Hi, I'm Claire, Ming Hwee's
  AI assistant. May I know your name?"*, a client on file gets *"Hi Vaidik, ... may I know
  your helper's name?"*. Every other flow is untouched — the old behaviour was itself a
  fix, and they asked for this flow.
  (C) **Suppressing the field was not enough.** `_contact_block` prints
  `- WhatsApp name: Vaidik` into the system prompt, so the model would have written "Hi
  Vaidik" and then asked for the name. The prompt state drops `customer_name` on these
  flows when no record name exists.
  (D) **The briefing now opens with the fee and the lead time.** Their words: *"It is
  going straight forward, like 'We handle it for you.' We don't want this thing: We have
  to tell the estimated time and the cost. and then We move forward to the process."* Both
  nationalities now open *"The fee is $450, and it usually takes about 3 working days"* /
  *"about 6 to 8 weeks"*, then the steps, then the invitation to ask.
  (E) **It also claimed a figure it had was missing.** The live briefing said *"The fee
  is not stated in our records, so I'll check the exact amount with the team"* — and the
  client asked in the next message and was told **$450**, which had been in the retrieved
  set all along. The note now says outright not to claim something is missing without
  reading for it, and not to soften an exact price into "approximately".
  (F) **`UnboundLocalError` a second time in two changes**, and caught the same way: the
  push-name suppression read `system_prompt_state` a hundred lines above where it is
  built. `smoke_nodes.py` failed seven states. The flag is set early and applied at the
  build, the same shape as `client_asked`. **Twice in one day is the pattern, not the
  accident: this file is long enough that any new flag read in more than one place needs
  `smoke_nodes.py` run before it is believed.**
  `selfcheck_flows.py` is 169 assertions; `smoke_nodes.py` is 31 states.

- **2026-09-08** — **Passport renewal now explains itself.** Agency: *"firstly ... ask
  for the employer's name by greeting them by name and ... the helper's name ... then the
  nationality ... then we have to tell them the whole process, the documents required, the
  cost/fees, and how long it takes ... a new user doesn't know how the process is going
  on."*
  (A) **The flow asks the client's own name, first.** It was the only thing rule 1c
  needed and the one field this flow never had, so on a number we do not already know
  Claire opened cold. Portable and filled from the WhatsApp push name. 5 fields → 6:
  name, helper's name, nationality, then the rest.
  (B) **`BRIEFING_AFTER` — a service that stops and explains itself, once, on the turn
  the field it depends on is answered.** For a passport renewal that is the nationality,
  and the agency's reasoning is the design: the documents, the embassy visit and the lead
  time all differ by it, so there is nothing honest to say before we know it and no
  reason to make the client drag it out a question at a time afterwards. Deliberately NOT
  the opening turn — briefing before the nationality produces *"it depends on her
  nationality"*, the exact defect fixed on 2026-09-04.
  (C) **The briefing turn retrieves what the client is about to be told, not what they
  just said** — they said "Indonesian". `BRIEFING_QUERY` asks for the process, the
  documents, the cost and the timing together, at **8 rows rather than 5**: measured, at
  5 the TIMING row was the one that fell off the end, so the briefing could not say how
  long it takes. Verified end to end against the live model for both nationalities, and
  every claim in both replies was checked back to the row it came from — the Filipino
  reply's *"processed and printed in the Philippines, shipped back"* is verbatim from the
  KB, not invented.
  (D) **It is route-correct, which is the whole point.** An Indonesian client is told the
  runner collects her from the home, a copy of the passport is enough, $450, about 3
  working days. A Filipino client is told she attends in person, her **original** passport
  is required, the five embassy forms need original signatures, $450, 6 to 8 weeks.
  Neither reply named the other's route, even though the retrieved set contains rows for
  both (the Myanmar no-embassy-contract row comes back inside a Filipino search at 0.445).
  (E) **Two things had to be gated, and the second was a real design flaw.** The briefing
  waits a turn if the client asked us something — answering them comes first. And it never
  fires on a **parked** topic: only the collector briefs, `blocked_topic_responder` never
  sets `briefed_services`, so without that test every remaining turn of a ticketed
  conversation would have retrieved the briefing set instead of its own answer — which is
  precisely the transcript the agency sent. Found by a self-check going red, not by review.
  (F) **Cross-questioning after the briefing, which is what they asked for.** Five
  follow-ups run live: *why do you need my NRIC*, *original or copy*, *is $450 final*,
  *what if her work permit expires too*, *can she go by herself*. Two failed and are
  fixed. *"what if"* carried no question mark and none of `_ASKS_SOMETHING`'s openers, so
  the client's question was simply ignored — and `_VALUE_IS_QUESTION` already read it as a
  question, so the two patterns disagreed about the same words, the exact mismatch the note
  between them warns about. And *"can she go to the embassy by herself"* was answered
  **"Yes"** for an Indonesian helper, reversing a fact the bot had stated three messages
  earlier; `ANSWER_THEN_ASK_INSTRUCTION` said how to answer but never said not to
  contradict the records, and a yes/no question is the shape most likely to be flipped by
  an agreeable model. It now leads with the runner accompanying her. **Not claimed as
  fully solved:** the reply still opens *"Yes, she can go by herself, but..."* — the
  operative fact is now there, the hedge is model style.
  (G) **`UnboundLocalError` again, caught again.** The briefing block reads whether the
  client asked a question ~100 lines above where `answer_first` was assigned — the same
  shape as 2026-09-04. `smoke_nodes.py` failed three states on it. Fixed the same way that
  one was: one value, `client_asked`, computed once and read by both.
  `selfcheck_flows.py` is 162 assertions; `smoke_nodes.py` is 29 states.

- **2026-09-08** — **Six defects from the agency's live round on new hiring and
  passport renewal. One is a regression shipped the same morning.**
  (A) **`requirement` was asked three times and the client had to say so.**
  *"General house work"* → re-asked → *"Only general housework"* → re-asked → *"I have
  tell several time I need general housework"*, and the flow only moved on because
  `max_asks` ran out. Cause: `_undecidable_gate_keys`, added that morning for the
  transfer gate deadlock, assumes a field's gates cover its whole answer space. They do
  on `transfer_direction` — two opposing gates, two options, every valid answer opens
  one. They do **not** on `requirement`, whose gates are `children_detail` (childcare)
  and `elderly_detail` (eldercare) while its own declared options include *"general
  housework and cooking"* — a complete, correct answer that opens neither. So a good
  answer was read as "the gate did not understand this", blanked, and asked again.
  `_gates_are_exhaustive` now derives the test from the field's **own options** — the
  rule fires only where every declared option opens some gate — so a new gate or a
  reworded option cannot leave it stale, and a field with no options is left alone. The
  transfer deadlock the rule exists for still fires.
  (B) **A promise to answer a question nobody asked.** *"6 bedroom and 6 bathrooms are
  there"* matched `_ASKS_SOMETHING`'s `are there`, so the collector believed a question
  was outstanding and closed with *"I'll confirm your question with the team and come
  back to you"* — leaving the client waiting for a reply that could never come. `is
  there`/`are there` now only count when they **open** the message, which is what they
  do as interrogatives; trailing, they are ordinary Singaporean and Indian English for
  "there are". `_VALUE_IS_QUESTION` anchors all of its own alternatives at `^` for
  exactly this reason — these two were the pair left unanchored.
  (C) **A bare "Yes" closed an open question.** *"Any preference on her age or how much
  experience she should have?"* → *"Yes"*, field closed, and the agent was handed a
  preference with no content. `_BARE_YES_NO` (a bare yes/no and **nothing else**, so
  *"Yes all"* still answers the extra-duties question) plus `_YES_NO_QUESTION` (does the
  question open with an auxiliary verb) re-ask once. `<=` on the ask count, not `<`: both
  fields this was written for, `helper_profile` and `additional_notes`, are `max_asks=1`,
  so the ordinary limit would have ruled out the re-ask on precisely the two cases.
  (D) **A parked passport renewal refused a price question we can answer.** *"Ok what is
  cost"* and *"I'll ask you the feesa"* both got the holding line. `_GENERAL_INFO`
  required *"what is **the** cost"* and listed no fee/price/charge in that branch, so a
  $450 answer was passed to a human. Both fixed.
  (E) **The widening retry reopened the wrong-price hole closed hours earlier.**
  `_service_filter` keeps the filter for `FEE_STATED_SERVICES` so another service's fee
  cannot be quoted — but the retry below it drops the filter whenever the filtered score
  is under the floor, and a terse *"what is cost"* inside a **passport** renewal scores
  0.361, so it widened and the top row was the **work permit** renewal at **$695** where
  the answer is **$450**. Scoped to a PRICE question via `_PRICE_QUESTION`, deliberately
  excluding timing words: the 2026-09-03 case this retry was written for is *"How much
  time it takes in renewal"*, and it still widens exactly as before.
  (F) **A fee question is tagged with the service's COST, not the service.** The KB
  phrases these rows *"How much does it cost to renew my helper's passport?"*, and
  `"(passport renewal)"` alone put *"what is cost"* at **0.367** — under the floor, so
  `_answerable()` read False and the client got a holding line for a figure we hold.
  Measured across six phrasings and every service: `"(cost of passport renewal)"` scores
  **0.503–0.566** against 0.362–0.465 and returns the right row every time. Renewal,
  transfer, replacement and direct hire all improve; home leave and replacement move by
  under 0.02 and keep the same top row.
  (G) **"okayyyyyyyyyyyyyyyyyyyyyyyyyyy" was answered** with the handover line the client
  had already been given twice. `_ACK` ends its alternatives on `\b`, and "okay" inside
  "okayyyy" has no word boundary after it. `_unstretched` tries the message as written
  and both de-elongated readings — collapsing a run to **one** letter recovers "okayyyy"
  → "okay", collapsing it to **two** recovers "goooood" → "good", and no English word
  carries three identical letters in a row, so neither reading can invent an
  acknowledgement.
  **What worked, verified in the same transcripts:** the stepped six-step process answer
  went out correctly on a parked topic, the per-nationality documents answer was right
  for an Indonesian helper, and the timing answer (3 working days) was grounded.
  `selfcheck_flows.py` is 148 assertions; `smoke_nodes.py` is 27 states.

- **2026-09-08** — **The consolidated cost + timeline table: the last content gap
  closed, three stale rows corrected, and the money-question defect that would have
  quoted the wrong price anyway.** 7 rows, 3 corrections, 2 code changes.
  (A) **The work permit renewal fee exists: $695.** *"No agency fee for work permit
  renewal anywhere in the KB"* has been the standing gap since 2026-09-04 and is why a
  cost question on that service could only ever be deferred. `renewal` is a small-ticket
  service and is not in `COST_WITHHELD_SERVICES`, so it goes straight out. **That was the
  last content gap.**
  (B) **A fee is stated only where the agency stated one.** Their instruction: *"the
  service which do not have the timeline and cost that means we dont have to open that
  live agent will handle that"*. New hiring, direct hiring, replacement and transfer get
  no figure — the first two were already withheld mechanically, and `replacement`,
  `transfer` and **`transfer_employer`** joined `COST_WITHHELD_SERVICES`. The employer
  key matters: an employer transfer runs under its own service key, so leaving it out
  would have withheld nothing on the half that actually asks about cost. Deferral rows
  were written for replacement, transfer and direct hire so the bot says *why* rather
  than going quiet.
  (C) **Three existing rows ANSWERED these questions with different numbers, and were
  corrected rather than stacked.** *"How long does it take to hire a domestic helper"*
  said **6-8 weeks** overseas and **3-4 weeks** for a transfer already here; the table
  says **4-6 weeks**, and that 3-4 also disagreed with the transfer rows (1-2 weeks).
  *"How do I renew my helper's work permit"* said **4 weeks**; the table says about a
  week, ~3 days processing. *"How long does a transfer take"* gave only the MOM approval
  window (1-3 working days) as the answer to how long a transfer takes, which understates
  it — the client is asking about interview-to-deployment, which is 1-2 weeks. Direct
  hire (2-3 / 4-6 weeks) and passport renewal already matched and were left alone.
  (D) **Loading the fees would not have worked on its own.** Measured after loading,
  before any code change: a bare *"how much does it cost"* returned **Form A's hiring fee
  schedule** (0.472) as the top record **inside every service** — renewal, passport
  renewal and home leave included, none of which withholds a price. Two causes, both
  already documented here in another form.
  First, `_search_query` tagged the query *"(fee enquiry)"* — **tagging it with itself**,
  exactly the 2026-09-07 `process_question` defect. The money intents were deliberately
  excluded from that fix on the grounds that "money IS a subject", and that is half
  right: *what we charge for X* is a question about X. `fee_enquiry` is now in
  `_SUBJECTLESS_INTENTS`; **`salary_enquiry` is not**, because what a helper earns is
  about the helper, and service-tagging it measured **worse** (0.505 → 0.446 on *"what
  salary should I budget"*, swapping a direct answer for a general one). A fee question
  with nothing else in flight still searches bare.
  Second, `_service_filter` drops the filter on any money turn — right when the figures
  live somewhere else (the 2026-09-02 salary case), wrong when this service states its
  own price. Unfiltered, *"how much does it cost"* inside a **passport** renewal returned
  the **work permit** renewal row: **$695 quoted where the answer is $450**, and the same
  on *"what is the fee"* and *"how much do you charge"*. That is not a vague answer, it
  is a false one. `FEE_STATED_SERVICES` keeps the filter for those three; none of them
  collects a money field, so the other two widening rules cannot want it dropped either.
  After both: every service returns its own cost row — renewal 0.466, passport renewal
  0.486, home leave 0.588, replacement 0.603, transfer 0.595, transfer_employer 0.626,
  direct hire 0.668 — **16 probes, none below the floor**, and the salary probes unmoved.
  `selfcheck_flows.py` is 130 assertions. **No content gap remains.** Still open and
  needing Ming Hwee, not code: the medical insurance minimum (§9.14), the Settling-In
  Programme window, the Myanmar passport route, and who receives leads (§9.1).

- **2026-09-08** — **Replacement: the document checklist and the nine steps, and the
  contract clause that was answering every question in their place.** 9 rows plus 1
  shared.
  (A) **Fourteen rows already carried `service_type='replacement'` and not one of them
  said how a replacement is done.** Two are FAQ answers (the guarantee, and what to do
  about performance); the other **twelve are raw clauses lifted from the Client Service
  Agreement** — refund percentages, entitlement tables, *"subject to the conditions in
  Clause 4"*. With nothing operational to compete, **clause 3.1 was the top match for
  seven different questions**, measured under `service=replacement` before anything was
  loaded: *"what is the process"* 0.417, *"what documents are needed"* 0.443, *"what
  forms do I have to sign"* 0.438, *"how does a replacement work"* 0.483, *"what happens
  when she arrives"* 0.489, *"how long does a replacement take"* 0.484. **Five of those
  clear the 0.40 floor**, so `_answerable()` read True and the widening retry never ran —
  a client asking what paperwork to gather would have been read a refund-entitlement
  clause, confidently. This is the 2026-09-08 direct-hire defect again with a worse
  source: not a wrong row, a *legal* row.
  After: **0.427–0.805, all 23 probes above the floor**, every one of the seven now
  returning a real replacement row, and the four service controls unmoved (new hiring
  0.473, direct hire 0.661, passport 0.502, home leave 0.623).
  (B) **Nothing shared was copied.** The agency's own line is that the incoming
  candidate's half *"mirrors New Hiring"*, and those steps were moved to `general` on
  2026-09-08 for direct hire — so they are already reachable here, verified rather than
  assumed (*"what is an IPA"* 0.530, *"what is the Settling-In Programme"* 0.554, both
  unchanged by this load). Only what is genuinely replacement-specific was written: the
  two forms that stand in for the new-hire fee schedule, the document checklist, and the
  nine steps.
  (C) **One `general` row added, because a real gap showed up while measuring.** *"Do I
  need to buy insurance"* scored **0.000** under `replacement` — nothing in the entire
  knowledge base matched it, because the only row that answers it is phrased *"for a
  direct hire"* and the service filter excluded it. Filed as `general` rather than copied
  three ways (§9.8). It is now top for that question under `direct_hiring`, `new_hiring`
  **and** `replacement`, with the direct-hire-specific row still second at 0.512, so
  nothing was displaced out of the set the model receives. **No insurance minimum is
  stated** (§9.14); the $5,000 bond is phrased the way the existing direct-hire row
  phrases it, deliberately clear of the words `quotes_hiring_package_cost` fires on.
  (D) **That exposed a hole in the vetting.** A `general` row is retrieved from inside a
  `new_hiring` or `direct_hiring` conversation, where the cost guard runs on the reply —
  but the row checks only tested `COST_WITHHELD_SERVICES` rows, because until now no row
  was deliberately written into `general`. `selfcheck_flows.py` now asserts no `general`
  row trips it, and that none states an insurance minimum.
  Kept OUT as internal: creating and attaching the employer account, the dashboard that
  shows the matched profiles, the partnering agent by that name, notifying the transport
  company, and case closure. **No replacement fee is stated** — the source names a
  "Replacement Services & Fees form" but gives no amount, and the existing FAQ row
  already says a replacement inside the guarantee period carries no additional agency
  service fee; a figure invented beside that is how the KB starts contradicting itself.
  `selfcheck_flows.py` is 114 assertions. **Remaining KB gap: the agency fee for work
  permit renewal.**

- **2026-09-08** — **Home leave: the per-nationality checklist, the fees, the process,
  and the question the whole service turns on.** 12 rows, one new field, one guard.
  (A) **The knowledge base held three home-leave rows and none of them was
  operational** — is it compulsory, who pays for the flights, and one untitled chunk.
  No documents, no process, no fee, no lead time, so every practical question about a
  home leave got the holding line. Retrieval through the real path after loading:
  **0.502–0.811, all 17 probes above the floor**, with the new rows top for 12 of them
  and the three controls unmoved (passport process 0.502, new-hiring process 0.473,
  passport cost 0.551).
  (B) **`home_leave` did not ask the helper's nationality**, and it is the field
  everything follows from — more so than on a passport renewal, where the nationality
  changes only the paperwork. Here it changes the paperwork **and the lead time and the
  price**: a Filipino helper needs her ORIGINAL passport, a ticket itinerary and six
  embassy forms returned with original signatures, takes about 4 weeks and costs $400; an
  Indonesian helper needs copies and one form we provide, takes about 2 weeks and costs
  $250. Without it the ticket does not say which embassy, and the retrieval filter is
  dropped when the nationality is unknown, so both routes compete and the top row is
  whichever phrasing scored best. Quoting Indonesia's $250 and 2 weeks to an employer of
  a Filipino helper is the wrong budget against the wrong deadline. The agency's own step
  1 is "confirm nationality and intended travel dates", so this is their question, not an
  invented one. Portable, and the same key `passport_renewal` uses, so a client who has
  already told us is not asked twice. 2 fields → 3.
  (C) **The route caveat generalised from one service to a table.**
  `_ROUTE_BY_NATIONALITY` maps a service to its own pattern **and its own list of what
  not to name**, because the two services are route-split on different things.
  `_HOME_LEAVE_ROUTE_DEPENDENT` carries the timing and the money words that
  `_NATIONALITY_DEPENDENT` deliberately leaves out — and the passport pattern is
  **unchanged**, on purpose: a passport renewal is $450 for either nationality, so making
  it fire on "how much" would suppress an answer it can safely give. Verified by the
  branch actually taken, not by reading it: the caveat is present for a cost, a timing
  and a documents question with the nationality unknown, absent once she is Filipino,
  absent on an ordinary answer, and absent for another service. 10 route-dependent
  phrasings fire, 9 ordinary answers stay quiet.
  Kept OUT as internal: *"confirm embassy appointment availability with the runner"* —
  that instructs our staff. The client-facing fact underneath, that we check what is
  available before giving them a date, is in the timing row.
  **No Myanmar fee, timeline or document list was given, so none is stated** — the same
  rule as the passport fee, and `home_leave` is not in `COST_WITHHELD_SERVICES`, so
  whatever is here goes out and an invented third would go out too.
  `home_leave` is 3 fields. `selfcheck_flows.py` is 106 assertions; `smoke_nodes.py` is
  25 states. **Remaining KB gap: the agency fee for work permit renewal.**

- **2026-09-08** — **A transfer collection drifted into `new_hiring`, and "returning
  client" was offered as an answer to a question our own database answers.** Both from
  the client's retest of the expanded transfer flow.
  (A) **The drift.** Deep into a `transfer_employer` collection the bot asked *"Any
  preference on her age or how much experience she should have?"* and the client answered
  *"yes i want 3 year experienced maid"*. "maid" beside a hiring preference classified as
  `new_hiring` — a **hard `SERVICE_INTENT`**, so it simply won — and the very next
  question was `new_hiring`'s own `hire_source`: *"Are you open to a first-timer, or would
  you prefer someone who has worked in Singapore, worked abroad, or is a transfer helper
  already here?"*, offering a transfer helper to a man who had opened with *"i am looking
  for a transfer helper"*. He replied *"in starting i started with the query i want
  transfer helper then why you asking me again ?"*. **`hire_source` is not in
  `transfer_employer`'s field list at all**, which is what made it diagnosable: the only
  way to be asked it is to no longer be in the transfer flow. The three existing
  stickiness rules all rescue a turn the classifier *could not label* — `other`, no
  service, or a money question — and none of them helps when it picks a different real
  service. `guards.answering_our_question` (already in place from the "3-4" fix that
  morning) now also holds the SERVICE: if our last line ended in a question and theirs is
  not one, the live collection keeps the turn. Guarded on `_named_service`, so *"I also
  want to renew my helper's passport"* is still a genuine switch — verified both ways.
  (B) **"returning client" was one of `referral_source`'s options**, and the model read
  the options into the question: *"How did you hear about Ming Hwee, such as through
  Google, a friend or family member, social media, or are you a returning client?"* —
  put to someone whose `placements` we count on every single turn. Offering it as an
  **answer** is the same defect as asking it outright, which has been banned since
  2026-09-04. Option removed.
  (C) **A returning client is no longer asked how they heard about us at all.**
  `_known_fields` fills `referral_source` from a positive `prior_hires`, the same way
  `first_time_hire` has been filled since 2026-09-04. Only on a positive count: zero
  means "no placement on record", which is not evidence of how a first-timer found us, so
  they are still asked.
  `selfcheck_flows.py` is 99 assertions.

- **2026-09-08** — **The take-on transfer branch asks what a hire actually needs.**
  Agency, after retesting: *"if user is new then ask every question that is related and
  needed for the hiring ... if user is existing then greet them by name and then ask
  further questions accordingly, not end conversation in 4 questions only ... after
  raising the ticket user should be satisfied that yaa i have provided enough details."*
  (A) **4 questions became 18 for a new client.** Working out which helper suits a
  household is the same job whether she is a transfer or a new hire, and the office
  filters on the same form (`candidates.biodata`), so the take-on branch now asks what
  `new_hiring` asks: who the care is for and their ages, the home and its size, her own
  room or sharing, pets, languages at home, nationality, age and experience wanted,
  cooking, extra duties, budget, rest days, anything else, and how to stay in touch.
  **Reused, not copied** — `_hiring_field()` takes `new_hiring`'s own `Field` and swaps
  the gate via `dataclasses.replace`, because duplicated constants that then diverge are
  already a live problem here (§9.8) and both `languages` and `household` have been
  reworded once each. Left out are the three things a transfer has already settled:
  `hire_source` (choosing a transfer IS the answer), `start_timeline` (the agency's own
  2026-09-07 instruction that asking a transfer client when they want it is not
  required), and `first_time_hire` (never asked of anyone).
  (B) **An existing client is asked 9, not 18**, six of them optional — ten of the keys
  are in `_PORTABLE_ACROSS_SERVICES`, so anything answered in an earlier enquiry carries
  over untouched.
  (C) **`returning_note` now fires on every flow.** It was gated on `first_time_hire`
  being in `known`, and `known` is filtered to the current service's own field keys — so
  a key only `new_hiring` defines meant a returning client asking about a transfer, a
  renewal or a passport was never welcomed back. It now also fires on the opening turn,
  which is the same say-once test `purpose_note` uses. Verified: new client gets the
  purpose note and no welcome-back, an existing one gets both, and neither repeats
  mid-collection.
  (D) **A releasing client is untouched** — still 2 questions. The dependent fields
  (`children_detail` off `requirement`, `pet_detail` off `pets`, `email` off
  `update_channel`) keep their own gate rather than `_TAKING_ON_TRANSFER`; their parents
  are take-on fields, so for a releasing client those gates stay *undecided* and the
  questions are never asked. One gate per field is all the dataclass allows, and this is
  why it is enough.
  `transfer_employer` is 25 fields defined. `selfcheck_flows.py` is 92 assertions;
  `smoke_nodes.py` is 23 states.

- **2026-09-08** — **Transfer, round 2 after the client's retest.** The 2026-09-08 gate
  fix worked: the same conversation that produced CB-2026-0004's two useless fields now
  produces `requirement: childcare, preferred_nationality: Myanmar, household: 5,
  budget: $500, full_name: Vaidik Dubey` — an agent can actually match against that. Two
  things the agency had already asked for were still not honoured, and the retest
  transcript showed a third.
  (A) **The timing question is gone from `transfer_employer`.** Their instruction on
  2026-09-07 was explicit — *"A person looking for Transfer helpers are naturally urgent
  to seek for help urgently. This question asked is not required."* — and the retest
  still ended *"When are you hoping to have this sorted?"* → *"ASAP"*, which is the
  answer they said was worthless. Removed from the list, not the codebase, the same way
  `_case_id()` was on 2026-09-04: the objection is to **asking**, and a volunteered date
  is still in the transcript and the ticket's `bot_note`.
  (B) **The household question showed three of its four brackets** — *"roughly 1 to 2, 3
  to 4, or 5 to 6?"* — so a household of seven is told the largest bracket is 5-6. The
  written question named none of its options, so `_field_guidance`'s general "drop two or
  three in as examples" rule applied. Identical to the `languages` defect they flagged on
  2026-09-07 and fixed the same way: the options go into the question, and the
  enumerated-options rule takes over. Fixed in **both** definitions, `new_hiring` and
  `transfer_employer`.
  `transfer_employer` is 8 fields; a take-on client answers four after the direction is
  known. `selfcheck_flows.py` is 88 assertions.

- **2026-09-08** — **Passport renewal: the per-nationality checklist, the fee at last,
  and the first submission that CONTRADICTED rows already loaded.** 4 new rows, 6
  corrections.
  (A) **The conflict.** The agency's document checklist disagrees with their own
  process-flow document loaded on 2026-09-07, in two places that both bite at an embassy
  counter. The Philippines needs the helper's **ORIGINAL passport**; the old rows said
  *"a copy of her passport"* for every nationality, and turning up with a copy wastes an
  appointment that is roughly 2 months out. And the old rows made the **Undertaking of
  Employer Form exclusive to helpers WITHOUT an embassy contract** — i.e. Myanmar — while
  the new checklist lists an Undertaking Form among the **Philippines** embassy
  documents, alongside Annex A and the OFW Information Sheet, neither of which appeared
  anywhere before. The newer document is the explicit per-nationality checklist, so it is
  taken as authoritative for PH and ID and the conflicting rows were **corrected rather
  than stacked** — leaving both would put a flat contradiction in front of a model that
  quotes either. Myanmar is not covered by the new document, so the Myanmar rows are
  untouched; only the claims that those forms belong *only* to the no-contract route were
  rewritten. **For Ming Hwee, not resolvable here: whether the Myanmar three-form route
  still stands as the 2026-09-07 document described it.**
  (B) **The fee exists.** *"No agency fee for passport renewal anywhere in the KB"* has
  been the standing gap since 2026-09-04 and is why a cost question on this service could
  only ever be deferred. **$450** for a Filipino and for an Indonesian helper. No Myanmar
  fee was given, so none is stated — `passport_renewal` is a small-ticket service and is
  not in `COST_WITHHELD_SERVICES`, so whatever is here goes out, and an invented third
  would go out too. Verified the quote does not trip `quotes_hiring_package_cost`.
  (C) **Three more rows**: original-passport-or-copy (the difference PH/ID that decides
  whether we hold her passport), when to start, and the forms we prepare. The process row
  was corrected to the agency's six steps.
  Kept OUT as internal: *"always confirm the appointment date with the runner first"* and
  *"check available appointment dates with the runner before advising the client"* —
  those instruct our staff. The client-facing fact underneath, that we confirm the
  appointment before committing to a date, is in the when-to-start row.
  Retrieval: **0.496–0.628, all 9 probes above the floor**, and every correction verified
  present in the stored text rather than assumed. `selfcheck_flows.py` is 86 assertions.
  **Remaining KB gap: the agency fee for work permit renewal.** Passport renewal's half
  of that long-standing pair is now closed.

- **2026-09-08** — **Work permit renewal: the document checklist and the full process,
  10 rows.** Same treatment as the new-hiring and direct-hire sets. The KB already held
  general FAQ material on renewals — permit validity, what happens if one lapses, the
  6-monthly medical — but not the agency's own checklist, and **not the MOM Renewal
  Notification**, which the source calls essential to apply at all. An employer who does
  not know to look for it cannot start, so it now has a row of its own alongside the
  documents, the steps, when to start, the e-authorisation and its two windows, what
  happens after authorising, the confirmation, what the employer personally does, and
  the medical. Retrieval through the real path: **0.464–0.724, all 15 probes above the
  floor**, with the new rows top for 13 of them.
  Dropped as internal: creating and attaching the user account, and the instruction to
  buy the insurance before 5pm so it clears overnight. The overnight wait itself is
  kept, because it is why the submission happens the following day and that is the
  client's business; the cutoff is ours.
  **No existing row was corrected.** The older FAQ answer to *"How do I renew my
  helper's work permit?"* frames it as what MOM requires (updated contract, insurance,
  medical, application) while the new rows say what we need **from the employer** —
  checked in context and they complement rather than contradict, with the new checklist
  inside the top 4 for that question.
  `renewal` is a small-ticket service and is not in `COST_WITHHELD_SERVICES`, so it may
  quote costs freely — but there is **still no agency fee for work permit renewal
  anywhere in the KB**, so no row names one and `selfcheck_flows.py` asserts none does.
  Load the fee and it will quote it with no code change. `selfcheck_flows.py` is 81
  assertions.

- **2026-09-08** — **The transfer flow collected one field and handed over. Three
  defects from the agency's testing, all reproduced from the live ticket before being
  touched.**
  (A) **A gate deadlock emptied the whole flow.** `Gate.state()` ends
  `return "open" if _mentions(value, matches) else "closed"` — an unrecognised value
  **closes** the gate. `transfer_employer` puts two *opposing* gates on one field, so a
  value neither recognises closes **both**, and with it all six fields that qualify the
  request. The only ungated field left is `timeline`. Live: *"Hi I'm looking for a
  transfer helper"* was extracted as `transfer_direction='transfer'` — true, useless,
  matching neither gate — so the entire conversation was *"when are you hoping to have
  the transfer arranged?"* → *"Asap"* → complete → live agent, and **ticket CB-2026-0004
  reached the agent reading exactly**:
  `{'timeline': 'as soon as possible - within 2 weeks', 'transfer_direction': 'transfer'}`.
  The client's own words: *"Live agent won't be able to do any candidate matching just
  base on"* that. `_undecidable_gate_keys` now treats a value that opens **no** branch as
  not an answer: it is blanked and the disambiguating question is put again, bounded by
  `max_asks` so it cannot loop. Deliberately generic — every gated service
  (`insurance`, `direct_hiring`, `new_hiring`) can hit this the moment the extractor
  returns a plausible-sounding value the gate does not know.
  (B) **An answer to our own question was answered with "what is your question about?"**
  Mid-intake the bot asked the household-size question, the client replied **"3-4"**, and
  got back *"Hi, I'm Claire, Ming Hwee's AI assistant. Could you share what your question
  is about?"* — a re-introduction and a request for the question, in reply to the answer
  we had just asked for. The client had to retype their request to restart the intake.
  Cause: the classifier misread a bare "3-4" as a money intent; its stickiness rule
  correctly put `service_type` back to `new_hiring` but **does not touch `intent`**, and
  the 2026-09-07 money branch in `route_after_rag` reads `intent` — so the correction
  never reached the routing, the turn went to `response_generator`, and its "nothing
  asked yet" path invited a fresh question. `guards.answering_our_question` (our last
  line ended in `?`, theirs does not) now blocks that diversion. It is the mirror of the
  rule `closure.needs_no_reply()` has held from the start. **Two theories were checked
  and disproved first** — a split conversation (one row, one thread) and the webhook
  payload overwriting `service_type` (it does not send it).
  (C) **`transfer_employer` never asked the client's name.** The only employer flow with
  no `full_name`, so rule 1c had nothing to use and the lead carried a phone number and
  nothing else. Added first, portable, and filled from the WhatsApp push name — so a
  client we already know is still never asked.
  **Order now matches what the agency asked for**: name, direction, then *what they
  need* — requirement, nationality, household, budget — with `timeline` last and
  optional. Their objection (*"A person looking for Transfer helpers are naturally
  urgent... this question is not required"*) was really that timing was the only thing
  asked; it was the only ungated field. It is kept, asked once, at the end.
  `selfcheck_flows.py` is 78 assertions, thirteen of them on this; `smoke_nodes.py` is 21
  states. **Still open, seen again on CB-2026-0002:** the client's name recorded as
  `claire` — the bot's own name written into the data. Rule 1b covers the reply text but
  not the extraction layer.

- **2026-09-08** — **Direct hire mirrors new hiring after sourcing, so the shared steps
  are filed once instead of twice.** The agency's own line: *"No candidate sourcing,
  matching or interviews. Everything else — MOM submission, bond, insurance, medical,
  embassy (overseas only), SIP, handover — mirrors New Hiring."*
  (A) **Those steps were invisible from inside a direct-hire conversation.** They were
  all filed under `new_hiring`, and the match function filters
  `service_type in (filter, 'general')`. Measured with the filter set to
  `direct_hiring`: *"what is an IPA"* **0.474**, *"do I need to attend a course"*
  **0.499**, *"what happens at the embassy after the IPA"* **0.551**, *"what happens
  when she arrives"* **0.702**. **Every one clears the 0.40 floor**, so `_answerable()`
  read True and the widening retry — which only fires BELOW the floor — never ran. The
  bot would have answered confidently from whichever `direct_hiring` row scored best:
  the top match for the arrival question was *"How long does a direct hire take?"*.
  A filter burying an answerable question is the 2026-09-03 defect again, except this
  time it does not even trigger the widening. Eight rows moved to **`general`**, the
  established catch-all (95 rows before this). After: **0.558–0.762**, and the
  sourcing/matching/interview rows verified as still NOT reachable from direct hire.
  Filed as `general` rather than duplicated under `direct_hiring` because duplication is
  how two copies drift apart (§9.8), and these are MOM steps that change for both
  services at once.
  (B) **The loader's "idempotent" claim held for exactly one run, and it inserted eight
  duplicates proving it.** The ROWS skip check keys on question + service_type; once a
  row moves to `general` it no longer matches the `new_hiring` its ROWS entry still
  declares, so the second run re-inserted all eight. Found by running the script twice
  rather than once. The duplicates were deleted (identical content, verified before
  deleting), and `_RELOCATED` — derived FROM `UPDATES`, so the two cannot disagree — now
  makes the skip check look where a row was moved to. Two consecutive runs are now a
  no-op. **A script that claims idempotency must be run twice, not once.**
  (C) **`UPDATES` generalised** from `{question, service_type, answer}` to
  `{where, set, reason}`, so it can change any field. It re-embeds whenever `answer`
  changes — a row updated without that is still retrieved on its old wording — and
  skips when already applied, in either the old or the new bucket.
  (D) **A duplicate Settling-In Programme row I added was removed.** One already existed
  under `new_hiring`, better written; it was moved to `general` instead.
  (E) **The seven-day SIP deadline was taken back out of both rows that stated it.** The
  agency's flow gives seven days; MOM's requirement for a first-time helper is tighter,
  and a missed registration is a penalty **on the employer** — so this was an unverified
  regulatory deadline stated as fact to the person who would pay for it being wrong.
  Replaced with "within the window MOM allows", which is true whatever the number turns
  out to be and costs the client nothing, since we register her either way. Put the
  figure back once Ming Hwee confirms it.
  `selfcheck_flows.py` is 65 assertions, five of them fixing the sharing boundary and
  the relocation map in place.

- **2026-09-08** — **Direct hire: the document checklist, the full process, and the
  route branch that decides the timeline.** The format work was already done that
  morning (`asks_for_process`, `PROCESS_INSTRUCTION`, the clamp and `allow_steps`), so
  this is content plus one guard.
  (A) **13 knowledge-base rows.** Four cover documents — what the employer provides,
  what we prepare for signature, what comes from the helper, and the extra a helper
  already in Singapore needs — and nine cover the process: the six steps, what happens
  after the client confirms, the two timelines, the two post-approval routes, the bond
  and insurance, the Settling-In Programme, and the handover. Rewritten from the
  client's side as before: the source routes a ticket to sales/admin, warns staff that
  a filing error costs two weeks, and names an internal owner per step, none of which is
  the client's business. Retrieval through the real path: **0.595 to 0.777, all 17
  probes above the floor**.
  (B) **`How long does a direct hire take?` was corrected, not duplicated.** That row
  said outright *"There is no fixed timeline for a direct hire"* — written on 2026-09-07
  because none had been given and inventing one would have been binned by
  `ungrounded_figures`. The agency has now supplied it, so leaving the old row in place
  would have put a flat contradiction in front of the model, which quotes either. The
  loader gained an **`UPDATES`** list for exactly this: keyed on question + service_type,
  it rewrites the answer, the content and **the embedding** (re-embedding matters — a row
  updated without it is still retrieved on its old wording), skips when already correct,
  and requires a stated `reason` per entry. It is for facts that have changed or arrived,
  never for rewording.
  (C) **The branch is where she is, and unlike the passport branch it changes the
  TIMELINE.** A helper already in Singapore on a valid permit skips the embassy and the
  flight (2 to 3 weeks); one overseas goes through both (4 to 6). Both routes are filed
  under `direct_hiring`, so the service filter does not separate them and whichever
  phrasing scores best wins. Quoting "2 to 3 weeks" to an employer whose helper is still
  in Manila is a delivery date they will plan around. `_LOCATION_DEPENDENT` +
  `_known_helper_location` mirror the passport `nationality_note`, with **timing words
  added to the pattern** — that is the whole reason it is a separate regex.
  `direct_hiring` already asks `helper_location`, so **no new question was added**.
  Verified by capturing the instruction actually built: present for a timing and a
  process question with the location unknown, absent once it is known, absent on an
  ordinary answer, and absent for another service.
  (D) **No insurance minimum was written into any row, deliberately** — see §9.14. The
  source states medical insurance at $15,000/yr, which is the pre-October-2023 figure,
  while Ming Hwee's own Service Agreement says $60,000. Picking a side in a legal
  minimum is not this repo's call, so the rows say "MOM's minimum coverage" and defer
  the figure. The vetting script asserts no row states one.
  **Also carried over without a figure:** the MOM application fee. **Carried over with
  one:** the $5,000 security bond, already documented as quotable — phrased to keep it
  clear of `quotes_hiring_package_cost`, which fires on a package term within 90
  characters of a figure ("cash deposit" beside it would have swapped the whole reply
  for the deferral line).
  `selfcheck_flows.py` is 60 assertions; `smoke_nodes.py` is 19 states.
  **Worth confirming with Ming Hwee:** the seven-day Settling-In Programme window in
  their flow. MOM's own requirement for a first-time helper is tighter than that, and
  the rows repeat the agency's figure.

- **2026-09-08** — **The new-hiring document checklist and the full hiring process,
  and the format needed to deliver them.** The agency supplied both. Loading the content
  alone would not have worked: **three separate mechanisms silently prevented a stepped
  answer from ever reaching a client**, and each had to be found by running the code.
  (A) **18 knowledge-base rows** via `load_service_notes.py` (idempotent on question +
  service_type; the 22 existing rows were skipped, not rewritten). Five cover documents —
  what the employer provides, what we prepare for signature, what comes from the helper,
  the foreign-employer set, and the additional-helper proof-of-care set — and thirteen
  cover the process: the five stages, matching, interviewing, what happens after the
  client confirms, what the employer personally has to do, the IPA, the EOP, the embassy
  stage plus one row each for PH/ID/MM, pre-departure, and arrival. Retrieval measured
  through the real path (`_search_query` + `_service_filter`, not `search()` — calling
  `search()` directly is the mistake that produced three bogus 0.000 readings on
  2026-09-07): documents 0.598–0.796, process 0.434–0.796, all 21 probes above the 0.40
  floor, and the two controls unmoved (passport process 0.444, cost 0.460).
  **Rewritten, not copied.** The source is staff-facing: it names an internal owner for
  every phase, the internal system, the page count of the MOM form and a retention
  target. None of that may enter the KB, because whatever is in the records is what the
  model quotes — the recorded failure is a "what's the process" question retrieving the
  internal pipeline brief and the bot reciting our own workflow to the person it is being
  run on. A vetting script asserted all 40 rows before they were loaded: no internal
  vocabulary, nothing over `rag_max_chunk_chars`, nothing tripping
  `quotes_hiring_package_cost`, no ungrounded figure, and **no duration in any new_hiring
  row** — the source gives none, so any would be invented and `ungrounded_figures` would
  bin the whole reply. The MOM application fee is described **without its amount** on
  purpose; the $5,000 security bond is carried over, being already documented as
  quotable.
  (B) **Two sentences cannot answer "what is the process".** `response_generator` clamped
  to 2 and asked the model for "one or two sentences — answer the question and stop", and
  `max_tokens=120` truncated anything longer regardless. `asks_for_process` +
  `PROCESS_INSTRUCTION` now widen that turn to a numbered list, on both answering paths
  (`response_generator`, and `blocked_topic_responder` via `PROCESS_ADDENDUM`, which
  explicitly does not reopen the parked topic). **Both halves of the trigger are
  required** — the detector AND retrieved records — because a long reply improvised from
  nothing is the worst of the three outcomes; verified, a process question with no
  records still gets the holding line.
  (C) **`clamp_reply` counted a list marker as a sentence.** `re.split` on `[.!?]\s+`
  ends a sentence at the `1.`, so a six-step answer scored twelve sentences and half of
  it was deleted. It now masks the marker while counting, and **slices instead of
  re-joining on `" "`** — the old join flattened every newline out of a clamped reply,
  precisely on the replies that most needed line breaks. Prose clamps exactly as before.
  (D) **`looks_like_document` binned every stepped reply and handed it to a human** —
  `_MARKDOWN` matches `^\s*\d+\.\s+`, so the numbered list this change exists to
  produce was read as a document dump. `allow_steps=` now drops that one branch for the
  callers that asked for a list; headings, bold and bullets stay banned everywhere,
  including on the stepped path.
  (E) **`asks_for_process` rejected the most natural phrasing of its own question.**
  "the full process **for** hiring a helper" matched the verb-usage exclusion on `for`.
  A determiner settles noun-vs-verb and the following word does not, so `_PROCESS_AS_NOUN`
  is tested first. 15 process phrasings fire, 12 ordinary messages stay quiet.
  **(D) and (E) were both caught by `smoke_nodes.py`, not by review** — on the run that
  first executed `response_generator`. Which is the real lesson: **this file had covered
  exactly one node**, so both reply-writing paths had no execution cover at all, while
  this change introduced a `process_question` flag read in three places — the same shape
  as the 2026-09-04 `UnboundLocalError` that silenced the bot. `smoke_nodes.py` is now 17
  states across three nodes; `selfcheck_flows.py` is 54 assertions.
  **Known and deliberately not changed:** "what is the process" under `new_hiring` still
  ranks the older 2026-09-07 requirements row top (0.473) ahead of the five-stages row
  (0.433). Both are inside the top 5 the model receives, and the stepped instruction
  orders it to lay out stages in sequence, so the fuller row is what gets used. Rewriting
  a row the agency signed off a day earlier was not in scope.

- **2026-09-07** — **Six defects from the agency's own testing round, five of them
  prompts or routing instructing the bad behaviour outright.** (A) **"(MDW)" after every
  mention.** `style.py` said *"use the format: Name (MDW). This is standard Ming Hwee
  practice"* with no scope, so a direct-hire intake said "Ruru (MDW)" six times — it reads
  like a case file being processed, not a person. First mention only now. (B) **A cost
  question started a second intake inside somebody else's flow.** `fee_enquiry` and
  `salary_enquiry` are real services with their own two fields (`nationality`,
  `care_type`), and `route_after_rag` sent every money question to the collector. Live:
  mid passport renewal, *"Ok and what is the cost"* came back *"I'll confirm the exact cost
  and come back to you. What kind of care would this be for?"*; the client asked *"Care??"*,
  the field was still empty so it asked **again**, and he wrote *"But I come here for
  passport renewal not for care"*. The same defect, after a finished hiring intake,
  produced *"For an Indonesian helper, the approximate salary is $550 to $600 ... Which
  nationality are you looking at?"* — `salary_enquiry`'s own `nationality`, asked in the
  same sentence as the answer that used it, because the hiring flow had stored it as
  `preferred_nationality`. A money question now routes to `response_generator` whenever any
  other service is in hand; a money question that IS the whole conversation still collects.
  (C) **The intent's name is not the question's subject.** The 2026-09-07 `other` fallback
  fixed one label and left three: `process_question`, `document_question` and
  `general_question` describe the SHAPE of a question, so `_search_query` tagged "what is
  the process" with "(process question)" — itself. Measured live at **0.394**, under the
  0.40 floor, so `_answerable()` read False and `blocked_topic_responder` answered *"a live
  agent is handling the direct hire process"* to a question the records answer at **0.680**.
  The identical words landed on `other` in a passport renewal the same afternoon and were
  answered in full — same question, opposite outcome, decided by a label. After: 0.680
  (direct hire), 0.507 and 0.579 (documents), 0.442 (new hiring). `greeting`/`smalltalk`
  still search bare, and the money intents stay out — money IS a subject.
  (D) **A care type was invented and filed as fact.** `requirement` was never asked in a
  25-field hiring intake (the collector opened on question six, 17 fields outstanding) yet
  the ticket read **"Care type: household chores"**, from an opening message that said only
  *"I want to hire a helper"*. `_states_a_care_type` existed for exactly this but was
  applied to the extracted VALUE — "household chores" has real content, so it passed. It is
  now applied to the CLIENT'S MESSAGE too, when the field was never asked: the bare enquiry
  fills nothing, while *"I need someone for my mum who is bedridden"* still does. Matching a
  helper against a requirement nobody gave is worse than having no requirement.
  (E) **A question mid-collection stranded the question already on the table.** Asked *"what
  is the best number to reach Ruru on?"*, the client replied *"What is MDW"* — classified
  `general_question` with **service=None**, so routing found no fields, `response_generator`
  answered the definition and ended. Correct answer, pending question gone, and the client
  volunteered the number unprompted. A live, unparked collection now survives any turn that
  resolves to no service at all, so the collector answers AND asks. (F) **The languages
  question still showed three of seven options** — rewritten on 2026-09-04 precisely to stop
  that, and still trimmed, because `_field_guidance` told it to: *"dropping two or three in
  as examples is how a person asks it. Never read the whole set out."* Two prompts pulling
  opposite ways and the general one won. Where the field's own written question already
  names 3+ of its options, they are now all named — a Tamil-speaking household shown three
  Chinese and Malay options can only conclude we do not place Tamil speakers.
  `selfcheck_flows.py` is 36 assertions. **Not a bug, recorded so it is not re-raised:**
  two tickets appeared to vanish mid-session (three tickets issued the number CB-2026-0003).
  `reset_conversation.py` deletes `cb_tickets`/`cb_handovers` for the conversation and was
  run between scenarios; ticket numbering is max+1, so a freed number is reissued. Nothing
  is lost in a real conversation, where tickets are never deleted.

- **2026-09-07** — **The FDW passport renewal process flow: documents, the embassy
  contract branch, and a guard against answering for the wrong nationality.** (A) **The
  document list existed nowhere.** This had been the open gap since 2026-09-04 and was
  measured at **0.000** the same morning the process rows went in — *"what documents are
  needed"*, asked under `service=passport_renewal` with `nationality=PH`, matched nothing
  in the entire knowledge base, so the single most practical question about a renewal got
  the holding line. Eight rows now carry it. After: 0.000 → **0.557**.
  (B) **The branch is the embassy contract, and it is decided by nationality, not by
  asking.** Philippines and Indonesia hold one; **Myanmar does not**, so three further
  forms — Undertaking of Employer, Standard Employment Contract, Information Sheet of
  Employer — are signed before anything is submitted. The visit differs too: a Filipino
  helper reports to the embassy **herself** and meets the runner there, while an
  Indonesian or Myanmar helper is collected from the employer's home and brought back.
  `passport_renewal` already collects `nationality`, so **no new question was added** —
  asking an employer whether their helper holds an embassy contract would be exactly the
  interrogation the agency objected to on 2026-09-04.
  (C) **The real risk was answering confidently for the wrong route**, and it is not
  hypothetical. The nationality filter is *dropped* when the nationality is unknown
  (deliberate — for most services a nationality-labelled row is still useful), so all
  three routes compete at once. Measured with nationality unknown: a bare *"what is the
  process"* returned the **Myanmar** row top at 0.472, and *"does someone go with her to
  the embassy"* returned the **Filipino** one at 0.609. Answer either to an employer of
  the other nationality and we have told them to prepare the wrong forms.
  `nationality_note` fires on a route-dependent question while `nationality` is unknown
  and tells the model to give only what is true for all three, say the documents depend
  on her nationality, and ask. Verified by capturing the instruction actually built: it is
  present for two process phrasings, absent once the nationality is known, absent on an
  ordinary answer, and absent for another service. The trigger is 10 phrasings firing and
  10 ordinary answers staying quiet. **`ungrounded_figures` is the second lock**: the
  source flow states outright that it gives the process and NOT a duration and warns
  against inventing one, so none of the eight rows contains a number or a duration word —
  asserted mechanically before they were loaded. The timings the bot may quote remain the
  separate 2026-09-03 rows from the agency's own timing table.
  `selfcheck_flows.py` is 27 assertions; `smoke_nodes.py` is 10 states, three of them new
  and covering this branch and the direct-hire flow.

- **2026-09-07** — **The agency's service process + timeline table, added end to end.**
  Purely additive: nothing was removed or reworded except where a question could not
  record the answer the table asks for. (A) **`direct_hiring` was an empty field list.**
  Not "no questions" — the intent routed to the collector, the collector found nothing to
  ask, `info_complete` fired on turn one, and the ticket that reached an agent said
  *"wants us to process a helper they have already chosen"* and nothing else. The agent
  restarted the conversation every time. It now collects the agency's own six — the
  helper's full name and number, her nationality, where she is (Singapore / home country /
  working abroad), her employment status, and when she can start — plus `full_name` and
  the update channel so the lead is contactable, and `notice_clearance` **gated on
  `_STILL_EMPLOYED`**: a helper already home has no notice to serve, and asking reads as
  though we did not listen. Keys are `helper_`-prefixed so none collide with the candidate
  flow's own `nationality`/`availability`, which mean the opposite person. (B) **`new_hiring`
  gained `home_size`** — "house type, bedrooms and bathrooms" in the table, and `home_type`
  only ever captured the first: a 5-bedroom landed house and a 2-bedroom condo are the same
  `home_type` answer and completely different jobs. Optional, asked once, portable. 24 → 25.
  (C) **`hire_source` widened from two options to four.** The table asks for the preferred
  *experience* type — first-timer / ex-Singapore / ex-abroad / transfer — and *"transfer, or
  a new hire from overseas?"* could not record it, because "new hire from overseas" collapses
  a helper who has never left home with one who has worked two contracts in Hong Kong.
  Different people, different salaries. Same key, so nothing downstream moved.
  (D) **Nine knowledge-base rows** via `scripts/load_service_notes.py` (idempotent on
  question + service_type; the five 2026-09-03 rows were skipped, not rewritten). The
  process half was simply absent: passport renewal held timings with no steps, and new
  hiring and direct hiring held nothing at all, which is the gap that produced a holding
  line in testing three times. Written from the client's side of the desk — the recorded
  failure here is a "what's the process" question retrieving the internal pipeline brief
  and the bot replying *"The process involves three main stages: first, we capture your
  requirements and match you with suitable candidates"*, our own workflow described to the
  person it is being run on. (E) **`_search_query` now falls back to `service_type` when
  the intent is `other`.** This was the real blocker and it is measured: three questions
  into a passport renewal, *"what is the process"* scored **0.000 filtered AND 0.000
  unfiltered** — no match at all, so even the widening retry had nothing to widen to. The
  subject was never missing, it just was not in the intent; `_service_filter` two functions
  down had trusted `service_type` for this exact reason all along. After: new hiring 0.473,
  passport renewal (MM) 0.472 returning the Myanmar steps, direct hire 0.661, transfer
  0.479. `greeting`/`smalltalk` are deliberately **not** included — they are not questions,
  and biasing them would go looking for an answer nobody asked for; an `other` with no
  service in flight still searches bare (verified 0.000, holding line). `selfcheck_flows.py`
  is 23 assertions now. **Still missing from the KB and not fixable in code:** the passport
  renewal **document list** ("what documents are needed" still scores 0.000) and the
  **agency fee** for passport renewal and work permit renewal.

- **2026-09-04** — **Two tone fixes, both prompts I had written badly.** (A) **The
  opening no longer promises a consultant.** `COLLECTOR_INTRO_NOTE` made Claire say *"and
  I'll bring in one of our consultants whenever needed"* on the first message; the client
  called it weird, and they are right — nobody has asked for a human and nothing has gone
  wrong, so offering one unprompted is hedging before the conversation starts. Rule 1
  already carries that line for when a client asks what she is, and a real handover is
  announced when it happens. The introduction is now the AI disclosure alone. (Thomas's
  original spec asked for both halves in the greeting; this drops one of them, so say if
  it should go back.) (B) **A question no longer lands with nothing said to the person who
  just answered.** *"Her name is Shushi"* → *"Which country is Shushi's passport from?"* →
  *"When does her passport expire?"* is three questions in a row and reads as a form
  advancing a field. `COLLECTOR_INSTRUCTION` was the cause: its no-echo rule ended with
  *"Most of the time just ask. If a short reaction is genuinely warranted..."*, which the
  model read as "do not acknowledge" — and it overrode `style.py`, which has said all
  along to vary "Noted"/"Got it"/"Sure"/"Okay". Rewritten to require a short varied
  acknowledgement and to name the actual ban: **repeating their answer** ("Noted, her name
  is Shushi. Which country...") is out; **reacting to it** ("Got it — which country is her
  passport from?") is what we want. `strip_repeated_opener` already enforces the variety,
  so the guard and the prompt now pull the same way instead of opposite ways.

- **2026-09-04** — **An empty extraction was being retried as a parse failure, and the
  opening message said three things where two would do.** (A) `complete_json` tested
  `if parsed:` — and `{}` is falsy. The extractor returns `{}` whenever the client's
  message fills no field, which is most of a conversation, so a **correct** result was
  logged as `JSON-mode response was unparseable` and the entire call re-run. Live: the
  warning fired on 5 of the first 6 turns of one passport renewal, doubling the LLM cost
  of each of those turns for an identical empty answer. Not a Luna problem and not a JSON
  problem. `try_parse_json()` now returns `None` for a genuine failure and `{}` for "it
  said nothing", and `complete_json` only retries on `None`. (B) The first message was
  *"Hi Vaidik, I'm Claire, Ming Hwee's AI assistant, and I'll bring in one of our
  consultants whenever needed. Passport renewal timing depends on your helper's
  nationality and embassy. May I know your helper's name?"* — the introduction, a
  briefing, and the question. The middle sentence said nothing, because on turn one we do
  not know the nationality it depends on, and the next thing we did was ask for it. The
  small-ticket briefing now waits one turn (`brief_on_turn`, exactly one turn per
  conversation either way), and the note tells it to **say nothing rather than something
  amounting to "it depends"** — if the records make the answer conditional on something
  we have not been told, there is no answer to give yet.

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
