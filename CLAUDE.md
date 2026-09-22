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
| ...and a cost question that NAMES a service is answered, not qualified | `intent_classifier` (the money rule) + `_named_service` | The door the row above leaves open: it keys on another service being ESTABLISHED, and here the service is named in the very message asking the price. The SERVICE moves and the INTENT deliberately does not — that is what routes it to `response_generator` instead of opening the named flow's intake. |
| A passport phrasing is not the work permit's price | `_NAMED_SERVICE` order (`passport` before `renew`) | $450 against $695, both in `FEE_STATED_SERVICES`, so the wrong one goes out STATED rather than deferred. `ungrounded_figures` cannot catch it — $695 is genuinely in the records. The table got this right for insurance and wrong for passports for a fortnight. |
| A price question that names nothing still has a subject if the conversation had one | `rag_retriever._subject_service` | "The whole conversation" holds for an opening *"how much do you charge?"* and is false for a repeat after the handover, where the query went out bare and returned three chunk rows with no fee at **0.436 — above** the floor. Retrieval only, like `_RETRIEVAL_ALIASES`: nothing re-parks the topic. `salary_enquiry` stays out, as it does from `_SUBJECTLESS_INTENTS`. |
| The subject is tagged in the words the records use | `rag_retriever._RETRIEVAL_LABELS` | `renewal` is the one key that does not read as itself — of WHAT? — while every row says "work permit renewal". Better on 5 of 5 renewal probes, worse on none (+0.197 on the process, +0.243 on the timing), and on the money path it is 0.399 (**under** the floor, so `weak_retrieval` discards the reply) against 0.558. |
| A client restating a question is asking it, not chasing | `guards.asks_again` + `ASKED_AGAIN_NOTE` + `asks_general_info` | Their phrasing is usually not interrogative at all, so every existing detector missed all six live phrasings. One definition, read by both answering paths. Gated on having records, so it can never turn an honest "I don't know" into a figure. Sits BELOW the chase test: *"still waiting"* overlaps genuinely and stays held. |
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
| No flow asks for a HELPER's name while reading the client's own off WhatsApp | `ticket.NAME_FROM_RECORD_ONLY` + `selfcheck_flows.py` | Adding the field alone changes nothing: `_with_push_name` fills it from the profile and the question is skipped before it is asked. |
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
| An adjective or an imperative does not hide the question on a parked topic | `blocked_topic_responder._GENERAL_INFO` | "what is the **further** process" and "tell me the process" both fell through, so a home leave with the process in the KB was handed to a human. The same shape as the missing "the" below. |
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
| A greeting is greeted back, and an announced question is told to go ahead | `guards.greeting_only` + `response_generator.greeting_back` | `has_no_request` covers two shapes and both got the string written for the second: *"Hi"* was answered *"Sure, go ahead. What would you like to know?"* Go-ahead answers a request to ask; to a greeting it presupposes the intent. The name comes from our RECORDS, and nothing of their file is read back - a greeting is not the place to prove we remember them. |
| ...and the PROMPT is what had to change, not just the canned string | `system.build_system_prompt` (the stage line) | Most greeting turns are answered by the model, and rule 7 plus the stage line both said *"do NOT greet again"* on every turn with history - right when they asked something, wrong when their whole message IS the greeting. One definition in `guards`, read by the node that picks the reply and by the prompt that tells the model whether it may greet at all (§9.8). |
| ...and a client who greets twice is still only greeting | `guards.without_greeting` | *"hello, good morning"* left *"good morning"* after one strip, which matches no announcement, so it read as a real request. Stripped in a loop now. |
| Nobody is asked WHICH RELATIVE referred them | `_REFERRED_BY_STAFF` (was `_WAS_REFERRED`) | Live: *"no , family member"* -> *"Who in your family referred you to Ming Hwee?"*. The gate opened on any referral at all, and the name was useless to us either way - a friend's or a relative's is somebody we hold no record of. OUR OWN STAFF is the case that survives, because that name is on our own payroll and the referral is credited to them. |
| ...and the excludes are what the mixed phrasing needs | the same gate | *"my friend who works near your office"* matches `your office` and is still a FRIEND's name. `excludes` is checked first, which is the shape the class docstring is written about. |
| A reason is written in the register of the office | `_WHY_WE_ASK` + the register rule in `_field_guidance` | *"so there are no surprises later"* went out live and the agency objected: *"sounds too casual"*. `additional_notes`' reason ended *"rather than discovered later"*, and that trailing clause is what the model compressed. A reason names what we DO with the answer; `helper_religion` had the same *"rather than X later"* tail and nobody had reported it - the sweep found it, which is why the rule is derived over the table. |
| ...and the rule may not hand the model a clause to reuse | the same rule, examples REMOVED | Its first draft quoted two good reasons as examples, and both leaked: the PETS question came back *"...and agree the house rules before they start"* and religion came back asking about house rules too. §8's rule about naming a thing in the prompt, one register along - the fix states the shape and names only the phrasing to avoid. |
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
| A LID is never mistaken for a phone number | `parser._is_phone_jid` / `_counterparty` | `normalize_phone` splits on `@` and keeps the front, so `116909177569373@lid` became the "number" `+116909177569373` and an allowlisted client was stood down. The counterparty is resolved from the first identifier whose JID is actually a phone. |
| Every numbered item goes on its own line, on BOTH answering paths | `PROCESS_INSTRUCTION` / `PROCESS_ADDENDUM` (+ `SERVICE_BRIEFING_NOTE`) | The rule was written for the passport briefing on 2026-09-08 and never reached the two general paths, so a documents answer arrived as one paragraph with `1. ... 2. ...` buried in it — one message after a correctly formatted process answer on the same conversation. |
| A value that restates the REQUEST does not answer a preference field | `info_collector._PREFERENCE_FIELDS` / `_states_a_preference` | `replacement_preferences` was filled with "replace her", so it was never asked and the ticket read "Wants in the replacement: replace her". Same shape as the 2026-09-07 care-type defect, one field along. |
| Handing the choice back to us is not a preference | `info_collector._NO_PREFERENCE` | Anchored hard at `^`, so "whatever you want" matched and "you do whatever you want" did not. A short filler lead-in is now allowed; "want" is deliberately not in it. |
| A LID is resolved to its phone number before ANY lookup keyed on the number | `webhook._resolve_lid` + `whapi.resolve_lid` | `GET /chats/<lid>` carries `phone`; `/contacts/<lid>` does not. Done in the webhook because parsing is sync and this is an HTTP call. Unresolvable ⇒ stand down, never a guess at whose number it is. |
| A row nobody has claimed is the bot's to take; only a PAUSE silences it | `webhook._is_paused_for_agent` | The test was `not bot_should_reply(existing)`, which is also true for `bot_status='none'` — and `none` is what the PORTAL writes when it creates the row for a brand-new client, which it usually does first. So the branch returned before `get_or_create` could promote it, and the bot was silent on that number **forever**. The two gates disagreed: `may_engage` had already said engage. |
| `GET /chats/<lid>` is not the last word on a LID | `whapi._sweep_chats` | The same chat, the same minute: `/chats/95786411008174@lid` → `{"type":"unknown"}` with no phone, `/chats/917354708111@s.whatsapp.net` → that chat **with** its number. We cannot use the second form — the phone is what we are looking for — so a miss sweeps the chat LIST, which carries the resolvable record. One sweep learned **2,544** mappings, so it is rate-limited and serves every LID on the channel at once. |
| An unresolvable LID is not handled at all, not merely not answered | `webhook._resolve_lid` returns False + `handle_payload` skips | It used to log "standing down" and then carry on holding the pseudo-number. Inbound the allowlist hid that; **outbound has no allowlist**, so `handle_outbound` called `get_or_create` and minted a conversation keyed on a LID — 145 rows on the live database by 2026-09-14, one of them a second thread for a client who already had one. |
| Cooking is a duty she is asked whether she will take on, not a thing assumed of her | `SERVICE_FIELDS["candidate_new_hiring"]` + the `work_scope` gate | A helper who had just said she does childcare and eldercare was asked what cooking she can do, and objected. The detail question survives for the pairing with the employer's `cooking`, gated the way `pet_detail` is off `pets`. Gated on `work_scope` and NOT on the duties answer: `Gate` matches substrings, so "no i dont want to cook" contains "cook" and opened it. |
| A registration explains what happens next before it hands over | `ticket.BRIEFING_AFTER["candidate_new_hiring"]` + `CANDIDATE_BRIEFING_NOTE` | An employer finishing a passport renewal is told what happens next; a helper who had just answered seventeen questions was thanked and handed over. |
| ...and a job seeker's closing message is not the one written for a buyer | `templates.CANDIDATE_BRIEFING_NOTE` + `rag_retriever.CANDIDATE_BRIEFING_QUERY` | `SERVICE_BRIEFING_NOTE` REQUIRES a cost section, so pointed at a registration it quoted the passport renewal's **$450** as the price of applying for work. `ungrounded_figures` binned it and it was logged as a lost briefing, so she got the bare handover line. The word `cost` is also absent from her query: it matches `_PRICE_QUESTION`, which DROPS the service filter, which is how $450 was in reach at all. |
| A candidate flow searches the candidate's shelf, whatever the master record says | `rag_retriever._retrieval_audience` | `effective_contact_type` puts an employers row above one message, rightly. For RETRIEVAL that hid all 27 helper-facing rows from a job seeker whose number we hold as an employer, and "what is the process" came back at **0.397** — four thousandths under the floor. As a candidate it is 0.437. Retrieval only; the lead, the ticket and the contact type are untouched. |
| A row that narrows its audience says so in its heading | `selfcheck_flows.py` | Was "everything except the transfer checklist". The helper's journey rows are the second set to narrow, and a list of exceptions has to be edited every time — which is how a tripwire stops being read. |
| A candidate's own name is never read off her WhatsApp profile | `ticket.NAME_FROM_RECORD_ONLY` + `selfcheck_flows.py` | The fifth arrival of the same complaint, and the derived rule could not catch it: that one sweeps `EMPLOYER_LEAD_SERVICES`, and a job seeker is not in it. `get_record_name` reads `employers`, so `record_name` is always empty for a helper and the question is always asked — which is right, because this name goes on a Work Permit application. A name she gave on an earlier enquiry still greets her, off `leads_candidate`. |
| Every question the employer is asked ABOUT a helper has a counterpart she is asked about herself | `selfcheck_flows._MATCHED_PAIRS` + `ticket._matched_options` | Or the consultant matches the two tickets by eye. The employer was asked 25 questions about the helper they want; she was asked 9 about herself, six of them identity and logistics. Nine of the eleven pairings had no candidate half at all. **One pairing was dropped on 2026-09-11** — `additional_notes` → `candidate_notes` — because the agency had her half removed by name; it is written in the table as a decision rather than deleted. |
| ...and both halves of a pairing offer the SAME words | `ticket._matched_options` | Taken from the employer's `Field`, never retyped, for the reason `_hiring_field` exists (§9.8). `preferred_nationality` is the one pair that cannot share a *list* — the employer picks a nationality (`Filipino`) plus "no preference", the helper names a country (`the Philippines`). **Same three countries since 2026-09-11**; until then she was asked an open question, and the note here argued that constraining her would turn a Sri Lankan applicant away. It now does exactly that, by instruction. |
| A candidate key never reuses a portable employer key | `selfcheck_flows.py` | `languages` and `budget` are in `_PORTABLE_ACROSS_SERVICES`, so reusing them would carry an employer's "Mandarin spoken at home" into a helper's file as a language SHE speaks. The direct-hire flow avoided the same trap with its `helper_` prefix, from the other side. |
| No bracket is read out on ANY service, not just the seven | `selfcheck_flows._reads_a_bracket` | The sweep was written over the agency's seven employer services, so a candidate flow was outside it entirely — the same "written for the set that was reported" shape the row itself was created to fix. |
| A documents question is recognised however it is phrased, and by BOTH paths at once | `guards._DOCUMENTS_QUESTION` / `asks_for_documents` | One definition, read by `asks_for_process` (may this reply be a list?) and by `asks_general_info` (does a parked topic answer at all?). They disagreed about the same sentence: "Tell me the documents I needed" matched neither, and "what are the documents I required" matched neither, so the one turn that worked was the classifier happening to return `document_question`. |
| A fee question survives being asked in the plural | `blocked_topic_responder._GENERAL_INFO` | `(?:fee|cost|charge)\b` cannot match "fees" — there is no word boundary inside it — so "is there any fee" was answered and "Is there any fees" was handed to a human. Third time a one-word gap in this pattern has cost a client an answer. |
| A new hire's cost is refused on ALL THREE paths that write a reply, not two | `blocked_topic_responder` (+ `info_collector`, `response_generator`) | The missing one is the path used once a topic is parked — i.e. exactly when a consultant already has it, which is what the rule is about. Live: with a hiring ticket parked, "Is there any fees I need to pay" returned **$4,225 / $4,285**. Every other guard passed it correctly; those figures ARE in Form A. Grounded is not sanctioned. |
| ...and that is derived from which nodes ground a reply, not from a list of three | `selfcheck_flows.py` + `smoke_nodes.py` | A node that runs `ungrounded_figures` over a generated reply is a node that sends one. The import check alone is not enough — it stayed green with the guard disabled outright, so `smoke_nodes` now asserts the REPLY on that state. |
| A job seeker is told plainly that she pays us nothing | `general` + `candidate` KB row, agency 2026-09-10 | And it does not deny the placement loan she can also retrieve: it says she pays **Ming Hwee** nothing and routes any loan question to a consultant, which is what the loan row itself instructs. No figure appears in it. |
| Her closing message says the registration is finished before it lists the steps | `CANDIDATE_BRIEFING_NOTE` | She answered "Here" to the update-channel question and the next thing she read was the hiring process, so she asked "why did you tell the process" — and the bot then apologised and disowned its own correct closing message. |
| The number the agency answers on is CONTENT, not configuration | `load_service_notes.TEXT_REPLACEMENTS` + `selfcheck_flows.py` | Changing the WhatsApp number is one `.env` line; 14 knowledge-base rows named the old one, 13 of them helper-facing, including the emergency and abuse-reporting rows. Swept from the loader's own needles, so a file nobody thought to check cannot keep a replaced string alive. |
| A raw imported chunk can be corrected too, not just a Q&A row | `load_service_notes.TEXT_REPLACEMENTS` | `UPDATES` keys on `question` and a `document_chunk` row has none — §9.15 predicted this gap and said it would need a chunk-level path. Finds rows by SEARCHING for the old text, so a second run is a no-op, and re-embeds, because a row edited without that is still retrieved on its old wording. |
| ...and a specific rewrite is never shadowed by a general one | `selfcheck_flows.py` | The rules run in sequence, so a general needle placed first rewrites the text the specific rule was looking for and that rule silently never fires. Directional, and the check had it backwards on its first run — it flagged the correct arrangement. |
| A job seeker is registered only if she is from one of the three countries we recruit from | `ticket.nationality_state` + `info_collector` (`UNPLACEABLE_NATIONALITY_NOTE`) | The Philippines, Indonesia, Myanmar — named in the question, which constrains the answer space so the extractor has something to map onto. Anyone else is told plainly, and **not** handed to a human: "we do not recruit from your country" is an answer we hold. |
| ...and the question does not then offer a fourth | `Field.options_are_exhaustive` + `info_collector._field_guidance` | The one closed option set in the codebase. Every other one is EXAMPLES — `languages` exists in its current form because the bot hid four of its seven from a Tamil-speaking household — so the guidance ends by saying the client may give something not on the list. On her country that invites the single answer the next turn has to refuse. |
| ...and an answer nobody recognises is asked again, never declined | `ticket._UNPLACEABLE_PATTERN` (a POSITIVE list) | The refusal fires only on a country we can name, never on "did not match the three". `Singapore` and `Hong Kong` are deliberately absent — a helper already here answers "which country are you from" with where she IS. A missed decline is a conversation a consultant closes; a wrong one is a woman told to go away who should not have been. |
| ...and the refusal is a conversation, not a canned line | `info_collector` (the branch runs every turn) | `nationality` lives in the checkpoint, so "why?", "can you still help me" and "I want a job" land in the same branch and are answered against the history instead of drawing the refusal a second time. A correction ("I meant I am IN India, I am FROM Indonesia") needs no special case at all — the field updates and the registration carries on. |
| The salary a HELPER is asked for is in SGD | `expected_salary`'s question | Only on her side: an employer reading "$600-700" is in Singapore and cannot mean anything else, while she is answering from Manila or Jakarta. The bands are still the employer's own, so the pairing still matches. |
| Nothing administrative follows the last question about her | `SERVICE_FIELDS["candidate_new_hiring"]` | `candidate_notes`, `update_channel` and `email` removed 2026-09-11 — she is messaging us ON WhatsApp, so the channel is not a question. The salary answer goes straight into the closing briefing. Asserted as what the flow must NOT contain. |
| The full step-by-step is the CLOSING message, never a preview of it | `templates.CANDIDATE_PROCESS_COMES_LAST_NOTE` | Asked "the further process" at the last question, she got a compressed out-of-order version with the next question tacked on, then the real briefing a turn later — told twice, first telling wrong. A DOCUMENTS question is excluded: that one is answerable at any point (2026-09-10). |
| A service the KB has never been labelled with searches under the label it HAS | `rag_retriever._RETRIEVAL_ALIASES` | `transfer_employer` is not a `service_type` any row uses, so the filter narrowed it to `general` forever. Retrieval only — the ticket, the lead, the field list and the **blocked-topic key** all still see `transfer_employer`, which is what keeps a new transfer off a parked hiring topic. |
| The prompt never names a branch we do not have | `IDENTITY` + `selfcheck_flows.py` | *"Ming Hwee operates three branches — Jurong (HQ), Tampines, and Woodlands"* was hardcoded, and `AGENCY_INFO_INSTRUCTION` licenses the model to state the identity block plainly. The `branches` table holds ONE row, `CHINA TOWN`; the Client Service Agreement names one registered address. So the bot volunteered two branches that do not exist and was then asked for one's address. It now says we have one office and **names no other** — denying them by name puts the name in the context, which is §8's non-Latin-script rule applied to a place. |
| Where we are is answered, never handed over | the six `general` office rows + `_GENERAL_INFO` + `AGENCY_INFO_INSTRUCTION` | A client asking where to come is asking the one thing answerable on the spot, and they act on it by getting on a train. All three paths carry it: an ordinary turn, an `agency_info` turn, and a topic parked with an agent. |
| The address is in the RECORDS, never in the prompt | `selfcheck_flows.py` | `ungrounded_figures` grounds on the retrieved records and on what the client said — **never on the identity block** — so a postal code stated from the prompt is binned and the client gets the holding line. Asserted both ways: no digit of it in either instruction, every part of it in the rows. |
| An agency question is searched for what it asks, not for its own label | `rag_retriever._search_query` | `agency_info` names the SHAPE of a question, so tagging it `(agency info)` is the 2026-09-07 `process_question` defect — it put the office rows outside the top 5 and served clause 6 of the service agreement at 0.425. Searched **bare**, and deliberately not via `_SUBJECTLESS_INTENTS`: that set makes the in-flight service the subject, and "where is your office" during a passport renewal is not about the passport renewal. |
| A care type is never INFERRED from a message that named none | `info_collector._mentions_care` (message, additive) + `_states_a_care_type` (value, subtractive) + `smoke_nodes.py` | The two tests answer different questions and one shape cannot serve both. Subtractive on the message meant "contains a non-filler word", which was true of all eighteen messages in the live transcript — `10`, `HDB`, `600`, `google` — so `requirement` was filled with "general housework" nobody said, never asked, and `children_detail`/`elderly_detail` closed with it. Fails towards asking: a missed volunteered care type costs one question, an invented one costs a wrong match. |
| ...and the RUN of it is checked, not just the predicate | `smoke_nodes.py` (`_stub_extraction` / `_expect_not_collected`) | The predicate was correct the whole time; the call site was not. Asserting the predicate left every check green with the broken call restored — the 2026-09-10 "imported and never called" hole. |
| No flow anywhere takes the client's own name off their WhatsApp profile | `ticket.NAME_FROM_RECORD_ONLY` + `selfcheck_flows.py` | `new_hiring` was the last one out, and joined 2026-09-16: *"why chatbot is not asking the user name like before"*. The set is now every flow that collects a name, so this is a derived rule rather than a list — a new flow with a `full_name` field and no entry fails by name. |
| The salary bands say SGD on both sides of the desk | `budget.options` → `_matched_options("budget")` | The currency is on the BANDS, not just the question, because `_field_guidance` reads the options into what is actually asked — live that produced "such as below $500, $500-600" with no currency named. The digits are untouched: they are the grounding `ungrounded_figures` reads (2026-09-09 D). |

| Claire introduces herself whichever instruction wins the turn | `templates.FIRST_CONTACT_INTRO_NOTE` + `response_generator` + `smoke_nodes.py` | A first message that is a PROCESS question swaps the whole instruction for `PROCESS_INSTRUCTION`, which says nothing about introducing yourself — so the client never learned what they were talking to. Appended AFTER the template is chosen, because the instruction that loses rule 1 is always the specialised one (2026-09-04 was the collector, 2026-09-17 the process path). |
| The reason for a run of questions is never bolted onto the first one | `info_collector.PURPOSE_NOTE_RULES` | "May I know your name so we can recommend a helper suited to your household?" — a name does not help match a helper, and the client came straight back with "why will knowing my name help you". The note said "give the reason before you ask"; "X so that Y" reads as before-you-ask and is still wrong. A reason that does not survive being questioned is worse than no reason. **2026-09-19: "its own clause" was not enough** — a clause joined by a semicolon is still one, and that is exactly what went out. It is its own SENTENCE now, and the semicolon is named. |
| ...and the opening turn NAMES the service it understood, so a wrong reading is correctable | `PURPOSE_NOTE_RULES` ("NAMING WHAT YOU HAVE UNDERSTOOD") | The agency, 2026-09-19: *"hey i need helper"* → *"I'll ask a few details so we can understand your household…"* → *"how you know for what service i need helper"*. An inference the client cannot see is one they cannot correct. Put back as a short clause at the FRONT of the reason sentence, not as a sentence of its own - measured, a third sentence of preamble pushes the question out of the message entirely. |
| ...and states nothing else about them at all | `PURPOSE_NOTE_RULES` ("STATE NOTHING ABOUT THEIR SITUATION") + the `_COLLECTION_PURPOSE` sweep | *"their household"* in the reason is a decision about a client who has written four words. Derived over the whole table rather than fixed on `new_hiring`, so a reason added tomorrow that describes the client fails by name. |
| ...and the message still ends with the question | `PURPOSE_NOTE_RULES` ("ENDS WITH THE QUESTION") | All of that is framing for the question we are there to ask, and the first draft of it produced three sentences of preamble and no question - on the employer side AND the candidate side. Measured 0/4 before the line, 8/8 after. |
| Every employer intake closes by explaining itself | `ticket.BRIEFING_AFTER` | `new_hiring` was the last one without it, and the biggest: 22 questions and then the bare handover line, so the client asked the process, the documents, the timeline and the charges in four consecutive messages. Keyed on `start_timeline`, the last REQUIRED field - everything after it is optional, and an optional field a client declines is never filled. Adding the entry is also what REMOVES the opening overview, so no employer intake briefs at the start any more. |
| ...and the closing briefing is cost-GUARDED, not just cost-instructed | the completion `_write` (`withhold_cost=`) | It was the only `_write` call in the collector with no cost guard on it, and it is the one message that talks about price by construction - `SERVICE_BRIEFING_NOTE` requires a cost section. Four services in `COST_WITHHELD_SERVICES` now brief at the end, so four closing messages were relying on the prompt alone for a rule the agency gave by name on 2026-09-04. The same "wired into two paths and never the third" shape as `blocked_topic_responder` on 2026-09-10. |
| ...and a briefing swapped for the deferral is LOST, not delivered | `briefing_lost` | Marked given it would never be retried, and the client reads a sentence about the price and nothing about the process - the 2026-09-08 defect, in the branch added the same day as the guard above it. |
| ...and it asks for its own clock by name | `rag_retriever.BRIEFING_QUERY_BY_SERVICE` | Measured: under the general briefing query NO timing row came back for `new_hiring` - not in the top 10 and not in the top 18 - so the closing message had no lead time to give. Naming the clock ("from signing to her first day") puts all three timing rows in the set and lifts the whole set from 0.42-0.52 to 0.61-0.70. It deliberately drops "how much does it cost", which matches `_PRICE_QUESTION` and would drop the service filter (2026-09-10). |
| An answer to one question does not rewrite a DIFFERENT field that is already answered | `info_collector._ASKS_FOR_CARE` | *"In my family there are 8 peoples ... and 1 is elderly care"* - an answer to "who lives in your household" - rewrote `requirement` from *"childcare"* to *"childcare, eldercare"*. The two care-type guards beside it only run while a field has NEVER been asked, on the principle that once asked their answer is their answer; this is the case neither covers. A client who actually asks for the care still changes it. |
| ...so the workload warning cannot fire on a care type nobody asked for | the same rule | Measured: `_heavy_workload` fires on the rewritten value and does NOT fire on the one he gave. He was told one helper could not manage his household because of a service he never asked for, corrected it, and the advice stood - it is said once and never revisited. |
| A client who has asked for an experienced helper is not then offered a first-timer | `info_collector._already_wants_experience` | *"she should be 4+ year experienced"* → *"Are you open to a first-timer...?"* → *"yes i am open for first timer"*, and the ticket carried both for a consultant to ring about. The question is not dropped - where the experience was got is a real question nothing else asks - only the half already answered. `\d+ years OLD` is excluded by a lookahead: an age answer is not four decades of experience. |
| The closing briefing gives the SPAN or nothing, and never defers the timing to a person | `SERVICE_BRIEFING_NOTE` ("THE TIMING LINE IS A SPAN OR IT IS NOTHING") | Measured on new hiring's new briefing: 3 runs of 4 gave "approximately 4 to 6 weeks" and the fourth wrote *"The timeline will be confirmed by our agent based on your requirements and selected helper"* - the "it depends" sentence the agency had removed from the OPENING overview on 2026-09-17, arriving at the other end of the flow, because the rule lived only in that note and `new_hiring` no longer gets one. A record saying there is no single answer is our filing explaining why the question is hard, sitting beside a row that answers it. 9 runs of 10 after. |
| ...while the COST may still be deferred where we hold none | the paragraph beside it | Or the rule swallows the fee deferral this same note asks for everywhere else. Verified live on a MYANMAR passport renewal, which has no fee on record: the timing is given and *"Our agent will confirm the exact fee for her embassy"* survives. |
| A hire has ONE lead time | `load_service_notes.TEXT_REPLACEMENTS` | The knowledge base stated **2-3, 3-4, 3-6, 3-8, 4-8 and 6-8 weeks** for a hire, five reachable under `new_hiring` and three in the SAME retrieved set, so the model quoted whichever. The agency gives one: about 4 to 6 weeks from signing overseas, 1 to 2 weeks from the interview for a transfer. The per-nationality spans are REMOVED rather than replaced - we hold no lead time per nationality, which is the call the 2026-09-17 salary sweep made in the same sentence. |
| A question about US is a question | `info_collector._ASKS_SOMETHING` (the how-you-know family) | *"how you know for what service i need helper"*, *"why you are asking about my helper name"*, *"who said i want to hire"* - none carries a question mark, none matched anything else, so `ANSWER_THEN_ASK` never fired and the collector simply asked its next field. `_VALUE_IS_QUESTION` read four of them as questions and threw the VALUE away, so the client's question was discarded AND unanswered in the same turn — the exact mismatch the note above that pattern exists to prevent. |
| ...and it is answered from their own words, never from the service list | `ANSWER_THEN_ASK_INSTRUCTION` | *"We help with new hiring, direct hiring, replacement, transfer, Work Permit renewal, home leave arrangement and passport renewal. Which service do you need?"* — a menu in reply to "how do you know" reads as though we are still guessing. The answer is *"You said you need a helper, so I took that as hiring - tell me if it is something else."* |
| ...and never by taking the reading back | the same note | It was a reasonable reading and they asked how we knew, not for it to be withdrawn. Disowning it leaves them with no service, no question to answer and an apology nobody asked for - §9.19, reported a second time and therefore acted on rather than guessed at. |
| The seven services are the EMPLOYER's side of the desk, and the prompt says so | `system.IDENTITY` | A job seeker reading them finds nothing she is the client for - and registering helpers is a built flow with 15 fields, its own closing briefing and its own lead table. Live before: a helper asking what we do got *"I'll confirm the suitable options with our team and come back to you."* After: *"We register helpers looking for work and match them with employers."* |
| A question that names its own options names ALL of them | `home_type` (and `languages`, `nationality`) | "Option of asking landed property is missing" — it was in the options and never in the question, so `_field_guidance` picked two as examples and a client in a landed house was never shown their own. The room counts went so the list could be read out without brackets (2026-09-10), and `home_type` left `_DIGITS_ON_PURPOSE` with them. |
| The household question asks who lives there, not just how many | `SERVICE_FIELDS[...]["household"]` | Six people is two adults and four children, or four adults and two elderly parents — different jobs. `children_detail` and `elderly_detail` are both GATED on `requirement`, so an elderly parent in a childcare-only household was never asked about at all. |
| More work than one helper can carry is said once, before moving on | `info_collector._heavy_workload` + `state.flagged_once` + `smoke_nodes.py` | BOTH halves required — more than one kind of work AND a large household, by headcount or home size. A big family with one clear job is an ordinary placement, and telling that client their job is too big talks them out of a hire, so it fails towards silence. Advice, never a refusal, and no figure: a number gets the reply binned and they lose the advice with it. Recorded only when the reply was not the bare fallback (2026-09-08 `briefing_lost`). |
| A guard may not flatten the reply it is cleaning | `guards.strip_handover_talk` | It re-joined on `" "`, so a seven-step process answer came back as "1. Consultation … 2. 3. Interview …" — the same defect `clamp_reply` had until 2026-09-08, on the same replies. Line structure survives, and a step stripped to a bare "2." is dropped whole. |
| Home leave closes by telling them to book the ticket | `BRIEFING_AFTER["home_leave"]` + `templates.HOME_LEAVE_TICKET_NOTE` + `smoke_nodes.py` | It had no `BRIEFING_AFTER` entry, so it closed on the bare handover line and the client had to ask *"how long will the documents take before i can buy the air ticket?"* to learn it. The answer was right; the question should not have been theirs. Confirmed dates are what let the assigned agent submit the embassy paperwork immediately. |
| ...and the itinerary is a document for PH and only a date confirmation for ID | `HOME_LEAVE_TICKET_NOTE` | The records list the ticket itinerary among a **Filipino** helper's embassy documents and do not list it for an Indonesian one. Asking an ID employer for it as a required document contradicts the document list three lines above it in the same message. `FEE_BY_NATIONALITY`'s rule applied to a document instead of a price. |
| A briefing is keyed on a field its own flow collects | `selfcheck_flows.py` | Keyed on anything else it can never come due, and the flow closes on the handover line with nothing — silently, because a briefing that never happens looks exactly like one working quietly. Derived over `BRIEFING_AFTER`, not a list of three. |
| Naming the other channel is not a refusal of this one | `ticket._WANTS_EMAIL` | `excludes` is checked FIRST and held the names of the ALTERNATIVE — so every way of saying "both" closed the email gate, including the ones saying "email" outright. "both email and whatsapp" was closed. Those excludes were never needed: an answer naming only WhatsApp matches nothing, and a gate with no match is closed already. `excludes` now holds what it is for — a negation that contains the match word ("no email"), the same shape as "no, I don't have pets" containing "have". |
| ...and the question offers "both", so nobody has to volunteer it | `_UPDATE_CHANNEL` | An either/or question hides the third real answer. Three options named literally, which is what puts `_field_guidance` on its "name them all" branch. |
| A numbered step may state how long OUR OWN service takes | `guards._CONTACT_PROMISE` | "you receive 3 to 5 matched profiles within 48 hours" — the agency's own published turnaround, in the records and passed by `ungrounded_figures` — was deleted as an invented callback time. Inside a step the TIME half fires only when the step also promises somebody will contact them, so "a live agent will call you within 2 hours" still goes. Prose is untouched. |

