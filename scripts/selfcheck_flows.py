"""Prove the deployed image actually behaves as the flow specs require.

Reads nothing and writes nothing — pure in-process checks of the field
lists, the gates and the guards. Safe to run against production.

    docker compose exec chatbot python /app/scripts/selfcheck_flows.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib
import app.services.ticket as t
from app.graph.guards import quotes_hiring_package_cost as q
ic = importlib.import_module("app.graph.nodes.intent_classifier")
ico = importlib.import_module("app.graph.nodes.info_collector")
S = ico._SMALL_TICKET_SERVICES
P = ico._COLLECTION_PURPOSE
from app.graph.prompts.system import RULES
D = chr(36)
take = [f.key for f in t.applicable_fields("transfer_employer", {"transfer_direction": "taking on a transfer helper"})]
rel  = [f.key for f in t.applicable_fields("transfer_employer", {"transfer_direction": "releasing my current helper"})]
dh_emp  = [f.key for f in t.applicable_fields("direct_hiring", {"employment_status": "currently employed"})]
dh_free = [f.key for f in t.applicable_fields("direct_hiring", {"employment_status": "between jobs"})]
hire_src = next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "hire_source")
import app.graph.graph as g
rr = importlib.import_module("app.graph.nodes.rag_retriever")
from app.graph.prompts.style import STYLE_BLOCK
import app.graph.guards as gd
import app.graph.prompts.templates as tpl
from app.graph.nodes.intent_classifier import _named_service as _named_svc
import app.graph.closure as cl
import app.services.message as ms
import app.services.handover as hs
btr = importlib.import_module("app.graph.nodes.blocked_topic_responder")
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("lsn", str(Path(__file__).resolve().parent / "load_service_notes.py"))
lsn = _ilu.module_from_spec(_spec); _spec.loader.exec_module(lsn)
lang_f = next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "languages")
money_on_top = {"intent": "fee_enquiry", "service_type": "passport_renewal",
                "collected_service": "passport_renewal", "blocked_topics": {}}
money_alone = {"intent": "fee_enquiry", "service_type": "fee_enquiry",
               "collected_service": None, "blocked_topics": {}}

# A stepped answer, the shape a process question now gets back. Written here
# rather than inline because two assertions compare against it.
STEPPED = (
    "Here is how it runs:\n"
    "1. We go through what your household needs.\n"
    "2. We shortlist helpers and you interview them.\n"
    "3. We apply to MOM for her work pass.\n"
    "4. Her embassy paperwork and medical are done.\n"
    "5. She arrives and we hand her over."
)

# An employer we have met before: the portable keys carry over from an earlier
# enquiry, so a transfer request does not march them through it all again.
_RETURNING = {
    "full_name": "Vaidik Dubey", "requirement": "childcare", "household": "3-4",
    "home_type": "condo", "home_size": "3 bed 2 bath", "languages": "English",
    "preferred_nationality": "Filipino", "budget": "$600-700",
    "email": "v@example.com", "referral_source": "Google search",
    "transfer_direction": "looking for a transfer helper",
}

rows = [
 ("transfer TAKE-ON asks her name", "helper_name" in take, False),
 ("transfer RELEASE asks her name", "helper_name" in rel, True),
 ("helper-initiated transfer -> candidate",
  ic._detected_contact_type("transfer", None, "please transfer me to a new employer"), "candidate"),
 ("employer transfer -> employer",
  ic._detected_contact_type("transfer", None, "I want to transfer my helper"), "employer"),
 ("insurance is a service", "insurance" in t.SERVICE_FIELDS, True),
 ("small-ticket services", sorted(S), ["insurance", "passport_renewal", "renewal"]),
 ("blocks the hiring total", q(f"The total first-year cost is S{D}14,000-17,500."), True),
 ("still quotes salary", q(f"Salaries range from {D}600 to {D}800."), False),
 ("new_hiring field count", len(t.SERVICE_FIELDS["new_hiring"]), 25),
 ("passport_renewal asks case id", any(f.key == "case_id" for f in t.SERVICE_FIELDS["passport_renewal"]), False),
 ("renewal asks case id", any(f.key == "case_id" for f in t.SERVICE_FIELDS["renewal"]), False),
 ("NO flow asks for a case id",
  [s for s, fl in t.SERVICE_FIELDS.items() if any(f.key == "case_id" for f in fl)], []),
 ("every long flow explains why it asks",
  [s for s, fl in t.SERVICE_FIELDS.items()
   if len(fl) > 4 and s not in P and s not in S], []),
 ("prompt tells Claire to use their name", "1c." in RULES and "Hi Thomas" in RULES, True),
 ("chose WhatsApp -> not asked for an email",
  any(f.key == "email" for f in t.applicable_fields("new_hiring", {"update_channel": "WhatsApp"})),
  False),
 ("chose email -> asked for an email",
  any(f.key == "email" for f in t.applicable_fields("new_hiring", {"update_channel": "email"})),
  True),
 ("no field mentions swimming",
  [f.key for fl in t.SERVICE_FIELDS.values() for f in fl
   if "swim" in (f.label + f.question).lower()], []),
 ("first message is told to introduce Claire",
  "introduction is NOT optional" in ico.COLLECTOR_INTRO_NOTE, True),
 # --- the agency process table, 2026-09-07 ---------------------------
 # direct_hiring was an empty list, so it raised a ticket that said only
 # "wants us to process a helper they have already chosen". These six are
 # the agency's own list; if the flow is ever emptied again this fails.
 ("direct_hiring collects the agency's six",
  [k for k in ("helper_name", "helper_contact", "helper_nationality",
               "helper_location", "employment_status", "helper_availability")
   if k not in dh_emp], []),
 ("employed helper -> asked about notice / clearance",
  "notice_clearance" in dh_emp, True),
 ("helper between jobs -> NOT asked about notice",
  "notice_clearance" in dh_free, False),
 # "First-timer / Ex-Singapore / Ex-abroad / Transfer" - the old two-way
 # question could not tell a first-timer from someone with two contracts
 # behind her, which is a different person at a different salary.
 ("new_hiring offers all four experience types", len(hire_src.options) >= 4, True),
 # --- FDW passport renewal process flow, 2026-09-07 -------------------
 # The routes genuinely differ (Myanmar holds no embassy contract, so three
 # more forms), and with the nationality unknown the retrieval filter is
 # dropped and all three compete. Measured: a bare "what is the process"
 # returned the MYANMAR row top. Naming a route we have not established is
 # how an employer prepares the wrong paperwork.
 ("process question fires the nationality caveat",
  bool(ico._NATIONALITY_DEPENDENT.search("what documents are needed")), True),
 ("an ordinary answer does not",
  bool(ico._NATIONALITY_DEPENDENT.search("her name is Shushi")), False),
 ("nationality known -> no caveat needed",
  ico._known_nationality({"collected_info": {"nationality": "Myanmar"}}), "MM"),
 ("nationality unknown -> caveat needed",
  ico._known_nationality({"collected_info": {}}), None),
 ("new_hiring asks bedrooms / bathrooms",
  any(f.key == "home_size" for f in t.SERVICE_FIELDS["new_hiring"]), True),
 # --- 2026-09-07 live testing round -----------------------------------
 # A cost question on top of a service we are already handling is answered,
 # never qualified. It used to start fee_enquiry's own intake, which asked a
 # passport-renewal client "What kind of care would this be for?" twice.
 ("cost question mid-service is answered, not collected",
  g.route_after_rag(money_on_top), "response_generator"),
 ("a cost question on its own still collects",
  g.route_after_rag(money_alone), "info_collector"),
 # The intent names the SHAPE of the question; the in-flight service is its
 # subject. Tagging "what is the process" with "(process question)" scored
 # under the floor and got the parked-agent line on a question the KB answers.
 ("process question searches the service",
  "(direct hiring)" in rr._search_query(
      {"incoming_text": "What is the process", "intent": "process_question",
       "service_type": "direct_hiring"}), True),
 ("document question searches the service",
  "(passport renewal)" in rr._search_query(
      {"incoming_text": "What documents are needed", "intent": "document_question",
       "service_type": "passport_renewal"}), True),
 ("a greeting is still searched bare",
  rr._search_query({"incoming_text": "hello", "intent": "greeting",
                    "service_type": "new_hiring"}), "hello"),
 # "I want to hire a helper" names no care type, so a care type extracted
 # from it was invented. A volunteered one still lands.
 ("bare enquiry states no care type",
  ico._states_a_care_type("I want to hire a helper"), False),
 ("a volunteered care type does",
  ico._states_a_care_type("I need someone for my mum who is bedridden"), True),
 # The tag belongs on the first mention only; it went out six times in one
 # intake and read like a case file being processed.
 ("MDW tag is first-mention only", "first mention only" in STYLE_BLOCK, True),
 # --- 2026-09-08: process and documents answered in steps ----------------
 # "What is the process" and "what documents do I need" are the two questions
 # whose honest answer does not fit in two sentences. Both halves are needed:
 # without records there is nothing to lay out, and a long reply improvised
 # from nothing is the worst of the three outcomes.
 ("a process question is recognised",
  gd.asks_for_process("What is the further process?"), True),
 ("a documents question is recognised",
  gd.asks_for_process("what documents are needed"), True),
 ("process as a VERB is not a process question",
  gd.asks_for_process("can you help me process her paperwork"), False),
 ("an ordinary answer is not a process question",
  gd.asks_for_process("her name is Shushi"), False),
 # clamp_reply used to score "1." as a sentence of its own, so a six-step
 # answer cost twelve sentences and half of it was deleted before sending.
 ("a numbered list is not counted twice",
  gd.clamp_reply(STEPPED, 10), STEPPED),
 ("clamping keeps the line breaks",
  "\n" in gd.clamp_reply(STEPPED, 3), True),
 ("prose is still clamped exactly as before",
  gd.clamp_reply("One. Two. Three.", 2), "One. Two."),
 # The instruction has to change too. Widening the clamp while
 # RESPONDER_INSTRUCTION still says "one or two sentences - answer the
 # question and stop" is two prompts pulling opposite ways, which is how the
 # 2026-09-07 languages defect happened.
 ("a stepped answer has its own instruction",
  "number the steps" in tpl.PROCESS_INSTRUCTION.lower(), True),
 ("the stepped instruction forbids invented figures",
  "do not invent a duration" in tpl.PROCESS_INSTRUCTION.lower(), True),
 ("the stepped instruction keeps our internals out of it",
  "internal" in tpl.PROCESS_INSTRUCTION.lower(), True),
 ("a parked topic can still answer in steps",
  "numbered" in tpl.PROCESS_ADDENDUM.lower(), True),
 ("the addendum does not reopen the parked topic",
  "leave that where it is" in tpl.PROCESS_ADDENDUM, True),
 # A determiner settles noun-vs-verb; the word after it does not. Testing the
 # verb first rejected "the full process for hiring a helper" - the most
 # natural phrasing of the very question the detector exists for.
 ("the noun survives a following 'for'",
  gd.asks_for_process("what is the full process for hiring a helper"), True),
 ("a bare verb usage is still excluded",
  gd.asks_for_process("can you process the documents for me"), False),
 # A numbered list IS the requested format on the stepped path, and is still a
 # document everywhere else. The first build of this change was binned by
 # looks_like_document on every process question and handed to a human.
 ("steps are allowed where they were asked for",
  gd.looks_like_document(STEPPED, allow_steps=True), False),
 ("steps are still a document elsewhere",
  gd.looks_like_document(STEPPED), True),
 ("headings are a document even on the stepped path",
  gd.looks_like_document("### Where to Find\n**Plumbers**\n- one", allow_steps=True), True),
 # --- 2026-09-08: direct hire branches on where she is --------------------
 # Unlike the passport branch this changes the TIMELINE as well as the
 # paperwork - 2 to 3 weeks against 4 to 6 - so committing to a route before
 # being told which applies hands the client a date they will plan around.
 ("a direct-hire timing question is route-dependent",
  bool(ico._LOCATION_DEPENDENT.search("how long does it take")), True),
 ("so is a process question",
  bool(ico._LOCATION_DEPENDENT.search("what is the process")), True),
 ("an ordinary answer is not",
  bool(ico._LOCATION_DEPENDENT.search("her number is 98765432")), False),
 ("location unknown -> caveat needed",
  ico._known_helper_location({"collected_info": {}}), None),
 ("location known -> no caveat needed",
  ico._known_helper_location(
      {"collected_info": {"helper_location": "already in Singapore"}}),
  "already in Singapore"),
 # direct_hiring already asks where she is, so the route is derivable and no
 # new question was added for it.
 ("direct_hiring still asks where she is",
  any(f.key == "helper_location" for f in t.SERVICE_FIELDS["direct_hiring"]), True),
 # --- 2026-09-08: what direct hire shares with new hiring -----------------
 # The agency's own line: "No candidate sourcing, matching or interviews.
 # Everything else mirrors New Hiring." The shared steps are filed as
 # 'general', which the match function lets through for every service, so
 # neither flow needs a duplicate copy that can drift.
 ("the shared steps are the MOM ones",
  {"What is an IPA?",
   "What happens before my helper flies to Singapore?",
   "What happens when my helper arrives in Singapore?"}
  <= set(lsn._SHARED_WITH_DIRECT_HIRE) | {"What is the Settling-In Programme?"}, True),
 ("sourcing and matching are NOT shared",
  any(q in lsn._SHARED_WITH_DIRECT_HIRE for q in (
      "How do you match me with a helper?",
      "Can I interview the helper before I decide?",
      "What are the stages of hiring a helper from start to finish?")), False),
 # A relocated row is no longer where ROWS says it is, and the skip check keys
 # on question + service_type. Deriving the map from UPDATES is what stops the
 # loader re-inserting all eight as duplicates on its second run.
 ("the relocation map is derived from UPDATES",
  lsn._RELOCATED == {u["where"]["question"]: u["set"]["service_type"]
                     for u in lsn.UPDATES if "service_type" in u["set"]}, True),
 ("every relocated row lands in a bucket both flows can see",
  set(lsn._RELOCATED.values()), {"general"}),
 ("every correction states a reason",
  all(u.get("reason") for u in lsn.UPDATES), True),
 # --- 2026-09-07 client testing round: the transfer flow -----------------
 # Ticket CB-2026-0004 reached an agent carrying two fields and nothing else,
 # because "I'm looking for a transfer helper" was extracted as
 # transfer_direction='transfer' - a value matching NEITHER gate, so both
 # closed and the only ungated field left was `timeline`. One question, then
 # completion, then handover.
 ("a direction that opens no branch is not an answer",
  ico._undecidable_gate_keys("transfer_employer",
                             {"transfer_direction": "transfer"}),
  ["transfer_direction"]),
 ("a direction that opens the take-on branch is",
  ico._undecidable_gate_keys("transfer_employer",
                             {"transfer_direction": "taking on a transfer helper"}), []),
 ("so is one that opens the release branch",
  ico._undecidable_gate_keys("transfer_employer",
                             {"transfer_direction": "releasing my current helper"}), []),
 ("an unanswered gate key is left alone",
  ico._undecidable_gate_keys("transfer_employer", {}), []),
 ("an ungated service is never touched",
  ico._undecidable_gate_keys("passport_renewal", {"nationality": "Myanmar"}), []),
 # With the direction answered, the client is asked what they NEED - the
 # client's own words: it "should be asking for preferred nationality and
 # needs and household requirements", not when they want it arranged.
 ("taking on a transfer opens with what they need",
  [f.key for f in t.missing_fields(
      "transfer_employer",
      {"full_name": "Thomas", "transfer_direction": "taking on a transfer helper"})][0],
  "requirement"),
 # Agency, 2026-09-07: "This question asked is not required." Asking a client
 # in a hurry when they want it produces "ASAP" every time.
 ("transfer never asks when they want it sorted",
  any(f.key == "timeline" for f in t.SERVICE_FIELDS["transfer_employer"]), False),
 # Agency, 2026-09-08: "if user is new then ask every question that is related
 # and needed for the hiring ... not end conversation in 4 questions only."
 ("a new take-on client is asked more than four things",
  len(t.missing_fields(
      "transfer_employer",
      {"full_name": "V", "transfer_direction": "looking for a transfer helper"})) > 10,
  True),
 # ... while an EXISTING one is not marched through it again. Ten of these keys
 # are portable, so anything answered in an earlier enquiry carries over.
 ("an existing client is asked materially fewer",
  len(t.missing_fields("transfer_employer", _RETURNING)) <
  len(t.missing_fields("transfer_employer",
                       {"full_name": "V",
                        "transfer_direction": "looking for a transfer helper"})) - 5,
  True),
 ("releasing a helper is still short",
  [f.key for f in t.missing_fields(
      "transfer_employer",
      {"full_name": "V", "transfer_direction": "releasing my current helper"})],
  ["helper_name", "reason"]),
 # Reused from new_hiring, not copied - a reworded question must land in both.
 ("the take-on questions are new_hiring's own",
  next(f.question for f in t.SERVICE_FIELDS["transfer_employer"] if f.key == "household")
  == next(f.question for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "household"),
  True),
 ("no timing question crept back in",
  any(f.key in ("timeline", "start_timeline")
      for f in t.SERVICE_FIELDS["transfer_employer"]), False),
 # A household of seven was told the largest bracket was 5-6, because the
 # written question named none of its four options so the generic "drop two or
 # three in as examples" rule applied. Same defect as `languages`, same fix.
 ("the household question names all four brackets",
  all(o in next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "household").question
      for o in ("1-2", "3-4", "5-6", "7 or more")), True),
 # "Bot doesn't ask for my name or addresses me if it knows."
 ("transfer_employer asks the client's name",
  t.SERVICE_FIELDS["transfer_employer"][0].key, "full_name"),
 # "i stated my request and it replied me with the correct follow up question
 # of residence type, but once i respond it asks me for what my request is."
 ("a bare answer to our question is an answer",
  gd.answering_our_question(
      "Client: hi\nYou: How many people live in your household, 1-2, 3-4?", "3-4"), True),
 ("a question back at us is not",
  gd.answering_our_question("You: How many people live in your household?",
                            "What is MDW?"), False),
 ("nor is anything, if we did not ask",
  gd.answering_our_question("You: I have passed this to our team.", "3-4"), False),
 ("an answer is never diverted away from its own collection",
  g.route_after_rag({**money_on_top,
                     "history_text": "You: How many people live in your household, 1-2, 3-4?",
                     "incoming_text": "3-4"}), "info_collector"),
 ("a real money question still is",
  g.route_after_rag({**money_on_top,
                     "history_text": "You: How many people live in your household, 1-2, 3-4?",
                     "incoming_text": "how much does it cost?"}), "response_generator"),
 # --- 2026-09-08: work permit renewal documents + process ----------------
 # The Renewal Notification is the one thing an employer cannot start without,
 # and it was nowhere in the KB. MOM sends it before expiry; without a copy
 # there is no application to make.
 ("the renewal rows name the Renewal Notification",
  any("Renewal Notification" in r["answer"]
      for r in lsn.ROWS if r["service_type"] == "renewal"), True),
 ("renewal collects documents and the steps",
  {"What documents do I need to renew my helper's work permit?",
   "What are the steps to renew my helper's work permit?"}
  <= {r["question"] for r in lsn.ROWS if r["service_type"] == "renewal"}, True),
 # This asserted the OPPOSITE until 2026-09-08 - "there is still no agency fee
 # for work permit renewal anywhere in the KB", open since 2026-09-04. The
 # agency's consolidated table closed it. renewal is a small-ticket service and
 # is not in COST_WITHHELD_SERVICES, so the figure goes out as written.
 ("the work permit renewal fee is in the KB at last",
  any(f"{D}695" in r["answer"]
      for r in lsn.ROWS if r["service_type"] == "renewal"), True),
 ("and a $695 quote is not withheld as a package cost",
  q("Work permit renewal is $695. That covers us handling the whole thing."), False),
 # --- 2026-09-08: the passport-renewal checklist supersedes 2026-09-07 -----
 # First submission that CONTRADICTED rows already loaded. Two differences with
 # consequences at an embassy counter: the Philippines needs the ORIGINAL
 # passport (the old rows said "a copy" for everyone), and the Undertaking Form
 # is NOT exclusive to helpers without an embassy contract.
 ("the passport fee is in the KB at last",
  any("$450" in r["answer"] for r in lsn.ROWS
      if r["service_type"] == "passport_renewal"), True),
 ("a $450 quote is not withheld as a package cost",
  gd.quotes_hiring_package_cost(
      "It is $450 for a Filipino helper and $450 for an Indonesian helper."), False),
 ("the PH correction demands the ORIGINAL passport",
  any("ORIGINAL" in u["set"].get("answer", "")
      for u in lsn.UPDATES if "Filipino helper need" in u["where"]["question"]), True),
 ("the ID correction says a copy is enough",
  any("original is not required" in u["set"].get("answer", "")
      for u in lsn.UPDATES if "Indonesian helper need" in u["where"]["question"]), True),
 ("no Myanmar passport fee was invented",
  any("Myanmar" in r["answer"] and "$" in r["answer"] for r in lsn.ROWS
      if r["service_type"] == "passport_renewal"), False),
 # --- 2026-09-08: the transfer collection drifted into new_hiring ---------
 # "yes i want 3 year experienced maid", answering our own age/experience
 # question, reclassified as new_hiring - a hard SERVICE_INTENT, so none of the
 # stickiness rules applied - and the next question was new_hiring's own
 # hire_source: "are you open to a first-timer, or ... a transfer helper
 # already here?", put to a man who opened with "i am looking for a transfer
 # helper".
 ("hire_source is not a transfer question",
  any(f.key == "hire_source" for f in t.SERVICE_FIELDS["transfer_employer"]), False),
 ("the live turn reads as an answer, not a new topic",
  gd.answering_our_question(
      "You: Any preference on her age or experience, such as younger or at "
      "least 2 years of experience?", "yes i want 3 year experienced maid"), True),
 ("naming another service is still a real switch",
  _named_svc("i also want to renew my helper passport"), "renewal"),
 ("an ordinary answer names no service",
  _named_svc("yes i want 3 year experienced maid"), None),
 # "returning client" was offered as an ANSWER to how they heard about us, so
 # the model read it into the question - asking a man whose placements we count
 # every turn whether he is returning.
 ("returning client is not an answer we offer",
  any("returning client" in (f.options or ())
      for fl in t.SERVICE_FIELDS.values() for f in fl), False),
 ("a returning client is never asked how they found us",
  ico._known_fields({"prior_hires": 2}).get("referral_source"),
  "returning client - placed with us before"),
 ("a first-timer still is",
  ico._known_fields({"prior_hires": 0}).get("referral_source"), None),
 # --- a broadcast is not an agent, 2026-09-08 -------------------------
 # The agency's number-migration notice went to ~50 clients, landed in the
 # bot's own threads, and the agent detector read it as a human taking over.
 # A client's "hi i want to renew my helper passport" got silence.
 ("a mass announcement is recognised by how it addresses people",
  "dear valued customer" in ms._BROADCAST_MARKERS, True),
 # An agent answering "what time do you open" says this. A marker that fires
 # on a real agent is far worse than the bug it fixes.
 ("...and 'operating hours' is deliberately not one of them",
  any("operating hours" in m for m in ms._BROADCAST_MARKERS), False),
 ("two conversations in one run is already a broadcast",
  ms._AUTO_REPLY_MIN_IN_PROCESS, 2),
 ("a stand-down can be undone without minting a new thread",
  hasattr(hs, "undo_agent_takeover"), True),
 # --- warmth, 2026-09-09 ----------------------------------------------
 # Thomas: "she currently feels quite transactional - like a form, not a
 # conversation ... Customers are more likely to answer fully and feel at ease
 # if she briefly explains why she's asking." He named four; he was equally
 # clear that the plain ones stay plain: "Keep the simpler ones (number of
 # children, home type) short and direct as they are now."
 ("the intrusive questions say why they are asked",
  sorted(ico._WHY_WE_ASK), ["additional_notes", "pets", "rest_day"]),
 ("the plain ones are left plain",
  [k for k in ("home_type", "household", "languages", "home_size")
   if k in ico._WHY_WE_ASK], []),
 # budget was named too and is deliberately absent - see the note beside it.
 # Explaining why we want a budget made the model volunteer a salary range.
 ("budget deliberately does not explain itself",
  "budget" in ico._WHY_WE_ASK, False),
 ("and explaining is never an excuse to quote a figure",
  "NOT an invitation to give examples" in ico._field_guidance(
      "new_hiring", {}, next(f for f in t.SERVICE_FIELDS["new_hiring"]
                             if f.key == "pets")), True),
 # A field's own options are OUR figures, written in SERVICE_FIELDS, and
 # _field_guidance tells the model to offer two or three as examples. The
 # guard checked the message, the history, the records and the collected
 # values - never the field list the question came from - so every budget
 # turn was discarded and fell back to the bare question.
 ("a field's own options count as grounded",
  gd.ungrounded_figures(
      "Do you have a budget in mind, such as $500-600 or $600-700?",
      " ".join(next(f for f in t.SERVICE_FIELDS["new_hiring"]
                    if f.key == "budget").options)), []),
 ("a figure that is NOT one of them is still caught",
  gd.ungrounded_figures(
      "Most families pay around $1,200 a month.",
      " ".join(next(f for f in t.SERVICE_FIELDS["new_hiring"]
                    if f.key == "budget").options)), ["1200"]),
 # "Beyond the usual cleaning and cooking, WOULD she need to..." is a yes/no
 # question wearing a subordinate clause, and "no" was being re-asked.
 ("an auxiliary after a comma still makes it a yes/no question",
  ico._yes_no_question(
      next(f for f in t.SERVICE_FIELDS["new_hiring"]
           if f.key == "special_duties").question), True),
 ("'no' to the extra-duties question is an answer",
  [f.key for f in ico._unfinished("new_hiring", {"special_duties": "no"},
                                  {"special_duties": 1})[0]], []),
 ("a question with no auxiliary anywhere still re-asks a bare yes",
  [f.key for f in ico._unfinished("new_hiring", {"helper_profile": "Yes"},
                                  {"helper_profile": 1})[0]], ["helper_profile"]),
 # "Recognise them by name if known, reference their last enquiry ... This
 # alone will make repeat customers feel remembered rather than processed."
 ("a returning client is asked whether this follows on from last time",
  "follow-up on that or something new" in ico.RETURNING_NOTE, True),
 ("but is never read their own file",
  "reading out their file" in ico.RETURNING_NOTE, True),
 # --- the briefing moved to the END of the collection, 2026-09-08 -----
 # Agency, after testing: "After all the questions it should reply with that
 # process message ... the cost should be first, then the estimated time, and
 # then the process ... add the heading of the message."
 ("the briefing leads with a heading",
  "your reply MUST begin with it" in tpl.SERVICE_BRIEFING_NOTE, True),
 # Reordered 2026-09-09 at the client meeting. Shirley's summary of what the
 # client must be told is "nationality -> timeline -> requirements/documents",
 # with the fee up front so they can say yes or no.
 ("timeline first, then the cost, then the documents",
  tpl.SERVICE_BRIEFING_NOTE.index("HOW LONG it takes")
  < tpl.SERVICE_BRIEFING_NOTE.index("WHAT IT COSTS")
  < tpl.SERVICE_BRIEFING_NOTE.index("WHAT DOCUMENTS"), True),
 # Their exclusion list, in their words: the bot must not explain that an
 # appointment is booked, how it is handled, or that a runner accompanies her.
 ("the briefing never explains how we do the work",
  "DO NOT EXPLAIN HOW WE DO THE WORK" in tpl.SERVICE_BRIEFING_NOTE, True),
 ("no runner, no appointment mechanics",
  all(w in tpl.SERVICE_BRIEFING_NOTE for w in ("no runner", "No embassy appointment")),
  True),
 ("and it asks whether they want to go ahead",
  "whether they would like to go ahead" in tpl.SERVICE_BRIEFING_NOTE, True),
 # "approximately" was asked for by name, over "roughly".
 ("'approximately', not 'roughly'",
  'Say "approximately", not "roughly"' in tpl.SERVICE_BRIEFING_NOTE, True),
 # Where she is was called irrelevant; the work permit is a separate service.
 ("passport renewal is four questions now",
  [f.key for f in t.SERVICE_FIELDS["passport_renewal"]],
  ["full_name", "helper_name", "nationality", "passport_expiry"]),
 ("it no longer asks where the helper is",
  any(f.key == "helper_location" for f in t.SERVICE_FIELDS["passport_renewal"]), False),
 ("nor when the work permit expires",
  any(f.key == "permit_expiry" for f in t.SERVICE_FIELDS["passport_renewal"]), False),
 ("the briefing turn no longer goes looking for the process",
  "process" in rr.BRIEFING_QUERY, False),
 ("every step gets its own line",
  "ITS OWN LINE" in tpl.SERVICE_BRIEFING_NOTE, True),
 # A briefing discarded by a guard used to be recorded as GIVEN, so it was
 # never retried and the client simply never got it. Live: after "myanmar" the
 # reply was passport_expiry's hand-written question verbatim - the fallback.
 ("a discarded briefing is not recorded as given",
  "briefing_lost" in (Path(__file__).resolve().parents[1]
                      / "app/graph/nodes/info_collector.py").read_text(
                          encoding="utf-8"), True),
 # $450 is the Filipino and Indonesian price. It is in the records, so
 # ungrounded_figures passes it - and it is not Myanmar's price.
 ("we hold a passport fee for PH and ID",
  [n for n in ("PH", "ID") if not t.fee_is_known_for("passport_renewal", n)], []),
 ("and none for Myanmar, so none may be quoted",
  t.fee_is_known_for("passport_renewal", "MM"), False),
 ("home leave is priced the same two ways",
  t.FEE_BY_NATIONALITY["home_leave"], frozenset({"PH", "ID"})),
 ("a service with one price for everyone is unaffected",
  t.fee_is_known_for("renewal", None), True),
 # --- the name is asked, not taken off WhatsApp, 2026-09-08 -----------
 # Agency, on seeing "Hi Vaidik, I'm Claire ... May I know your helper's
 # name?" go to a number we had never spoken to: "We are picking the name
 # from WhatsApp automatically ... this is what we don't want from now on in
 # passport renewal flow." Ask when our records do not have it; greet when
 # they do. The push name is a profile label, not the name on a document.
 ("passport renewal never takes the name off WhatsApp",
  "passport_renewal" in t.NAME_FROM_RECORD_ONLY, True),
 ("a new number is asked for it",
  ico._known_fields({"customer_name": "Vaidik"},
                    "passport_renewal").get("full_name"), None),
 ("a client on our file is greeted, not asked",
  ico._known_fields({"customer_name": "Vaidik", "record_name": "Vaidik Dubey"},
                    "passport_renewal").get("full_name"), "Vaidik Dubey"),
 # Scoped deliberately: the opposite behaviour was itself a fix (2026-09-01).
 ("every other flow still uses the push name",
  ico._known_fields({"customer_name": "Vaidik"},
                    "new_hiring").get("full_name"), "Vaidik"),
 # The briefing leads with the money and the time. Their words: "It is going
 # straight forward, like 'We handle it for you.' We don't want this thing:
 # We have to tell the estimated time and the cost. and then We move forward
 # to the process."
 ("the briefing leads with the timing and the money",
  "Lead with the timing and the money" in tpl.SERVICE_BRIEFING_NOTE, True),
 ("and no longer opens by reassuring them",
  "Do not open with reassurance" in tpl.SERVICE_BRIEFING_NOTE, True),
 # It claimed the fee was not in the records and produced it a message later.
 ("it may not claim a figure is missing when it is there",
  "do NOT claim something is missing" in tpl.SERVICE_BRIEFING_NOTE, True),
 # --- passport renewal explains itself, 2026-09-08 --------------------
 # Agency: "we have to tell them the whole process, the documents required,
 # the cost/fees, and how long it takes ... a new user doesn't know how the
 # process is going on." Greet by name, learn the nationality, then explain.
 ("passport renewal asks the client's name first",
  [f.key for f in t.SERVICE_FIELDS["passport_renewal"]][:3],
  ["full_name", "helper_name", "nationality"]),
 ("the briefing hangs off the nationality",
  t.BRIEFING_AFTER.get("passport_renewal"), "nationality"),
 ("nothing to brief before we know it",
  t.briefing_due("passport_renewal", {"helper_name": "Michan"}, []), False),
 ("due the moment we do",
  t.briefing_due("passport_renewal", {"nationality": "Indonesian"}, []), True),
 ("and never twice",
  t.briefing_due("passport_renewal", {"nationality": "Indonesian"},
                 ["passport_renewal"]), False),
 # It must survive the per-turn reset, or it is given again every turn.
 ("the briefing is remembered across turns",
  "briefed_services" in g._TURN_RESET, False),
 ("the briefing turn searches for the whole picture, not the last answer",
  rr._search_query({"incoming_text": "Indonesian", "intent": "passport_renewal",
                    "service_type": "passport_renewal",
                    "collected_info": {"nationality": "Indonesian"},
                    "briefed_services": []}),
  rr.BRIEFING_QUERY + "\n(passport renewal)"),
 ("...and goes back to normal once it has been given",
  rr._search_query({"incoming_text": "Indonesian", "intent": "passport_renewal",
                    "service_type": "passport_renewal",
                    "collected_info": {"nationality": "Indonesian"},
                    "briefed_services": ["passport_renewal"]}),
  "Indonesian\n(passport renewal)"),
 # Eight rows, not five: at five the TIMING row fell off the end and the
 # briefing could not say how long it takes.
 ("the briefing turn is given more rows than usual",
  rr.BRIEFING_MATCH_COUNT > 5, True),
 ("the briefing forbids inventing what the records do not give",
  "say nothing at all about that one" in tpl.SERVICE_BRIEFING_NOTE, True),
 ("and forbids naming another nationality's route",
  "leave the rest" in tpl.SERVICE_BRIEFING_NOTE, True),
 # Cross-questioning after the briefing is the point of it.
 ("'what if' is heard as a question",
  bool(ico._ASKS_SOMETHING.search("what if her work permit expires too")), True),
 ("'what about' too",
  bool(ico._ASKS_SOMETHING.search("what about the fee")), True),
 ("a yes/no answer may not reverse the records",
  "opposite of what the records say" in tpl.ANSWER_THEN_ASK_INSTRUCTION, True),
 # --- the 2026-09-08 live round: new hiring + passport renewal --------
 # REGRESSION, same day. _undecidable_gate_keys assumed a field's gates cover
 # its whole answer space. True for transfer_direction, false for requirement,
 # whose gates are childcare -> children_detail and eldercare -> elderly_detail
 # while "general housework and cooking" is a first-class option opening
 # neither. Live: "General house work" was blanked and re-asked, "Only general
 # housework" was blanked and re-asked, and the third message was "I have tell
 # several time I need general housework".
 ("general housework answers the requirement question",
  ico._undecidable_gate_keys("new_hiring", {"requirement": "general housework"}), []),
 ("every declared option answers it",
  [o for o in next(f for f in t.SERVICE_FIELDS["new_hiring"]
                   if f.key == "requirement").options
   if ico._undecidable_gate_keys("new_hiring", {"requirement": o})], []),
 ("a transfer direction that opens no branch is still caught",
  ico._undecidable_gate_keys("transfer_employer",
                             {"transfer_direction": "transfer"}),
  ["transfer_direction"]),
 ("the rule is off for a field with no options to check against",
  ico._gates_are_exhaustive("new_hiring", "requirement",
                            [f.gate for f in t.SERVICE_FIELDS["new_hiring"]
                             if f.gate and f.gate.field == "requirement"]), False),
 # "6 bedroom and 6 bathrooms are there" matched `are there`, so the collector
 # believed a question had been asked and promised to "confirm your question
 # with the team and come back to you" - a promise to answer nothing.
 ("a trailing 'are there' is a statement, not a question",
  bool(ico._ASKS_SOMETHING.search("6 bedroom and 6 bathrooms are there")), False),
 ("an opening 'is there' still is",
  bool(ico._ASKS_SOMETHING.search("Is there a fee for this?")), True),
 ("and the 2026-09-02 case still fires",
  bool(ico._ASKS_SOMETHING.search("In 2 weeks can you provide")), True),
 # A bare "Yes" closed "Any preference on her age or how much experience she
 # should have?" and the agent got a preference with no content.
 ("a bare yes does not answer an open question",
  [f.key for f in ico._unfinished("new_hiring", {"helper_profile": "Yes"},
                                  {"helper_profile": 1})[0]], ["helper_profile"]),
 ("a bare yes DOES answer a yes-or-no question",
  [f.key for f in ico._unfinished("new_hiring", {"pets": "Yes"},
                                  {"pets": 1})[0]], []),
 ("'Yes all' answers the extra-duties question",
  [f.key for f in ico._unfinished("new_hiring", {"special_duties": "Yes all"},
                                  {"special_duties": 1})[0]], []),
 # "Ok what is cost" and "what is cost" got the holding line on a parked
 # passport renewal, for a figure the KB holds ($450).
 ("a bare price question reads as a general question",
  [m for m in ("Ok what is cost", "what is cost", "what's the cost",
               "what is the fee")
   if not btr.asks_general_info(m)], []),
 ("chasing the case still is not one",
  btr.asks_general_info("any update on my passport renewal?"), False),
 # The widening retry dropped the filter below the floor, which handed back
 # the WORK PERMIT fee ($695) inside a PASSPORT renewal ($450).
 ("a price question is recognised",
  bool(rr._PRICE_QUESTION.search("what is cost")), True),
 ("a timing question is not, so it keeps its widening retry",
  bool(rr._PRICE_QUESTION.search("how much time it takes")), False),
 # "okayyyyyyyyyyyyyyyyyyyyyyyyyyy" was answered with the handover line the
 # client had already been given twice.
 ("an elongated acknowledgement is an acknowledgement",
  [m for m in ("okayyyyyyyyyyyy", "Okayyy", "thankssss", "sureee")
   if not cl.is_pure_acknowledgement(m)], []),
 ("an elongated 'goooood' survives the other collapsing",
  cl.is_pure_acknowledgement("goooood"), True),
 ("a question is still not an acknowledgement",
  cl.is_pure_acknowledgement("ok what is cost"), False),
 ("'??' is still answered",
  cl.needs_no_reply("??", history_text="You: shortly."), False),
 # --- a price question names the shape, not the subject ---------------
 # A price question names the shape of the question, not its subject - the
 # same defect fixed for process_question/document_question on 2026-09-07,
 # with the money intents deliberately left out then. Measured: a bare "how
 # much does it cost" inside a renewal returned Form A's HIRING schedule
 # (0.472); tagged with the service it returns the renewal's own row.
 ("a fee question takes the service as its subject",
  "fee_enquiry" in rr._SUBJECTLESS_INTENTS, True),
 # What a helper EARNS is about the helper, not the service. Service-tagging
 # it measured worse (0.505 -> 0.446), so it stays out.
 ("a salary question does not",
  "salary_enquiry" in rr._SUBJECTLESS_INTENTS, False),
 ("a fee question with nothing else in flight still searches bare",
  rr._search_query({"incoming_text": "how much does it cost",
                    "intent": "fee_enquiry", "service_type": "fee_enquiry"}),
  "how much does it cost"),
 # Tagged with the COST of the service, not just the service. The KB phrases
 # these rows "How much does it cost to renew my helper's passport?", and a
 # terse "what is cost" tagged only "(passport renewal)" landed at 0.367 -
 # under the floor, so the client got a holding line for a figure we hold.
 ("and inside a service it is tagged with that service's cost",
  rr._search_query({"incoming_text": "how much does it cost",
                    "intent": "fee_enquiry", "service_type": "home_leave"}),
  "how much does it cost\n(cost of home leave)"),
 # Widening a money question is right when the figures live elsewhere and
 # WRONG when this service states its own price - the widened search then
 # returns another service's fee, which is false rather than merely vague.
 # Live measurement: "how much does it cost" inside a PASSPORT renewal
 # returned the WORK PERMIT row, $695 where the answer is $450.
 ("a service that states its own fee keeps the filter",
  [rr._service_filter({"incoming_text": "how much does it cost",
                       "service_type": svc})
   for svc in ("renewal", "passport_renewal", "home_leave")],
  ["renewal", "passport_renewal", "home_leave"]),
 ("a service that withholds its fee still widens",
  rr._service_filter({"incoming_text": "how much does it cost",
                      "service_type": "new_hiring"}), None),
 ("the two cost sets are exact opposites",
  gd.FEE_STATED_SERVICES & gd.COST_WITHHELD_SERVICES, frozenset()),
 # --- the consolidated cost + timeline table, 2026-09-08 --------------
 # A fee is stated where the agency stated one and deferred where they did
 # not: "the service which do not have the timeline and cost that means we
 # dont have to open that live agent will handle that".
 ("a fee is withheld on every service the table left blank",
  {"new_hiring", "direct_hiring", "replacement", "transfer", "transfer_employer"}
  <= gd.COST_WITHHELD_SERVICES, True),
 ("and quoted on every service it filled in",
  {"renewal", "passport_renewal", "home_leave"} & gd.COST_WITHHELD_SERVICES, set()),
 # An employer transfer runs under its OWN service key, so leaving it out
 # would have withheld nothing on the half that actually asks about cost.
 ("the employer half of a transfer is covered too",
  "transfer_employer" in gd.COST_WITHHELD_SERVICES, True),
 ("replacement and transfer defer the fee to a person",
  all(any("consultant will confirm" in r["answer"] for r in lsn.ROWS
          if r["service_type"] == svc and "cost" in r["question"].lower())
      for svc in ("replacement", "transfer")), True),
 # Three rows ANSWERED the question the table answers, with a different
 # number. Stacking them would put a flat contradiction in front of a model
 # that quotes either.
 ("the stale new-hiring timeline is corrected, not stacked",
  any("4 to 6 weeks" in u["set"].get("answer", "")
      for u in lsn.UPDATES
      if u["where"]["question"].startswith("How long does it take to hire")), True),
 ("so is the renewal one",
  any("around 3 days" in u["set"].get("answer", "")
      for u in lsn.UPDATES
      if u["where"]["question"] == "How do I renew my helper's work permit?"), True),
 ("and the transfer one, which gave only the MOM window",
  any("1 to 2 weeks" in u["set"].get("answer", "")
      for u in lsn.UPDATES
      if u["where"]["question"].startswith("How long does a transfer take")), True),
 ("every correction still states why",
  all(u.get("reason") for u in lsn.UPDATES), True),
 # --- replacement, 2026-09-08 -----------------------------------------
 # Fourteen rows carried service_type='replacement' and NONE of them said how
 # one is done: two FAQ answers and twelve raw Client Service Agreement
 # clauses. So clause 3.1 - the two-replacements-in-six-months entitlement -
 # was the top match for seven different questions, five of them ABOVE the
 # 0.40 floor, which is why the widening retry never fired. A client asking
 # what paperwork to gather would have been read a refund clause.
 ("a replacement states its own document checklist",
  any("Income Tax Assessment" in r["answer"]
      for r in lsn.ROWS if r["service_type"] == "replacement"), True),
 ("and the forms we prepare, including the two that replace the fee schedule",
  all(term in "".join(r["answer"] for r in lsn.ROWS
                      if r["service_type"] == "replacement")
      for term in ("Replacement form", "Replacement Services and Fees form",
                   "Job Offer Form", "Authorisation Form")), True),
 ("and the nine steps",
  any("nine steps" in r["answer"]
      for r in lsn.ROWS if r["service_type"] == "replacement"), True),
 # The incoming candidate's half mirrors new hiring in the agency's own words,
 # and those steps live in 'general' since 2026-09-08 - reachable from every
 # service. Copying them under 'replacement' is how two copies drift (§9.8).
 ("the shared MOM steps are not duplicated under replacement",
  any(w in r["answer"] for r in lsn.ROWS if r["service_type"] == "replacement"
      for w in ("IPA is issued",)), True),
 ("no replacement fee is invented",
  any(D in r["answer"] for r in lsn.ROWS if r["service_type"] == "replacement"), False),
 # 'general' is retrieved from INSIDE a new_hiring or direct_hiring
 # conversation, where quotes_hiring_package_cost runs on the reply. A general
 # row that trips it would swap the whole answer for the cost-deferral line.
 ("no 'general' row trips the cost guard",
  [r["question"] for r in lsn.ROWS
   if r["service_type"] == "general" and q(r["answer"])], []),
 ("insurance and the bond are answerable from any service",
  any(r["service_type"] == "general" and "security bond" in r["answer"]
      for r in lsn.ROWS), True),
 ("and still state no insurance minimum",
  any(f"{D}15,000" in r["answer"] or f"{D}60,000" in r["answer"]
      for r in lsn.ROWS if r["service_type"] == "general"), False),
 # --- home leave, 2026-09-08 ------------------------------------------
 # The nationality decides the documents, the lead time AND the price - PH
 # needs her ORIGINAL passport plus a ticket itinerary, 4 weeks, $400; ID
 # needs copies, 2 weeks, $250. Quote the wrong route and the client has
 # budgeted the wrong amount against the wrong deadline. The flow asked
 # only her name and the travel dates, so there was nothing to route on.
 ("home leave asks which country she is from",
  [f.key for f in t.SERVICE_FIELDS["home_leave"]],
  ["helper_name", "nationality", "leave_dates"]),
 ("the nationality carries over from another enquiry",
  "nationality" in ico._PORTABLE_ACROSS_SERVICES, True),
 ("home leave is route-split by nationality",
  "home_leave" in ico._ROUTE_BY_NATIONALITY, True),
 # Unlike passport renewal, the timing and the money are route-split too, so
 # this pattern has to catch them and the passport one must NOT (a passport
 # renewal is $450 either way, and suppressing that answer helps nobody).
 ("a home-leave cost question fires the caveat",
  bool(ico._HOME_LEAVE_ROUTE_DEPENDENT.search("how much does home leave cost")), True),
 ("so does a timing question",
  bool(ico._HOME_LEAVE_ROUTE_DEPENDENT.search("how long does it take")), True),
 ("an ordinary answer does not",
  bool(ico._HOME_LEAVE_ROUTE_DEPENDENT.search("she is going in December")), False),
 ("the passport caveat still ignores cost",
  bool(ico._NATIONALITY_DEPENDENT.search("how much does it cost")), False),
 ("no caveat once we know the country",
  ico._known_nationality({"collected_info": {"nationality": "Filipino"}}), "PH"),
 # A field whose written question spells its options out is asking for all of
 # them; the generic "drop two or three in" rule was overriding that.
 ("enumerated options are named in full",
  "name them" in ico._field_guidance("new_hiring", {}, lang_f), True),
]
bad = 0
for label, got, want in rows:
    ok = got == want
    bad += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {label:40} {got!r}")
print("\nALL PASS" if not bad else f"\n{bad} FAILED")

raise SystemExit(1 if bad else 0)
