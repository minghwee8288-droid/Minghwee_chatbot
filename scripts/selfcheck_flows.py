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
 # renewal is a small-ticket service and may quote costs, but there is still no
 # agency fee for it anywhere in the KB - so no row may invent one.
 ("no renewal row names an agency fee",
  any("agency fee" in r["answer"].lower()
      for r in lsn.ROWS if r["service_type"] == "renewal"), False),
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
