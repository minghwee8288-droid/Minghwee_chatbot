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