| Only the one direct-hire answer that changes the route is asked for | `helper_transfer_case` + `selfcheck_flows.py` | `helper_location` asked where she is and `employment_status` whether she is working - four answers between them and only one that changes anything: on a Work Permit here under another employer is a transfer case, everything else is a standard placement. One yes/no, and no `options`, so `_field_guidance` has nothing to invite "or somewhere else" with. |
| Personal contact details are not asked before the client has been told anything | `direct_hiring` field order + `selfcheck_flows.py` | The helper's number was question THREE and was answered *"I'm not comfortable to provide this information now"*. Now after her nationality and the route question, and optional, so a client who still declines is not blocked. Asserted as a POSITION against the other fields, not as an index. |
| A salary band below the floor for that nationality is never offered | `info_collector._SALARY_FLOOR_BY_NATIONALITY` + `_effective_options` | A Filipino helper cannot be placed below S$650, and the question offered *"such as SGD 500-600 or SGD 600-700?"* - a budget no placement could be made at. The Philippines only: `FEE_BY_NATIONALITY`'s rule applied to a salary. A band that straddles the floor is rewritten to start at it rather than dropped, so the cheapest option is not overstated. |
| ...and the band the question offers is the band the guard grounds on | `grounded_options=_effective_options(...)` | One function, two readers. If they disagree the model is told to offer S$650 and then binned by `ungrounded_figures` for offering it, and the client gets the bare fallback question - the 2026-09-09 (D) defect, which is invisible because a guard falling back to a correct question looks like nothing going wrong. The only check that catches it reads the REPLY. |
| Every long intake explains itself at one end or the other | `_OVERVIEW_AT_START` + `selfcheck_flows.py` | `new_hiring` (25 questions) and `direct_hiring` explained themselves at neither end. Derived over the employer services, so a flow added tomorrow makes that choice deliberately; `replacement` and `transfer_employer` are the two that still do not, recorded as a decision. |
| ...and a 25-question hire is not called "a short, well-defined job we handle end to end" | the per-service opening clause | True of a permit renewal, false of a first-time hire. `_SMALL_TICKET_SERVICES` and `_OVERVIEW_AT_START` are kept apart for that one sentence. |
| A record saying the timing DEPENDS on something is not a lead time | the overview note | New hiring's own row says *"There is no single answer, because it turns on your requirements and on which helper you choose"*, and the model paraphrased it back as *"the exact timeline will be confirmed once we know more"* - a sentence that costs the client a line and tells them what they already assumed. It also may not remark on what our records contain. |
| A work permit renewal establishes whose helper she is — and it is READ, never asked | `renewal`/`replacement` + `info_collector._known_fields` | The service is not limited to helpers we placed, so the ticket has to say which it is. This row used to end *"which no record answers"*, and that was only ever true of a POSITIVE count: **a zero is the answer**, because we cannot have placed this helper with an employer we have never placed anyone with. Agency, 2026-09-17, having watched it asked live: *"if the user is new it means the work permit is not from Ming Hwee, then why this question come"*. The same reading `first_time_hire` has taken since 2026-09-04, with the same accepted cost in the same words — *"no placement on record"* is a statement about our RECORDS, not about the client. The `Field` is still the SAME object `replacement` asks (§9.8); its question is now dead text kept as the fallback. |
| ...and the one case we genuinely cannot answer reports what we hold instead of guessing | the third branch of the fill | `get_placed_helper` returns nothing unless there is exactly ONE live placement naming a candidate, and live only 2 of 6 rows did — so an employer with four placements is real. Claiming her puts a guess on a ticket, which is the failure that function was made cautious to avoid; asking is the question the agency has just had removed. The consultant has her name on the same ticket and the placement list one click away. |
| ...and the client's own words beat the fill | `extracted = {**known, **extracted}` | The record fill goes in UNDER the extraction, so *"no she is hired from somewhere else"* corrects us and is what reaches the ticket. Reversed, a client correcting our records is filed with our guess — worse than the question ever was. Asserted by RUNNING the collector, because the merge order is one character wide. |
| ...and no branch of it carries a figure | the three values | They reach the prompt as `collected_info`, which is grounding for `ungrounded_figures` — so a count in here is a number the model may quote back at the client (2026-09-09 D). `first_time_hire` says *"2 placements on record"* and gets away with it; this one does not try. |
| Direct hire does not say "paperwork" | `_COLLECTION_PURPOSE` + the direct-hire documents row | Agency instruction, and scoped to direct hire only - the other services keep the word, because rewriting rows nobody objected to is how a correction turns into a rewrite. |

| Religion is asked IN PLACE OF the pork/beef question, on both sides | `helper_religion` / `religion` + `_MATCHED_PAIRS` | The agency's decision, taken after the trade was put to them: *"in place of this ... ask the religion question because that is priority."* The pork/beef pair was the only place a placement's dietary constraint was captured on both sides of the desk, and it is gone from the QUESTIONS **and** from the `cooking` OPTIONS - `_field_guidance` reads options into the spoken question, so leaving "no pork" in the list would have half-asked the question that was just removed. `halal kitchen` and `vegetarian` stay: those describe the client's own kitchen. |
| ...and the employer's list is the helper's list plus "no preference" | `ticket._RELIGIONS` | One tuple, two lists, derived rather than typed twice (§9.8). The second pairing whose halves cannot literally share one, after `preferred_nationality` - "no preference" is an answer to his question and not a thing she can BE. Asserted as that exact difference rather than skipped as an exception, so a religion added to one side and not the other still fails. |
| A question only one answer can fit is not offered "or more than one" | `Field.multiple_answers` | Live 2026-09-17: *"may I know your religion, such as Muslim, Christian, Catholic, Hindu, Buddhist, another faith, **or more than one**"*. That clause is `_field_guidance` doing as it is told, and it is right for `languages` and `requirement`. Deliberately **not** `options_are_exhaustive`, which is still only ever her country: a helper whose faith is not one of the five still has to be able to give it. The employer's half keeps multiple - "Muslim or Christian is fine" is a real preference. |

| The WhatsApp profile name is never used as the client's name, on ANY turn | `system._contact_block` | It was suppressed inside `info_collector`, gated on `service_type in NAME_FROM_RECORD_ONLY` — and a GREETING turn has no service and never reaches that node. Live 2026-09-17: *"Hi Vaidik, I'm Claire"* to a number we hold no name for, then *"May I know your name?"* two messages later. Moved to the one place the name ENTERS the prompt, so every node gets the same answer instead of each having to remember. The label is not printed as a fallback and not mentioned in order to forbid it — §8's rule for the branches that did not exist. |

| Passport renewal is a HELPER's service, and a client asking about their own is told so | `info_collector._asks_about_own_passport` + `OWN_PASSPORT_NOTE` | All 21 `passport_renewal` rows and all four questions are about her — but none of that was a TEST, so when a client said *"There isn't any helper here"* the model simply reworded the questions and produced a closing briefing quoting **$450**, **3 working days** and asking for *"a copy of your NRIC"* AND *"a copy of your Work Permit"*. Answered, never handed over — the same reasoning as the unplaceable-nationality refusal. |
| ...and the records are STRIPPED from that turn, not just forbidden | the branch passes `rag_context=""` to `_write` | `ungrounded_figures` grounds on the retrieved set, which on that turn really does hold $450 — so the prompt rule alone would have been the only thing standing between a client and a price for a service we do not sell them. With the records blanked, any figure the model produces is ungrounded and takes the whole reply with it. `FEE_BY_NATIONALITY`'s lesson pointed at a person instead of a country. |
| ...and "my passport" is read as the CLIENT's own only once we already hold a helper's name | `_MY_PASSPORT` + the `helper_name` test | *"Also I want to renew my passport also"* cannot mean hers once she is named, and a named helper is positive evidence that the sender is the employer — so that one goes straight to the refusal with no nationality test. With no helper named it is the opposite reading (below), which is the 2026-09-19 correction. |

| A passport renewal is for a HELPER's passport, and she may be the one asking | `info_collector._whose_passport` | The 2026-09-17 branch refused her: *"Ming Hwee handles passport renewal for domestic helpers only, not clients' own passports"*, to a helper renewing her own. Agency, 2026-09-19: the service is open to whoever approaches, "since the passport being renewed will always be the helper's passport". |
| "my passport", with no helper named, IS the sender's | `_whose_passport`, decided on the message that says it | The first answer to this was a disambiguating question and the agency removed it the same day: *"the intent is clear naa, it means the user is helper ... this question does not make any sense."* It was buying a certainty the nationality question already gives for free, at the price of a question put to every helper who asks us plainly. Decided on the OPENING message, so the helper-name question is never queued - a decision that waits for her name arrives one question too late. |
| ...and a denial beats the word "helper" inside it | `_OWN_PASSPORT_EXPLICIT` before `_HELPER_WORD`, in the decision AND in the release | *"i dont have any helper"* names a helper in order to say there is not one; *"i am a helper"* is the sentence only she writes. Read helper-word-first, either one reads as "my helper's" - the opposite of what she said - and once the answer is settled either one would silently undo it. |
| The refusal fires on the nationality, not on a guess about who is asking | `nationality_code` in the refusal condition | We renew through the Philippine, Indonesian and Myanmar embassies and nowhere else, so a passport from anywhere else is not a domestic helper's. That is the one test that separates a Singaporean employer from a Filipino helper, because the two write the same sentence. It costs the 2026-09-17 case one extra turn and buys back a client we were turning away. **It is also what let the disambiguating question go on 2026-09-19** - with the evidence test underneath, the question was asking for a certainty that arrives anyway. |
| Her document list says whose documents they are | `HELPER_PASSPORT_BRIEFING_NOTE` | All 21 rows are written to the employer, so "a copy of your NRIC" in them means HIS. Sent to her unchanged it asks a Work Permit holder for an NRIC - the 2026-09-17 contradiction arriving from the opposite direction. Her list reads "a copy of your employer's NRIC". Same timing, same fee, same steps: the agency asked for "process, documents, timeline, fees accordingly". |
| The person who picks the case up has ONE name, and it is "our agent" | `SERVICE_BRIEFING_NOTE` + `TEXT_REPLACEMENTS` | The closing briefing deferred the cost to "a consultant" four lines above closing with "a live agent will connect with you shortly" - one message, two names, one person. The agency asked for the word by name on 2026-09-19. Swept over the ROWS rather than listed, so a row written tomorrow that reaches for the old word fails by name. |
| ...and the two guards keyed on the old word were changed with it | `guards._CONTACT_PROMISE` + `info_collector._ANNOUNCES_HANDOVER` | Neither is about vocabulary: one decides whether a time inside a numbered step is a callback nobody promised, the other whether a closing briefing announced the handover at all. A rename that left them behind would have switched both off silently. Both accept the old word too, for a row or a reply that has not caught up. |
| A timeline we hold only a floor for is stated as a floor | the three Indonesian passport rows | "Approximately 3 working days" was the only one of the three nationalities claiming a hard number with nothing about the wait in front of it - PH says 6 to 8 weeks, MM says outright that the appointment slot is the unpredictable part. The agency tested it and said it "may take more than 3 working days". They gave no replacement span, so none is invented: it says more than 3 working days and defers the rest. |
| A direct hire explains itself at the END, with the cost and the timeline beside the process | `ticket.BRIEFING_AFTER["direct_hiring"]` | It arrived welded onto question two instead - "Thanks, john. Direct hire involves processing the MOM application, documents, insurance and bond, and getting the helper here and settled; may I know the full name of the helper you would like to hire?" Adding the entry is also what REMOVES the opening overview, so this is one change and not two. Keyed on `helper_availability`, the last REQUIRED field: the three after it are optional, and keying on one of those loses the briefing whenever a client declines it. |
| A question about WHEN is not answered by a sentence about WHERE | `info_collector._ASKS_WHEN` + `_echoes_another_answer` | "No she is on Myanmar right Now" filled `helper_availability` with "Myanmar right now", so the start date was never asked and the ticket carried a country. The test is an ECHO of another answer we hold, not "does this state a time" - the value genuinely contains one, and it is attached to the country. Derived over the QUESTION, so all eight when-fields across the services are covered and not just the one reported. |
| ...and the name of the service they asked for answers none of its own questions | `info_collector._restates_the_service` | "i want to do direct hire" filled `helper_transfer_case` - the ONE question that decides the route - with the value "direct hire", 4 runs of 4. Scoped to the service IN HAND, because `current_helper_exit` offers "going home" (which reads as `home_leave`) and `transfer_direction`'s junk value reads as `transfer`, not `transfer_employer`. |
| A send that may already have been delivered is never sent again | `whapi.client._post` (`_SAFE_TO_RESEND`) | It retried on any httpx error and any 5xx - and a ReadTimeout or a 502 means the request was WRITTEN and the response was lost. Whapi has no idempotency key, so the second POST is a second WhatsApp message, under a new id we then do not recognise as ours. The cost of the retry is not a duplicate, it is the row below. |
| ...and our own words coming back are never a human agent | `message.echoes_our_own_send` + `handle_outbound` | The duplicate arrived as a from_me webhook whose id `was_sent_by_bot` had never heard of, so the bot stood down for an agent who was never there - permanently, on a live thread, with the client's next question unanswered and the row on the database reading `sent_by='agent'`. Marked BEFORE the request goes out, because the echo can beat our own insert by 1.9 seconds. |
| An employer who says which kind of transfer he means is not asked which kind of transfer he means | `info_collector._settled_transfer_direction` + `_TAKES_ON_A_TRANSFER` / `_RELEASES_THEIR_OWN` | The fix §9.12 named and did not take. The extractor keeps returning the bare word "transfer", which opens neither gate, so `_undecidable_gate_keys` re-asks - correctly, and at a client who answered in his first four words. Read off his own words instead, and applied AFTER the extraction because `known` loses to it by design. The two patterns are not mirror images: a release carries a possessive ("transfer MY helper"), taking one on does not ("a transfer helper"). |
| ...and an undecidable value never overwrites a settled one | the `previous` branch of the same function | Found by REPLAYING the transcript, not by reading the code. `collected = {**previous, **extracted}`, so the same junk word handed back on a LATER turn wipes a direction already settled and the question comes back. A real correction decides something, opens the other gate, and still wins. |
| "i want transfer helper" is an employer asking for one, not a helper asking for herself | `intent_classifier._HELPER_SPEAKING` lookahead | It matched "i want transfer" and read the sender as the HELPER, so turn one ran the candidate flow and asked for her name; the switch to `transfer_employer` a turn later then wiped the collection. Swept as a SET in both directions - six employer phrasings and six of hers - because the cost is symmetrical. |
| The household question does not ask again about the children it has just been told about | `info_collector._care_details_already_told` | "i have 4 childrens" -> "how many people live in your household, and who are they, such as adults, elderly parents or children?" -> "AS I TOLD THEN WHY ASKED ME AGAIN". Derived from the fields gated on `requirement`, and it NAMES the rule it overrides - the general "ask for everything the question asks for" beat it otherwise (2026-09-07). |
| The cooking question asks IF she will cook, not only which kind | `cooking`'s question | Reworded and deliberately NOT gated on `requirement`: a gate there makes `_gates_are_exhaustive` true for that field - measured - which switches `_undecidable_gate_keys` on and reinstates the 2026-09-08 blank-and-re-ask defect. |
| A withheld price is said as what WE will do, never as a gap in our files | `SERVICE_BRIEFING_NOTE` item 2 | "The transfer fee is not stated in our records" went out live: it tells the client about our filing and reads as though we do not know our own prices. |
| A passport that runs out before the renewal could finish is said out loud | `info_collector._expires_before_we_finish` + `EXPIRING_SOON_NOTE` | Live: *"in 5 days"* answered with *"It takes approximately 6 to 8 weeks"*, the two figures one line apart and nothing connecting them — 2 runs out of 2. `passport_expiry` had been collected since the flow was written and put on the ticket; **nothing ever read it.** Coarse on purpose and fails towards SILENCE: "next March", "when the contract ends" and a formatted date all return None, because guessing at a date and then calling somebody's passport urgent is worse than the omission. 60 days, which covers the slowest route we hold — a per-nationality table would be a second copy of lead times that live in the knowledge base (§9.8). |

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

**`branches` holds exactly ONE row: `CHINA TOWN` (code `CT`).** Confirmed live
2026-09-16. That is the whole agency — 101 Upper Cross Street, #03-54, People's Park
Centre, Singapore 058357, which is also the Registered Business Address on the Client
Service Agreement. The system prompt claimed three branches (Jurong, Tampines,
Woodlands) until that date and they exist in no table and no knowledge-base row; the
bot volunteered them to a client and was then asked for an address it could not have.
`resolve_branch_id()` picking "the tenant's HQ" therefore has exactly one candidate.

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
- `WHAPI_SENDER_PHONE` — **our own** WhatsApp number, and it does less than the name
  suggests: it drops self-chat messages in `parser.parse_webhook` and stamps
  `to_number`/`from_number` on stored rows for the portal. It authenticates nothing and
  routes nothing — that is `WHAPI_API_TOKEN` plus the webhook registered on the channel.
  So when the agency's number changes, **which of the two happened decides the work**:
  the same Whapi channel re-paired to a new handset keeps its token, its channel id and
  its webhook, and this is the only line that moves (2026-09-11, channel `SPRWMN-VC9N4`);
  a genuinely new channel also needs the new token and the webhook set on it. Confirm by
  comparing the token, not by assuming: `docker compose exec chatbot sh -c 'printenv
  WHAPI_API_TOKEN | tail -c 5'`. **Neither case touches the knowledge base, and that is
  the half that bites** — see the 2026-09-11 change log.
- `WHAPI_WEBHOOK_SECRET` — ours, not Whapi's. It is carried in the URL path
  (`POST /webhook/whapi/{secret}`), which is how the portal's webhook on this channel is
  configured, so a channel change does not require a new one.
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
    so they are recorded rather than edited. **The chunk-level correction path
    predicted here now exists** — `load_service_notes.TEXT_REPLACEMENTS`, built
    on 2026-09-11 when the agency's phone number changed and fourteen
    question-less chunk rows named the old one. It keys on the text rather than
    on `question`, so these timelines can be corrected with it the day one
    surfaces; they still have not, in any of the ten probes.
    Separately and **not** a contradiction: `27-helper-rights-simple-english.md`
    tells a helper a transfer takes **2-4 weeks**. That measures from her asking
    us to transfer, which includes finding an employer; the agency's 1-2 weeks
    is measured from the interview. Different clock, different audience, and the
    row is correctly `contact_type='candidate'` so an employer never sees it.
    Worth having Ming Hwee confirm rather than assuming.

16. **WhatsApp has migrated most of this channel to LIDs, and a migrated
    payload carries no phone number at all.** Fixed the same day — see the change log — but
    kept here because it will keep happening as more accounts migrate, and
    because the wrong fix is tempting. Live 2026-09-10: both `from` and
    `chat_id` were `116909177569373@lid`, `normalize_phone` turned that into
    the "number" `+116909177569373`, and an allowlisted tester was stood down
    on every message. `whapi.resolve_lid()` now asks `GET /chats/<lid>`, which
    carries `{"phone": "917970027379"}` — measured against the live channel;
    `GET /contacts/<lid>` returns the push name and **no** phone, so it is the
    wrong endpoint and reverting to it makes resolution return None silently.
    **That endpoint is necessary and not sufficient** (2026-09-14): for some
    chats it answers `{"type":"unknown"}` with no phone, or 404s outright,
    while the chat LIST holds the same chat WITH its number — so a miss falls
    back to sweeping the list. See the change log.
    **Do NOT put a LID in `BOT_ALLOWED_NUMBERS`**: every lookup is keyed on the
    number, so it would open a second conversation on an identifier that is not
    a phone number — the split-conversation bug `fix_split_conversations.py`
    exists to repair. **And that happened anyway, by a door this entry did not
    name** — see the 2026-09-14 change log. Nobody had to allowlist anything:
    an unresolvable LID kept its pseudo-number, and `handle_outbound` has no
    allowlist to stop it, so an agent replying in such a chat created the row.
    Fixed at source; **the 145 rows already on `wp_chat_conversations` are left
    alone and are the portal's call**, since that is their table and 14 real
    messages hang off them. If Whapi ever cannot resolve one, the bot stands down and
    says so in the log rather than guessing whose number it might be.
    **The scale, measured rather than assumed (2026-09-10):** the channel holds
    **3,422 chats**; of 2,000 scanned, **1,892 (94%) are already LIDs** and only
    40 still arrive as a phone JID. All three allowlisted Indian test numbers are
    LID chats. **Every one of the 1,892 carries a phone number Whapi returns**,
    so resolution covers the whole estate — but it also means this was never a
    one-tester problem: without the fix the bot stands down on nearly every
    conversation. It is *not* intermittent, which is what it looks like from the
    outside: it is a rollout that is nearly complete, and the ~2% still on phone
    JIDs are the only messages that get through untouched.

17. **A live candidate-facing row still carries an unfilled editorial
    placeholder.** `27.1a Your Placement Loan (Very Important - Read This)`,
    `contact_type='candidate'`, contains verbatim:
    *"**[INSERT - Ming Hwee to complete before launch]** The actual loan
    structure for each source country: who the creditor is, the typical total,
    the monthly deduction ... Until this is filled in and verified, the bot
    answers loan questions by routing to a human."* That is an instruction to
    Ming Hwee sitting in the retrievable text, and **whatever is in the records
    is what the model quotes** — the same rule that kept the internal pipeline
    brief out of the KB on 2026-09-08. It has not been seen in a reply and it
    did not top any of the fee probes run on 2026-09-10, so it is recorded
    rather than edited: the content is the agency's, the instruction it carries
    is still true (nothing here answers a loan question), and deleting somebody
    else's row on a hunch is not this repo's call. Two ways out, both theirs:
    fill it in, or have the placeholder stripped and the row left as the rights
    material it otherwise is.
18. **The candidate application checklist's preamble outranks her own journey
    rows.** `Purpose & Important Note` is top for *"what is the process"*
    (0.440 vs 0.428) and for both ways she asked for her documents (0.504 vs
    0.482), measured through the real retriever on 2026-09-10. It is the
    preamble to an internal consent form, written **about** her in the third
    person. Both of her own rows are still inside the top 5 the model receives,
    and the live replies used them and not this — verified, not assumed. So it
    is the accepted state, the same shape as the 0.009 transfer/new-hiring
    margin on 2026-09-10, and recorded because a future re-embed can flip it.
    Rewording her rows to widen the gap was **not** done: that trade was
    measured and rejected once already, and it swaps a real answer for a
    comfortable margin.
19. **The bot apologised for, and disowned, its own correct closing message.**
    Live 2026-09-10: after the candidate briefing she asked *"why did you tell
    the process"* and got *"Sorry, Ruru, I misunderstood - you meant WhatsApp
    for updates, not that you wanted the hiring process."* Nothing had been
    misunderstood; the briefing is the sanctioned closing message and the
    agency asked for it. Addressed at the CAUSE rather than the symptom - the
    message now says her registration is complete, so the question does not
    arise - because the apology is written a turn later by
    `response_generator`, which has no way to know that the message it is being
    asked about was deliberate. A prompt rule telling it never to disown an
    earlier reply is the obvious next move and is deliberately not taken on a
    hunch: it is broad, it would also suppress genuine corrections, and nobody
    has reported the shape twice.

20. **A bracketed placeholder reached a client, and no guard catches one.**
    Live 2026-09-16: *"Our Tampines branch is at [address not available in my
    records]. A live agent will confirm the exact office location shortly."*
    Measured against every guard that runs on that path —
    `strip_meta_commentary` leaves it (its `_META_MARKERS` do not match, and
    cutting it would produce *"Our Tampines branch is at ."*, which is worse),
    `leaks_internal_reasoning`, `is_degenerate` and `looks_like_document` all
    return False. The **cause** is fixed — the branch did not exist, and both
    the identity block and `AGENCY_INFO_INSTRUCTION` now forbid a placeholder
    by name — so this is the residue, not the defect.
    It is recorded rather than guarded because a guard here is a one-line
    temptation with a real cost: the honest fallbacks this bot sends are
    *"I'll confirm ... and come back to you"*, and a pattern loose enough to
    catch *"[address not available in my records]"* is loose enough to catch a
    legitimate bracketed aside — `(MDW)` is one, and `strip_meta_commentary`
    was deliberately written to leave those alone. **What makes it worth
    writing down is that this is the second arrival, from the opposite
    direction:** §9.17 is an unfilled `[INSERT — Ming Hwee to complete before
    launch]` sitting in a live candidate-facing KB row, which the model would
    quote verbatim if it ever topped a search. One placeholder written BY the
    model and one sitting IN the records. A third, by either route, is the
    point at which a shared `looks_like_a_placeholder` guard stops being a
    hunch — and both halves would use it, which is the §9.8 argument for one
    definition rather than two.

21. **A two-part question is closed by an answer to half of it.**
    `children_detail` asks *"How many children, and how old are they?"* and is
    filled by *"2 kids"* — so the count reaches the consultant and the **ages
    never do**, on the field that exists to carry them. Seen 2026-09-16 while
    verifying the care-type fix: *"childcare for my 2 kids"* fills
    `requirement` **and** `children_detail` in one turn, and the age question
    is then never asked.
    It is the same shape as the care-type defect above, one field along, and
    deliberately not fixed with it. Three reasons. The value is **grounded** —
    the client really did say "2 kids", so this is an incomplete answer rather
    than an invented one, which is a different and milder failure. Nobody has
    reported it. And the machinery that would fix it is the collection gating
    `_unfinished()` runs, which this file already says twice is not to be
    changed in a hurry: `_undecidable_gate_keys` stranded `requirement` for
    three asks on 2026-09-08 by assuming a field's gates covered its whole
    answer space, and §9.12 is still open for the same reason.
    The shape of the fix, when somebody wants it: `_unfinished()` already
    re-asks a field whose value asserts something without saying what
    (`_ASSERTS_WITHOUT_DETAIL`), and it runs only on fields that were ASKED.
    The missing case is a field that was never asked and was filled from half
    an answer. `_field_guidance` already knows which questions name more than
    one thing — it tells the model "if it names three things, a question that
    gets one of them is not this question" — so the test exists in prose and
    would need deriving in code rather than inventing.

22. **A client who answers the channel question with their bare email address
    closes the gate that would have asked for it.** `_mentions` anchors on a
    leading word boundary — `(?<![a-z])` — so `mail` does not match inside
    `gmail`, and `vd@gmail.com` contains none of `email`, `e-mail`, `mail`,
    `both`, `either` or `any`. The gate closes and `email` is never asked.
    Found on 2026-09-17 while fixing the "both" family above, and left alone on
    purpose. The damage is usually nil: the extractor files a bare address into
    `email` on its own, so the value reaches the ticket even though the question
    was skipped — this is a missed QUESTION, not a lost address, which is a
    milder failure than the one just fixed. Nobody has reported it. And the fix
    is not a one-liner: `@` cannot simply go into `matches`, because that
    lookbehind rejects it whenever a letter precedes it (`vd@` fails, `vd1@`
    passes), so it would work on some addresses and not others — which is worse
    than not working at all. The honest shape is to recognise an address as an
    answer to the EMAIL field rather than to the channel field, and that is
    collection gating, which §9.12 and §9.21 both say is not to be changed in a
    hurry.

23. **A country answers `helper_from_us`, whose three values are not countries.**
    Found 2026-09-18 while re-running the fixed transcript: replaying the
    original client's *"indonesia"* — which after the fix is a reply to a
    message that asked nothing — the extractor filed it as
    `helper_from_us = "Indonesia"`. That field carries where the current
    helper's Work Permit came from, and since 2026-09-17 it is filled from the
    records in three fixed wordings and **never asked**, so the only way a
    country reaches it is the extractor. The client's own words beat the fill
    by design (`extracted = {**known, **extracted}`), which is right for a real
    correction and is what lets this through.
    Left alone on purpose. The turn that produced it cannot arise from this
    path any more — the fee question is now answered without asking anything,
    so there is no bare nationality to mis-file. Nobody has reported it. The
    damage is bounded: it reaches a consultant as one odd line on a ticket
    beside the helper's name, not as a wrong price or a wrong document. And the
    fix is collection gating, which §9.12, §9.21 and §9.22 each say is not to be
    changed in a hurry. The honest shape, when somebody wants it: the field's
    three values are a closed set, so this is `Field.options_are_exhaustive`
    applied to a field nobody asks — a value outside the set is not an answer
    and should fall back to the record fill rather than overwrite it.

24. **The bot echoes a client's own capitalisation, so one name appears two ways
    in one conversation.** Live 2026-09-18: "sushi" in one turn and "Sushi" two
    turns later, "hoohoo" then "Hoohoo". Reported as a minor issue with the
    request to "normalise once on capture".
    Left alone deliberately. `RECORD_NAME_NOTE` tells the model to use the name
    the client gave and explicitly **not to change the spelling of what they
    wrote**, which is there because a client who types their name one way and
    reads it back another has been corrected by a machine. Title-casing is
    arguably not a spelling change, but the line between them is exactly the
    kind of judgement that needs the agency rather than a guess: "de Silva",
    "binti Abdullah" and "MARY GRACE" are all names that a naive
    `.title()` damages, and two of the three are common here.
    It is also cosmetic — nothing downstream keys on the casing, the lead and
    the ticket carry whatever was typed — and name handling is the single most
    reported area in this file (five separate complaints since 2026-09-08), so
    a change here is far more likely to reopen one of those than to fix
    anything. If the agency wants it normalised, it is one function at the
    point of capture plus a decision about the particles.

25. **Five internal pipeline chunks are retrievable under `new_hiring`, and
    they read as staff instructions.** Found on 2026-09-19 while sweeping the
    knowledge base for the word "consultant". Rows `6cf60cdb`, `f0bf5da9`,
    `ec41d419`, `6e6570a4` and `cacd5925` carry verbatim
    *"Milestone stage: IPA issued & recorded → HANDOFF | What happens: MOM
    issues the IPA letter; consultant records it; case hands to Admin for
    deployment logistics. | Owner: Sales → Admin"*. Four are
    `contact_type='employer'` and one is `all`, so a hiring client can reach
    all five.
    **This is the exact content the 2026-09-08 load was written to keep OUT** -
    that entry records a "what's the process" question retrieving the internal
    pipeline brief and the bot reciting our own workflow to the person it is
    being run on, and it is why every row written since has been rewritten from
    the client's side. These five predate that rule; they came in with the bulk
    import.
    They have not been seen in a reply and did not top any of the process
    probes run since, which is why this is recorded rather than acted on - and
    the honest fix is not a word swap. It is either deleting five rows of
    somebody else's imported content, or rewriting them from the client's side,
    and both are the agency's call. `TEXT_REPLACEMENTS` is the path if they
    only want the wording corrected; §9.15 is the same shape for stale transfer
    timelines in the same import.

**Waiting on Ming Hwee, not on code.** None of these is a defect; each is a decision or
a figure only the agency can give, and the bot quotes or does the right thing the day it
arrives. Gathered here so they are asked in one conversation instead of rediscovered one
at a time.

- **The final pricing and fee structure, per service** — their own point 4, and the
  thing that blocks the Cost/Fee step of the flow they asked for in point 5. The bot
  already quotes a fee wherever the agency has given one (`FEE_STATED_SERVICES`:
  renewal, passport renewal, home leave) and defers everywhere else. Nothing here
  needs changing when the figures arrive; they are rows.
- **And the half of that which is a decision, not a figure: may a new hire's cost be
  quoted at all?** Their 2026-09-17 flow puts Cost/Fee immediately after Process &
  Timeline. Their 2026-09-04 instruction says the opposite for exactly these two
  services — a new hire's price never reaches anyone before a salesperson has spoken
  to them — and `quotes_hiring_package_cost` enforces it. Both cannot hold. Salary,
  the levy and the $5,000 bond already go out; it is the package total that does not.
  One sentence from them settles it.
- ~~**Should a helper's religion be collected?**~~ **ANSWERED 2026-09-17, and
  built** — see the change log. What was put to them: we already hold
  `candidates.religion` on every live row, no flow asked it, and the thing it decides
  in a placement was already asked of both sides as a matched pair (*"would she need to
  handle pork or beef?"* / *"are you able to handle pork or beef?"*). Their decision was
  to ask religion **in place of** that pair, on both sides, because it is the higher
  priority for them. Done. **What is still open is the half that leaves behind:** an
  employer's pork/beef requirement is now captured nowhere, and religion does not
  predict handling reliably. The helper's own side survives on the registration form
  (`biodata.commitments.handle_pork` / `handle_beef`) and `halal kitchen` is still a
  cooking option, so a consultant is not blind — but if they want the employer's
  dietary requirement back as a question, it is one field beside the religion one
  rather than inside the cooking question it used to share.
- **The Indonesian and Myanmar salary floors.** The Philippines now has one (S$650
  fresh, from S$670 experienced) and it is enforced in the budget question. The KB's
  own figures for the other two are the ones that were already there and nobody has
  confirmed them, so they are not treated as floors. If Indonesia and Myanmar have
  minimums too, they are one line each and the same machinery applies.
- ~~**Whether `replacement` and `transfer_employer` should explain themselves too.**~~
  **ANSWERED 2026-09-18, both halves, and built.** They said so the way they say
  everything - by testing the flow and objecting to the silence. `replacement`
  first ("after getting all the required details the bot should reply the process,
  required documents, timeline and the cost"), then `transfer_employer` hours later
  ("after getting all the required details bot didnt message the process,
  documents, timline, cost/fees"). **No employer intake briefs at neither end any
  more**, and the self-check's list is empty rather than deleted. Recording them
  here as decisions rather than omissions is what made each one a single entry to
  close. What is still open is one BRANCH, not a service: an employer RELEASING
  their helper is gated out of `rest_day`, so that half still closes on the bare
  handover line. Its flow is four questions and both of its own fields are
  optional, so there is no key reliably filled a turn before the end - the honest
  fix is a second key, not a guess at one, and nobody has tested that half.
- **The WhatsApp Business away message.** Their own team asked, 2026-09-17: *"Why is
  there an immediate message that says reply next day when it is during working
  hours??"* - *"Thank You for your message. Our team will reply to you next following
  working day."* arriving at 1:42pm on a weekday, before Claire's own reply. **That
  string is in no file in this repo**: it is the away message configured on the
  WhatsApp Business handset, and nothing here can change it. `message.is_auto_reply`
  exists to RECOGNISE it so it is not mistaken for an agent taking the conversation
  over, which is our entire involvement. Turning it off, or setting its hours to match
  the office's, is two taps in WhatsApp Business and theirs to do.
- **Which MRT station the office brief meant.** Their words were *"Find Ming Hwee
  Agency at Exit D MRT Station"* — an exit with no station. The rows say **Chinatown
  MRT, Exit D**, which is the station People's Park Centre sits on and matches the
  `branches` row's own name (`CHINA TOWN`), but it is the one detail in the 2026-09-16
  load that was inferred rather than given. A client acts on it by getting on a train,
  so it is worth one line of confirmation. **And the other half: are Jurong, Tampines
  and Woodlands genuinely gone, or never existed?** The prompt claimed all three; the
  database, the Client Service Agreement and their own brief all say one office. The
  bot now says one. If there are others, their addresses are one row each.
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
- **Myanmar passport renewal AND Myanmar home leave**: no fee, no timeline, no
  document list, and no confirmation that the three-form passport route still
  stands as the 2026-09-07 document described it. Nothing is quoted for Myanmar
  because nothing was given — both closing briefings correctly defer the price to
  a consultant. The one thing that does leak through is a Myanmar home leave being
  told we need "a copy of her passport", which is the INDONESIAN row's item
  arriving via the `nationality='all'` row; NRIC and work permit are grounded for
  everyone. Recorded 2026-09-17 rather than guarded, because the honest fix is a
  Myanmar row from them, not a rule inferred from the other two.
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
- ~~**What a HELPER pays us, if anything.**~~ **ANSWERED 2026-09-10** — *"there is no
  fees, the candidate does not have to pay any fees for this"*. It is now a row of its
  own (`general` + `candidate`), and a job seeker asking it is answered rather than
  handed to a human. What is **still** open is the half beside it: the KB tells her, in
  her own language, that *"Often you do not pay cash — instead, money is taken from your
  salary for the first months. This is called a placement loan"*, and lists **"Ming
  Hwee?"** among the possible creditors. Those two are compatible — we charge her
  nothing, her home-country agency may not — but only Ming Hwee can say so, and the
  new row deliberately does not deny the loan. **Does a helper placed by Ming Hwee
  carry a placement loan, and to whom?** See also §9.17.
- **What a helper can expect to earn.** *"What salary will I get"* scores **0.000** for a
  candidate - nothing in the KB answers it. The same missing grounded salary band as
  §9's entry above, from the other side of the desk.

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
python scripts/seed_case_testdata.py   # the ONLY selfcheck that writes. Creates three
                                       #   cases down the three paths get_cases reads,
                                       #   runs the real lookup, deletes them again.
                                       #   `cases` is empty, so there is nothing live to
                                       #   test against. Add --keep to leave them, and
                                       #   --phone +65... to hang them off a number you
                                       #   can message from, which is the only way to see
                                       #   the case context in a reply. --remove clears
                                       #   a --keep run. Never touches a row it did not
                                       #   create.
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

- **2026-09-22** - **"Hi" was answered "Sure, go ahead. What would you like to
  know?"** The agency, testing on a thread whose last exchange was an insurance
  application months earlier: *"I think this is very weird. The reply from hi
  results to this? Clients won't be able to clear chats in future when it goes
  live ... Can it greet user and ask about user intent?"*
  (A) **Two different messages, one string.** `has_no_request` covers a
  GREETING and a question ANNOUNCED but not asked - *"can I ask you
  something?"* - and both were answered with `PROMPT_FOR_QUESTION`, which was
  written for the second. *"Go ahead"* answers a request to ask; said to *"Hi"*
  it skips the greeting and presupposes the intent. `greeting_only` tells them
  apart and the greeting gets its own reply.
  (B) **The name is from our RECORDS, and nothing else of their file is.** The
  WhatsApp profile label is not a name (2026-09-17) and this string bypasses
  the model, so nothing downstream could have caught it here. No dates, no
  counts, no *"your last enquiry"* - `RETURNING_NOTE`'s rule: a greeting is not
  the place to prove we remember them.
  (C) **And the canned string was the smaller half.** Measured live: most
  greeting turns never reach that branch at all, because the model writes the
  reply - and the PROMPT told it not to greet. Rule 7 and the stage line both
  say *"do NOT greet again"* for any turn with history, which is right when the
  client asked something and wrong when their whole message IS the greeting. So
  not greeting back was the INSTRUCTED behaviour, not a slip. The stage line now
  has a third branch that says so, and says outright not to assume from earlier
  messages what they want this time - which is the agency's own *"not blur the
  two regardless of what's in the conversation history"*.
  (D) **One definition, two readers** (§9.8). `greeting_only` lives in
  `guards.py` because the node that picks the reply and the prompt that decides
  whether the model may greet have to agree about the same sentence - the two
  disagreeing is the whole defect one level up.
  (E) **A client who greets twice was not greeting.** *"hello, good morning"*
  left *"good morning"* after a single strip, which matches no announcement, so
  it fell through as a real request. Stripped in a loop now - the same one-word
  gap this file records for `_GENERAL_INFO` four times.
  (F) **Why neither suite caught it: this path had no cover at all.** No
  assertion anywhere named `PROMPT_FOR_QUESTION` or `has_no_request`, and
  `e2e_services.py` opens every walk with the service sentence, never a bare
  *"Hi"* - the same blind spot recorded on 2026-09-17. It now has six states
  that RUN the node, plus `_forbid_reply` in the harness, because here the
  defect is a WRONG canned string rather than a missing one and the only
  assertion that bites is that *"go ahead"* is absent from this turn.
  (G) **Verified live, the reported turn and five controls.** *"Hi"* with a
  name on file -> *"Hi Ratna, what can I help you with?"*; with no name ->
  *"Hi! How can I help you?"*; *"hello, good morning"* -> *"Good evening,
  Ratna. How can I help you?"* First contact still introduces Claire, *"can i
  ask you something"* still gets *"Sure, what would you like to ask?"*, and a
  real request is still answered on the spot. Seven faults injected, seven red.
  `selfcheck_flows.py` is **648 assertions**; `smoke_nodes.py` is **165
  states**.

- **2026-09-22** - **"Who in your family referred you to Ming Hwee?"** The
  agency, testing new hiring: *"this sounds a bit too weird and personal,
  asking specifically which member of the client's family recommended"*.
  (A) **The gate opened on any referral at all.** `_WAS_REFERRED` matched
  `friend`, `family`, `relative`, `word of mouth` and `recommend` alongside
  `staff`, so answering the how-did-you-hear question with *"family member"*
  queued a follow-up asking which relative it was. The question's own wording
  was neutral - *"Who was it that referred you?"* - and the model did the
  natural thing with the answer in front of it.
  (B) **And the name was useless to us either way.** A friend's or a relative's
  name is somebody we hold no record of and can do nothing with. OUR OWN STAFF
  is the one case where the name means something: it is on our own payroll and
  the referral is credited to them. So the gate narrowed to that rather than
  the field being deleted, and the question and the ticket line both say
  `staff` now - a neutral wording is what let it drift in the first place.
  (C) **The excludes are what the mixed phrasing needs.** *"my friend who works
  near your office"* matches `your office` and is still a FRIEND's name;
  `excludes` is checked first, which is the shape `Gate`'s own docstring is
  written about (*"no, I don't have pets"* containing "have"). 16 phrasings
  measured, 9 closed and 6 open, an empty answer still undecided.
  (D) **Proved on the collection rather than on the gate**, because a gate that
  answers correctly and a question that is never put are two different claims.
  `applicable_fields` with *"family member"* leaves `update_channel`; with
  *"staff referral"* it leaves `referrer_name` then `update_channel`. Live: the
  reported turn goes straight to the channel question, and a client who says
  *"one of your staff told me about you"* gets *"may I know which Ming Hwee
  staff member referred you?"*
  (E) **Derived over every flow that asks it**, so `transfer_employer` - which
  reuses the same `Field` through `_hiring_field` - is covered by the same
  check and a flow added tomorrow cannot reopen the question. Five faults
  injected, five red.
  `selfcheck_flows.py` is **643 assertions**; `smoke_nodes.py` is **159
  states**.

- **2026-09-22** - **"so there are no surprises later" - the right reason in the
  wrong register.** The agency, testing new hiring: *"the phrase 'no surprises'
  sounds weird, can change it / phrase it better as it sounds too casual. e.g.
  so we can set the expectations for the helper / so we can lay out clear
  ground rules."*
  (A) **The phrase is in no file.** `_WHY_WE_ASK` supplies the REASON and never
  the wording - deliberately, since 2026-09-09: a fixed lead-in repeated four
  times in one conversation is the formula `strip_repeated_opener` exists to
  stop. So this is the model paraphrasing, and the thing it was paraphrasing
  invited it: `additional_notes` read *"...agreed with the helper up front
  rather than discovered later"*, and *"rather than discovered later"*
  compresses to *"so there are no surprises later"* in one step. It now names
  what we DO with the answer - *"so your house rules are set out clearly with
  the helper and agreed before she starts"* - which is the register `rest_day`
  has had since the table was written.
  (B) **And the rule is derived over the table, not applied to the field that
  was reported.** The new sweep - no reason ends on what it saves them from -
  went red on `helper_religion`, whose tail was *"rather than becoming
  something either of you has to work around later"*. Same shape, nobody had
  reported it, and it now ends the way the HELPER's own half of that pairing
  already did.
  (C) **The register is named in the instruction too, because the reason alone
  is not enough.** A reason is put in the model's own words by construction, so
  a well-written one can still arrive casual. The note says outright that this
  conversation ends in a Service Agreement, and names the phrasing the agency
  objected to.
  (D) **Its first draft quoted two good reasons as examples and both leaked -
  which the control found, not the measurement.** The PETS question came back
  *"To help us put forward helpers who are comfortable with animals **and agree
  the house rules before they start**..."* and religion came back asking about
  house rules as well: the model was reusing the example clause wherever a
  reason was due. That is §8's own lesson - the strongest signal for a phrase
  is our own prompt printing it - arriving in a new register. The examples are
  gone; the rule states the shape, says not to borrow another question's
  reason, and names only the phrasing to avoid. After: pets is about animals,
  rest days about rest days, religion about the household's practices.
  (E) **Measured on the agency's own turn, 6 runs of 6**: no casual phrasing,
  and the reason present in every one - *"are there any other requirements,
  house rules or preferences to note, so we can set them out clearly and agree
  them with the helper before she starts?"* Before the change the same turn
  produced the sentence they objected to.
  `selfcheck_flows.py` is **640 assertions**; `smoke_nodes.py` is **159
  states**.

- **2026-09-19** - **New hiring, tested end to end as an employer: 22 questions
  and then "a live agent will connect with you shortly".** The agency sent four
  screenshots. What they show is a flow that collects well and explains nothing,
  plus three smaller faults they did not report and one the replay found
  underneath.
  (A) **The closing briefing, which is what the transcript is really about.**
  After the last question the client asked *"ok what is the further process"*,
  *"and what are the documents needed"*, *"and what is the timeline"* and
  *"what are the charges"* - four consecutive messages, all four answerable,
  none of which he should have had to think of. `new_hiring` was the LAST
  employer service with no `BRIEFING_AFTER` entry; the same complaint closed
  `replacement` and `transfer_employer` on 2026-09-18 and `direct_hiring` the
  day after.
  (B) **Keyed on `start_timeline`, the last REQUIRED field**, for the reason
  this table now states six times: the retriever runs before the collector, so
  the key has to be filled a turn before the collection completes. Everything
  after it is optional and an optional field a client declines is never filled
  - which is exactly the argument `direct_hiring` made on 2026-09-18. Asserted
  as a derived rule now: no briefing is keyed on the last field of its own
  flow.
  (C) **And the briefing had no lead time to give**, which is a retrieval
  problem and not a prompt one. Measured at BRIEFING_MATCH_COUNT: under the
  general briefing query NOT ONE timing row came back for `new_hiring`, in the
  top 10 or the top 18, because "how long does it take" does not read as "how
  long does it take TO HIRE A HELPER" among forty rows about process steps.
  `BRIEFING_QUERY_BY_SERVICE` names the clock - "from signing to her first
  day" - and all three timing rows come back with the whole set lifting from
  0.42-0.52 to 0.61-0.70. It also drops "how much does it cost", which matches
  `_PRICE_QUESTION` and DROPS the service filter (the 2026-09-10 defect that
  put $450 into a job seeker's briefing); the cost is withheld on this service
  anyway.
  (D) **Then the timing rows disagreed with each other.** Swept: the knowledge
  base stated **2-3, 3-4, 3-6, 3-8, 4-8 and 6-8 weeks** for a hire - five of
  them reachable under `new_hiring` and THREE in the same retrieved set, so the
  model could quote any of them. Live it gave up and passed the question to an
  agent; replayed at HEAD before the fix it answered "around 6 to 8 weeks",
  which is not the agency's figure. The agency gives one: about 4 to 6 weeks
  from signing for an overseas hire, 1 to 2 weeks from the interview for a
  transfer. Eleven needles, 16 row edits, loader idempotent on the second run,
  and a re-sweep of the live database shows **1-2 and 4-6 and nothing else** -
  the 2-3 that remains is `direct_hiring`'s helper already in Singapore, which
  is a different question. The PER-NATIONALITY spans are removed rather than
  replaced: we hold no lead time per nationality, and inventing three is the
  mistake section 9 records for Myanmar twice. The same call the 2026-09-17
  salary sweep made in the same sentence.
  (E) **"1 is elderly care" rewrote what he had asked for.** Asked how many
  people live in his household and who they are, he wrote *"In my family there
  are 8 peoples ... and 1 is elderly care"* - describing WHO IS AT HOME, which
  is precisely what that question asks. `requirement` went from *"childcare"*
  to *"childcare, eldercare"*, `elderly_detail` opened on it, and he wrote
  *"no no i dont want elderly care help service you just ask me that how many
  peoples are there in you household ... by mistake i wrote the elderly care i
  am writing elderly person"*. The two care-type guards beside this one run
  only while a field has NEVER been asked, on the principle that once asked
  their answer is their answer - and this is the case neither of them covers:
  asked, answered, then overwritten by a turn answering something else. A
  client who actually ASKS for the care still changes it, so a real correction
  is not lost.
  (F) **And the one-helper workload warning fired on that false premise and was
  never withdrawn.** Measured both ways: `_heavy_workload` returns True on the
  rewritten value and **False** on the one he gave. So he was told one helper
  could not manage his household because of a service he never asked for, he
  corrected it, the bot said "We'll focus on childcare only" - and the advice
  stood, because it is said once and never revisited. Fixed at the cause rather
  than by building a withdrawal: with (E) in place it does not fire at all.
  (G) **"4+ year experienced" and then "are you open to a first-timer?"** He
  said yes, and the ticket carried `helper_profile: 4+ years experienced` and
  `hire_source: first-timer` - two contradictory instructions for a consultant
  to ring him about. The question is NOT dropped: where the experience was got
  is a real question and nothing else asks it. Only the half he had already
  answered goes. Live after: *"Should her experience be from Singapore,
  overseas, or as a transfer helper already in Singapore?"*
  (H) **The replay found a guard missing from the path this commit was
  adding.** The completion `_write` - the one that writes the closing briefing
  - was the ONLY `_write` call in the collector with no `withhold_cost` on it,
  and it is the one message that talks about price by construction, because
  `SERVICE_BRIEFING_NOTE` requires a cost section. FOUR services in
  `COST_WITHHELD_SERVICES` brief at the end, so four closing messages have been
  relying on the prompt alone for the rule the agency gave by name on
  2026-09-04. The same "wired into two paths and never the third" shape as
  `blocked_topic_responder` on 2026-09-10. And a briefing swapped for the
  deferral is now recorded as LOST rather than delivered - marked given it
  would never be retried, which is the 2026-09-08 defect arriving in the branch
  added the same day as the guard.
  (I) **Fourteen faults injected, thirteen red. The one that stayed green is
  worth more than the thirteen.** Breaking a timeline needle in the loader
  changes nothing any check can see, because the needles have already been
  applied and an applied needle matches nothing by construction - the property
  that makes the loader idempotent is the same one that makes a broken needle
  invisible. The 2026-09-17 answer to this ("count through `old`, which is the
  half that has to match something") does not reach it. What protects those
  corrections is the live sweep in (D), not an assertion, and that is written
  down here rather than papered over. Two other greens were the checks and both
  are fixed: `"any experience is fine"` contains the word "experience" and
  needed the `_NO_PREFERENCE` test in front of the pattern, and the cost-guard
  injection had only MOVED the string, which a source-count assertion cannot
  see - re-injected as a clean removal it goes red in both suites.
  (J) **The salary-floor sweep was reading a figure and calling it a subject.**
  The 2026-09-17 checks selected the floor rules by `"S$650" in new`, and this
  round's timeline sweep edits the same nationality sentence a second time, so
  its `new` carries the corrected salary while its `old` has nothing to do with
  the floor. It read as a fourth floor rule with nothing to replace. Keyed on
  the OLD figure now. And the comment explaining that was caught by the needle
  sweep for quoting a replaced string - the third time that check has caught
  this file.
  (K) **Verified live against the real model, all 27 turns of their own
  transcript.** `requirement` stays "childcare" and the workload warning never
  fires; the elderly-detail question is never asked; the experience question
  offers no first-timer; and the collection closes with *"It takes
  approximately 4 to 6 weeks from signing with us for an overseas helper to
  start"*, the cost deferred to an agent, the two documents, the six steps and
  the handover line. The four questions he had to ask are all answered inside
  that one message, and asked again afterwards they answer consistently -
  *"and what is the timeline"* now returns **4 to 6 weeks** where it previously
  returned a handover.
  **Reported and NOT changed, because each is the agency's to settle:** the
  cost is still withheld on a new hire (their 2026-09-04 instruction against
  their 2026-09-17 flow); the salary bands offered to an Indonesian client
  still start below $500, because the Philippines is the only floor they have
  given; the name is still echoed in the client's own casing (section 9.24);
  and an Indonesian Buddhist helper was accepted without comment, which is a
  very thin pool.
  (L) **And the briefing deferred the timing in 1 run of 4**, which only
  showed up because the closing message was measured rather than read once.
  *"The timeline will be confirmed by our agent based on your requirements and
  selected helper"* is the "it depends" sentence the agency had removed from
  the OPENING overview on 2026-09-17 - and the rule against it lived only in
  that note, which `new_hiring` no longer gets. The closing note now carries
  it: give the span if the records state one, leave the line out if they do
  not, and never hand the timing to a person. The COST deferral is explicitly
  exempted in the paragraph beside it, or the rule would swallow the one this
  note asks for everywhere else. 9 runs of 10 after, and verified live on a
  MYANMAR passport renewal - no fee on record - where the timing is given and
  *"Our agent will confirm the exact fee for her embassy"* is untouched. Five
  more faults, five red.
  `selfcheck_flows.py` is **637 assertions**; `smoke_nodes.py` is **159
  states**.

- **2026-09-19** - **"hey i need helper" was answered with a decision about the
  client's household, and the question about that decision was answered with a
  menu.** The agency's new-hiring test, and the second half of it is a gap that
  had also been swallowing the HELPER's own objection two days earlier.
  (A) **What they saw.** *"hey i need helper"* -> *"Hi, I'm Claire, Ming Hwee's
  AI assistant. I'll ask a few details to understand your household and find a
  suitable match; may I know your name?"* -> *"how you know for what service i
  need helper"* -> *"We help with new hiring, direct hiring, replacement,
  transfer, Work Permit renewal, home leave arrangement and passport renewal.
  Which service do you need?"* Their words: *"their intent is not clear. I
  didn't say that I want household help. How can the bot say I want to
  understand your household?"*
  (B) **They are right, and the first half is the 2026-09-17 rule with a
  loophole in it.** That rule already said the reason for a run of questions
  belongs to the questions as a whole and must not be bolted onto the name
  question - and it asked for the reason as "its own CLAUSE". A clause joined
  by a semicolon is still its own clause, which is what shipped: *"...find a
  suitable helper; may I know your name?"* It is its own SENTENCE now, ending
  in a full stop, and the note names the semicolon, the dash and the "so" by
  which the model keeps re-welding it.
  (C) **And the reason itself was a decision about them.** *"so we can match a
  helper who actually suits THEIR HOUSEHOLD"* said to somebody who has written
  four words. Swept over the whole `_COLLECTION_PURPOSE` table rather than
  fixed on the one service, so a reason added tomorrow that describes the
  client fails by name.
  (D) **The opening turn now NAMES the service it understood.** *"So you are
  looking to hire a helper - I'll ask a few details..."* An inference the
  client cannot see is one they cannot correct, which is `recognised_note`'s
  argument from 2026-09-04 pointed at the service instead of at a record. It
  goes at the FRONT of the reason sentence and not as a sentence of its own,
  because measured, a third sentence of preamble pushes the question out of the
  message entirely - 0 of 4 on both the employer and the candidate side until
  the note said outright that the message still ends with the question, then
  8 of 8.
  (E) **The second complaint is a one-word gap, and it is the sixth of its
  family.** `_ASKS_SOMETHING` decides whether the collector knows a question has
  been put. It did not match *"how you know for what service i need helper"* -
  no question mark, and "how you know" is not "how do you know". So
  `ANSWER_THEN_ASK` never fired, nothing was answering anything, and the model
  simply restated what it was going to do. `_VALUE_IS_QUESTION`, which is
  broader, DID read four phrasings of this family as questions and threw the
  VALUE away - so the client's question was discarded AND unanswered in the
  same turn, which is the exact mismatch the note above that pattern was
  written about in 2026-09-02. **Measured: 5 of 8 phrasings disagreed**, and
  one of the five is the HELPER's own line from the passport transcript two
  days ago - *"why you are asking about my helper name"*.
  (F) **The answer to "how do you know" is their own words.** *"You said you
  need a helper, so I took that as hiring - tell me if it is something else."*
  Not the service list: a menu in reply to that question reads as though we are
  still guessing and answers nothing. And **not an apology** - the first version
  of the note produced *"You're right, I shouldn't assume the service. What
  would you like help with?"*, which leaves them with no service, no question
  and an apology nobody asked for. That is §9.19, which said a prompt rule
  against disowning our own replies was deliberately not taken because
  *"nobody has reported the shape twice"*. It has now been reported twice, so
  it is taken - and scoped to a reading that has been QUESTIONED rather than
  CONTRADICTED, so a real correction still switches the service.
  (G) **The helper's half, which is what the agency asked to be checked
  separately.** Her routing is right - *"hey i need job"*, *"i am looking for
  work"* both reach `candidate_registration` - but `IDENTITY` said *"the agency
  provides seven core services"* and listed seven employer services. Registering
  helpers is a built flow with 15 fields, its own closing briefing and its own
  lead table, and it was missing from the one place the bot says what we do.
  Live before: a job seeker asking what we provide got *"I'll confirm the
  suitable options with our team and come back to you."* After: *"We register
  helpers looking for work and match them with employers."*
  (H) **And the replay found a regression from this morning's commit.** With the
  passport branch now deciding on the OPENING message, `HELPER_OWN_PASSPORT_NOTE`
  was being read on the turn before she has given her name - and it asserted
  *"her name is already on file"*. The model did as it was told and skipped
  straight to the nationality, **0 runs of 4**, on the one field five separate
  complaints since 2026-09-08 have been about NOT being asked. The note now says
  the branch removes the HELPER-name question and never the client's own. 4 of 4
  after, and it has a smoke state of its own.
  (I) **Fourteen faults injected, fourteen red**, plus a separate check that the
  new smoke state bites on its own rather than riding on the predicate
  assertions - the "imported and never called" hole, guarded for the fifth
  time.
  (J) **Verified live against the real model.** The agency's two turns, the two
  ways a helper says she wants work, and an employer asking what we do before
  naming anything. The passport flows from this morning replay unchanged, both
  hers and the employer's.
  `selfcheck_flows.py` is **617 assertions**; `smoke_nodes.py` is **155
  states**.

- **2026-09-19** - **The disambiguating question lasted one round, and the
  agency were right to take it back.** Their words on reading it: *"when someone
  is messaging i want to renew my passport then what is the need for this
  question - the intent is clear naa, it means the user is helper and wants to
  renew their passport. if someone message like i want to renew my helper
  passport it means the user wants to renew their helper passport, then this
  question does not make any sense."*
  (A) **The question was buying a certainty the flow already gets for free.**
  The entry above argues at length that *"There isn't any helper here. I want to
  renew my passport"* and *"i dont have any helper"* are the same sentence, so
  guessing picks one of two bad outcomes. That is still true of the SENTENCE -
  and it stopped mattering the moment the refusal moved onto the nationality
  answer in the same commit. We renew through three embassies; a Singaporean
  employer reaches *"Which country is your passport from?"* and is refused one
  turn later on evidence. So the question was protecting against a case the
  next question already catches, and charging every helper who asks us plainly
  a turn for it.
  (B) **It is now decided on the OPENING message**, not on the turn the
  helper-name question would have gone out. That is the same reasoning the
  `_SAID_MINE` memory was built for, arriving at the simpler answer: if "my
  passport" settles it, it settles it where it is written, and the helper-name
  question is never queued at all. `_SAID_MINE`, `_HOLDER_ASKED`,
  `_HOLDER_ASKED_TWICE`, `_HOLDER_IS_A_HELPER`, `_PASSPORT_IS_MINE`,
  `_PASSPORT_IS_HERS` and `WHOSE_PASSPORT_NOTE` all existed only to ask the
  question and read its answer, and are gone with it.
  (C) **One case survives as a refusal on this branch rather than on the
  nationality**, and it is the 2026-09-17 client's other half: an employer who
  has ALREADY named his helper and then asks about *"my passport"*. A named
  helper is positive evidence that he employs one, which is exactly what the
  nationality test stands in for everywhere else - so `_whose_passport` returns
  `"own"` there and the refusal fires at once. Live: *"Lily"* -> *"and also i
  want to renew my passport"* -> *"We handle passport renewal for domestic
  helpers from the Philippines, Indonesia and Myanmar only."*
  (D) **A denial now beats the helper word in the RELEASE as well as in the
  decision.** The settled answer is released the moment a helper is named
  (2026-09-11), and *"i dont have any helper"* and *"i am a helper"* both
  contain that word - so read carelessly, either one silently undoes what she
  has just told us. Same precedence as (G) above, one branch along, and the
  reason it matters more now is that the flag is set on turn one and has four
  more turns to survive.
  (E) **Thirteen faults injected, thirteen red.** Both halves of the new
  decision; the `named_helper` guard in each direction; the `"own"` refusal and
  the nationality refusal separately; the flag unwritten and written-but-unread;
  the release undone by her own denial; the `helper_name` fill; and both notes.
  (F) **And one existing check was green for a reason unrelated to what it was
  checking.** The smoke state asserting she is not asked for a helper's name
  forbade the string `"field to collect: helper's name"`, which appears in no
  prompt this codebase builds - the collector writes *The office asks this as:
  "May I know your helper's name?"*. It would have passed with the fill deleted.
  Now keyed on the question itself, and used as a POSITIVE assertion on the
  employer control so the same string is proved to appear where it should.
  (G) **Verified live against the real model, four transcripts.** Hers:
  *"i want to renew my passport"* -> *"Could you share your name, please?"* ->
  *"my self kareena"* -> **"Thanks, kareena. Which country is your passport
  from?"** -> the expiry -> a briefing headed *"Here is everything for Kareena's
  passport renewal"* with *"1. A copy of your employer's NRIC 2. A copy of your
  Work Permit 3. A copy of your passport"*. **No helper-name question on any
  turn.** The employer's, unchanged turn for turn, list still *"1. A copy of
  your NRIC"*. The Singaporean, refused on *"Singapore"* and again on *"why
  not? can you still help me"*. And the employer in (C).
  `selfcheck_flows.py` is **606 assertions**; `smoke_nodes.py` is **149
  states**.

- **2026-09-19** - **The helper renewing her OWN passport was refused the service
  she was asking for, twice, by the branch built to protect it.** The agency
  tested passport renewal as a new user and this is the 2026-09-17 fix meeting
  the case it was written one client too narrow for.
  (A) **What she saw.** *"i want to renew my passport"* -> *"May I know your
  name?"* -> *"my self kareena"* -> *"Thanks, kareena. May I know your HELPER's
  name?"* -> *"i want to renew my passport then why you are asking about my
  helper name i dont have any helper"* -> **"Ming Hwee handles passport renewal
  for domestic helpers only, not clients' own passports."** She wrote back
  *"what ?? i can understand that i just to your agency to renew my passport
  because agency provide this service then why you are telling me these things
  ?"* and was refused a second time. Their instruction: the service is open to
  whoever approaches - the helper about her own passport, the employer about
  their helper's - "since the passport being renewed will always be the
  helper's passport, the flow should handle both scenarios accordingly".
  (B) **THE TWO CASES ARE THE SAME SENTENCE, and that is the whole difficulty.**
  The employer the branch was built for on 2026-09-17 wrote *"There isn't any
  helper here. I want to renew my passport"*; she wrote *"i dont have any
  helper"*. Nothing in either message says which of them is a domestic helper,
  so a pattern that guesses picks one of two bad outcomes: refusing a client we
  serve, or quoting a helper's embassy fee to a Singaporean. So the flow ASKS -
  once, and only where no helper has been named. An employer who writes "renew
  my helper passport" never reaches it, which is what leaves the working flow
  exactly where it was.
  (C) **The question arrives INSTEAD of the helper-name question, not after
  it**, which is the half the agency actually asked for. "my passport" is
  written in the OPENING message and by the turn it matters it is two messages
  back, so it is remembered (`_SAID_MINE`) rather than re-read -
  `_asks_about_own_passport` looks at this turn's message only, by design. Live
  after: *"my self kareena"* -> *"Is the passport for yourself or your
  helper?"* She is never asked for a helper's name at all.
  (D) **What finally settles it is a question the flow already asks.** The
  passport we renew goes through the Philippine, Indonesian or Myanmar embassy
  here; a passport from anywhere else is not a domestic helper's. So the
  refusal moved off a guess about who is asking and onto the nationality
  answer - the same `nationality_code` normalisation the retrieval filter uses,
  so it refuses exactly where the records could not have answered anyway. It
  costs the 2026-09-17 client one extra turn and buys back a client we were
  turning away. Verified live: *"Singapore"* -> the refusal, and *"why not? can
  you still help me"* still lands in the same branch rather than being met with
  the next collection question (the 2026-09-11 rule).
  (E) **Her name IS the name on the passport**, so `helper_name` is filled from
  it and the question is never put. Filled early, before `missing_fields` runs,
  because that is what decides which questions are left - the two are eighty
  lines apart and a fill placed after it changes nothing.
  (F) **Her briefing had to say whose documents are whose.** All 21
  `passport_renewal` rows are written to the employer, so "a copy of your NRIC"
  in them means HIS. Sent to her unchanged the list asks a Work Permit holder
  for an NRIC, which is the 2026-09-17 contradiction arriving from the opposite
  direction. Live after: *"1. A copy of your employer's NRIC 2. A copy of your
  Work Permit 3. A copy of your passport"*. Nothing else is watered down -
  same timing, same $450, same steps, because the agency asked for "process,
  documents, timeline, fees accordingly".
  (G) **A denial beats the word "helper" inside it, and the LIVE REPLAY is what
  found that** - planning it, before running it. Her own reply to the new
  question is *"...why you are asking about my helper name i dont have any
  helper"*, and read helper-word-first that is an answer of **"my helper's"**,
  the exact opposite of what she said. `_OWN_PASSPORT_EXPLICIT` is tested first
  now, the same precedence it already has one function along, and for the same
  reason: a denial names a helper in order to say there is not one.
  (H) **One service key, not two.** `transfer`/`transfer_employer` is the
  precedent for splitting by audience, and it was measured and rejected here:
  all 21 rows are `contact_type='all'`, so she can already retrieve every one
  of them, and a second key would need its own ticket fallback, retrieval
  alias, lead kind, briefing key and a dozen derived sweeps. The accepted cost,
  recorded rather than buried: her enquiry still opens an EMPLOYER lead,
  because `kind_for` keys on the service. The ticket carries the truth and
  nobody has asked for the lead table to change; `kind_for`'s `transfer` branch
  is the shape of the fix if they do.
  (I) **Fourteen faults injected, fourteen red - after one came back GREEN and
  it was the check.** Nothing asserted that her briefing note ever reaches the
  prompt: the note existed, was correct, and was checked by reading its
  CONTENT, so deleting the line that appends it left every assertion passing.
  That is the "imported and never called" hole for the fourth time (2026-09-10,
  -16, -17, here), and the cure is the same one - a state that RUNS the node
  and reads the system prompt it was handed, plus an employer control on the
  same turn so it cannot pass by firing on everything.
  (J) **Verified live against the real model, three transcripts.** Hers, turn
  for turn, ending in the briefing above. The employer's from the same day -
  *"Hey i want to Renew my Helper Passport"* - which is **unchanged in every
  turn**, including *"1. A copy of your NRIC"*, which is correctly his. And the
  Singaporean, refused.
  `selfcheck_flows.py` is **611 assertions**; `smoke_nodes.py` is **148
  states**.

- **2026-09-19** - **Passport renewal: two names for one person in one message,
  and the only timeline we stated as a hard number.** The agency tested it and
  reported two things, both small and both right. A third was found underneath
  and is left alone deliberately.
  (A) **"it should not be (Consultant) it should be (Our agent)".** They
  pointed at one line of the closing briefing - *"A consultant will confirm the
  exact cost for her embassy."* **The argument for doing it everywhere is four
  lines below that sentence in the same message**: it closes with *"a live
  agent will connect with you shortly"*. One message, two names, one person,
  and the client has no way of knowing they are the same. The word is not a
  fact about passport renewal - it is what the agency calls its own staff - so
  it moved on every service, the cost-deferral fallback, and 14 live rows.
  (B) **The two guards keyed on the old word would have gone quiet, and that
  is the half that mattered.** `_CONTACT_PROMISE` decides whether a time
  inside a numbered step is a callback nobody promised, and it matched
  `consultant will` and `live agent`; `_ANNOUNCES_HANDOVER` decides whether a
  closing briefing announced the handover at all, and matched the same two. A
  find-and-replace would have left *"our agent will call you within 2 hours"*
  walking straight through the first and a briefing ending on *"our agent will
  be in touch"* reading as a briefing that ended on nothing. Both take either
  word now, so a row or a reply that has not caught up is still caught.
  (C) **"Renewal of Indo passport may take more than 3 working days."** Three
  rows said *"approximately 3 working days"* flat, and Indonesia was the only
  one of the three nationalities claiming a hard number with nothing about the
  wait in front of it: the Philippines says 6 to 8 weeks, and Myanmar says
  outright that the appointment slot is the unpredictable part. The agency gave
  no replacement span, so **none is invented** - the figure is stated as the
  floor it is, and the rest is deferred to a person. A plausible-sounding span
  here would reach a client as though it came from them.
  (D) **Corrected in the entries that already own those rows, not in new
  ones.** A second `UPDATES` entry for a question that already has one is the
  2026-09-09 defect: the later of the two wins on every run and the loader
  stops being idempotent. Two were edited in place and the third - the ID
  process row, which carried the same figure in its last clause and had no
  entry - got a new one. Correcting one and leaving the other is how the
  knowledge base ends up stating two timelines for one service (2026-09-10).
  (E) **The needles were not enough, and reading the rows BACK is what showed
  it.** Two phrasings covered nine rows; a live read-back found five more in
  the imported material under wordings the needles could not see - *"fully
  managed by our consultants"* and *"A Ming Hwee consultant will reach out
  within 24 hours"*. Two more needles, deliberately longer than they need to
  be: `"Ming Hwee consultant"` alone would also have rewritten a line of
  `scripts/TEST_SCRIPT.md` that no client reads. The same shape as the S$570
  salary figure turning up in two more rows on 2026-09-17, and the same cure -
  sweep the database, do not assume the first pass was the whole set.
  (F) **The self-check caught ITSELF, twice, and both times it was right.**
  The sweep asserts that no file but the loader carries a replaced string, so
  the first draft of the new assertions failed for quoting the agency's own
  sentence - which is exactly what the phone-number check did to itself on
  2026-09-11. The old phrasing is now read FROM the loader's needles by
  `_was_called()`, which returns a placeholder rather than raising if they
  vanish, so a missing needle is a RED line naming the assertion instead of a
  traceback (2026-09-10).
  (G) **One injection came back GREEN and it was the check.** *"our agent will
  call you within 2 hours"* stays matched with the agent clause removed,
  because **"call you" is a trigger in its own right** - so the assertion
  proved the guard works and said nothing whatever about the word this commit
  changed. Reworded to a sentence carrying no other trigger, plus a control
  that a step promising nobody anything is still left alone. Ten faults, ten
  red after it.
  (H) **Verified live against the real model, both routes, the agency's own
  five turns.** Myanmar: *"Our agent will confirm the exact fee for your
  Myanmar helper's passport renewal."* with the closing line unchanged, so the
  message now names one person twice instead of two people once. Indonesia:
  *"It takes more than 3 working days."* and *"The cost is approximately $450,
  and our agent will confirm the exact fee for your situation."* The rest of
  the flow is untouched - four questions, no question repeated, and the
  expiring-passport warning still fires on a passport three weeks out.
  **Reported and NOT changed, both flagged to the agency rather than guessed
  at.** The Myanmar document list asks for *"a copy of her Singapore work
  pass"* and *"a copy of her Work Permit"* as two numbered items, which for a
  helper is one card; it survives in the two rows the 2026-09-08 rewrite did
  not reach, which is why an Indonesian renewal does not show it. And the
  closing process told the employer to *"sign and return the Application for
  Passport Renewal Form"*, which our own records say is the form **she**
  completes. Both are content questions only Ming Hwee can settle, and the
  agency asked for the first to be left alone until they have checked their own
  checklist. Five internal pipeline chunks are §9.25.
  `selfcheck_flows.py` is **593 assertions**; `smoke_nodes.py` is **140 states**.

- **2026-09-18** - **Direct hire: the process arrived on question two, the start
  date never arrived at all, and one retried send silenced the conversation for
  good.** The agency's own test. One of the four things here is what they
  reported; the other three were found underneath it and the last is the most
  serious thing in this round.
  (A) **"after asking the name question why bot is telling these thing [Direct
  hire involves processing the MOM application, documents, insurance and bond,
  and getting the helper here and settled] ... this is the process related and
  documents related things we should tell this at last with the process,
  requirements, timeline, cost/fees when all the requirements are gathered bot
  have to message these things in single message without waiting for the user
  to ask for."** They are right, and the fix is one table entry: adding
  `direct_hiring` to `BRIEFING_AFTER` gives it the closing briefing AND removes
  the opening overview, via the test `briefs_on_this_turn` has carried since
  2026-09-09 - a service that briefs at the end does not also brief at the
  start. The same trade `passport_renewal`, `renewal`, `replacement` and
  `transfer_employer` have all now made.
  (B) **Their transcript is the argument twice over.** It closed on the bare
  handover line, and he then asked "what is the further process", "And what are
  the documents required ?" and "And how much time this process takes" - three
  messages, all three answered correctly and in full, and not one of them a
  question he should have had to think of.
  (C) **Keyed on `helper_availability`, the LAST REQUIRED field**, for the
  reason spelled out four times in that table: the retriever runs before the
  collector, so the briefing has to be due one turn before the collection
  completes or it is built with no records (the 2026-09-09 defect). The three
  questions after it are all optional, and keying on one of those loses the
  briefing entirely whenever a client declines it - which is not hypothetical,
  it is what their own 2026-09-17 transcript did with `helper_contact` ("I'm
  not comfortable to provide this information now"). Measured before it was
  added: all four sections come back inside the top ten and above the floor -
  the process (0.653), what happens at the end (0.623), the documents from the
  client (0.572), the step-by-step (0.572), the documents we prepare (0.563),
  the cost (0.541) and both timelines (0.535 and 0.504). No query change, no
  new row.
  (D) **The ticket said the helper's availability was "Myanmar right now".**
  Nobody reported this and it is only visible on the ticket. Asked "Is she
  currently in Singapore, working under a Work Permit with another employer?"
  he answered "No she is on Myanmar right Now", and the extractor returned the
  transfer-case answer AND `helper_availability: 'Myanmar right now'` out of
  the same seven words - reproduced 3 runs of 3. A filled field is never put to
  anyone, so "When would she be available to start?" was never asked, and
  CB-2026-0009 reached a consultant with a country where a start date belongs.
  The test is an ECHO of an answer we already hold, NOT "does this state a
  time": the value genuinely contains one - "right now" - and it is attached to
  the country. Derived over the QUESTION rather than written as a list of keys,
  so all eight when-fields across the nine flows are covered, and gated on the
  field never having been asked - once asked, their answer is their answer.
  (E) **And the REPLAY found a second one, on the field that decides
  everything else.** "i want to do direct hire" filled `helper_transfer_case`
  with the value **"direct hire"**, 4 runs of 4 - so the one question that
  establishes the route was never asked. That is not a cosmetic loss: it is the
  difference between 2-3 weeks and 4-6 in the closing briefing, and it decides
  whether the notice-period question is asked at all. The 2026-09-07 care-type
  defect one flow along, and `replacement_preferences` one field along. Scoped
  to the service IN HAND, and the two controls are why: `current_helper_exit`
  offers "going home", which reads as `home_leave`, and `transfer_direction`'s
  junk value reads as `transfer` rather than `transfer_employer`, so this
  morning's machinery is untouched rather than quietly duplicated.
  (F) **The same reply went out twice, and that is not the defect - it is the
  symptom of one that ends the conversation.** Live on conversation 3766,
  "Usually about 4 to 6 weeks for a direct hire from overseas." was sent at
  14:20:36 and again at 14:20:38, 1.9 seconds apart, which is `_post`'s own
  1.5s backoff. It retried on any httpx error and any 5xx - and a ReadTimeout
  or a 502 means the request was WRITTEN and the response was lost, not that
  nothing was delivered. Whapi has no idempotency key, so the second POST is a
  second WhatsApp message with a NEW id, and only the id of the attempt that
  finally returned is handed back to `send_bot_reply`. So only that one was
  marked as ours.
  (G) **When Whapi echoed the FIRST copy back, we did not recognise our own
  sentence.** `was_sent_by_bot` had never heard of its id, `handle_outbound`
  read it as a human agent picking the thread up, and the bot stood down. Read
  from the live rows rather than inferred: the two outbound messages are on the
  database as `is_bot=False, sent_by='agent'` and `is_bot=True, sent_by='bot'`,
  the conversation flipped to `human_active` at 14:21:16, and the client's next
  message - "ok and what is the fees for this", 14:21:13 - **got no reply at
  all**, on a thread that was still `human_active` when this was written. A
  retry here does not cost a duplicate message. It costs the conversation, and
  the transcript blames an agent who was never there.
  (H) **Both locks, because the second is the one that matters.** `_post` now
  retries only where the request provably never reached Whapi - a connection
  never established, or one that never left the pool - and everything else is
  reported and not repeated. And `handle_outbound` recognises our own words
  whatever id they arrive under, keyed on the recipient as well as the text so
  an identical line sent to two clients cannot mask a real agent on one of
  them. The body is marked BEFORE the request goes out, because the echo can
  beat our own insert - live, by 1.9 seconds. The duplicate is then stored as
  OURS, not as an agent's: a row saying `sent_by='agent'` is what
  `last_agent_message_at` would later measure an idle window against.
  (I) **Twenty-seven faults injected, twenty-seven red - after two came back
  GREEN and both were the checks.** Removing the `not asked.get(key)` gate from
  the restates-the-service rule stayed green, because nothing asserted that a
  client who IS asked and answers with the service name is believed; and
  removing its three-word length guard stayed green, because nothing asserted
  that an answer merely MENTIONING the service still lands - "as soon as the
  direct hire is approved" is a real answer to "when would she be available to
  start?". Both controls exist now and both injections go red. A green
  injection is a result about the injection, which is the fifth time this file
  has recorded that and the first time it has been true twice in one run.
  (J) **Verified live against the real model, both routes.** His own
  transcript: no overview on question two, the route question asked, the start
  date asked and answered, and a closing message carrying "It takes
  approximately 4 to 6 weeks", "A consultant will confirm the exact fee for
  your situation", the two documents, the five-step process and the handover
  line - with `helper_transfer_case: no` and `helper_availability: next month`
  on the ticket. The control, a helper already here under another employer:
  the notice question is asked (it was skipped entirely before, because the
  gate had never been decided) and the briefing says **2 to 3 weeks** rather
  than 4 to 6, which is the whole reason that field exists.
  (K) **The live thread was put back by hand.** Conversation 3766 was still
  `human_active` days later, so `unsilence_conversation.py` was run on it -
  thread and checkpoint kept, nothing collected discarded. One other
  allowlisted conversation is stood down (id=26, 6597499527) and is left alone:
  its cause was not investigated and it may be a real agent.
  `selfcheck_flows.py` is **584 assertions**; `smoke_nodes.py` is **140 states**.

- **2026-09-18** - **An employer transfer: told he was asked which kind of transfer
  he meant, asked twice about his own children, and handed over without being told
  anything.** The agency's own test, and the first complaint is the one that
  unpicks the other three.
  (A) **"why is bot asking that are you looking to take on transfer helper already
  in singapore or you want to release your current helper ... if someone is coming
  and telling that i want transfer helper it means user intent is clear".** They
  are right, and this is the fix **§9.12 named and did not take**: "resolving the
  direction from the client's own opening message, which almost always says it".
  `transfer_direction` has options and the extractor is not constrained to them, so
  it keeps returning the bare word **"transfer"** - true, useless, recognised by
  neither gate. `_undecidable_gate_keys` then does its job correctly and re-asks,
  which is how a question the client had answered in his first four words came back
  at him.
  (B) **The two patterns are deliberately not mirror images**, and that asymmetry is
  the whole of the accuracy: a release is always written with a possessive
  ("transfer MY helper", "release HER"), taking one on is written with "transfer" as
  an adjective ("a transfer helper"). So "transfer my helper" matches only the
  release pattern and "transfer helper" only the take-on one, which is the pair of
  sentences this has to tell apart. 19 phrasings measured, 13 decided and 6 left
  alone - it **fails towards asking**, so a message matching both, or neither,
  leaves the question exactly where it is.
  (C) **Applied AFTER the extraction, not inside `_known_fields`.** The value it has
  to beat is the extractor's own, and `known` goes in UNDER the extraction by design
  (2026-09-17, so a client correcting our records wins). A fill placed there would
  lose to "transfer" every time - which is §9.12, and is the defect.
  (D) **And the half that makes the rest hold was found by REPLAYING the transcript
  rather than by reading the code.** `collected = {**previous, **extracted}`, so the
  extractor handing the same undecidable word back on a LATER turn silently
  overwrites a direction already settled and the question returns. Live: turn one
  settled it, turn two ("myself sanjay dutt") returned "transfer" once more, and the
  disambiguating question came straight back. A value that opens no branch may not
  replace one that does. A real correction decides something, opens the other gate,
  and still wins.
  (E) **Turn ONE was running the wrong questionnaire entirely, and nothing in the
  report said so.** `_HELPER_SPEAKING` matched "i want transfer" inside **"hi i want
  transfer helper"** - its lookahead excluded "my/our/the" and not a word for a
  helper - so the sender was read as the HELPER, the candidate flow ran, and "may I
  know your name?" meant HERS. He answered with his own name, the contact type
  firmed up to employer on the next turn, and `switched` then wiped the collection.
  Swept as a SET in both directions, six employer phrasings and six of hers, because
  the cost is symmetrical: an employer in her questionnaire, or a helper in his.
  (F) **"i have 4 childrens and all are under 15" -> "How many people live in your
  household, and who are they, such as adults, elderly parents or children?" -> "AS
  I TOLD THEN WHY ASKED ME AGAIN".** They are genuinely different questions - twelve
  people is not four children, and the count is what sizes the job - so the question
  stays and only what it asks FOR narrows. It is the residue of the 2026-09-17 fix
  that added "and who are they": before that it was a bare headcount and could not
  collide with anything. Derived from the fields gated on `requirement` rather than
  from a list of two keys, and it **names the rule it overrides**, because the
  general instruction above it ("ask for everything that question asks for") beat it
  otherwise - the 2026-09-07 languages defect.
  (G) **"i want childcare then why you are asking the cooking related question".**
  The old wording took it as read that she would be cooking and asked only which
  kind. Reworded rather than **gated**, and that is the decision rather than the lazy
  option: a gate on `requirement` would make `_gates_are_exhaustive` TRUE for that
  field - measured, childcare opens children_detail, eldercare opens elderly_detail,
  "all of the above" opens children_detail and a cooking gate would cover "general
  housework and cooking" - which switches `_undecidable_gate_keys` on and reinstates
  the 2026-09-08 defect where "General house work" was blanked and re-asked three
  times. An employer hiring for childcare may still want her to cook for the
  children; it was the presumption that was wrong. It now opens with an auxiliary,
  so a bare "no" closes it (2026-09-09).
  (H) **The closing briefing, which is what they actually asked for.** Keyed on
  `rest_day`, which is neither the last field nor the obvious one. NOT the last,
  for the reason spelled out three times in `BRIEFING_AFTER` - the retriever runs
  before the collector. NOT `referral_source`, the true second-to-last question,
  because `_known_fields` fills it from the RECORDS for any returning client
  (2026-09-08): keyed there the briefing would be due from turn one and the
  retriever would spend a twenty-question intake searching for a briefing instead
  of for what the client just said.
  (I) **Measured before it was added, because a briefing with no records is the
  2026-09-09 defect.** BRIEFING_QUERY under `transfer_employer` at
  BRIEFING_MATCH_COUNT returns all four sections above the floor: the employer's own
  document checklist (0.576), the timeline and process row (0.557), the steps
  (0.549), the forms we prepare (0.547), the releasing employer's own documents
  (0.547) and the cost (0.539). No query change and no new row. `transfer_employer`
  is in COST_WITHHELD_SERVICES, so the cost section defers to a consultant exactly
  as `replacement` does.
  (J) **A withheld price is said as what WE will do, never as a gap in our files.**
  Verifying (H) produced "The transfer fee is not stated in our records, so a
  consultant will confirm the exact amount" - true, and it tells the client about
  our filing and reads as though we do not know our own prices. The same rule the
  opening overview has had since 2026-09-17, now on the closing briefing.
  (K) **Eighteen faults injected, eighteen red - after one CRASHED and one came
  back GREEN, and both were the check rather than the code.** Removing the briefing
  entry made an assertion raise `KeyError`, so the harness printed a traceback and
  no FAIL line: **a crash tells you less than a red**, for the third time
  (2026-09-10, 2026-09-17, here), and it uses `.get()` now. The green one deleted
  the noun-phrase half of the take-on pattern and every assertion held, because
  every sentence being tested was also caught by the verb half - so a phrasing no
  verb of ours appears in ("can you find me a transfer helper") was added, and it
  goes red.
  (L) **And the live replay was wrong three times before the code was, which is
  worth more than the fixes.** A harness that replays a transcript has to get three
  things right or it grades a conversation that cannot happen: `collected_info` and
  its three siblings are **reducer** fields, merged and not overwritten (the
  2026-09-09 lesson, learned again); the per-turn blanking is `graph._TURN_RESET`
  and nothing else, and `intent` is NOT in it; and the transcript is rendered
  **"Client:" / "You:"**, which is `message.format_history`'s own format. Written
  any other way, `last_bot_line` finds nothing and `answering_our_question`,
  `strip_repeated_opener` and `near_duplicate` are all silently switched off - which
  showed up as the transfer drifting into `new_hiring` halfway through, a defect
  that does not exist.
  (M) **Verified live against the real model, three full replays of their own
  twenty-turn transcript.** The direction question appears on **no turn of any of
  them**; the household question reads "And who else lives in the household besides
  the 4 children? How many people live there altogether?"; the cooking question
  reads "Would she need to do any cooking, and if so, what kind"; and the flow
  closes with the timeline (1 to 2 weeks from the interview), the cost deferred to a
  consultant, the two documents, the five-step process and the handover line, with
  `transfer_direction` on the ticket as "taking on a transfer helper" without the
  question ever being put.
  `selfcheck_flows.py` is **557 assertions**; `smoke_nodes.py` is **124 states**.

- **2026-09-18** — **The same fee question, asked twice, answered neither time.**
  Reported as the renewal flow collecting slots and dumping to a live agent without
  ever answering. Second distinct failure in this flow in a day, and the reporter's
  sharpest observation — *"same question, same flow, different behaviour ... suggests
  non-deterministic routing"* — is the one that found the defect.
  (A) **Three quarters of it was already fixed and undeployed.** The four routing
  shapes were measured at HEAD, 4 runs each: the fee question **opening** the
  conversation answers 4/4; **after our own greeting** 4/4; **labelled as the service
  rather than as money** 4/4. The 12:05 session predates the deploy of the fix
  committed hours earlier, which is also why it ran `renewal`'s fields rather than
  `fee_enquiry`'s. Measuring first is what stopped three of the four "fixes" from
  being written against behaviour that already worked.
  (B) **The fourth shape failed 0 of 4, and it is the last turn of the transcript.**
  *"i have asked for the fees"* names no service, so the money rule from this morning
  cannot fire, and the classifier's stickiness deliberately does NOT glue a question
  back onto a **parked** topic — that is the 2026-09-03 rule, and it is right. So the
  turn resolved to a bare `fee_enquiry`, `_search_query` blanked the topic on the
  grounds that a money question whose service is also money "is the whole
  conversation", and the query went out **bare**.
  (C) **It came back at 0.436 — ABOVE the soft floor — on three question-less
  `document_chunk` rows carrying no fee at all.** A confident score on rows that do
  not answer, which is the 2026-09-08 clause-3.1 shape exactly, and why nothing
  anywhere registered a problem.
  (D) **"The whole conversation" is true of an opening "how much do you charge?" and
  false of a repeat.** `_subject_service` recovers the last real service — the flow
  that ran, then the parked topic — for **retrieval only**, the `_RETRIEVAL_ALIASES`
  precedent: the ticket, the lead, the field list and the blocked-topic key all still
  say `fee_enquiry`, so nothing re-parks the topic or drags the turn onto it.
  (E) **That was not enough, and the four thousandths are the reason.** With the
  subject restored the right row came top — *"How much does it cost to renew my
  helper's work permit?"*, $695 in the set — at **0.399**, under the 0.40 floor, so
  `weak_retrieval` **replaced the reply and handed over anyway** with the answer
  sitting in the context. The same four-thousandths shape as the 2026-09-10 candidate
  retrieval at 0.397.
  (F) **Fixed by naming the subject properly rather than by lowering the floor.**
  `renewal` is the one service key that does not read as itself — "renewal", of WHAT? —
  while all 20-odd rows it must match say **work permit renewal**. Measured through the
  real retriever: the process 0.464 → **0.661**, the timing 0.509 → **0.753**, when to
  start 0.567 → **0.723**, what the employer does 0.584 → **0.687**, the documents
  0.625 → **0.711**. **Better on 5 of 5, worse on none**, so this is a general subject
  fix and not a money patch; the repeated fee question goes 0.399 → **0.558**. Lowering
  `RAG_SOFT_FLOOR` would have bought the same turn by loosening every path in the
  system, which is the opposite trade.
  (G) **And with the records recovered the model still would not use them, 1-2 runs of
  4.** *"i have asked for the fees"* is a complaint, not a question, so it reached for
  *"I'll check the fees and come back to you shortly"* — **the exact line the client
  was objecting to**. `asks_again` is deliberately NOT a question detector:
  `asks_something` already sees the word "fees", and what no existing pattern could see
  is that this is a SECOND attempt, which is the fact that decides whether
  acknowledging is reasonable or the worst possible reply. All six live phrasings
  scored False on every detector we had. 3-4 of 4 after, against 0 of 4 before.
  (H) **Both answering paths, one definition** (§9.8). The parked path is where the
  live client actually was, and `asks_general_info` missed every repeat phrasing too —
  the sixth arrival of that one-word-gap family. It sits BELOW the chase test on
  purpose, and that costs one phrasing: *"i am still waiting for the fees"* reads as a
  chase and is still held. A parked topic silences chasing, the two genuinely overlap
  in those words, and `_answerable()` still requires records above the floor, so the
  failure is a holding line rather than an invented answer.
  (I) **Gated on having records**, so the note can never turn an honest "I don't know"
  into a figure — asserted in both directions.
  (J) **Eight faults injected, eight red — and the parked-topic state was GREEN under
  its own fault, for the fourth distinct reason this file has now recorded.** The
  stubbed model hands back the same reply whichever instruction won, so reading the
  reply could not tell "decided to answer" from "decided to acknowledge and happened
  not to bin my stub". It proved the guards do not discard a grounded fee, which is
  not what it claimed. `_run_blocked_topic_responder` now captures the system prompt
  the way the other two runners do, and the state asserts the ANSWER instruction was
  chosen; `intent` is deliberately `renewal`, which is **not** in
  `KB_QUESTION_INTENTS`, so `_answerable` has to reach `asks_general_info` to get
  there. The same state had also failed on its first run for the opposite reason —
  the stub quoted $695 while that state's records were about passport timing, so
  `ungrounded_figures` correctly binned it. **A state that expects a figure has to
  ground it, and a state that expects a decision has to read the decision.**
  (K) **Two of the reporter's six requested fixes were already satisfied and one rests
  on a wrong premise**, and saying so is cheaper than building them. *Answer first,
  qualify second* is what the morning's fix already does — turn 2 now reads *"The work
  permit renewal fee is approximately $695 ... May I know your name?"* *Make routing
  deterministic* describes a real symptom with the wrong cause: routing was already
  deterministic, conditioned on something invisible — whether our own greeting ended in
  a question mark, which flips `answering_our_question` and sends the turn to the
  collector instead of the responder. Both paths answer, so it no longer matters.
  **Not changed, and it is cosmetic:** the model echoes a client's own capitalisation
  ("sushi" then "Sushi"). `RECORD_NAME_NOTE` forbids changing the SPELLING of what they
  wrote, name handling has been reported five times, and normalising it is a persona
  decision rather than a defect. §9.24.
  `selfcheck_flows.py` is **518 assertions**; `smoke_nodes.py` is **116 states**.

- **2026-09-18** — **"what do you mean by care? i am not asking for any new hiring...
  i came here for work permit renwal."** The agency's own test, 23:05, reported as the
  renewal flow leaking new-hire questions. It is not that, and the difference is the
  whole fix.
  (A) **Reproduced 1 run of 1 before anything was touched, and it is WORSE than the
  report.** "what is the fees for work permit renewal?" → *"...approximately $695 ...
  Which nationality are you looking at?"* → "indonesia" → *"What kind of care would this
  be for?"* → "i didn't understand" → a correct rephrase of the same question → and then,
  after the client wrote the sentence above, **the care question a third time**.
  (B) **Those two questions are `fee_enquiry`'s own fields, verbatim** — `nationality`
  and `care_type` — not `new_hiring`'s. Nothing leaked and no flow was mixed: the
  renewal flow never ran, because the turn never resolved to `renewal`. `fee_enquiry` is
  a *service* with a two-field intake, and those fields are hiring-shaped because they
  exist to pin down a question that cannot be answered without them.
  (C) **Three of the four suspected causes in the report are not what happened**, and
  acting on them would have made it worse. The intent was `fee_enquiry` on **all four
  turns** — perfectly stable — so *locking the active intent* would have locked the wrong
  one harder. `renewal` has four fields, so *"the renewal flow has no slot list"* is not
  the case. And the clarification handler asked for already exists and already ran: turn
  3 **did** rephrase (*"Sorry, I meant what care will the helper provide..."*). It
  rephrased a question that should never have been asked, which is why rephrasing harder
  is not the fix.
  (D) **It is the 2026-09-07 defect** (*"But I come here for passport renewal not for
  care"*) **arriving through the one door that fix left open.** That one keys on another
  service being ESTABLISHED, and `_other_service_established` deliberately leaves an
  opening money question collectible so a bare *"how much do you charge?"* can be
  qualified at all. Here the client named the service **in the message asking the
  price**, and nothing read it — `_named_service` has returned `renewal` for that
  sentence the whole time and is consulted on no money path.
  (E) **The SERVICE moves and the INTENT deliberately does not.** That is the entire
  mechanism: with `service_type` set, `_other_service_established` is true, so
  `route_after_rag` sends the turn to `response_generator` and the price is **answered**.
  Promoting the intent as well routes it to the collector and opens the named service's
  own four-question intake — a price question turned into a form, which is the defect
  being fixed one service along. Both halves are asserted, because a check reading only
  the service stays green with the intent promoted too.
  (F) **The answer was in hand before the first question was asked** — $695, retrieved
  on turn 1 at **0.654**. Three questions were put to a client to reach a figure the bot
  already held.
  (G) **And the obvious fix would have introduced a WRONG PRICE, which is the more
  serious half of this commit.** `_NAMED_SERVICE` tested `\brenew` before `\bpassport`,
  so **every** passport phrasing resolved to `renewal` — the work permit service —
  measured 4 of 4. A passport renewal is **$450** and a work permit renewal is **$695**,
  and `FEE_STATED_SERVICES` holds both, so the wrong figure goes out **stated** rather
  than deferred, and `ungrounded_figures` waves it through because $695 really is in the
  records. The table had reasoned about exactly this hazard for insurance
  (*"renew my insurance names both"*) and not for passports. Live before: *"how much for
  passport renewal"* → **$695**. After: **$450**.
  (H) **An existing assertion went red, and the check was wrong rather than the code.**
  *"naming another service is still a real switch"* pinned
  `_named_svc("i also want to renew my helper passport") == "renewal"` — its CLAIM is
  satisfied by any truthy answer, and the value it recorded was simply what the function
  returned. So the tripwire was **holding the defect in place instead of catching it**.
  Corrected to `passport_renewal` with its reasoning kept (§0.3).
  (I) **Five faults injected; one came back GREEN in both suites and it was the checks.**
  Widening the rule to fire on every turn rather than only a money one left all four new
  states passing — so they proved what the rule does on a money turn and said nothing
  about any other turn, where it would hijack *"her passport is expiring"* mid-hire, the
  case `_WANTS_SERVICE` guards. A second control now asserts an ordinary turn naming a
  service is left alone, and that injection goes red. Fourth time a green injection has
  been the injection's or the check's fault and not the code's.
  (J) **`intent_classifier` had no execution cover at all** until this commit, which is
  why (I) was possible. Every classifier fix in this log lives in the post-processing
  that runs on top of the model's verdict — the stickiness rules and the named-service
  corrections — and all of it was tested by calling predicates. `smoke_nodes.py` now runs
  the node with the model's verdict stubbed to what it returned live.
  (K) **Verified live against the real model.** The reported turn: *"The work permit
  renewal fee is approximately $695, and the exact amount will be confirmed"* — and the
  care question appears on **no turn of the transcript, ever**. Passport → **$450**.
  Transfer → the consultant deferral, no intake. Home leave → $400/$250 by nationality.
  The three controls are untouched: *"how much do you charge"*, *"how much does it cost
  to hire a helper"* and *"what salary should I budget"* all still reach `fee_enquiry`'s
  two questions, which is what that carve-out is for.
  **Not changed, and recorded rather than rushed:** replaying the original client's words
  after the fix, *"indonesia"* — now a reply to a message that asked nothing — was
  extracted into `helper_from_us`, whose three values are record-derived strings about
  where a helper came from, not a country. It is a mis-extraction on a turn that can no
  longer arise from this path, nobody has reported it, and it is collection gating, which
  §9.12/§9.21/§9.22 all say is not to be changed in a hurry. §9.23.
  `selfcheck_flows.py` is **507 assertions**; `smoke_nodes.py` is **112 states**.

- **2026-09-17** — **"is Polo's current Work Permit from Ming Hwee, or was she hired
  elsewhere?" — asked of a client we have never placed anyone with.** The agency
  tested the work permit renewal built earlier the same day and objected to the
  question that build added: *"chatbot should check the backend - if the user exists
  then this question didn't come, and if the user is new then also this message should
  not, because if the user is new it means the work permit is not from Ming Hwee, then
  why this question come."*
  (A) **They are right in both directions, and the second half is the one that settles
  it.** `prior_hires` counts every non-archived `placements` row on this number, and the
  transcript's client had none — no employer record at all, which is why the flow had
  just asked them their own name. **A zero is not an absence of evidence, it is the
  answer**: we cannot have placed this helper with an employer we have never placed
  anyone with. That is exactly the reading `first_time_hire` has taken since 2026-09-04
  (*"not being in the database IS the answer"*), and it carries the same accepted cost
  in the same words — a client who hired through us on a different number reads as *"no
  placement on record"*, which is a statement about our records rather than about them,
  so a consultant can tell the two apart.
  (B) **This REVERSES the note written on the field this morning**, and that note is
  corrected rather than deleted (§0.3). It argued the question had to be asked because a
  positive count says whether we placed ANYONE with them, not whether we placed THIS
  helper — `placements.candidate_id` is null on most rows, so there is nothing to match
  her against. That half is still true, and it is why the third branch exists. It was
  never an argument for asking a brand-new client, **which is most of the people this
  service is for** — the agency's own reason for wanting the distinction was that work
  permit renewal *"is not limited to existing agency clients"*.
  (C) **Three branches, and the third is the honest one.** No placement → *"hired
  elsewhere - no placement on record"*. A single live placement that names her →
  *"from Ming Hwee - placed by us"*, which is exactly as safe as the `helper_name` fill
  already riding on that same row. Placements on file but none we can pin to a helper →
  *"placed with us before - this helper not matched on file"*. Claiming her there would
  put a guess on a ticket, which is the failure `get_placed_helper` was deliberately
  made cautious to avoid (2 of 6 live rows name a candidate, and one employer holds four
  placements); asking is the question just removed. The consultant has her name on the
  same ticket and the placement list one click away.
  (D) **No branch carries a digit, and that is not tidiness.** These values reach the
  prompt as `collected_info`, which is grounding for `ungrounded_figures` — a count in
  here is a number the model may then quote at the client, the 2026-09-09 (D) shape.
  `first_time_hire` says *"2 placements on record"* and has got away with it; this one
  does not try.
  (E) **The client can still correct us**, because the fill goes in UNDER the
  extraction. *"No she is hired from somewhere else"* wins over a record that says
  otherwise. Reversed, a client correcting our own records would be filed with our guess
  — worse than the question ever was — and the merge that decides it is one character
  wide, so it is asserted by RUNNING the collector rather than by reading the line.
  (F) **The question is now unreachable on every record shape, on every flow that
  defines it** — asserted as that, derived over `SERVICE_FIELDS` rather than written
  about `renewal`, so `replacement` is covered by the same check and a flow added
  tomorrow either is or fails by name. Five record shapes, two flows, ten combinations,
  none of which leaves it to be asked.
  (G) **Seven faults injected, seven red** — the fill removed; the zero branch dead; an
  unmatchable placement claimed as ours from either side; a count leaked into the value;
  the fill placed over the client's own words; and the field dropped from `renewal`.
  **Two of the new smoke states were GREEN on the second of those and the check was
  wrong, not the code.** They asserted only that the question was absent from the prompt
  — and with the zero branch dead the field is still FILLED, by the wrong branch, so the
  question is still never asked. **A state that proves a question is gone proves nothing
  about what reached the ticket in its place.** All three branches now assert the value
  that reaches `collected_info`, and each goes red on its own.
  (H) **Verified live against the real model.** The transcript's own turn, three runs:
  *"Got it — when does Polo's Work Permit expire?"*, no Ming Hwee question in any of
  them. The control, an employer whose one placement we can name: *"Hi Ratna Choukade,
  I'm Claire, Ming Hwee's AI assistant. We have Liza Fernandez's details on record—when
  does her Work Permit expire?"* — **the whole renewal in one question**, her name, her
  file and where she came from all read rather than asked.
  The ticket label moved with the values, from *"Current helper placed by us"* to
  *"Where the current helper came from"*: the old one read *"Current helper placed by
  us: hired elsewhere"*, which contradicts itself on the line a consultant reads.
  `selfcheck_flows.py` is **500 assertions**; `smoke_nodes.py` is **107 states**.

- **2026-09-17** — **The passport expires in 5 days; the renewal takes 6 to 8 weeks;
  the bot printed both and said nothing.** From the agency's second passport-renewal
  test, on the transcript where everything else worked.
  (A) **Reproduced 2 runs out of 2 before anything was written.** The closing briefing
  reads *"It takes approximately 6 to 8 weeks."* two lines under a client who has just
  answered *"in 5 days"*, and joins them up nowhere. `passport_expiry` has been
  collected since this flow was built and goes onto the ticket under "Passport
  expires" — **nothing has ever read it.** The 2026-09-04 note that removed the urgency
  question ("an expiring passport IS the urgency, and the expiry date says it more
  precisely") was right about the data and never followed through to using it.
  (B) **Deterministic about WHEN to speak, model-written about what to say.** A coarse
  parse of the client's own words gives a rough number of days; the note then tells the
  model to compare it against the lead time it is about to quote and say so if the
  renewal would not finish first. The comparison is left to the model because it has
  both figures in the message it is writing, and the alternative — a per-nationality
  lead-time table — would be a second copy of numbers that live in the knowledge base,
  which is how two figures drift apart (§9.8).
  (C) **It FAILS TOWARDS SILENCE**, which is the whole safety of it and the
  `_heavy_workload` shape. Anything it cannot read plainly returns None and the briefing
  goes out exactly as it does today: *"next March"*, *"when the contract ends"*, *"not
  sure"* and **"27 September 2033"** — the format `contact._passport_expiry` produces
  off `biodata.passportExpiry` — are all silent on purpose. Guessing at a date and then
  calling somebody's passport urgent on the strength of it is worse than the omission
  being fixed. 19 phrasings measured: 9 flagged, 4 comfortable, 6 silent.
  (D) **60 days**, because a Filipino renewal is quoted at 6 to 8 weeks — 42 to 56 days
  — so anything past it finishes comfortably whatever her nationality, and the note
  stays out of the way.
  (E) **Three things the note forbids**, all asserted: inventing a date, a deadline or a
  faster route; promising it can be rushed or expedited beyond saying the team will see
  it is urgent, because we do not control an embassy's timetable; and telling them what
  happens if the passport lapses or what MOM will do — we hold no record of that, and
  frightening somebody with a consequence nobody has checked is worse than silence.
  (F) **Nine faults injected, nine red** — the note never appended and appended to
  every briefing; a short expiry not read as short; the threshold dropped to nothing and
  widened until it fires on everything; a vague answer guessed at rather than left
  alone; and each of the three prohibitions removed.
  (G) **Verified live**: *"It takes approximately 6 to 8 weeks. This is tight as Bella's
  passport expires in 5 days, so please send the documents to us as soon as possible and
  our team will know it is urgent."* — immediately after the timing line, with no
  invented date and no promise. The control, a passport with two years left, is silent.
  **Measured in the same transcript and NOT changed:** the bot opened one turn *"Ellena.
  When does Bella's current passport expire?"* — the client's name standing alone as a
  sentence, which reads like a form calling out a row and is the shape the 2026-09-10
  fix was written about. Re-run four times on that exact turn it did not reproduce once
  ("Thanks, when does Bella's current passport expire?"), so it is model variation
  rather than an instruction, and there is nothing to fix until it is seen again.
  `selfcheck_flows.py` is **494 assertions**; `smoke_nodes.py` is **103 states**.

- **2026-09-17** — **Passport renewal quoted a client $450 to renew their own passport,
  and asked them for a Work Permit they do not hold.** Tested on two numbers; the second
  transcript is the serious one.
  (A) **What the client saw.** *"I want to renew my passport"* → *"May I know your
  helper's name?"* → *"There isn't any helper here. I want to renew my passport"* →
  *"Got it — which country is your passport from?"* → and four questions later a full
  closing briefing: *"Here is everything for your Indonesian passport renewal: It takes
  approximately 3 working days. The cost is approximately $450. Here is what we will
  need from you: 1. Copy of your NRIC 2. Copy of your Work Permit 3. Copy of your
  passport."* That list contradicts itself on its face — somebody holding an NRIC does
  not hold a Work Permit — because it is the HELPER's document list with the pronouns
  swapped, priced at her embassy's fee, for a service Ming Hwee does not offer to that
  person at all.
  (B) **Nothing in the flow knew whose passport it was.** All 21 `passport_renewal`
  knowledge-base rows are about a helper and all four questions are written about her.
  None of that is a **test**, so when the client said there was no helper the model did
  the reasonable thing and reworded the questions. The service has no concept of an
  owner, and had none since it was written.
  (C) **The first transcript is the same gap from the other side.** After a helper's
  renewal had completed: *"Also I want to renew my passport also"*. Measured — that
  classifies as **`renewal`**, the WORK PERMIT service, because the word carrying the
  intent is "renew" — so the client was asked *"may I know whether Polo's current Work
  Permit was issued through Ming Hwee or hired elsewhere?"*, which is `helper_from_us`,
  added to that flow earlier the same day. The new field did not cause the misroute; it
  made it conspicuous. Both services are in the fix for that reason.
  (D) **Detected positively, which is what keeps it safe.** Two ways in: an EXPLICIT
  denial (*"there isn't any helper"*, *"not my helper"*, *"my own passport"*), which
  wins even when the sentence also contains the word "helper" because that is how a
  denial is written; and a CONTEXTUAL one — *"my passport"* with no helper named, but
  only once `helper_name` is already collected, because then we know her name and it
  cannot be hers. A bare *"I want to renew my passport"* on the first message is
  deliberately **not** caught: employers say that meaning their maid's, and the flow's
  own first question is what surfaces it — which is exactly how the second transcript
  reached the explicit denial. Measured on 15 phrasings, 6 caught and 9 left alone.
  (E) **Answered, not handed over**, for the reason the 2026-09-11 nationality refusal
  is not: *"we only renew helpers' passports"* is an answer we hold, and a consultant
  repeating it is the 2026-09-08 shape of waste. And it is a CONVERSATION — the branch
  remembers itself in `flagged_once`, so *"why not?"* lands there too instead of being
  met with *"May I know your helper's name?"*, while naming a helper releases it and the
  collection carries straight on. A correction needs no special case, same as that fix.
  (F) **The records are stripped from that turn, and that is the guard rather than the
  prompt rule.** `ungrounded_figures` grounds on the retrieved set, which on a
  passport-renewal turn genuinely contains *"approximately $450"* — so it would have
  passed the figure happily. With `rag_context` blanked the model is never offered it,
  and any figure it produces anyway is ungrounded and takes the whole reply with it,
  leaving the fallback. **`history_text` is deliberately NOT stripped**: the model needs
  it to answer coherently, and on the first transcript it still carries $450 from the
  helper's completed briefing — so the note forbids quoting a fee as well.
  (G) **Eleven faults injected, eleven red** — the branch never firing; the explicit
  and contextual halves separately; the follow-up memory; the release on naming a
  helper; the flag never written; the records handed back to the model; the refusal
  turned into a handover; the note's price ban and its do-not-redirect ban; and the
  work-permit flow dropped from the set.
  **Two came back GREEN and one SKIPPED, and all three were the checks.** The
  remembering injection passed because the assertion SUPPLIES `flagged_once` itself, so
  it proved the predicate reads the flag and not that anything writes it — "imported and
  never called", one field along; a smoke state now asserts the returned state. The
  redirect injection passed because the needle had been loosened to *"tell them where to
  go"* after a case mismatch, and *"Feel free to tell them where to go"* still contains
  it — a needle that survives the fault is not a check. And the handover injection
  anchored on text that does not exist.
  (H) **Verified live against the real model, six cases.** *"There isn't any helper
  here"* → *"Ming Hwee handles passport renewal for domestic helpers only, not clients'
  own passports. We can help with your helper's passport renewal or another Ming Hwee
  service."* *"why not? can you still help me"* → the same position, held. *"ok then I
  want my helper passport renewed"* → *"Got it. May I know your helper's name?"* — the
  collection, resumed. Both misrouting phrasings answered on both services. And the
  control, a Filipino helper's renewal mid-collection, is untouched: *"Got it — when
  does Polo's current passport expire?"* **No fee, no timeline, no NRIC and no Work
  Permit in any of the five refusals.**
  `selfcheck_flows.py` is **487 assertions**; `smoke_nodes.py` is **100 states**.

- **2026-09-17** — **"Hi Vaidik" to a number we hold no name for, and then "May I
  know your name?" one message later.** Reported from a live chat, on the first two
  messages of the conversation.
  (A) **The rule existed and could not reach the turn.** `info_collector` has dropped
  the WhatsApp push name from the prompt since 2026-09-08, gated on
  `service_type in NAME_FROM_RECORD_ONLY`. A client who opens with *"Hello"* satisfies
  neither half: they have not said what they want, so there is no `service_type`, and
  the turn goes to `response_generator`, which has no such rule. `_contact_block` then
  printed `- WhatsApp name: Vaidik`, and prompt rule 1c says to use the client's name
  when you know it — so the model did exactly as instructed.
  (B) **Reproduced before touching anything**, word for word against the real model:
  *"Hi Vaidik, I'm Claire, Ming Hwee's AI assistant. How can I help you?"* then
  *"I'll ask a few details ... May I know your name?"*
  (C) **Why it appeared NOW, having never been reported before.** Until 2026-09-16
  `new_hiring` filled `full_name` from the push name, so the question was skipped and
  the contradiction could not arise — the bot said "Hi Vaidik" and then never asked.
  The agency's own complaint that day (*"why chatbot is not asking the user name like
  before it is again picking name automatically"*) is what made it ask. That fix was
  right and stays; this is its mirror image, one turn earlier, and the two together are
  what a client actually sees.
  (D) **Two nodes disagreeing about whether the push name is the client's name** is
  §9.8's duplication hazard applied to a POLICY rather than a constant. It is now one
  decision, at the single point the name enters the prompt: `_contact_block` prints the
  name from our RECORDS or prints none, and the local copy in `info_collector` is gone
  with its incident note moved rather than stripped (§0.3).
  (E) **Not printed as a fallback and not named in order to forbid it.** A line saying
  *"WhatsApp profile name: Vaidik (not their real name, do not use it)"* puts the name
  in the context, and a model that wrote "Hi Vaidik" was reading it from the prompt in
  the first place — which is exactly what §8 settled for the three branches that did
  not exist. Nothing is lost: a name the client types still reaches the model as an
  answered field.
  (F) **The warmth half is asserted in both directions**, because it is a 2026-09-09
  fix and the obvious over-correction here is to stop greeting anybody. A client on
  file still gets *"Hi Ratna"* on a greeting and *"Hi Ratna Choukade, welcome back"* on
  an intake; a client not on file gets *"Hi, I'm Claire"* and is asked.
  (G) **Four faults injected, four red** — the label used as the name again; the label
  named in order to forbid it; the record name stopped reaching the model; and a node
  going back to blanking it for itself, which is the duplication the fix is about.
  (H) **Why neither suite caught it, which is the part worth keeping.** Both were green
  the moment the fix landed, because nothing covered the path. `smoke_nodes.py` had no
  `response_generator` greeting state carrying a push name, and **`e2e_services.py`
  opens every one of the seven services with the service sentence** (*"Hi, I want to
  hire a helper"*) — never with a bare *"Hello"*. So the first turn in every end-to-end
  walk IS a collector turn, where the old suppression worked. A harness that always
  starts the conversation the same way cannot see a defect that only happens when it
  starts differently. The greeting turn now has its own states, asserted on the PROMPT
  rather than the reply — checking the reply passes on any run where the model simply
  chose not to use the name.
  `selfcheck_flows.py` is **476 assertions**; `smoke_nodes.py` is **96 states**.

- **2026-09-17** — **Religion replaces the pork/beef question, by instruction, after
  the trade was put to the agency and they took it.** Their answer to the §9 note
  below: *"but i want this thing so in this case do one thing in place of this 'are you
  able to handle pork or beef' and this 'would she need to handle pork or beef?' ask the
  religion question because that is priority."*
  (A) **The concern was raised once, answered, and is not raised again.** What was put
  to them: we already hold `candidates.religion` on every live row, the employer flow
  asks nobody's religion, and the thing religion decides in a placement - pork and beef
  - was already asked of BOTH sides as a matched pair. They have decided the religion
  question is worth more than the dietary one. It is their business and their form; the
  change is made in full.
  (B) **It is a REPLACEMENT, and the options had to go with the question.** Stripping
  *"and would she need to handle pork or beef?"* from the wording alone would have left
  `no pork` and `no beef` sitting in `cooking`'s option list - and `_field_guidance`
  drops two or three options into the spoken question as examples, so the bot would have
  gone on saying *"such as no pork or no beef"* with the question no longer asking it.
  Both are out of both lists. `halal kitchen` and `vegetarian` STAY: those describe the
  client's own kitchen, which is a cooking requirement and not a question about anyone's
  faith.
  (C) **Both halves of the pairing, or a consultant matches them by eye.** The employer
  states a preference and the helper states a fact, which is what `_MATCHED_PAIRS` is
  for. `_RELIGIONS` is one tuple; his list is that plus `no preference`, hers is that
  exactly, and the assertion is that they differ **by exactly that one entry** rather
  than an exception saying "these two are allowed to disagree". Her key is `religion`
  and his is `helper_religion`, which is the 2026-09-10 rule - a shared key would carry
  an employer's stated preference into a helper's file as her own faith.
  (D) **Asked on the two flows where a helper is still being chosen** - `new_hiring` and
  `transfer_employer` - and deliberately NOT on `direct_hiring`, where the employer has
  already picked her and a preference is not something anyone can act on. Their own
  instruction asked for it in the relevant workflow *"rather than being asked
  universally"*.
  (E) **Both sides say why, and the two reasons are the same reason told to two
  different people.** `_WHY_WE_ASK` is keyed on the field key with no idea which flow is
  asking, which is exactly why the two keys are different: his says *"your household's
  practices"*, hers says *"a household whose practices you are comfortable with"*. A
  reason that talks about the client in the third person TO the client is the defect
  that pair of rules exists to stop, and it is asserted in both directions.
  (F) **The rule about a candidate inheriting an employer's reason was restated, not
  suppressed.** It asserted that no candidate key appears in `_WHY_WE_ASK` at all — but
  the hazard is a SHARED key, because a reason written for an employer is only ever read
  out to a helper when both flows use that key. `religion` exists on no employer flow,
  so it cannot inherit anything. Derived from the employer services now, which also
  makes it survive a new flow.
  (G) **And verifying it live found a reading defect in a rule five years of option
  lists had not exposed.** *"may I know your religion, such as Muslim, Christian,
  Catholic, Hindu, Buddhist, another faith, **or more than one**"*. `_field_guidance`
  appends *"they may give more than one, or something not on the list"* to every
  non-exhaustive option set — correct for `languages`, which takes four, and for
  `requirement`, which takes childcare and cooking at once; nonsense for a person's own
  faith. New `Field.multiple_answers`, false on exactly one field and asserted as such.
  **Deliberately not `options_are_exhaustive`**: that flag suppresses the whole
  invitation, and a helper whose faith is not one of the five still has to be able to
  say so. The employer's half keeps multiple, because *"Muslim or Christian is fine"* is
  a real preference.
  (H) **Eleven faults injected, eleven red** — the question removed from either side;
  the pork/beef clause restored to each; `no pork` restored to the options; either
  question stripped of its own option names; `no preference` offered to her as a faith;
  her reason replaced with his; the transfer flow losing it; a direct hire gaining it;
  and the reason removed entirely. Three initially CRASHED rather than failing by name
  and two of those were the checks, now fixed to use `.get()`. **The third still
  crashes and is left that way on purpose**: deleting `helper_religion` from
  `new_hiring` breaks `transfer_employer` at import, because `_hiring_field` reuses the
  employer's own `Field` — the same coupling recorded for `requirement` on 2026-09-16,
  and a property worth having rather than a hole.
  (I) **Verified live against the real model.** Employer: *"to help her faith fit
  comfortably with your household's practices from day one, would you prefer Muslim,
  Christian, Catholic, Hindu, Buddhist, no preference, more than one option, or another
  religion?"* Helper: *"to help match you with a household whose practices you're
  comfortable with ... may I know if you're Muslim, Christian, Catholic, Hindu,
  Buddhist, or another faith?"* — every option named on both, no "more than one" on
  hers, and the reason in the client's own sentence. Asked *"why does her religion
  matter to you?"* it answers straight. The cooking question that follows is now *"any
  particular cooking you'd want her to handle, such as Chinese, Malay, Western or a
  halal kitchen?"* — no pork, no beef.
  **The accepted cost, recorded rather than buried:** the EMPLOYER's pork/beef
  requirement is now captured nowhere. The helper's own side survives on the office
  form — `candidates.biodata.commitments.handle_pork` / `handle_beef` are two of the
  nineteen commitments every helper answers — so a consultant can still read hers, and
  `halal kitchen` remains a cooking option an employer may choose. But religion does not
  predict handling reliably in either direction, and the direct question is gone by
  instruction.
  `selfcheck_flows.py` is **471 assertions**; `smoke_nodes.py` is **92 states**.

- **2026-09-17** — **Ten things from the agency's review, and the one they circled
  was a salary band no Filipino placement could be made at.** Their list ran from
  "explain the process proactively" to the tone of one question; four of the ten are
  theirs to answer rather than ours to build, and those are in §9 rather than here.
  (A) **The question they objected to hardest asked four things and only one of them
  mattered.** *"Where is Lwin lwin Nwe currently — in Singapore, in Myanmar, working
  in another country, or somewhere else?"* Their reading is exactly right: *"That is
  the only scenario that requires a different regulated process, i.e. a transfer case
  governed by MOM requirements. Whether the helper is in her home country, unemployed,
  working in another country, or otherwise outside Singapore, does not change the
  standard placement workflow."* `helper_location` and `employment_status` are now one
  yes/no — *"Is she currently in Singapore, working under a Work Permit with another
  employer?"* — and nothing else is asked, because no other answer can change what we
  then do.
  (B) **"or somewhere else" was the options list, not the model.** The old field
  carried three, and `_field_guidance`'s non-exhaustive branch appends *"make clear
  they may give something not on the list"* — the same mechanism that produced *"or
  another country?"* on 2026-09-11. A yes/no has nothing to enumerate and nothing to
  invite. It opens with an auxiliary so `_yes_no_question` recognises it and a bare
  "no" closes it rather than being re-asked by `_BARE_YES_NO` (2026-09-08).
  (C) **`_STILL_EMPLOYED` and the route guard both moved onto it, and both got
  tighter.** The notice-period question now keys on the helper who actually has a
  release to get; the old gate could open on a helper employed overseas, for whom
  "clearance from her current employer" means something else. `_known_helper_location`
  became `_known_transfer_case`: it was a three-way field answering a two-way
  question. Verified on 12 phrasings — "yes she works for another employer now" opens,
  "no she is not working for anyone" and "she is between jobs" close, "" stays
  undecided.
  (D) **The helper's number was question THREE, and their own transcript shows what
  that costs.** *"I'm not comfortable to provide this information now."* The agency:
  *"Contact information should only be requested after the user has received the
  relevant process, timeline and applicable cost information and has shown intent to
  proceed."* Moved to last and made optional. **`full_name` deliberately stays at
  question one** — that is the client's own name, it goes on a Service Agreement, and
  five separate complaints since 2026-09-08 have been about it NOT being asked.
  (E) **The circled screenshot: *"such as SGD 500-600 or SGD 600-700?"* asked of a
  client who had said Filipino.** Both bands sit at or below the S$650 a Filipino
  helper cannot be placed below, so the question invited a budget no placement could
  be made at — and the client would have learned that from a consultant later, having
  already been asked to think in the wrong numbers. `_effective_options` drops a band
  wholly below the floor and rewrites the one that straddles it to start AT the floor,
  so a Filipino client is offered *"SGD 650-700 or SGD 700-800"* and everyone else is
  untouched. `FEE_BY_NATIONALITY`'s rule, one column along.
  (F) **The floor has to be GROUNDED as well as offered, and nothing else catches
  that.** `_field_guidance` builds the question and `grounded_options` tells
  `ungrounded_figures` which figures the reply may contain; they now read ONE function.
  Reverting only the grounding line leaves every question-level check green while the
  model is told to say S$650 and then binned for saying it — the 2026-09-09 (D) defect,
  where every budget turn was being discarded and nobody noticed because the fallback
  is a correct question. The one check that catches it reads the REPLY.
  (G) **The figures contradicted what was already loaded, in BOTH directions, in five
  rows.** The agency gave S$650 fresh and from S$670 experienced. Three live rows said
  a Filipino helper *"starts at S$570-650"* — below the floor — and that an experienced
  one is *"S$700-850+"*, above where she starts from. Corrected rather than stacked
  (the 2026-09-08 rule). **Two needles for one fact**, because the sentence is written
  two different ways: measured first, and a single needle would have corrected two rows
  of three and left the third contradicting the pair.
  **Then the sweep found two more nobody had asked about, and they were the worst
  placed** — *"(salary: S$570-850/month, timeline: 4-8 weeks)"* inside a
  nationality comparison filed under **new_hiring**, which is exactly where a hiring
  client reads it. Found by sweeping the database for the old figure AFTER the first
  three were corrected, not by the targeted search that found those three: that search
  keyed on the words "salary" and "minimum", and these two rows say neither.
  **Indonesia and Myanmar are untouched in the same sentence** — the agency gave
  figures for one country, and inferring the other two from it is the mistake §9
  records for Myanmar twice already. Retrieval after: the new row is top for 4 of 5
  employer probes at 0.686–0.778 (before: 0.586–0.652, on a row that said there is no
  minimum wage), rank 2 on the fifth, and **no sub-650 salary figure survives anywhere
  in the knowledge base** — the 19 that remain are flight costs, home-leave and
  passport fees.
  (H) **Process and timeline now come before the questions, not after.** The agency's
  flow is *"Intent → Process & Timeline → Cost/Fee → continue the workflow → live agent
  handoff"*. The opening overview has existed since 2026-09-04 and covered the three
  small-ticket services; `new_hiring` and `direct_hiring` — the two longest flows in
  the codebase — explained themselves at **neither** end. Both are in it now, with
  their own opening clause: *"a short, well-defined job we handle end to end"* is true
  of an insurance renewal and false of a 25-question first-time hire.
  (I) **And the timeline half is honestly not available on new hiring, which is a
  finding for them rather than a defect.** Their own row says *"There is no single
  answer, because it turns on your requirements and on which helper you choose"*, so
  the model correctly gives the process and no lead time. Asking for it anyway
  produced *"the exact timeline will be confirmed once we know more"* — the "it
  depends" shape the note has forbidden since 2026-09-04, costing a sentence to say
  what the client already assumed. The note now says outright that a record saying the
  timing depends on something is not a lead time, and that our filing is never
  described to a client. Measured across four runs each: the overview reaches the
  client in **3 runs of 4**, against the 2-in-4 this note has managed since it was
  written.
  (J) **The Cost/Fee step of their own flow is blocked by their own earlier
  instruction, and that is theirs to resolve.** `new_hiring` and `direct_hiring` are in
  `COST_WITHHELD_SERVICES` because on 2026-09-04 they said a new hire's price must
  never reach anyone before a salesperson has spoken to them, and
  `quotes_hiring_package_cost` enforces it whatever a prompt says. The overview is told
  not to spend its one sentence on a figure that is about to be swapped for the
  deferral line. Their point 4 already says the pricing is still to come, so both
  halves are in §9.
  (K) **Small ones.** "paperwork" is out of the direct-hire purpose note and out of the
  one direct-hire row that used it — scoped to direct hire, because rewriting rows
  nobody objected to is how a correction becomes a rewrite. And `renewal` asks whether
  the helper came from us, which is the agency's existing/new distinction; it is the
  SAME `Field` object `replacement` already asks, not a copy (§9.8), and it is not the
  banned "have you hired with us before".
  (L) **Sixteen faults injected, sixteen red — after two came back GREEN and two
  CRASHED, and all four were the checks, not the code.** One injection was a no-op: my
  patch script went through a bash heredoc, which doubled the backslashes, so the
  anchor never matched and the run reported GREEN rather than SKIPPED — the hazard
  already recorded in this session's own memory, hit anyway. The needle check counted
  the CORRECTED text, so blanking a needle outright left it green: the rule still
  installed S$650, at a string that no longer existed. It counts through `old` now,
  which is the half that has to match something. And two assertions used
  `next(...)`/`list.index(...)`, so a renamed field raised StopIteration or ValueError
  and took the harness down printing no FAIL line — **a crash tells you less than a
  red**, for the third time (2026-09-10, 2026-09-16, now). Both use safe lookups and
  fail by name.
  One more thing this round could not do and should not have faked: the assertion for
  the corrected needles cannot spell the old string out, because `selfcheck_flows.py`
  sweeps the repo for any replaced string and would catch itself — which is exactly
  what it did on the first run, the same way the phone-number sweep caught itself on
  2026-09-11.
  (M) **Verified live against the real model.** Direct hire: *"Got it — is she
  currently in Singapore and working under a Work Permit with another employer?"*, and
  no country list anywhere. Budget for a Filipino client: *"such as SGD 650 to 700 or
  SGD 700 to 800?"*; the control with no nationality stated still gets *"below $500,
  $500-600"*. Asked outright: *"A fresh Filipino helper's minimum basic salary is
  approximately S$650 per month, while an experienced helper starts from about S$670;
  I'll confirm the exact amount for her profile"* — the experienced figure presented as
  a starting point and not a price, which is what the agency asked for. Renewal: *"is
  Lwin Lwin Nwe from Ming Hwee, or was she hired through another agency?"* Direct hire
  overview: *"For direct hire, we process the MOM application, documents, insurance and
  bond."*
  `selfcheck_flows.py` is **456 assertions**; `smoke_nodes.py` is **87 states**.

- **2026-09-17** — **Home leave closes by telling them to book the air ticket, which
  is the one thing they can do while they wait.** The agency: *"After collecting the
  helper's nationality and other required details, the bot should advise the client to
  purchase the air ticket and send a copy of the ticket to us. This will allow our
  agent, once the case is assigned, to immediately prepare and submit the required
  embassy appointment/documentation based on the confirmed travel details."*
  (A) **It already ANSWERED this correctly — that is what makes it a flow defect rather
  than a knowledge one.** Their own screenshot: *"how long will the documents take to
  process before i can buy the air ticket? or can i buy the air tickets forst"* →
  *"For a Filipino helper, allow about 4 weeks because an embassy appointment is
  required. You can buy the air ticket first, as we need the ticket itinerary for the
  documents."* Right on both halves, off the records. But the client had to think to
  ask, one message AFTER the handover — so a client who does not ask books nothing, and
  the agent picks up a case they cannot start.
  (B) **`home_leave` had no `BRIEFING_AFTER` entry at all**, so it closed on the bare
  handover line: four questions, "a live agent will connect with you shortly", and
  nothing about cost, timing, documents or next steps. Passport renewal has had a
  closing briefing since 2026-09-08 and the candidate registration since 2026-09-10;
  this is the third service to get one, and the machinery was already generic —
  `briefing_due`, `BRIEFING_QUERY`, `SERVICE_BRIEFING_NOTE` and `BRIEFING_MATCH_COUNT`
  needed no change.
  (C) **Keyed on `nationality`, for the reason passport renewal is.** The documents,
  the lead time AND the price all differ by it — PH original passport plus her ticket
  itinerary, approximately 4 weeks, $400; ID copies and one form we provide,
  approximately 2 weeks, $250 — so before the nationality the only honest briefing is
  "it depends", which is the 2026-09-04 defect. It is question 3 of 4 here, so it is
  always answered and answered a turn before the collection completes, which is what
  the RETRIEVER needs.
  (D) **Measured before building anything**, because a briefing with no records is the
  2026-09-09 defect. Through the real retriever at `BRIEFING_MATCH_COUNT` 10, under
  `home_leave`: PH gets its own documents row (0.568), the timing row, the process row
  and **$400**; ID gets its own documents row (0.543), the timing row, the process row
  and **$250** — and, correctly, **no $400 anywhere in its set**, which is the
  nationality filter doing its job. No change to the query or the match count.
  (E) **The ticket itinerary is a FILIPINO document and only a date confirmation for an
  Indonesian helper.** The records list it among PH's embassy set and do not list it for
  ID, so a general "send us the itinerary, your embassy needs it" would contradict the
  document list three lines above it in the same message. The note therefore asks for
  the ticket copy as **confirmation of the dates** — true either way, and the agency's
  own reasoning — and says the itinerary is additionally a document only where the
  records say so. That is `FEE_BY_NATIONALITY`'s rule applied to a document instead of
  a price: what we hold for one nationality is not automatically the other's.
  (F) **No figure, no airline, no route, no deadline, and never when she must fly by.**
  A number here gets the entire briefing binned by `ungrounded_figures` and they lose
  the advice with it — the same reasoning as the workload note earlier today.
  (G) **Eight faults injected, eight red** — the briefing removed; keyed on a field the
  flow never collects; the note never appended; the note appended on every service; the
  "send us a copy" half dropped; the reason dropped; the PH/ID distinction collapsed;
  and the deadline ban dropped.
  **One came back GREEN first, and the control was the thing that was wrong.** The
  passport-renewal control forbade the note on a turn that still had `passport_expiry`
  outstanding — and the briefing is the CLOSING message, so that turn never builds one
  at all. It forbade something that could not have appeared either way and stayed green
  with the service gate removed outright. Made complete, it goes red. **A `_forbid_` on
  a turn that has no briefing proves nothing**, which is the "green for the wrong
  reason" shape this file has now recorded five times.
  Three of the new assertions also failed first on **line wrapping** — `"send us a\ncopy"`
  is not `"send us a copy"` — the 2026-09-16 (H) trap. Fixed with the existing `_flat()`
  helper rather than by rewording the prose to fit the check, which would have been the
  wrong way round.
  (H) **Verified live against the real model, all three nationalities.** PH: 4 weeks,
  $400, the itinerary IN the document list, and step 1 *"Please book Jenny Rose Ann's
  air ticket now and send us a copy; the confirmed travel dates let us prepare and
  submit her embassy paperwork straight away."* ID: 2 weeks, $250, the itinerary NOT in
  the documents, and *"send us a copy so we can work to her confirmed travel dates
  without waiting."* Myanmar: **no price quoted** — *"a consultant will confirm the cost
  for her embassy"* — with the ticket advice still given.
  **Measured and NOT changed:** a Myanmar home leave is given "a copy of her passport"
  in its document list, which is the INDONESIAN row's item reaching her through the
  `nationality='all'` row's "either way" clause. NRIC and work permit are grounded for
  every nationality; the passport copy is not, strictly. It is one benign document
  rather than a wrong price or a wrong deadline, nobody has reported it, and the real
  gap is that the agency has given **no Myanmar home leave content at all** — the same
  hole §9 already carries for Myanmar passport renewal, now recorded for both.
  `selfcheck_flows.py` is **422 assertions**; `smoke_nodes.py` is **79 states**.

- **2026-09-17** — **"i said both then why you didnt ask for email" — every way of
  saying both closed the gate, including the ones that said the word.** The agency
  tested direct hire, answered the channel question with *"both"*, was handed over
  without ever being asked for an address, and said so in the chat.
  (A) **`excludes` is checked FIRST, and it held the names of the OTHER channel.**
  `_WANTS_EMAIL` excluded `whatsapp`, `whats app`, `here`, `this number`, `phone`,
  `text` and `chat` — so an answer that named WhatsApp alongside email was read as a
  refusal of email. Measured across the family before touching anything: **"both",
  "both email and whatsapp", "email and whatsapp", "whatsapp and email", "email as
  well as whatsapp" and "send to both my email and here" were ALL closed.** Only
  *"email too"* survived, and only because it happens to contain no exclude word. So
  this was never about the single word "both": a client could say **email** outright
  and still never be asked for one.
  (B) **Those excludes were never needed, which is why the fix is a deletion.** An
  answer naming only WhatsApp matches nothing in this gate, and a gate with no match
  is **closed already** — verified on nine WhatsApp-only phrasings, all still closed
  with the excludes gone. What `excludes` is FOR is a negation that contains the match
  word — *"no email"* — the same shape as *"no, I don't have pets"* containing "have",
  which is the case the class docstring is written about. That is what it holds now,
  and *"only whatsapp not email"* is still correctly closed.
  (C) **`both`, `either` and `any` are answers to a two-way question that include the
  email half**, so they are matches. The leading word boundary in `_mentions` does the
  delicate part unaided: **"neither" does not match "either"**, because a letter
  precedes it.
  (D) **The question now offers it.** *"Would you prefer updates by email, or here on
  WhatsApp?"* is an either/or that hides the third real answer, so a client who wanted
  both had to volunteer a word the question never showed them — the same shape as
  landed property being in the options and never in the question, earlier the same
  day. Three options named literally, which puts `_field_guidance` on its "name them
  all" branch. Live: *"would you prefer updates by email, here on WhatsApp, both, or
  another way?"*
  (E) **One gate, three flows.** `update_channel`/`email` appear in `new_hiring`,
  `direct_hiring` and `transfer_employer` and share one `Gate` object, so the fix
  lands on all three at once — and the assertion sweeps **every flow that asks the
  question** rather than the one that was reported, which is the correction §9 has
  now forced on this file four times.
  (F) **Asserted through `applicable_fields` and by RUNNING the collector**, not on
  the gate alone. A gate that returns "open" to nobody is the "imported and never
  called" hole (2026-09-10, -16, -17); the smoke state reads the system prompt the
  model was handed and checks the email question is in it.
  Five faults injected, five red — the old excludes restored, the both/either/any
  matches dropped, the real negations dropped, the question back to either/or, and
  `both` dropped from the option list. The matches injection goes red in **both**
  suites, the predicate and the run.
  (G) **Verified live against the real model**, five turns: *"both"* → *"Got it — what
  email address should we use for the updates?"*; *"both email and whatsapp"* → the
  same; and the control, **WhatsApp alone**, still completes and is never asked for an
  address, which is the 2026-09-04 defect this gate was built for.
  **Measured and NOT changed, because it needs the gating work this file twice says
  not to rush:** a client who answers the channel question with their bare address
  (*"vd@gmail.com"*) closes the gate — `_mentions` anchors on a leading word boundary,
  so `mail` does not match inside `gmail`. The extractor usually files it correctly
  anyway, and nobody has reported it. Recorded as §9.22.
  `selfcheck_flows.py` is **415 assertions**; `smoke_nodes.py` is **76 states**.

- **2026-09-17** — **The agency's team tested new hiring end to end. Five findings, four
  of them ours, and a sixth nobody reported that the verification run found.**
  (A) **"Why is there an immediate message that says reply next day when it is during
  working hours?" — that message is not ours.** *"Thank You for your message. Our team
  will reply to you next following working day."* arrives at 1:42pm on a Wednesday,
  before Claire's own reply. Checked rather than assumed: that string appears **nowhere**
  in `app/` or `scripts/`. It is the agency's **WhatsApp Business away message**, set on
  the handset, and this repo cannot reach it — `message.is_auto_reply` exists to
  RECOGNISE such messages so an away message is not mistaken for an agent taking over,
  which is the only involvement we have. It is in §9's waiting-on-Ming-Hwee list because
  turning it off is two taps in WhatsApp Business and no amount of code will do it.
  (B) **"it does not introduce it as a chatbot but gives this reply."** The first message
  was *"hi i would like to hire a helper ... what is the process to go about it?"* — a
  PROCESS question, so `response_generator` swapped its whole instruction for
  `PROCESS_INSTRUCTION`, which says to answer as an ordered list and says nothing at all
  about introducing yourself. The clamp was not the blocker (a process turn already gets
  ten sentences); the instruction was. Exactly the 2026-09-04 collector defect, and fixed
  the same way — `FIRST_CONTACT_INTRO_NOTE` is appended to **whichever** instruction won
  the turn, because the one that loses rule 1 is always the specialised one.
  (C) **"Why will knowing my name help you in recommending a helper that suits my
  household?"** — the client's own words, and they were right. The bot had said *"May I
  know your name so we can recommend a helper suited to your household?"* The purpose
  note supplies the reason for the RUN of questions, and the first question in the run is
  the client's NAME, so the model welded the two into one sentence and produced a claim
  that is not true. The note already said to give the reason "before you ask"; *"X so
  that Y"* reads as before-you-ask and is still the wrong shape. It now says outright
  that the reason belongs to the questions as a whole, quotes the sentence that caused
  this, and says why it does not survive being questioned.
  (D) **"Option of asking landed property is missing."** It was in the OPTIONS the whole
  time and never in the QUESTION, so `_field_guidance`'s "drop two or three in as
  examples" branch picked HDB and condo and the client — who lives in a landed house —
  was never shown the one that described their home. Same fix `languages` got on
  2026-09-07 and `nationality` on 2026-09-11: the question names them, so the "name them
  all" branch takes over. **The room counts had to go for that to be possible**: with
  "HDB 1-3 room" in the list, spelling the options out reads brackets at the client,
  which is the 2026-09-10 complaint. They were redundant anyway — `home_size` has asked
  bedrooms and bathrooms outright since 2026-09-07, which is a better answer than a
  bracket. **`home_type` is out of `_DIGITS_ON_PURPOSE`: one fewer exemption**, and that
  assertion now reads "the two that carry digits".
  (E) **"Ask how many people living in household but doesn't ask the people staying and
  ages."** The count on its own cannot be matched against anything: six people is two
  adults and four children, or four adults and two elderly parents, and those are
  different jobs. `children_detail` and `elderly_detail` do ask the ages — but **both are
  gated on `requirement`**, so a household with an elderly parent and a childcare-only
  requirement is never asked about them at all. The question now asks who lives there,
  which closes that and is also what sizes the job for (F).
  (F) **"bot should highlight that one helper cannot manage all the duties assigned ...
  consider limited scope to focus rather than move on."** Six people, landed property,
  twelve bedrooms and ten toilets, two children aged 3 and 7, five dogs and two rabbits,
  childcare AND cleaning — and the bot collected every word of it and moved on without
  comment. `_heavy_workload` is deliberately conservative and **both halves must hold**:
  the scope must cover more than one kind of work AND the household must be large, by
  headcount or by the size of the home so a twelve-bedroom house counts even with four
  people in it. A big family with one clear job is an ordinary placement, and telling
  that client their job is too big talks them out of a hire we could have made — so this
  fails towards silence. Eight shapes asserted, three firing and five not, including
  "general housework and cooking", which is ONE kind of work however it reads.
  The note is advice and not a refusal: it says one helper is unlikely to cover it all
  well, asks whether they would rather focus her scope or look at more than one helper,
  and **quotes no figure of any kind** — a number there gets the whole reply binned by
  `ungrounded_figures` and they lose the advice with it. Said once, via a new
  `flagged_once` state field with `briefed_services`' shape and reasons, and recorded
  **only when the reply was not the bare fallback**, because a note a guard threw away
  must be tried again rather than filed as delivered (the 2026-09-08 `briefing_lost`
  defect).
  (G) **And the verification run found a guard breaking the reply it was protecting.**
  Running (B) live, the seven-step process answer came back as
  *"1. Consultation ... 2. 3. Interview ..."* — every line break gone and step 2 deleted.
  Two faults in `strip_handover_talk`, neither reported by anyone:
  it re-joined on `" "`, which is the same defect `clamp_reply` had until 2026-09-08 and
  it lands on the same replies; and it read *"you receive 3 to 5 matched profiles within
  48 hours"* as a promised callback time. That is the agency's own published turnaround,
  grounded in the records and passed by `ungrounded_figures`. This guard exists for
  *"Grace will call you back at 3pm"*, not for how long our own service takes. Line
  structure now survives, and inside a numbered step the TIME half only fires when the
  step also promises somebody will contact them — so *"a live agent will call you within
  2 hours"* still goes, and goes whole rather than leaving a bare "2." behind. Prose is
  unchanged, which is the case the guard was built for.
  (H) **Fifteen faults injected, fifteen red.** Both halves of `_heavy_workload`
  separately; the note not appended to the instruction; the note fired on every turn;
  `flagged_once` wiped by `_TURN_RESET`; the introduction never appended and appended
  always; the home question un-named and its brackets restored; the household question
  back to a bare count; the purpose rule removed; and four on the guard. One of the two
  workload injections **crashes** rather than failing (removing the scope test breaks the
  indentation), which tells you less than a red — its companion covers the same half
  through the assertion.
  **The instruction-level fixes are checked by RUNNING the node, not by reading the
  predicate.** `smoke_nodes.py` now captures the system prompt the model was actually
  handed (`_expect_prompt` / `_forbid_prompt`), because the workload note sets its own
  state flag whether or not anything appends it to the prompt — so without that, an
  instruction nobody sends stays green. That is the "imported and never called" hole for
  the third time (2026-09-10, 2026-09-16, now).
  (I) **Verified live against the real model**, all six turns. The process answer opens
  *"Hi Thomas, I'm Claire, Ming Hwee's AI assistant."* and then seven steps, one per
  line, turnaround intact. The name question is *"I'll ask a few details so we can
  understand your household needs and find a suitable helper. May I know your name?"* —
  the reason its own sentence, the question its own. The home question offers *"HDB flat,
  condo, private apartment, landed property, or something else"*. The household question
  asks *"how many people live in your household, and who are they, such as adults,
  elderly parents, and children?"*. And the one they asked for: *"That is a large
  household, and one helper may find it difficult to manage childcare and cleaning well.
  Would you prefer to focus her duties on what matters most, or consider more than one
  helper?"* — no figure, no refusal. The control, a three-person condo wanting childcare,
  is not lectured and simply gets its next question.
  `selfcheck_flows.py` is **408 assertions**; `smoke_nodes.py` is **73 states**.

- **2026-09-16** — **New hiring, tested as an employer: the flow never asked what kind
  of help they needed, and invented an answer instead.** Four things reported, and the
  one nobody named is the one that reached the ticket.
  (A) **`requirement` was filled with a care type the client never said, so it was never
  asked.** The opening message was *"hey i want to hire a helper"*; the extractor
  returned `requirement = "general housework"`. A filled field is never put to anyone, so
  the collection opened on *"how many people live in your household?"*, and
  `children_detail` and `elderly_detail` — both gated on it — closed with it. Seventeen
  questions later the client wrote *"one thing to flag i didnt mention what type of
  service i need then how you move forward"*, was told *"You're looking for general
  housework, including Western cooking and car washing"*, and replied *"how you will
  pretend it is general housework"*. Reproduced before touching anything: with
  `requirement` empty the first question is *"What would you mainly need help with…"*;
  with it pre-filled the first question is the household one, which is exactly what they
  got.
  (B) **The guard for this has existed since 2026-09-07 and was the wrong shape.** It
  tested the client's MESSAGE *subtractively* — "does this contain a word that is not
  hiring filler" — which is true of almost any sentence. Measured against that
  transcript it returned True for **all eighteen** of the client's messages, `10`, `HDB`,
  `600` and `google` included: a bare digit is not filler, so it survives the subtraction
  and reads as a stated care type. It only ever blocked a message made **entirely** of
  filler, which is one sentence.
  (C) **And that one sentence was defeated by one word.** *"i want to hire a helper"*
  subtracts to `""` and is correctly blocked — the exact wording of the 2026-09-07
  incident. *"hey i want to hire a helper"* subtracts to `"hey"`, which is not in the
  filler, so it passed. Same one-word gap as *"what is THE cost"* (2026-09-08), *"what is
  the FURTHER process"* and the plural *"fees"* (2026-09-10). Greetings are filler now,
  but that alone would have left `10` and `600` through.
  (D) **The message test is ADDITIVE now, and the value test is still subtractive** —
  they are answering different questions and one shape cannot serve both. Subtractive is
  right for the VALUE ("does this name work rather than restate the enquiry"), and a
  whitelist there would drop *post-natal* the first time anyone said it. For the MESSAGE
  the question is "did they mention care or housework at all", and the costs are
  lopsided: **missing a volunteered care type costs one question we were going to ask
  anyway; accepting an invented one costs a helper matched against a requirement nobody
  gave.** So it fails towards asking. 17 messages from the transcript blocked, 11
  volunteered phrasings still taken.
  (E) **`new_hiring` now asks for the client's name, reversing the note that said it
  should not.** Their words: *"why chatbot is not asking the user name like before it is
  again picking name automatically"*. It was the **last** employer flow reading the
  WhatsApp push name — the others were fixed one at a time on 2026-09-08, -09 and -10 —
  and the old note argued it should stay out because it has no existing helper to ask
  about, so it never produces *"Hi <push name> … may I know your HELPER's name?"*. True,
  and beside the point: the name on a Service Agreement and a Work Permit application is
  not a label someone set on their own profile. With it in, the set is now **every flow
  that collects a name at all**, so the check stops being a list and becomes the rule.
  The hard-coded tripwire beside it went red on this change, which is what it is for.
  (F) **SGD on both sides of the desk.** *"also not asking salary range in SGD"*. The
  2026-09-11 change put it on the helper's side only and argued the asymmetry was the
  point — *"an employer reading '$600-700' is in Singapore and cannot read it as anything
  else"*. The agency disagreed on seeing the employer flow. The **bands** carry the
  currency rather than the question alone, because `_field_guidance` reads the options
  into the spoken question: live, that produced *"such as below $500, $500-600, or
  $600-700?"* with no currency anywhere. `expected_salary` takes these same options
  through `_matched_options("budget")`, so her side moved with it and the pairing still
  offers the same words. **The digits are untouched**, because they are what
  `ungrounded_figures` grounds the reply on (2026-09-09 D, where every budget turn was
  being binned).
  (G) **Verified live against the real model, the same opening the client ran.**
  *"hey i want to hire a helper"* → *"Hi, I'm Claire, Ming Hwee's AI assistant. I'll ask
  a few details so we can find a suitable helper for your household. May I know your
  name?"* → *"Vaidik Dubey"* → *"Thanks, Vaidik. What would you mainly need help
  with—childcare, eldercare, general housework and cooking, or a combination of these?"*
  — the agency's own two-part rule, and the question that was missing, in the first two
  turns. *"childcare for my 2 kids"* fills it and moves on. The salary turn now reads
  *"below SGD 500, around SGD 500–600"*.
  (H) **Ten faults injected, ten red — after four came back green and every one was the
  check's fault, not the code's.** Two were the same hole this file has recorded before:
  the new assertions called the PREDICATE, so putting the broken call site back left
  everything green — *"imported and never called"*, which is the state
  `quotes_hiring_package_cost` was in for two days (2026-09-10). A value the extractor
  invented is something only the NODE can drop, so `smoke_nodes.py` now runs
  `info_collector` with a stubbed extraction and asserts what reaches `collected_info`.
  The third green was a probe that leant on the wrong half of the vocabulary. The fourth
  was worse: **removing `requirement` outright CRASHES at import** — `transfer_employer`
  reuses that field through `_hiring_field`, so the module raises `StopIteration`, no
  FAIL line is printed and the harness reads silence as success. Gated shut instead, it
  goes red naming the assertion. **A crash tells you less than a red**, and that is now
  twice (2026-09-10 was the other).
  **Found while fixing this, NOT changed, and recorded as §9.21:** `children_detail` asks
  *"How many children, and how old are they?"* and is filled by *"2 kids"*, so the ages
  never reach the consultant. Same shape one field along, but nobody has reported it, it
  is grounded in something the client actually said, and this file is explicit that a
  rushed change to collection gating can strand a live collection.
  **Open, and theirs:** `new_hiring` has no `BRIEFING_AFTER` entry, so it closes on the
  handover line and the client had to ask *"what is the further process"* to get the
  eight steps — which it then answered correctly. Passport renewal and the candidate
  registration both brief at the end because the agency asked for it on those flows. If
  they want the same on the biggest flow it is one entry plus a query.
  `selfcheck_flows.py` is **393 assertions**; `smoke_nodes.py` is **68 states**.

- **2026-09-16** — **The office address was in the records the whole time, and the bot
  was asking the client about a branch it had invented itself.** The agency sent the
  Chinatown outlet address, the opening hours and the MRT exit and asked for them to be
  loaded. Three of the five things they asked for were content; the other two were not,
  and the transcript is only readable once they are separated.
  (A) **"Jurong (HQ), Tampines and Woodlands" existed in the system prompt and NOWHERE
  else.** The IDENTITY block said *"Ming Hwee operates three branches"* and
  `AGENCY_INFO_INSTRUCTION` told the model that is *"our own information and you may
  state it plainly"* — so it did, unprompted, and the client reasonably asked for the
  **Tampines** address. Checked against three independent sources rather than reasoned
  about: the platform's own `branches` table holds **one** row, `CHINA TOWN` (code `CT`);
  the Client Service Agreement names one **Registered Business Address**; and the
  agency's own brief names one outlet. Jurong, Tampines and Woodlands appear in no table
  and in no knowledge-base row. **Every location failure in that transcript starts here** —
  the bot was answering a question about a place that does not exist, and *"Our Tampines
  branch is at [address not available in my records]"* is what that looks like from the
  inside.
  (B) **And the address was ALREADY retrievable, which is what a content-only fix would
  have missed.** Measured through the real path before anything was loaded:
  *"what is your office address"* **0.466**, *"where is your office"* **0.450**,
  *"can i have the office location"* **0.523** — all above the floor, all returning the
  Client Service Agreement's `EMPLOYMENT AGENCY'S DETAILS` chunk, which carries the
  address in full. The bot held it and correctly would not use a Chinatown address to
  answer a question about Tampines.
  (C) **The hours, the MRT and the directions were genuinely missing, and they failed the
  dangerous way rather than the honest one.** *"is it near an mrt station"* scored
  **0.503** and *"what are your opening hours"* **0.423**, both above the soft floor, both
  topped by **clause 6 of the Client Service Agreement** — so `_answerable()` read True,
  the widening retry never fired, and a legal clause was the top record for two questions
  it does not address. That is the 2026-09-08 replacement defect exactly (clause 3.1 was
  top for seven questions, five of them above the floor). Six rows now, filed `general` so
  every service reaches them: address, hours, weekends and public holidays, nearest MRT,
  how to get here, and visiting. After: **17 of 17 probes return an office row first, at
  0.403–0.755**, against a before of 0.000–0.523 mostly on a legal clause.
  (D) **`agency_info` was tagging the query with itself** — the 2026-09-07
  `process_question` defect, one intent along, and it was never added to that fix. Tagged
  `(agency info)`, *"how can i get there"* and *"where are you located"* put the office
  rows **outside the top 5 entirely** and handed the model five rows answering neither
  question. It is now searched **bare**, in its own branch rather than in
  `_SUBJECTLESS_INTENTS`: that set makes the in-flight service the subject, and *"where is
  your office"* during a passport renewal is not about the passport renewal. Verified: the
  office question is still answered from inside a live passport-renewal collection.
  (E) **The parked path was the one the client was actually on, and `_GENERAL_INFO` knew
  none of it.** All twelve phrasings returned False, so a hiring topic parked with an
  agent turned every address question into the holding line — which is screenshots 2 and
  3. **Fifth gap of this shape in this one pattern**: *"what is THE cost"* (2026-09-08),
  *"what is the FURTHER process"*, the plural *"fees"*, and the two documents phrasings
  (all 2026-09-10). Written wide this time — where/address/location, hours and opening
  times, the weekend and public holidays, MRT and nearest exit, getting there and
  directions, and visiting. **20 negative controls stay quiet**, including *"where is my
  helper now"* and *"where is she from"*, which is why the where-branch requires an office
  word beside it.
  (F) **The bracketed placeholder is caught by nothing, and it is not a guard's job.**
  `strip_meta_commentary` cuts a bracket only when it reads as commentary, and this one
  does not — cutting it would leave *"Our Tampines branch is at ."*, which is worse.
  Addressed at the cause, like the 2026-09-10 apology: both instructions now forbid a
  placeholder outright and say that where we are is answered rather than passed on. That
  it reached a client at all is the **second** arrival of a placeholder in front of one
  (§9.17 is the other, from a KB row), and it is recorded as §9.20 rather than guarded
  against on a hunch.
  (G) **The address lives in the RECORDS and deliberately not in the prompt.**
  `ungrounded_figures` grounds a reply on the retrieved records and on what the client
  said — never on the identity block — so a postal code stated from the prompt is binned
  and the client gets the holding line instead. Asserted both ways: no digit of the
  address is in either instruction, and every part of it is in the rows.
  (H) **Fourteen faults injected, thirteen red, and the one that stayed green did so twice
  for reasons that were the injection's fault both times.** First, *"Exit D is dropped"*
  removed the phrase from one row of the three that carry it, so the assertion was right
  to pass; re-cut across all three it goes red. Then *"the closing days are dropped"*
  replaced `public holidays` in the file and the check still passed — one of the four
  occurrences is **split across two source lines** (`"...public "` / `"holidays."`), so the
  file-level edit never changed the runtime string. That is the 2026-09-11 skipped
  injection in a new place. Injected at the runtime level it goes red. **A green injection
  is a result about the injection, not about the code** — fourth time that has held.
  (I) **One assertion went red on my own wording, and it was right.** The first version of
  the prompt fix read *"There is no Jurong, Tampines or Woodlands branch"* — naming them
  in order to deny them. §8 already settled that argument for language: the prompt carries
  no non-Latin script at all, because the strongest foreign-language signal in the context
  was **our own prompt**, and the rules describe the wrong output rather than printing an
  example of it. A model that wrote *"Our Tampines branch"* was echoing the prompt in the
  first place. It now says we have one office and names no other.
  (J) **Verified live against the real model**, the three screenshotted turns and thirteen
  more. *"can i have the office location ? for tampines"* → *"Our only office is at 101
  Upper Cross Street, #03-54, People's Park Centre, Singapore 058357, near Chinatown MRT
  Exit D."*; *"i would like to visit the outlets"* → the address, the hours and the
  closures; *"are you open on sunday"* → *"We're closed on Sundays and public holidays, and
  open on Saturday from 10:30am to 5:00pm."* **No handover on any of them, no placeholder,
  and no second branch named anywhere.** All six weak phrasings answer on the **parked**
  path too, and *"any update on my case?"* still gets the holding line, which is the
  control that matters. Four `agency_info` controls (services, who we are, introduce
  yourself, nationalities) are unchanged and none of them mentions the opening hours.
  **Measured and NOT changed:** *"what services do you offer"* now returns the
  opening-hours row at **0.380**. It is below the floor, the services list is in the
  identity block, and the live reply names all seven correctly — and it replaced clause 6
  of the service agreement at **0.435**, which was above it. A benign row below the floor
  in place of a legal clause above it is the trade going the right way, and it is recorded
  rather than tuned.
  **Needs Ming Hwee, not code (§9):** the brief said *"Find Ming Hwee Agency at Exit D MRT
  Station"* without naming the station. The rows say **Chinatown MRT, Exit D**, which is
  the station People's Park Centre sits on and matches the branch record's own name — but
  it is the one detail here that was inferred rather than given, and a client acts on it
  by getting on a train.
  `selfcheck_flows.py` is **385 assertions**; `smoke_nodes.py` is 65 states.

- **2026-09-14** — **A brand-new allowlisted number could never be answered, and the
  log said a human was on a thread no human had ever touched.** `+917999600865` was
  added, the gate came up with it, and three messages in two minutes got nothing but
  `Conversation 4432 still with a human — bot quiet until the agent has been idle 10
  min`. Read from the row rather than inferred: conversation 4432 is that number,
  `bot_status='none'`, and `last_agent_message_at()` returns **None** — nobody had ever
  been on it.
  (A) **`bot_should_reply()` answers a different question than the call site was
  asking.** It is true only for `bot_active`, so `not bot_should_reply(existing)` is
  true for **`none`** as well — and `none` is exactly what a row looks like when the
  PORTAL created it, which it does for a brand-new client because its webhook usually
  wins the race with ours. The branch returned before `get_or_create(engage=True)`
  could promote the row, so the status stayed `none`, so the next message took the same
  branch. **Permanently silent, and self-reinforcing.**
  (B) **The two gates disagreed, which is the actual bug.** `may_engage()` had already
  decided this conversation was the bot's — the allowlist passed and no agent had
  replied inside the grace window, so it returned *"no recent agent activity"*. Then a
  second, stricter test overrode it. A row nobody has claimed is one to take; a row an
  agent is on is `human_active`, and that is what this stands down for. Now a named
  predicate, `_is_paused_for_agent`, because the expression it replaced was both wrong
  and unreadable at the call site.
  (C) **Why it looked fine on every earlier tester.** `reset_conversation.py` leaves a
  thread at `bot_active`, so every number that had ever been reset skipped this branch
  entirely. The bug needs a number that is new *and* has never been reset — which is
  precisely what a fresh tester is, and what every real client will be at go-live.
  (D) **An injection came back GREEN and it was the wiring, again.** Five checks proved
  the predicate answers correctly for `human_active` / `none` / `bot_active` / missing —
  and `if False:` at the call site left all five green, because a perfect predicate
  nobody asks is the state `quotes_hiring_package_cost` was in on 2026-09-10. There is
  now a check that RUNS `handle_inbound` against a stubbed conversation and reads
  whether the row was claimed. Three faults, three red after it.
  **Nothing to clean up:** conversation 4432 stays at `none` until the next message,
  which now claims it. `smoke_nodes.py` is **65 states**.

- **2026-09-14** — **An allowlisted tester went unanswered all morning, and the
  allowlist was not why.** `+917354708111` was added to `BOT_ALLOWED_NUMBERS`, the
  gate came up with all 18 numbers, and the bot still said nothing. There was no log
  line for that number at all — which is the clue, because a number the gate rejects
  says so.
  (A) **The messages were arriving, under a LID.** Three inbound messages sat on
  conversation 3968 at 10:29:35, 10:30:10 and 10:40:48, and the bot logged an
  unresolvable LID `95786411008174@lid` at 10:29:37, 10:30:11 and 10:40:50. Three for
  three, two seconds apart each time; `GET /contacts/95786411008174@lid` then returned
  `pushname: "Kapil Puri"`, which is that conversation's own `customer_name`. The
  allowlist had never been the blocker on this number.
  (B) **`GET /chats/<lid>` is necessary and NOT sufficient, which is new.** Measured
  against the live channel within one minute of itself:
  `/chats/95786411008174@lid` → `{"type":"unknown"}`, **no phone**, while
  `/chats/917354708111@s.whatsapp.net` → `{"id":"95786411008174@lid",
  "phone":"917354708111","type":"contact"}` — the same chat, resolvable by phone JID
  and not by its own LID. The second form is useless to us: the phone is the thing we
  are looking for. And `77262519025804@lid` is worse, **404 "specified chat not
  found"**, while the list gives it as `6589466562`.
  (C) **So a miss sweeps the chat list, which has the record the single-chat endpoint
  will not give.** One sweep of 3,622 chats learned **2,544** mappings, so the second
  unresolvable client costs nothing — the tester's own lookup took 16s and every LID
  after it returned in 0.00s. Rate-limited to one sweep per 5 minutes, because a
  client on a genuinely unknown LID would otherwise sweep on every message. Verified
  end to end against the live channel: `95786411008174@lid` → **`+917354708111`**,
  `77262519025804@lid` → `+6589466562`, and `11875735592960@lid` (which already
  worked) unchanged.
  (D) **The duplicate is the trap, and it is the same one that made `/chats/<lid>`
  wrong.** A chat appears in the list TWICE — once `type: "unknown"` with no phone,
  once `type: "contact"` with one — so reading the list carelessly reproduces the bug
  it is fixing. Entries with no phone are skipped, and the first entry that HAS one
  wins.
  (E) **Three fault injections came back GREEN and every one was about the check, not
  the code.** The duplicate assertion tested only ONE ordering, so removing the
  phone-less filter stayed green (the first-wins guard covered it) — it now tests both
  orderings, each caught by a different half. The comment on that guard **described
  the wrong mechanism** and was rewritten to say what it actually does (two different
  numbers for one LID, not a phone-less one). And every fixture fitted on one page, so
  an injection that stopped the sweep after the first page stayed green while the live
  channel needs **eight** — there is now a LID on page two. Five faults, five red
  after that. Third time in four days that a green injection was the injection's
  fault; it has never once been the code's.
  **Still true, and it is the honest limit:** a LID the chat list does not carry
  either is dropped, not guessed. `smoke_nodes.py` is **56 states** (42 node states, 14 webhook checks).

- **2026-09-14** — **"Standing down" was doing half of what it said, and the other
  half wrote 145 rows.** Found while answering why the bot is silent on the live
  number (it is the allowlist, working as designed). The log line underneath is what
  gave it away:
  `Whapi could not resolve LID 95786411008174@lid ... standing down, because every
  lookup here is keyed on the number`, and then, 400ms later,
  `Bot standing down on +95786411008174: number not in BOT_ALLOWED_NUMBERS`.
  **Two stand-downs for one message means the first one did not happen.**
  (A) **`_resolve_lid` returned from ITSELF, not from the message.** It logged the
  warning and `return`ed, and `handle_payload` carried straight on into the handler
  with `customer_number` still holding the number `normalize_phone` had minted out of
  the LID. Inbound that is invisible, because the allowlist blocks the fabricated
  number and the log looks like the gate doing its job.
  (B) **Outbound there is no allowlist, and that is where the damage is.**
  `handle_outbound` has no `may_engage` call at all — it goes straight to
  `get_by_phone` and then `get_or_create`. So an agent replying in a chat whose LID
  Whapi cannot resolve **creates a `wp_chat_conversations` row keyed on a LID**.
  Measured on the live database rather than reasoned about: **145 such rows**, first
  on 2026-09-10 and still arriving (138 in one burst at 13:04–13:07 that day, then 2,
  1, 1, 3 on the days since), carrying **14 messages, 3 handovers** and 3 rows in
  `human_active`. And the split §9.16 warns about is among them, realised: LID
  `11875735592960` holds conversation **4417** while the same client's real number
  `+6598713652` holds **4032** — two threads, one person.
  (C) **The pseudo-number is not always obviously fake, which is why this survived.**
  The one in the agency's own log is `+95786411008174` — **`+95` is Myanmar's country
  code**, so at a glance it reads like a helper's number rather than a LID wearing a
  plus sign. The tell is `customer_name`: a fabricated row is named `+<its own
  digits>`, because there is no push name to use. That is how the 145 were counted
  apart from the six genuine `+62 88…` Indonesian mobiles, which are legitimately 14
  digits.
  (D) **The fix is the honest version of what the docstring already claimed.**
  `_resolve_lid` returns False and `handle_payload` `continue`s, so the message is not
  handled at all rather than merely not answered. **Asserted on BOTH handlers** — the
  old check went through `handle_inbound` only, and outbound is the side that wrote.
  Two faults injected (the old wiring restored; the failure branch reporting success),
  both red, naming the handler each time.
  **NOT done, deliberately: the 145 rows are still there.** `wp_chat_conversations` is
  the portal's table, 14 real client messages hang off those rows, and deleting
  somebody else's rows on a number we cannot identify is not this repo's call —
  §9.16 now carries it. `fix_split_conversations.py` is the tool for the 4417/4032
  pair if they want it. `smoke_nodes.py` is **48 states**; `selfcheck_flows.py` is
  unchanged at 374.

- **2026-09-11** — **The case lookup could be proved to work and never be seen
  working.** Asked how to test it. `seed_case_testdata.py` hardwired
  `+65 9000 0399` — a reserved number nobody can send a WhatsApp message from — so
  every path ended inside the script. `--phone` attaches the three test cases to a
  number a tester actually messages from, which is the whole difference between
  "`get_cases` returned three rows" and "the bot answered me about my case".
  (A) **It BORROWS an employer record rather than adding one.** If the number already
  resolves to an employer — most testers' numbers do, because the portal creates that
  row the moment it converts a lead — the cases hang off that row untouched. Two
  employer rows on one phone would be worse than no flag at all: `identify()` takes the
  first match, so the cases could sit on the row the bot does not pick and the test
  would fail for a reason with nothing to do with cases. Found with the **same** match
  function the webhook uses, because `employers.phone` is stored spaced
  (`+65 9188 4442`) and an exact comparison misses most of the table.
  (B) **The delete had to widen with it, and this is the half that would have bitten.**
  `remove()` deleted service requests by MARKED EMPLOYER — fine when the script owned
  the employer, and a foreign-key failure the moment it borrows one, because the case
  delete then hits an `employer_service_requests.converted_case_id` still pointing at
  it. The run would have stopped half way, leaving test rows on a live employer, which
  is the one outcome this script exists to avoid. Both it and the lead sweep are now
  keyed on the test CASE they reference, never on the employer.
  (C) **Verified against the live database, both shapes**, counting every table before
  and after: with its own reserved number, and borrowing a real employer. Three cases,
  three paths, the structural path still seeing only its own — then `employers 3,
  placements 10, cases 0, leads 5, employer_service_requests 1` before and after, the
  borrowed employer and its ten placements untouched.
  (D) **And what a client actually sees, run against the real model with real seeded
  rows.** *"any update on my case?"* → *"Your case is currently with our team for
  documents."*; *"what is the status of my application now?"* → *"Your application is
  currently at the documents stage."* — **no handover, and no case number, stage name or
  status read out**. An unrelated question in the same state (*"do you also handle work
  permit renewal?"*) leaks none of it. With NO case the same question still hands over,
  which is `response_generator` reading `case_summary` and is correct.
  **Worth knowing before testing:** a case question is answered from
  `matched_case_id` → `case_summary`, and `_should_identify` re-reads the identity when
  an employer has no case id on their row — so a case seeded now is picked up on the
  next inbound message with no reset. Nothing was changed about the bot itself; it
  still only ever READS the eleven `case_*` tables.

- **2026-09-11** — **"or another country?" — the bot offering the one answer it
  would have to refuse on the very next turn.** From the first candidate conversation
  on the agency's new number, minutes after the country check went live: *"Thanks,
  wooocom. Which country are you from — the Philippines, Indonesia, Myanmar, **or
  another country?**"* → *"china"* → the decline. Their instruction: *"bot should not
  ask for or another country thing in country question"*.
  (A) **The field's own written question names exactly three.** The fourth was added by
  `_field_guidance`, whose "name them all" branch ends by telling the model to make
  clear the client may give *"something not on the list"* — so this is the same shape as
  the 2026-09-07 languages defect and the 2026-09-04 bracket questions: **the prompt
  instructing the behaviour outright**, not the model improvising. Reading the field list
  and concluding the question was fine would have missed it entirely.
  (B) **And that clause is right everywhere else, which is why it is a flag and not a
  deletion.** It exists because `languages` was hiding four of its seven options from a
  Tamil-speaking household (2026-09-04, and again on 2026-09-07 when the general rule
  overrode the specific one). `requirement`, `work_scope` and the rest are examples of
  what the office works with, and an answer outside them is still an answer — the
  `Field.options` comment has said so since it was written. Her country is the one set
  where the list IS the answer space, so `options_are_exhaustive` marks that one field
  and the guidance takes the other branch: name all three, invite nothing else, and say
  outright not to add *"or another country"*.
  (C) **Checked through the real guidance builder, not by grepping for the sentence.**
  What matters is the instruction the model is handed, so the assertion calls
  `_field_guidance` and reads it — with the languages/requirement controls asserting the
  opposite, that they still invite what they do not name. Plus a tripwire: hers is the
  **only** closed option set anywhere, so a second one has to be a decision somebody
  makes rather than a flag that spread. Three faults injected, three red (the flag
  removed, the invitation restored unconditionally, and `languages` wrongly closed).
  **One of the three initially SKIPPED rather than failing** — its anchor did not match,
  because that field is built over two lines — and a skipped injection proves exactly as
  little as a green one, so it was re-anchored and run.
  (D) **Verified live, three runs of the exact failing turn**: *"Thanks, wooocom. Which
  country are you from — the Philippines, Indonesia or Myanmar?"*, no fourth offered in
  any of them; and the languages control still goes out naming all seven *"or any other
  language"*.
  `selfcheck_flows.py` is **374 assertions**; `smoke_nodes.py` is 46 states.

- **2026-09-11** — **Four things from the agency's candidate retest, and two of them
  reverse decisions this file had argued for.** Their words: *"the overall flow is good,
  but I noticed a few small issues"* — country validation, SGD, the process arriving at
  the wrong moment, and one question too many at the end.
  (A) **Only three countries, and the note here said not to do this.** The agency:
  *"The bot should only proceed with the hiring flow if the candidate is from one of
  these three countries: Myanmar, Indonesia, Philippines."* Until now her `nationality`
  was an open question with **no options**, and the note beside the matched pair said in
  as many words that constraining her to three *"would turn a Sri Lankan applicant away
  at the first question"*. That is now the intended behaviour, so the note is rewritten
  rather than left to contradict the code, and the self-check assertion that asserted the
  absence of options is inverted with its reason. **The tripwire caught it**, which is
  what these assertions are for.
  (B) **The safety of it is in the THIRD state, not the second.** `nationality_state()`
  returns supported / unsupported / **undecided**, and the unplaceable list is
  **positive** — a named country we cannot place — never "did not match the three". So
  *"Java"*, *"Cebu"*, *"from my village"* and an empty answer are simply asked again,
  and only *"India"*, *"Sri Lanka"*, *"Bangladesh"* and their kin are declined.
  **`Singapore` and `Hong Kong` are deliberately absent from that list**: a helper
  already working here, or who has worked two contracts in Hong Kong, can easily answer
  *"which country are you from"* with where she **is** — and `current_location` asks her
  that separately four questions later. The cost runs the right way round: a missed
  decline is one conversation a consultant closes, a wrong decline is a woman told to go
  away who should not have been. Naming the three **in the question** is half the fix on
  its own, because it constrains what she writes in the first place.
  (C) **The refusal is a conversation, and that is what the agency actually asked for** —
  *"the bot should be able to handle follow-up questions ... such as 'Why?', 'I want a
  job.', 'Can you still help me?'"* The branch therefore runs on **every** turn while her
  country reads as unplaceable, rather than once: `nationality` is in the checkpoint, so
  the follow-ups land in the same place and are answered against the history instead of
  drawing the refusal a second time. Live: *"why?"* → *"Because Ming Hwee recruits only
  from the Philippines, Indonesia and Myanmar, where we work with partner agencies."*;
  *"but i really want a job can you still help me"* → honest, no false hope. **And a
  correction needs no special case whatsoever**: *"sorry i meant i am in india now but i
  am from indonesia"* → *"Got it, thanks for clarifying ... may I know your age?"*, the
  registration simply carrying on. **Deliberately not a handover** — "we do not recruit
  from your country" is an answer we hold, and a consultant spending their time to repeat
  it is the 2026-09-08 shape of waste. The note forbids the three inventions that a
  refusal invites: a waiting list, a promise to call if this changes, and another agency
  by name. None of those exists in our records.
  (D) **SGD, spelled out.** *"The bot should consistently use SGD, not USD/dollars."*
  Changed on her side only, and that asymmetry is the point: an employer reading
  *"$600-700"* is in Singapore and cannot read it as anything else, while she is
  answering from Manila or Jakarta. The **bands are untouched** and still taken from the
  employer's `budget` via `_matched_options`, so the pairing still matches word for word.
  (E) **Three fields removed, which is what actually fixes the process defect.** She
  asked *"can you please tell me the further process"* at the last question and got a
  compressed, out-of-order version of the briefing with the channel question tacked on
  the end — then the real briefing one turn later. **She was told the process twice and
  the first telling was the wrong one.** `candidate_notes` goes because the agency named
  it; `update_channel` and `email` go because **she is messaging us on WhatsApp**, so
  asking which channel she would prefer is asking a question we can already see the
  answer to (their wording: *"Do not ask the communication preference again if WhatsApp
  has already been selected"* — for a helper it always has been). Both stay on every
  employer flow, where a client may genuinely want profiles emailed. 18 fields → **15**,
  and the salary answer now goes straight into the briefing with nothing in between.
  **Accepted cost, recorded not buried:** `candidate_notes` is what produced *"I do smoke
  and i can't leave that"* in their own test the day before — a real matching fact a
  consultant will now meet at the interview instead.
  (F) **And the other half, for when she asks it earlier.**
  `CANDIDATE_PROCESS_COMES_LAST_NOTE` turns a mid-collection process question into one
  sentence of reassurance plus the next question, with no numbered steps. Live: *"Yes,
  Ruru. Once we have the last few details, I'll guide you through each step clearly.
  Would you need your own room...?"* A **documents** question is deliberately excluded —
  answering that one at any point is its own agency instruction from 2026-09-10.
  (G) **The closing message offers further help**, asked for by name: *"If you need any
  further help or have any questions, please let us know."* It is rule 2's standing offer
  and this message is a handover, so it belongs — and the note says outright that it does
  not reopen the collection, which is the 2026-09-09 *"would you like to go ahead?"*
  defect.
  (H) **Verified live end to end against the real model**, eight cases: the decline, the
  three follow-ups, the correction, a supported country carrying on normally, the SGD
  question, the premature-process question, and the close — which now reads *"Thanks,
  Ruru — that is everything we need from you for now. Your registration with Ming Hwee is
  complete, and here is what happens next:"*, six steps one per line, the consultant line,
  and the offer of help. No figure anywhere.
  (I) **Eight faults injected, eight red — after one came back GREEN for a reason worth
  keeping.** The injection meant to prove the undecided state replaced `return
  "undecided"` in **`Gate.state()`** instead: three functions in `ticket.py` end on that
  same line, and the needle matched the first. So the check was never exercised and the
  run said nothing. Re-anchored on `nationality_state`'s own tail, it goes red naming all
  seven unrecognised answers. Second time in two days that a fault injection was itself
  the thing that was wrong — **a green injection is a result about the injection, not
  about the code.**
  `selfcheck_flows.py` is **369 assertions**; `smoke_nodes.py` is **46 states**.

- **2026-09-11** — **The agency's WhatsApp number changed, and the `.env` line was the
  easy half.** Asked whether updating the number on the live server was enough. Read
  against the Whapi dashboard they sent: channel **`SPRWMN-VC9N4`**, number now
  **+65 6534 2277**, and the token in `.env` is **the same token** — so this is one
  channel re-paired to a new handset, not a new channel. `WHAPI_API_TOKEN` does not move,
  and neither does `WHAPI_WEBHOOK_SECRET`, which is ours rather than Whapi's and rides in
  the URL path. **`WHAPI_SENDER_PHONE` is the only line that changes.**
  (A) **What that setting actually does, checked rather than assumed**, because the name
  invites the opposite conclusion: it drops self-chat in `parse_webhook` and stamps
  `to_number`/`from_number` on stored rows for the portal. It authenticates nothing and
  routes nothing. Had this been a new channel the answer would have been different, which
  is why §7 now says how to tell the two apart in one command.
  (B) **And then the half that is not configuration at all.** Swept the knowledge base:
  **14 live rows named the old number, 13 of them `contact_type='candidate'`** — the
  helper-rights material. Among them **`NOT EMERGENCY — Call Ming Hwee`** and
  **`27.8a If Someone in the House Touches You or Pressures You`**. A helper reporting
  abuse was being told, in simple English, to message a line the agency no longer
  answers. Nothing in the codebase could have caught that: the number is content.
  (C) **All 14 are `document_chunk` rows with no `question`, so `UPDATES` could not reach
  one of them.** §9.15 predicted this exactly — *"If one ever does surface, it needs a
  chunk-level correction path, not another `UPDATES` entry"* — and assumed it would be a
  stale transfer timeline. `TEXT_REPLACEMENTS` is that path: it keys on the TEXT, applies
  to `question`/`answer`/`content`, and **re-embeds**, for the reason `UPDATES` re-embeds.
  Idempotent by construction rather than by bookkeeping — it finds its targets by
  searching for the old string, so the second run finds nothing. Run twice, 14 edits then
  **0**.
  (D) **One row told us something we had not asked.** `Section E — My Rights & Where to
  Get Help` read *"(WhatsApp: 80119456 / Tel: 6534 2277)"* — so **6534 2277 was already
  the office telephone line** and WhatsApp has simply moved onto it, which is what the
  agency confirmed when asked (*"same number"*). A blind digit swap there produces
  *"(WhatsApp: 65342277 / Tel: 6534 2277)"* — true, and daft — so that sentence gets its
  own specific rewrite, **ordered before** the general swap. The ordering is now a rule
  rather than a coincidence: a needle contained in a LATER needle is dead, because the
  earlier rule rewrites the text the later one was looking for.
  (E) **Verified live, on the rows that matter most.** Through the real retriever and the
  real model as a candidate: *"my employer has not paid my salary who do i call"* →
  *"Please WhatsApp Ming Hwee at +65 6534 2277"*; *"someone in the house is touching me
  what do i do"* → the police line first, then a live agent; *"what number do i contact
  ming hwee on"* → *"6534 2277"*. Swept the whole KB for Singapore phone numbers
  afterwards: **one number, 24 mentions, all of them the new one.**
  (F) **The repo named it too**, and that is swept rather than listed:
  `scripts/TEST_SCRIPT.md` told a tester to message the old number, and three parser
  fixtures used it to stand for "us". The check reads the needles **from** the loader, so
  it generalises to the next replacement — and the first version spelled the digits out
  in its own comment and caught itself, which is the right answer to the wrong question.
  (G) **One injected fault came back GREEN and the check was right.** The ordering
  assertion was stated backwards on its first run — it flagged the correct arrangement —
  and the injection that was meant to prove it moved the wrong rule, so neither the check
  nor the test of the check meant anything. Both corrected; injecting the real hazard
  (the short general needle first) now goes red naming the pair. Five faults, five red.
  **Not done, and it is theirs:** the portal is subscribed to this same channel, so it
  needs the same look-over; and Whapi's safety meter flags *Lifetime of phone number —
  Needs Attention*, which is ordinary for a fresh number but means it is more ban-prone
  under heavy outbound volume in its first weeks.
  `selfcheck_flows.py` is **354 assertions**; `smoke_nodes.py` is 43 states.

- **2026-09-10** — **Third pass as a job seeker: two questions she asked twice, a fee
  the agency has now put a number on (none), and a guard missing from a whole reply
  path.** Their words: *"if the bot knows the documents required, then why didn't it
  tell me when I said 'Tell me the documents I needed'"*, *"at this point if the
  candidate is asking 'Are there any fees?' the bot has to tell them there is no
  fees"*, and *"the documents needed response should be in a list, like 1, 2, 3, not in
  this raw message"*.
  (A) **The same question, asked twice, answered once — and the deterministic net
  caught neither phrasing.** *"Tell me the documents I needed"* got *"I'll check with
  the team and come back to you shortly."*; two messages later *"No i ask for what are
  the documents I required"* got the full correct answer, off records that had been
  there the whole time. Measured before touching anything: **both** phrasings were
  `False` on `asks_general_info` (does a parked topic answer this?) **and** on
  `asks_for_process` (may the answer be a list?). So the turn that worked was the
  classifier happening to return `document_question`, and the net that exists precisely
  for when it does not was blind to both. The old alternations wanted *"what documents"*
  adjacent — *"what ARE THE documents"* missed — or *"documents needed"* adjacent —
  *"documents I needed"* missed — and the imperative branch knew only about a process,
  because it was written for *"tell me the process"* on 2026-09-10 and nobody asked it
  about documents. Third time this exact shape has cost a client an answer: *"what is
  THE cost"* (2026-09-08), *"what is the FURTHER process"* (2026-09-10).
  (B) **One definition, read by both, rather than the same words typed into two
  files.** `_DOCUMENTS_QUESTION` lives in `guards.py`; `asks_for_process` and
  `asks_general_info` both call it. Those two disagreeing about one sentence is what
  produced a holding line and then, on the phrasing that did get through, a paragraph
  — because the second detector missed it too, which is (C). §9.8 is the reason it is
  shared and not copied.
  (C) **And that is why the answer was a wall of text.** The reply that did land read
  *"For your application, we need a copy of your passport, medical report and school
  certificate..."* — correct, and one paragraph, because `asks_for_process` was False so
  `PROCESS_ADDENDUM` never applied and the two-sentence path did. Verified live after:
  the same sentence now returns a lead-in, four numbered items one per line, and a
  closing sentence — **including when the classifier returns `other`**, which is the
  half that was luck before.
  (D) **The fee question was lost to a missing letter.**
  `\bis\s+there\s+(?:a|any)\s+(?:fee|cost|charge)\b` cannot match *"fees"* — there
  is no word boundary inside it — so *"is there any fee"* was answered and *"Is there
  any fees I need to pay"* was handed to a human. `do i have to pay any fees` and
  `what fees do i need to pay` matched nothing either.
  (E) **What a helper pays us is now a fact, not a gap — and it is worded around the row
  that was already there.** This had been an open item in §9 since the day before. The
  agency's answer is *no fee*, so there is a row for it, `general` + `candidate`.
  **The care is in what it does NOT say.** `27.1a Your Placement Loan` is also
  `contact_type='candidate'` and tells her *"Often you do not pay cash — instead, money
  is taken from your salary for the first months"*, listing **"Ming Hwee?"** among the
  possible creditors. A flat *"there are no fees at all"* would contradict a row she can
  retrieve in the same breath, which is §9.14 in a new place. So the row says exactly
  what the agency said — she pays **us** nothing — and routes any loan question to a
  consultant, which is what the loan row itself instructs. No figure appears in it, and
  that is asserted. Measured after loading: *"Is there any fees I need to pay"* **0.561**,
  *"do i have to pay any fee"* **0.597**, *"do i need to pay money to ming hwee"*
  **0.763**, top row every time, and it beats the employer's direct-hire cost comparison
  that used to win at 0.488. Live: *"No, you do not need to pay Ming Hwee any fee to
  register, apply, attend an interview or get placed."*
  (F) **The closing briefing landed as a non-sequitur, and then the bot disowned it.**
  The last question was *"Would you prefer updates by email or here on WhatsApp?"*, she
  answered *"Here"*, and the next thing she read was the numbered hiring process. She
  wrote *"Here I mean WhatsApp why did you tell the process"* — and the reply was
  *"Sorry, Ruru, I misunderstood — you meant WhatsApp for updates, not that you wanted
  the hiring process."* Nothing had been misunderstood: that briefing is the closing
  message the agency asked for on 2026-09-10. The message simply never said her
  registration was **finished**, so a list of steps arriving on the back of a one-word
  answer read as a mistake. Fixed at the cause: the opening line now does two jobs.
  Live after: *"Thanks, Ruru — your registration is complete and that is everything we
  need from you for now. Here is what happens next:"*, then six steps, then the
  consultant line, and no figure anywhere. The apology itself is recorded as §9.19
  rather than prompted against — it is written a turn later by a node with no way of
  knowing the message was deliberate, and a broad "never disown an earlier reply" rule
  would also suppress genuine corrections.
  (G) **Running the employer controls found a defect nobody reported, and it is the
  worst thing in this commit.** With a **hiring** ticket parked, *"Is there any fees I
  need to pay"* came back *"The approximate total service fee and third-party costs are
  $4,225, with a combined total of about $4,285."* — a new hire's price, which has been
  forbidden before a salesperson speaks to the client since 2026-09-04. Every guard
  passed it, correctly: those figures **are** in Form A, so `ungrounded_figures` waves
  them through — that is the entire reason `quotes_hiring_package_cost` exists
  separately. It was wired into `info_collector` and `response_generator` and never into
  `blocked_topic_responder`, which is the path used **after** the handover — i.e. the
  exact moment the rule is about. Not caused by this round's changes: the intent alone
  already made that turn answerable. Now first in that node's guard chain, so the client
  gets the deferral, which says why, rather than the bare holding line.
  (H) **A check that was green with the guard switched off.** The new assertion asked
  whether every reply-writing node *imports* the guard — derived from which nodes ground
  a reply, so a fourth path cannot reopen the gap. Injecting `if False:` left the import
  in place and both scripts stayed green: **imported and never called is precisely the
  state that guard had been in.** `smoke_nodes.py` states can now name the reply the
  model would have written (`_stub_reply`) and what the reply must contain
  (`_expect_reply`), so that state executes the guard and reads the result. Seven faults
  injected, seven red — the first version of this list was six red and one green.
  (I) **Employer controls, all unmoved or improved.** New hiring, passport renewal and
  transfer documents questions now come back as numbered lists on phrasings that
  previously fell through entirely; passport renewal still quotes its own **$450**
  (`FEE_STATED_SERVICES` is untouched); the helper's fee row is `contact_type='candidate'`
  so an employer can never retrieve it.
  **Found, measured and deliberately NOT changed**, all three in §9: an unfilled
  `[INSERT — Ming Hwee to complete before launch]` placeholder sitting in a live
  candidate-facing row (§9.17); the candidate consent-form preamble outranking her own
  journey rows, with both of hers still in the set and the live replies using hers
  (§9.18); and the apology above (§9.19).
  `selfcheck_flows.py` is **349 assertions**; `smoke_nodes.py` is **43 states**.

- **2026-09-10** — **Retested as a job seeker: a cooking question she never invited,
  and no explanation of what happens next.** Their words: *"when the employee says that
  she can work in elderly care and childcare, the bot should not separately ask about
  cooking ... Also, the bot did not explain the next steps/process to the candidate."*
  (A) **The cooking question presumed an answer she never gave**, and she said so:
  *"but i am not going to do cooking work then why asked me cooking related question i
  am applying for childcare and eldercare jobs"*. Cooking is now one of the DUTIES she
  is asked whether she is willing to take on, in the agency's own words. The detail
  question — which cuisines, and pork or beef — survives, because the EMPLOYER is asked
  it and it has to be matchable, but it is **gated**, the same shape as `pets` →
  `pet_detail`.
  (B) **Gated on `work_scope`, and the first attempt is why.** Keying it on the duties
  answer was tried and measured: `Gate` matches on substrings, so *"no i dont want to
  cook, only the window cleaning"* contains "cook" and opened it — asking the cooking
  question of someone who had just refused it in writing, which is the complaint again.
  Catching that needs a list of every way a person writes a negation, which is the trap
  the note on `Gate.excludes` describes for pets. `work_scope` has a controlled
  vocabulary, so the test is decidable. **Accepted cost, recorded rather than hidden:** a
  childcare-only helper who volunteers in the duties answer that she would also cook is
  not asked which cuisines. A consultant can ask her; that is the right way round.
  (C) **"The bot did not explain the next steps" was three defects stacked, and the
  first is the one nobody would have found by reading.** Counted: **27 rows carry
  `contact_type='candidate'` and every one is rights, behaviour or settling-in advice**
  — what she is owed on food, rest days, her passport, who to call in an emergency.
  There was nothing at all about the journey she is on. Measured through the real
  retriever: `candidate_new_hiring` is not a `service_type` any row uses, so
  `_labelled_filter` narrows her to `general` (the §9.15 shape, one service along), and
  the top match for *"what is the process"* was the **candidate application checklist**
  at 0.440; *"what happens next"* returned *"I am applying of my own free will, without
  being forced"* at 0.426; *"what is the further process I have to follow"* returned
  **"Do I need to attend a course before hiring a helper?"** — an employer's question
  answered to a helper. Every one **above** the floor, so `_answerable()` read True and
  the widening retry never fired. Six rows written for her: what happens after she
  registers, the documents she provides, the interview, what happens once an employer
  chooses her, arrival, and how long it takes. **Rewritten, not copied** — every fact is
  already in the KB on the employer's side and is re-expressed from hers.
  (D) **Filed `general` + `candidate`, and the alias was measured and REJECTED.** Pointed
  at `new_hiring`, *"what is the process"* returns *"What is the process for **hiring** a
  new helper"* (0.455) and the documents question returns *"What documents do I need to
  provide **to hire** a helper?"* (0.584) — both `contact_type='all'`, both written to the
  employer, and both would have told a job seeker to produce her NRIC and her income tax
  assessment. `general` is in scope for every service, so her rows are reachable with no
  routing change at all, and `candidate` means an employer never sees them — so none of
  this can displace an employer's own row, which is the collision the transfer checklist
  had to be reworded for. **Six employer controls re-measured, every one unmoved.**
  (E) **And the reason SHE got a holding line was none of the above.** With the rows
  loaded it still failed, and the diagnosis is worth keeping: `effective_contact_type`
  puts a master record above anything one message says — rightly — but the tester's
  number is on file as an **employer**, so `contact_type` narrowed her search to employer
  rows and **all 27 helper-facing rows were invisible to her**. *"What is the process"*
  came back at **0.397**, four thousandths under the 0.40 floor. As a candidate the same
  question scores 0.437 and returns her own row. `_retrieval_audience` reads the
  candidate's shelf whenever the flow in hand is a candidate service — the service key is
  the stronger evidence, being the questionnaire we have been putting to her for a dozen
  turns. **Retrieval only**, exactly like `_RETRIEVAL_ALIASES`: the lead, the ticket and
  every master record are untouched. This is not only a tester's problem — a helper
  messaging from the household phone produces the identical state.
  (F) **The closing briefing quoted her $450, and the guard is what caught it.**
  `SERVICE_BRIEFING_NOTE` is written for somebody buying a service and **requires** a
  cost section, so pointed at a registration it did as it was told and offered the
  passport renewal's fee as the price of applying for work. `ungrounded_figures` binned
  the whole reply and `briefing_lost` logged it, so what she would have seen is the bare
  handover line — the 2026-09-08 lost-briefing signature. Two causes: the note, and
  `BRIEFING_QUERY` literally containing *"how much does it cost"*, which matches
  `_PRICE_QUESTION` and **drops the service filter**, which is how another service's
  fee was in reach at all. She now has her own note and her own query, and the note
  forbids every figure outright — no fee, no salary, no deduction — because **there is no
  helper-side fee anywhere in the knowledge base**, so every number within reach belongs
  to somebody else. It also forbids promising her a job or a date for being matched.
  (G) **A condition I added and then took back out.** `_briefing_turn` was given a
  "collection nearly done" test to stop the briefing query hijacking retrieval for the
  nine optional turns of the new flow. It broke an existing assertion, and looking at why
  showed the change was worse than the thing it fixed: if a client answers two fields at
  once, the briefing turn arrives with no records and the briefing is silently lost,
  which is the 2026-09-09 defect. The hijack is benign by comparison — those are turns
  where she is plainly answering, a question from her stands the briefing down already,
  and the rows are only used as grounding. Reverted, with the trade-off written at
  `BRIEFING_AFTER` rather than left as a silent choice.
  (H) **Verified live end to end.** The closing message now reads *"Here is what happens
  next, Muang:"* followed by eight steps one per line, nationality-correct (*"our partner
  in Indonesia"*), a closing sentence, and no figure anywhere. On the parked path the
  exact turn that failed — *"what is the process"* — returns her eight steps; *"what are
  the documents required"* returns **her** documents rather than an employer's NRIC; and
  *"any update on my application"* still gets the holding line. The duties question goes
  out as *"Would you also be willing to do duties like cooking, high-rise window
  cleaning, car washing, gardening, grocery shopping, or hand-washing laundry?"* and a
  childcare-and-eldercare helper is never asked about cuisines.
  Eight faults injected, eight red — and two of them exposed a check that **crashed**
  rather than failing, which tells you less than one that goes red; that assertion now
  uses `.get()` and names itself.
  The loader is a no-op on two consecutive runs. `selfcheck_flows.py` is **333
  assertions**; `smoke_nodes.py` is **40 states**.

- **2026-09-10** — **Tested as a job seeker: the candidate flow read her name off
  WhatsApp and then ended after nine questions.** The agency's words: *"bot didnt ask
  the name at first like all services then it should greet after taking name with
  followup question as flow has also it didnt ask for the age and any other question
  that are needed it end the conversation by taking few details"*.
  (A) **The name half is the same defect for the fifth time, and the rule written on
  2026-09-10 to stop it could not see this flow.** That rule is derived from
  `EMPLOYER_LEAD_SERVICES` — correctly, for what it covers — and a job seeker is not in
  that set, so the sweep passed while the flow opened *"Hi Vaidik Dubey, I'm Claire ...
  Which country are you from?"* with no name question at all. Filed under
  `NAME_FROM_RECORD_ONLY`, which behaves differently here on purpose:
  `get_record_name()` reads `employers` and a helper has no employer record, so
  `record_name` is **always** empty and the question is **always** asked. That is the
  right outcome — this is the name that goes on a Work Permit application, and a
  WhatsApp display label is not it. A helper who gave us her name on an earlier
  enquiry is still greeted rather than asked: that comes off `leads_candidate.full_name`
  in `_known_fields`, which runs before the push name is ever considered. The rule is
  now asserted from **both** sides — `CANDIDATE_LEAD_SERVICES` as well.
  (B) **The missing questions were an asymmetry, and it is measurable.** The EMPLOYER is
  asked 25 questions about the helper they want — her age and experience, the languages
  spoken at home, whether she would have her own room, whether she can handle pork or
  beef, which extra duties are needed, how rest days would work, what they would pay.
  The HELPER registering was asked **9**, six of them identity and logistics. So a
  consultant holding a hiring ticket that reads *"no pork, sharing with a child, weekly
  day off, around $600, window cleaning needed"* had, on her side of the desk, her
  country, her work scope and her years — and had to ring her back for the rest. This is
  the 2026-09-04 exercise (*"the employer flow now asks what the candidate form
  profiles"*) run in the one direction it was never run in. **9 fields → 18**, 17
  questions asked in practice, and the eight that identify and place her stay first so a
  helper who stops answering has still told us the things that matter most.
  (C) **Asserted as a RULE, not as the list of fields that were missing.**
  `_MATCHED_PAIRS` maps each employer question about a helper to the one she is asked
  about herself, and the self-check fails when one side gains a question and the other
  does not. The option lists are taken FROM the employer's `Field` rather than retyped
  (`_matched_options`), for the reason `_hiring_field` exists: *"eldercare"* against
  *"caring for the elderly"*, or *"$600-700"* against *"600 to 700 dollars"*, is a match
  made by eye — which is what the note on `work_scope` has said since that field was
  written. **`preferred_nationality` is the one pair that deliberately cannot share a
  list**, and it is named rather than skipped: an employer picks from the three
  nationalities we place plus "no preference"; a helper states the country she is
  actually from, and constraining her to those three would turn a Sri Lankan applicant
  away at the first question.
  (D) **Every new key is new on purpose.** `languages` and `budget` are in
  `_PORTABLE_ACROSS_SERVICES`, so reusing them would carry an employer's *"Mandarin
  spoken at home"* into a helper's file as a language **she** speaks — the same trap the
  direct-hire flow avoided with its `helper_` prefix, from the other side. Her notes are
  `candidate_notes` and not `additional_notes` for a related reason: `_WHY_WE_ASK` is
  keyed on the field key with no idea which flow is asking, and that entry reads *"so
  anything that matters to them is agreed with THE HELPER up front"* — said to the
  helper herself, a sentence about somebody else. Both are asserted.
  (E) **Deliberately NOT asked, each for its own reason.** Her health and any illness:
  `biodata.health` holds it and the MOM medical examination is what establishes it, so a
  self-report over WhatsApp is neither reliable nor ours to collect. Her passport number:
  Rule 4a, the same reason `passportNo` is never read out of biodata even though it sits
  beside the expiry we do read. Marital status and children: on the form, but the
  employer flow asks no question they would be matched against, so they fail the rule in
  (C) — the office takes them on the registration form.
  (F) **The bracket sweep was widened from the seven services to every service there
  is**, because the seven ARE the agency's employer-facing list and a candidate flow was
  outside it — the same "written for the set that was reported" shape that row was
  created to fix, one level up. Measured before widening: only `expected_salary` is new,
  and it earns its digits for exactly the reasons `budget` does (the bands are salary
  bands, and they are the grounding `ungrounded_figures` reads), so it is named in
  `_DIGITS_ON_PURPOSE` rather than quietly allowed.
  (G) **Two of the new assertions were wrong about correct code, and the run caught
  them** — the nationality pair above, and one requiring every matching question to be
  optional when her country, her scope and her age are deliberately not. Both are now
  stated as decisions with their reason, which is the 2026-09-09 (C) lesson: a check that
  fails on correct code is noise, and noise is how a real failure gets ignored.
  (H) **Verified live against the real model, both halves and both directions.** A new
  number gets *"Hi, I'm Claire, Ming Hwee's AI assistant. May I know your name so we can
  match you with the right employers?"* and, on the very next message, *"Thanks, Siti.
  Which country are you from?"* — the agency's own two-part rule. A helper already on
  file is greeted rather than asked (*"Hi Siti Rahayu, I'm Claire ... May I know your
  age..."*). The matching half runs (*"which languages do you speak, and how well?"*),
  and the ticket a consultant receives now reads **Age / Languages she speaks / Cooking
  she can do / Extra duties she will do / Comfortable with pets / Room / Rest days she
  wants / Salary she is looking for** beside the employer's own headings.
  The five new assertions were proved by injecting five faults — the flow dropped from
  the name set, the age question removed, an option list retyped instead of derived, her
  notes filed under the employer key, and the salary bands renamed past their allowance
  — and each went red naming the problem.
  `selfcheck_flows.py` is **311 assertions**; `smoke_nodes.py` is **39 states**.

- **2026-09-10** — **"the bot is not telling the process" on home leave — and the
  process was in the knowledge base all along.** The agency asked me to check whether it
  was there and to load it if not, and sent the full six steps. It is there, and the
  stored row already says all six of them: intake and nationality, checking embassy
  appointment availability and the lead time (PH 4 weeks / ID 2 weeks), the documents by
  nationality, the endorsement forms and signatures, **the levy waiver and the deferred
  six-monthly medical**, and the flights and the return. Nothing was loaded — a second
  copy is how two rows drift apart (§9.8), and `selfcheck_flows.py` now asserts the six
  are present so nobody "fixes" this by adding one.
  (A) **The defect was that the bot would not USE it.** On a parked topic
  `_answerable()` requires the intent to be one of `KB_QUESTION_INTENTS` **or**
  `asks_general_info()` to match the message. Both missed, which is why the documents
  question one message later was answered in full and this one was not:
  the classifier returned **`intent=home_leave`** — the SERVICE, not a question type —
  and the deterministic net wanted *"what is (the) process"* with nothing in between,
  while the client wrote *"Ok but tell what is the **further** process"*.
  (B) **On its own, "what is the further process" classifies correctly** as
  `process_question`. It is the *"Ok but tell"* preamble that tips the model — which is
  the entire argument for having a deterministic net underneath it, and the net had a
  gap. Exactly the 2026-09-08 failure, where this pattern required *"what is THE cost"*
  and *"Ok what is cost"* was handed to a human for a $450 answer.
  (C) **Fixed by allowing an adjective and an imperative**: `further`, `next`, `whole`,
  `entire`, `full`, `complete`, `overall`, `remaining`, `rest of the`, plus
  *"tell me the process"* / *"just tell the steps"* / *"what are the next steps"*.
  `_CHASING_STATUS` still gates the whole thing, so a real chase is unaffected — verified
  in both directions.
  (D) **Verified live on the exact failing turn**, with the intent the classifier really
  returned: it now answers with the six steps, one per line, lead-in and closing sentence
  — and *"any update on my case"* still gets the holding line.
  (E) **And a new assertion was, again, green for the wrong reason.** "A chase is still a
  chase" listed six phrases, none of which reaches `_GENERAL_INFO` at all, so it passed
  whether or not the chase guard existed. It now also tests a COMPOUND — *"any update?
  and what is the cost"* — which hits both patterns, so deleting the guard flips it.
  Second time in one day that injecting the fault found an assertion proving nothing.
  **Noticed while doing that and NOT changed:** that compound is currently held as a
  chase, so the cost question inside it goes unanswered. Arguably the same class as the
  defect above; not reported by anyone, and the guard is there for a reason, so it is
  recorded rather than tinkered with.
  `selfcheck_flows.py` is **300 assertions**; `smoke_nodes.py` is 36 states.

- **2026-09-10** — **The name rule closed across every flow that asks about a helper,
  and asserted as a rule instead of a list.** Shown the three flows the entry below
  recorded as still open, the agency's answer was to close them: *"fill that gap in all
  of these three services as well ... in direct hiring also if this gap is there"*. It
  was: `direct_hiring`, `insurance` and `transfer_employer` all ask `full_name` FIRST and
  all three had it pre-filled from the WhatsApp profile, so the question was skipped and
  the flow opened by greeting the client with a label they set on their own account and
  then asking about their helper.
  (A) **That is the whole shape of the complaint, and it has now arrived four times** —
  passport renewal (2026-09-08), renewal and home leave (2026-09-09), replacement and
  now these three (2026-09-10). *"May I know your current helper's name?"* reads as
  though we already know who the client is, when all we know is a WhatsApp display name.
  (B) **Verified live on all three, both ways.** A new number is asked
  (*"...May I know your name?"*) and greeted on the very next message (*"Thanks, Vaidik.
  May I know the full name of the helper you would like to hire?"*), and a client on file
  is greeted rather than asked (*"Hi Ratna Choukade, I'm Claire..."*). No other change:
  `_contact_block` already drops `customer_name` for this whole set when no record name
  exists, which is the 2026-09-08 fix that stopped the model saying "Hi Vaidik" and then
  asking for the name.
  (C) **The check is now a RULE, not a list.** *"No employer flow asks for a helper's
  name while reading the client's own off WhatsApp"* is derived from
  `EMPLOYER_LEAD_SERVICES`, so a flow added tomorrow cannot reopen the gap — proved by
  injecting a new service that asks a helper's name and watching it go red by name,
  rather than by reading the assertion. The hard-coded list of the seven stays alongside
  it **on purpose**: it is the tripwire, and it has already caught one field-set change
  it was written for.
  (D) **`new_hiring` is deliberately still out**, and is now the only employer flow
  reading the name off WhatsApp. It has no existing helper to ask about, so it never
  produces the shape that drew all four complaints, and filling the name there is itself
  the 2026-09-01 fix ("stop asking for a name it just used in its greeting"). Asserted
  explicitly so it reads as a decision rather than an omission.
  `selfcheck_flows.py` is **294 assertions**; `smoke_nodes.py` is 36 states.

- **2026-09-10** — **A replacement asks the client their name instead of reading it off
  WhatsApp — the same request, in almost the same words, for the third time.** The
  agency, retesting after the two fixes below: *"i also want the chatbot to ask name of
  user before asking helper name and then greet by name and then with followup question
  of helper name like other flows"*.
  (A) **It was the push name, and the live row proves it.** The flow opened *"Hi Vaidik
  Dubey, I'm Claire ... may I know your current helper's name?"* — read as a greeting it
  looks right, but conversation 3766 has `matched_employer_id` **NULL**, so there is no
  employer record and no record name. That name came off the WhatsApp profile, which is
  the 2026-09-08 passport-renewal complaint exactly: a label the client set on their own
  account, used as the name that goes on the Replacement form.
  (B) **It was left out of `NAME_FROM_RECORD_ONLY` deliberately on 2026-09-09**, on the
  reasoning that *"nobody has objected to the push name on these two"*. They have now.
  The comment saying so has been replaced rather than left to contradict the code.
  (C) **Verified both halves, live.** A number with no record now opens *"Hi, I'm Claire,
  Ming Hwee's AI assistant. May I know your name so we can match you with the right
  replacement service?"* and, on the very next message, *"Thanks, Vaidik. May I know your
  current helper's name?"* — the agency's own two-part rule. A client on file is still
  greeted rather than asked (*"Hi Ratna, I'm Claire..."*), and a returning one still gets
  *"Hi Ratna, welcome back"*.
  (D) **Three flows still have this gap, and they are recorded rather than quietly
  changed.** `direct_hiring`, `insurance` and `transfer_employer` all ask for a HELPER's
  name while taking the client's own off the WhatsApp profile — the exact shape objected
  to twice now. They were not what the agency tested, and `direct_hiring` in particular is
  a ten-field intake they have signed off, so changing it unasked is not this session's
  call. `selfcheck_flows.py` asserts that list **by name**, so it is a decision on the
  record rather than an oversight, and a fourth complaint has its answer ready.
  (E) **The existing tripwire did its job.** Adding `replacement` to the set turned the
  2026-09-09 assertion red on its hard-coded list of three — which is what that assertion
  is for, and why the counts in these entries are worth keeping honest.
  `selfcheck_flows.py` is **293 assertions**; `smoke_nodes.py` is 36 states.

- **2026-09-10** — **The replacement flow, tested end to end now that the bot can see
  who is messaging it. Two defects, and the one nobody reported is the worse one.**
  (A) **A documents answer arrived as a wall of text.** The agency's screenshot: the
  *process* answer came out as a properly broken-up list, and the *documents* answer one
  message later — same conversation, same parked path, same template — arrived as a
  single paragraph with `1. ... 2. ...` buried inside it. Nothing was stripping the line
  breaks (`clamp_reply` has sliced rather than re-joined since 2026-09-08); the model
  simply was not told to put them in. `SERVICE_BRIEFING_NOTE` has carried
  *"Every numbered item goes on ITS OWN LINE, with a real line break between them"*
  since 2026-09-08, when exactly this reached a client — and **neither of the two general
  answering paths ever got it**. That is the same "written for the one flow that was
  reported" shape §9 has already forced twice. Both now carry it; three live runs, three
  correctly formatted lists.
  (B) **The ticket said "Wants in the replacement: replace her", and that field was never
  asked.** CB-2026-0006. The client wrote *"I have not decided yet but I don't want her
  anymore you do whatever you want just replace her"*, the extractor filed
  `replacement_preferences = 'replace her'`, the field looked answered, and the
  collection went straight from the timeline to the handover. So the one field that
  exists to tell a consultant **who to look for** reached them saying nothing, and the
  transcript gives no hint that anything was missed — this is only visible in the ticket.
  Identical in shape to the 2026-09-07 care-type defect (a value that restates the
  ENQUIRY, filed as the answer to it), one field along. `_PREFERENCE_FIELDS` +
  `_states_a_preference` mirror `_CARE_TYPE_FIELDS` + `_states_a_care_type`, with **their
  own filler** so `requirement` is untouched — adding words to the shared one makes that
  test stricter and would start dropping real care types.
  (C) **And "you do whatever you want" should have been caught anyway.** `_NO_PREFERENCE`
  was anchored hard at `^`, so *"whatever you want"* matched and *"you do whatever you
  want"* did not — and the second is how people actually say it. A short filler lead-in
  is now allowed. **"want" is deliberately not in that prefix list**: *"I want any
  Filipino"* is a preference, not the absence of one, and it is asserted both ways.
  (D) **A new assertion was green for the wrong reason, and the fault injection caught
  it.** The formatting check tested for the words "own line" — but `PROCESS_INSTRUCTION`
  already said the LEAD-IN sentence goes *"on its own line"*, so the test passed with the
  per-item rule deleted. It now tests "real line break", the phrase unique to the rule it
  is about, and deleting the rule turns it red. **A check that passes for a reason
  unrelated to what it is checking is worse than no check**, and the only thing that
  found it was injecting the fault rather than reading the assertion.
  Verified: the exact live extractor output — `{'current_helper_exit': 'not decided yet',
  'replacement_preferences': 'replace her'}` — now keeps the first and drops the second,
  so the field stays open and gets asked. `selfcheck_flows.py` is **290 assertions**;
  `smoke_nodes.py` is 36 states.

- **2026-09-10** — **The LID had no phone number behind it, so we asked Whapi for one.**
  Follow-up to the entry below, which shipped a fallback and a diagnostic. The diagnostic
  answered the question on the first message: the payload carries **only** LIDs —
  `{'chat_id': '116909177569373@lid', 'from': '116909177569373@lid'}` — so there was
  nothing to fall back to, and the fallback stood down exactly as designed.
  (A) **Whapi can resolve it, and only one endpoint can.** Probed read-only against the
  live channel: `GET /chats/116909177569373@lid` returns
  `{"id":"...@lid","phone":"917970027379", ...}` — the tester's real number — while
  `GET /contacts/<lid>` returns the push name and **no phone**, and `GET /chats/<lid>`
  without the suffix is a 400. So `resolve_lid()` reads `/chats` and nothing else, and
  swapping it to `/contacts` makes it return None silently — verified by doing exactly
  that.
  (B) **It happens in the webhook, not the parser.** Parsing is sync and this is an HTTP
  call, and everything downstream is keyed on the phone — the allowlist, `get_by_phone`,
  `identify`, the lead, the ticket — so the number has to be right before any of them
  run. `parse_message` flags the message (`IncomingMessage.lid`) and
  `handle_payload` resolves it before dispatching to either handler, which covers the
  agent-detection path as well as the client one.
  (C) **The cache is bounded, which the four in §9.13 are not.** A LID identifies one
  WhatsApp account and cannot change, so the mapping is cached — oldest out first at 500,
  because a re-lookup is one cheap GET and a runaway LID stream must not grow it forever.
  (D) **Unresolvable means stand down, never guess.** If Whapi returns no phone the
  message keeps the LID and behaves exactly as it did before, with a log line saying so.
  Guessing whose number it might be is the one outcome worse than silence.
  (E) **A check that passed while the thing was broken, caught by injecting the fault.**
  The first three LID checks called `_resolve_lid` directly, so deleting the call from
  `handle_payload` left all three green — the exact failure mode `smoke_nodes.py` exists
  to prevent, and the same shape as the 2026-09-10 self-check that ran an old copy of
  itself. There is now a fourth that goes through `handle_payload` and reads what the
  inbound handler was actually handed; re-injecting the fault turns it red.
  Verified end to end against the live channel with the exact production payload:
  `+116909177569373` → **`+917970027379`**, allowlisted **True**, and the second lookup
  served from cache. `selfcheck_flows.py` is **283 assertions**; `smoke_nodes.py` is
  **36 states**.

- **2026-09-10** — **The bot went silent on an allowlisted tester, and the number in
  the log was not a phone number.** Reported from the server: every message logged
  *"Bot standing down on +116909177569373: number not in BOT_ALLOWED_NUMBERS"* while the
  tester was messaging from **+917970027379**, which is in the gate.
  (A) **116909177569373 is a Meta LID, not a mangled phone number.** WhatsApp identifies
  some senders by an opaque `@lid` instead of a phone JID, and the trigger is visible in
  the client's own chat: *"This business uses a secure service from Meta to manage this
  chat"*, dated the Monday. `normalize_phone` splits on `@` and keeps whatever is in
  front — by design, so `917970027379@s.whatsapp.net` works — so `116909177569373@lid`
  became the phone number `+116909177569373`. Reproduced exactly: that function turns
  the LID into the precise string in the production log.
  (B) **The gate did the right thing; minting the fake number was the bug.** Every
  downstream lookup is keyed on the phone — the allowlist, `get_by_phone`, `identify`,
  the lead — so a LID reaching them does not merely fail a gate. Allowlisting it, which
  is the obvious "fix", would open a SECOND conversation keyed on a non-number: the
  split-conversation bug `fix_split_conversations.py` exists to repair. Recorded in §9.16
  as a thing not to do.
  (C) **The counterparty is now resolved from the first identifier that is actually a
  phone** — `@s.whatsapp.net`, `@c.us`, or a bare number — trying `from` then `chat_id`
  inbound, and `chat_id`, `to`, `from` outbound. On every payload that already worked
  this changes nothing, which is asserted both ways; on a LID payload that carries the
  phone anywhere, it resolves.
  (D) **What could NOT be verified from here, and is written down rather than assumed:**
  whether Whapi still puts the phone in one of those fields. No raw payload is stored or
  logged, the stand-down path deliberately writes nothing, and the test conversation had
  been reset to zero messages — so there was no evidence to read. If a payload carries a
  LID and nothing else, the bot logs a WARNING naming **every identifier the payload
  did carry** and stands down as before. That line is the whole point: it is what makes
  the next occurrence diagnosable instead of a second round of guessing.
  `selfcheck_flows.py` is **279 assertions**; `smoke_nodes.py` is 32 states. The eight
  new assertions were proved by injecting two faults — the old counterparty logic, and
  treating every JID as a phone — and each went red naming the case.

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
