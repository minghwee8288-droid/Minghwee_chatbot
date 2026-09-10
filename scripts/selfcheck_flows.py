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
import re

import app.services.ticket as t
import app.services.lead as _lead
from app.graph.guards import quotes_hiring_package_cost as q
ic = importlib.import_module("app.graph.nodes.intent_classifier")
ico = importlib.import_module("app.graph.nodes.info_collector")
S = ico._SMALL_TICKET_SERVICES
P = ico._COLLECTION_PURPOSE
from app.graph.prompts.system import RULES
D = chr(36)

# The agency's seven services, in their words. Used by the bracket check below,
# which is written as a SET on purpose: the row it replaced was written about
# the two fields they happened to name and was silent on every other flow.
SEVEN_SERVICES = ("new_hiring", "direct_hiring", "transfer_employer", "renewal",
                  "passport_renewal", "home_leave", "replacement")

# Every option set on those seven was read on 2026-09-10. These three carry
# digits DELIBERATELY and are named here so a new one cannot arrive unnoticed:
#   budget          - the bands are salary bands, and they are also the
#                     grounding `ungrounded_figures` reads (2026-09-09 D), so
#                     removing them silently reintroduces that defect
#   home_type       - "HDB 4-5 room" is what the flat is called, not a bracket
#   start_timeline  - a timeframe is not a count
_DIGITS_ON_PURPOSE = {"budget", "home_type", "start_timeline"}
_BRACKET = re.compile(r"\d+\s*(?:-|to|\u2013)\s*\d+|\bat least \d+|"
                      r"\b\d+\s*(?:and\s+)?(?:above|or more)")


def _reads_a_bracket(field) -> bool:
    """Would this field put a numeric range in front of the client?

    `_field_guidance` drops a field's own options into the question as
    examples, so an option list is read out whether or not the written
    question mentions it. Both halves are checked.
    """
    text = " | ".join(field.options or ()) + " " + (field.question or "")
    return bool(_BRACKET.search(text))
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
wp = importlib.import_module("app.whapi.parser")
wc = importlib.import_module("app.whapi.client")
import re as _re
import pathlib as _pathlib
import app.services.contact as _contact
from app.graph.prompts.system import _known_cases_block as _cases_block

_APP_DIR = _pathlib.Path(__file__).resolve().parents[1] / "app"
_APP_SRC = {f: f.read_text(encoding="utf-8") for f in _APP_DIR.rglob("*.py")}
_CONTACT_SRC = (_APP_DIR / "services" / "contact.py").read_text(encoding="utf-8")
_COLLECTOR_SRC = (_APP_DIR / "graph" / "nodes" / "info_collector.py").read_text(encoding="utf-8")
_RAG_SRC = (_APP_DIR / "graph" / "nodes" / "rag_retriever.py").read_text(encoding="utf-8")

# The eleven case tables, named rather than pattern-matched: a `case_[a-z_]+`
# pattern also catches "case_enquiry", "case_id" and "case_summary", which are
# an intent, a field key and a state key, and none of them is a table.
_CASE_TABLES = (
    "cases", "case_stages", "case_tasks", "case_task_details",
    "case_task_comments", "case_task_documents", "case_requirements",
    "case_requirement_links", "case_candidate_suggestions",
    "case_salary_schedules", "case_salary_schedule_signing_links",
)
_TABLE_RE = "|".join(_CASE_TABLES)

# Any write against one of them, in either call style the db helper supports.
# The portal owns these rows; the bot reads them and must never touch them -
# see the read-only note above contact.get_cases.
_CASE_WRITE = _re.compile(
    rf'db\.table\(\s*"(?:{_TABLE_RE})"\s*\)\s*\.\s*(?:insert|update|delete|upsert)'
    rf'|db\.(?:insert|update|delete|upsert)\(\s*"(?:{_TABLE_RE})"'
)
_case_writes = sorted(
    {m.group(0) for src in _APP_SRC.values() for m in _CASE_WRITE.finditer(src)}
)

_CASE_A = {"case_id": "1", "case_number": "CS-2026-0007", "case_type": "First-time hire",
           "status": "active", "stage": "documents", "country": "PH",
           "opened_at": "2026-09-01", "helper_name": "Liza Fernandez"}
_CASE_B = {"case_id": "2", "case_number": "CS-2026-0002", "case_type": "Home leave",
           "status": "completed", "stage": "closing", "country": "ID",
           "opened_at": "2026-06-14", "helper_name": ""}
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
  "leave the topic where it is" in tpl.PROCESS_ADDENDUM, True),
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
 # INVERTED 2026-09-10, and the old assertion is left named here because it
 # was right for its day: the brackets were added on 2026-09-08 so that a
 # household of seven was not shown "5-6" as the largest choice. The agency has
 # now asked for the brackets themselves to go - "do not ask for no. like 1-2,
 # 3-4, 5-6 which is looking wierd so only how many family members are there" -
 # which solves the same problem the other way round: a question with no
 # options takes any number, including seven.
 ("no bracket is read out as a question",
  [f.key for f in t.SERVICE_FIELDS["new_hiring"]
   if f.key in ("household", "helper_profile")
   and any(ch.isdigit() for ch in f.question)], []),
 ("...and there are no options left to leak into it either",
  [f.key for f in t.SERVICE_FIELDS["new_hiring"]
   if f.key in ("household", "helper_profile") and f.options], []),
 ("the same in the transfer flow, which reuses the field",
  [f.key for f in t.SERVICE_FIELDS["transfer_employer"]
   if f.key in ("household", "helper_profile")
   and (f.options or any(ch.isdigit() for ch in f.question))], []),
 ("but a field whose options ARE the answer keeps them",
  bool(next(f for f in t.SERVICE_FIELDS["new_hiring"]
            if f.key == "languages").options), True),
 # ...and the same rule over the seven services as a SET, so a NEW field with
 # bracket options cannot land on a flow nobody thought to re-check. Three keys
 # are allowed through by name and the comment above says why each one earns it.
 ("no field on any of the seven reads a bracket out",
  sorted({f.key for svc in SEVEN_SERVICES for f in t.SERVICE_FIELDS.get(svc, [])
          if f.key not in _DIGITS_ON_PURPOSE and _reads_a_bracket(f)}), []),
 ("and the three that carry digits still do so deliberately",
  sorted({f.key for svc in SEVEN_SERVICES for f in t.SERVICE_FIELDS.get(svc, [])
          if f.key in _DIGITS_ON_PURPOSE}),
  ["budget", "home_type", "start_timeline"]),
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
 # It asked "Would you like to go ahead?" AND told them it had already been
 # passed to the team AND offered further help - three endings, two of which
 # contradict each other. The agency, 2026-09-09: "If it is asking, would you
 # like to go ahead, then why is it telling, I have passed everything to our
 # team?" The ticket is raised on this same turn, so the handover line is the
 # true half and the question is the one that goes.
 ("it no longer asks for a decision it has already acted on",
  "Do NOT ask whether they would like to go ahead" in tpl.SERVICE_BRIEFING_NOTE,
  True),
 ("there is exactly one ending",
  all(w in tpl.SERVICE_BRIEFING_NOTE
      for w in ("CLOSE IT ONCE", "One ending, not")), True),
 # "1. Copy of your NRIC" arrived with no sentence in front of it, and the
 # agency asked how the user is meant to know that is the document list.
 ("every list is introduced by a sentence",
  all(w in tpl.SERVICE_BRIEFING_NOTE
      for w in ("Say what the list IS before you write", "Every list gets a "
                "sentence naming what it is")), True),
 # The process is back, but only the client's half of it.
 ("the documents are followed by what happens next",
  tpl.SERVICE_BRIEFING_NOTE.index("WHAT DOCUMENTS")
  < tpl.SERVICE_BRIEFING_NOTE.index("WHAT HAPPENS NEXT"), True),
 ("and those steps are the client's, not our processing",
  "THE STEPS ARE THEIRS, NOT OURS" in tpl.SERVICE_BRIEFING_NOTE, True),
 ("the briefing turn asks the records what happens next",
  "what happens next" in rr.BRIEFING_QUERY, True),
 # ...and the KB has to hold an answer, or the instruction is an invitation to
 # improvise a process, which is the worst thing this bot can do.
 ("the records carry the client-side steps",
  any(r["question"] == "What happens next once I confirm my helper's passport "
      "renewal?" for r in lsn.ROWS), True),
 ("and they name no runner, no appointment and no embassy",
  [w for w in ("runner", "appointment", "embassy")
   for r in lsn.ROWS
   if r["question"].startswith("What happens next once I confirm")
   and w in r["answer"].lower()], []),
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
 # --- the same, for the other two flows that never asked, 2026-09-09 ---
 # Agency, testing a work permit renewal: "the chatbot is asking directly
 # name of helper, not saying that before, may I know your name". It was not
 # removed - `renewal` never had the field, and neither did `home_leave`,
 # which makes CLAUDE.md's "every employer flow asks the client's name"
 # false in two places.
 ("the name is asked before the helper's on every renewal",
  [t.SERVICE_FIELDS[s][0].key for s in
   ("renewal", "passport_renewal", "home_leave")],
  ["full_name", "full_name", "full_name"]),
 ("a work permit renewal is three questions now",
  [f.key for f in t.SERVICE_FIELDS["renewal"]],
  ["full_name", "helper_name", "permit_expiry"]),
 # ...and adding the field alone would have changed NOTHING: _with_push_name
 # fills full_name from the WhatsApp profile, so the question is skipped
 # before it is ever asked. That is the exact 2026-09-08 defect on passport
 # renewal, reported the same way both times.
 ("neither takes the name off WhatsApp either",
  [s for s in ("renewal", "home_leave") if s in t.NAME_FROM_RECORD_ONLY],
  ["renewal", "home_leave"]),
 ("so a new number is asked on a work permit renewal",
  ico._known_fields({"customer_name": "Vaidik"}, "renewal").get("full_name"),
  None),
 ("and a client on our file is greeted on a home leave",
  ico._known_fields({"customer_name": "Vaidik", "record_name": "Vaidik Dubey"},
                    "home_leave").get("full_name"), "Vaidik Dubey"),
 # Every employer flow, checked as a set rather than one at a time, so a new
 # one cannot be added without the field. lead.py's copy deliberately, not
 # ticket_creator.py's - the two disagree and the ticket_creator copy is dead
 # (CLAUDE.md 9.8).
 #
 # fee_enquiry and salary_enquiry are excluded on purpose. They are a money
 # QUESTION, not an intake - two fields, and route_after_rag only lets them
 # collect at all when nothing else is in hand. Asking a name there turns a
 # price question into a form, which is the 2026-09-07 defect the agency hit
 # ("But I come here for passport renewal not for care").
 ("no employer intake flow opens without asking who we are talking to",
  [s for s in sorted(_lead.EMPLOYER_LEAD_SERVICES)
   if s not in {"fee_enquiry", "salary_enquiry"}
   and t.fields_for(s)
   and not any(f.key == "full_name" for f in t.fields_for(s))],
  []),
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

 # --- the transfer document checklist, 2026-09-10 ----------------------
 # Transfer was the only service with no document rows at all, so "what
 # documents do I need" inside a transfer had nothing to retrieve.
 ("a transfer asks and answers both halves of the checklist",
  {"What documents do I need to provide for a transfer?",
   "What forms does Ming Hwee prepare for a transfer?",
   "What do I need to provide if I am releasing my helper to a new employer?"}
  <= {r["question"] for r in lsn.ROWS}, True),
 # THE decision in this change. An EMPLOYER asking about a transfer runs
 # under `transfer_employer`, which is not a service_type any row uses, so
 # _labelled_filter narrows to 'general' and every `transfer` row is
 # invisible to them (section 9.15). This checklist is written from the
 # employer's side, so filing it under `transfer` would put it in the one
 # bucket the person it is for cannot see - the change would look done and
 # do nothing. Measured 2026-09-10 before the load: "what documents do i
 # need for the transfer" under transfer_employer scored 0.359, below the
 # floor, and "what documents does ming hwee prepare" scored 0.570 - ABOVE
 # the floor, topped by the PDPA privacy notice, which is worse than a
 # holding line. After: 0.679 and 0.772 on the right rows.
 ("the transfer checklist is filed where an employer can see it",
  {r["service_type"] for r in lsn.ROWS
   if r["section_heading"].startswith("Transfer - documents")
   or r["section_heading"].startswith("Transfer - forms")
   or r["section_heading"].startswith("Transfer - releasing")}, {"general"}),
 # The cost of the 'general' bucket: these rows compete inside every OTHER
 # service too. Worded "What DOCUMENTS does Ming Hwee prepare for a
 # transfer?" this row was top for new_hiring's own question at 0.754
 # against 0.726 - a general row displacing the service-specific row beside
 # it. "forms" separates them (0.708 vs 0.717) and loses nothing on the
 # transfer side. It is also what replacement and passport_renewal already
 # call their own version of this row.
 ("the forms row does not collide with new_hiring's documents row",
  [r["question"] for r in lsn.ROWS
   if r["section_heading"] == "Transfer - forms we prepare"],
  ["What forms does Ming Hwee prepare for a transfer?"]),
 # The four things the agency asks the client for. A checklist missing one
 # of them sends somebody to an appointment without it.
 ("the employer's four items are all asked for",
  [term for term in ("Work Permit number", "expiry", "release", "NRIC",
                     "Income Tax Assessment", "Declaration of Monthly Income",
                     "Employment Pass", "tenancy agreement")
   if term not in "".join(r["answer"] for r in lsn.ROWS
                          if r["section_heading"].startswith("Transfer - "))], []),
 # And the seven we prepare.
 ("the seven forms we prepare are all named",
  [term for term in ("transfer agreement", "Authorisation Form",
                     "Employer Particulars form", "Job Offer Form",
                     "Employment Contract", "Safety Agreement", "Rest-Day form")
   if term not in "".join(r["answer"] for r in lsn.ROWS
                          if r["section_heading"] == "Transfer - forms we prepare")], []),
 # transfer_employer serves BOTH directions, and retrieval cannot know which
 # one the client is. A releasing employer told to produce the NEW employer's
 # income proof has been asked for a document that is not theirs to give, so
 # every row that lists documents says whose they are.
 ("the documents row says which side of the transfer the client is on",
  all(w in r["answer"] for r in lsn.ROWS
      if r["question"] == "What documents do I need to provide for a transfer?"
      for w in ("taking the helper on", "releasing her")), True),
 # ...and both sides are EMPLOYERS. resolve_service leaves service_type
 # 'transfer' only for a CANDIDATE - an employer always becomes
 # `transfer_employer` - so with contact_type 'all' this checklist was
 # retrieved for a HELPER asking what she needs. Live, 2026-09-10, that
 # produced "Your NRIC or IC and proof of income" addressed to the helper,
 # in the same reply as "the new employer provides their own
 # identification". contact_type narrows a search to this audience plus
 # 'all', so 'employer' is what makes it invisible to her.
 ("the transfer checklist is addressed to employers only",
  {r.get("contact_type", "all") for r in lsn.ROWS
   if r["section_heading"].startswith("Transfer - ")
   and r["service_type"] == "general"}, {"employer"}),
 # And the default is still 'all', so no other row changed audience.
 ("every other row is still written for anyone",
  {r.get("contact_type", "all") for r in lsn.ROWS
   if not (r["section_heading"].startswith("Transfer - ")
           and r["service_type"] == "general")}, {"all"}),

 # --- the transfer retrieval alias, 2026-09-10 -------------------------
 # transfer_employer is not a service_type any KB row uses, so
 # _labelled_filter narrowed every employer transfer search to 'general'
 # (section 9.15). Measured before the alias, as an employer saw it:
 # timing 0.472, steps 0.650, cost 0.453 - ALL above the 0.40 floor, so
 # _answerable() read True and the widening retry never fired. The client
 # was told "around 2 to 3 weeks" from a marketing FAQ against the
 # agency's corrected 1 to 2 weeks.
 ("an employer transfer searches the transfer bucket",
  rr._service_filter({"service_type": "transfer_employer",
                      "incoming_text": "how long does a transfer take",
                      "history_text": ""}), "transfer"),
 ("and the helper's own transfer is unchanged",
  rr._service_filter({"service_type": "transfer",
                      "incoming_text": "how long does a transfer take",
                      "history_text": ""}), "transfer"),
 ("no other service is aliased",
  set(rr._RETRIEVAL_ALIASES), {"transfer_employer"}),
 # The alias is RETRIEVAL ONLY. The blocked-topic key is the service key,
 # and mapping an employer's transfer onto another service made a new
 # request compute a parked topic's key - "a live agent will connect with
 # you shortly", indefinitely, collecting nothing (live 2026-09-02). The
 # key must stay distinct everywhere except the KB lookup.
 ("the alias never reaches the topic key",
  (t.topic_key_for("transfer_employer", "employer", "transfer"),
   t.topic_key_for("transfer", "candidate", "transfer")),
  ("transfer_employer", "transfer")),
 ("an employer still resolves to its own service",
  t.resolve_service("transfer", "employer"), "transfer_employer"),
 ("and still has its own field list",
  "transfer_employer" in t.SERVICE_FIELDS, True),
 ("which is not transfer's",
  [f.key for f in t.SERVICE_FIELDS["transfer_employer"]]
  == [f.key for f in t.SERVICE_FIELDS["transfer"]], False),
 # --- one transfer timeline, not four, 2026-09-10 ----------------------
 # The agency's 2026-09-08 correction went through UPDATES, which keys on
 # question + service_type, so it corrected the one row it named and left
 # ten others carrying a different figure. Once the alias landed, the
 # corrected row and a 2-4 weeks row arrived in the SAME set (0.587 and
 # 0.558) and the model could quote either.
 ("every corrected transfer row states the agency's figure",
  [u["where"]["question"] for u in lsn.UPDATES
   if "transfer maid" in u["where"]["question"]
   or u["where"]["question"].startswith("How do I release")
   if "1 to 2 weeks" not in u["set"].get("answer", "")], []),
 ("and none of them still states a competing one",
  [u["where"]["question"] for u in lsn.UPDATES
   for bad in ("2-4 weeks", "2-3 weeks", "3-4 weeks", "6-8 weeks")
   if bad in u["set"].get("answer", "")], []),
 # The two spans are not the same clock - 1-2 weeks is measured from the
 # INTERVIEW and 4-6 from SIGNING - so a row naming both names both.
 ("a row that compares the two names both spans",
  all(w in u["set"]["answer"] for u in lsn.UPDATES
      if "transfer maid" in u["where"]["question"]
      for w in ("from the interview", "from signing")), True),

 # --- a LID is not a phone number, 2026-09-10 --------------------------
 # Live: every message from an allowlisted tester logged "Bot standing down
 # on +116909177569373: number not in BOT_ALLOWED_NUMBERS" while they were
 # messaging from +917970027379. 116909177569373 is a Meta LID, and
 # normalize_phone splits on "@" and keeps the front, so "...@lid" became a
 # phone number nobody has ever heard of. Allowlisting it would have been
 # worse than the silence: every lookup is keyed on the number, so it would
 # open a SECOND conversation on a non-number - the split-conversation bug
 # scripts/fix_split_conversations.py exists to repair.
 ("a phone JID is a phone number",
  [j for j in ("917970027379@s.whatsapp.net", "6580119456@c.us", "917970027379")
   if not wp._is_phone_jid(j)], []),
 ("a LID is not", wp._is_phone_jid("116909177569373@lid"), False),
 ("nor is an empty identifier", wp._is_phone_jid(""), False),
 # The whole point: when the payload carries the phone anywhere, use it.
 ("an inbound LID falls back to the chat's phone",
  wp.parse_message({"id": "x", "type": "text", "from_me": False,
                    "text": {"body": "hi"}, "from": "116909177569373@lid",
                    "chat_id": "917970027379@s.whatsapp.net"}).customer_number,
  "+917970027379"),
 ("an outbound LID chat falls back to `to`",
  wp.parse_message({"id": "x", "type": "text", "from_me": True,
                    "text": {"body": "hi"}, "from": "6580119456@s.whatsapp.net",
                    "chat_id": "116909177569373@lid",
                    "to": "917970027379@s.whatsapp.net"}).customer_number,
  "+917970027379"),
 # And the ordinary shapes are untouched - this may only ever improve
 # resolution, never change a payload that already worked.
 ("an ordinary inbound message is unchanged",
  wp.parse_message({"id": "x", "type": "text", "from_me": False,
                    "text": {"body": "hi"}, "from": "917970027379@s.whatsapp.net",
                    "chat_id": "917970027379@s.whatsapp.net"}).customer_number,
  "+917970027379"),
 ("an ordinary outbound message still names the CLIENT, not us",
  wp.parse_message({"id": "x", "type": "text", "from_me": True,
                    "text": {"body": "hi"}, "from": "6580119456@s.whatsapp.net",
                    "chat_id": "917970027379@s.whatsapp.net"}).customer_number,
  "+917970027379"),
 # When there is genuinely nothing else, behaviour is unchanged (stand down)
 # rather than dropped - a dropped message logs nothing at all, and the
 # warning is what makes the next occurrence diagnosable.
 ("a payload with only a LID still resolves to something",
  wp.parse_message({"id": "x", "type": "text", "from_me": False,
                    "text": {"body": "hi"}, "from": "116909177569373@lid",
                    "chat_id": "116909177569373@lid"}).customer_number,
  "+116909177569373"),
 # ...and it is FLAGGED, which is what lets the webhook resolve it through
 # Whapi before anything keyed on the phone number runs. Measured against the
 # live channel 2026-09-10: GET /chats/<lid> carries {"phone":"917970027379"}
 # while GET /contacts/<lid> does not, which is why resolve_lid reads /chats.
 ("a LID-only message is flagged for resolution",
  wp.parse_message({"id": "x", "type": "text", "from_me": False,
                    "text": {"body": "hi"}, "from": "116909177569373@lid",
                    "chat_id": "116909177569373@lid"}).lid,
  "116909177569373@lid"),
 ("an ordinary message is not",
  wp.parse_message({"id": "x", "type": "text", "from_me": False,
                    "text": {"body": "hi"}, "from": "917970027379@s.whatsapp.net",
                    "chat_id": "917970027379@s.whatsapp.net"}).lid, None),
 ("nor is one where only the chat carries the phone",
  wp.parse_message({"id": "x", "type": "text", "from_me": False,
                    "text": {"body": "hi"}, "from": "116909177569373@lid",
                    "chat_id": "917970027379@s.whatsapp.net"}).lid, None),
 # The LID cache is bounded, unlike the four caches section 9.13 lists.
 ("the LID cache cannot grow without limit",
  isinstance(getattr(wc, "_LID_CACHE_MAX", None), int)
  and wc._LID_CACHE_MAX > 0, True),

 # --- the replacement round, 2026-09-10 --------------------------------
 # A numbered list has to arrive as a LIST. Live on a parked replacement:
 # the process answer came out correctly formatted and the documents answer
 # one message later arrived as one solid paragraph with "1. ... 2. ..."
 # buried in it. SERVICE_BRIEFING_NOTE had carried the rule since
 # 2026-09-08; neither answering path had it - the same "written for the one
 # flow that was reported" shape section 9 has forced twice already.
 # Tested on "real line break", NOT on "own line": PROCESS_INSTRUCTION already
 # said the LEAD-IN sentence goes "on its own line", so an "OWN LINE" test
 # passed whether or not the per-ITEM rule was there - it was green with the
 # rule deleted. Caught by injecting exactly that.
 ("both answering paths require one item per line",
  [n for n in ("PROCESS_INSTRUCTION", "PROCESS_ADDENDUM")
   if "REAL LINE BREAK" not in getattr(tpl, n).upper()], []),
 ("and so does the briefing that had it first",
  "REAL LINE BREAK" in tpl.SERVICE_BRIEFING_NOTE.upper(), True),
 # Ticket CB-2026-0006 reached an agent reading "Wants in the replacement:
 # replace her" - the client's own words for the REQUEST, filed as their
 # description of the helper they want. The field looked answered, so it was
 # never asked. Same shape as the 2026-09-07 care-type defect.
 ("the preference field is guarded against restating the request",
  "replacement_preferences" in ico._PREFERENCE_FIELDS, True),
 ("a request restated is not a preference",
  [t for t in ("replace her", "just replace her", "a new helper", "change her",
               "someone else", "a different one", "new maid")
   if ico._states_a_preference(t)], []),
 ("but a real preference survives",
  [t for t in ("a Filipino helper who can cook", "must speak Mandarin",
               "someone experienced with children", "older, patient, no pets",
               "a different nationality this time")
   if not ico._states_a_preference(t)], []),
 # _NO_PREFERENCE was anchored hard at ^, so "whatever you want" matched and
 # "you do whatever you want" did not - and the second is how people say it.
 ("deferring the choice back to us is not a preference",
  [t for t in ("you do whatever you want", "whatever you want", "up to you",
               "you decide", "anything")
   if not ico._NO_PREFERENCE.match(t)], []),
 ("but naming something still is",
  [t for t in ("i want any Filipino helper", "a Filipino who cooks")
   if ico._NO_PREFERENCE.match(t)], []),

 # The agency, twice in three days and in almost the same words: ask for the
 # name when our records do not have it, greet them when they do. Live on
 # 2026-09-10 the replacement flow opened "Hi Vaidik Dubey ... may I know
 # your current helper's name?" on conversation 3766, whose
 # matched_employer_id is NULL - so that was the WhatsApp push name being
 # used as the client's own, which is the 2026-09-08 passport-renewal
 # complaint exactly.
 ("a replacement asks the client's name rather than reading it off WhatsApp",
  "replacement" in t.NAME_FROM_RECORD_ONLY, True),
 ("and every flow in that set asks the name FIRST",
  [svc for svc in t.NAME_FROM_RECORD_ONLY
   if [f.key for f in t.SERVICE_FIELDS[svc]][:1] != ["full_name"]], []),
 # Closed on 2026-09-10, and asserted as a RULE rather than as a list, so a
 # new flow cannot reopen it: asking for a helper's name while reading the
 # client's own off WhatsApp is the shape that drew the same complaint four
 # times - passport renewal, renewal/home leave, replacement, then these
 # three. Derived, so it cannot go stale the way the hard-coded list above
 # deliberately can.
 ("no flow asks for a helper's name while assuming the client's",
  sorted(svc for svc in _lead.EMPLOYER_LEAD_SERVICES
         if any(f.key == "helper_name" for f in t.SERVICE_FIELDS.get(svc, []))
         and svc not in t.NAME_FROM_RECORD_ONLY), []),
 # new_hiring is the one employer flow still reading it off WhatsApp, and that
 # is deliberate: no existing helper to ask about, so it never produces the
 # shape above, and the push name there is the 2026-09-01 fix.
 ("new_hiring is deliberately not in the set",
  "new_hiring" in t.NAME_FROM_RECORD_ONLY, False),

 # --- the parked-topic net, 2026-09-10 ---------------------------------
 # Live on a parked home leave: "Ok but tell what is the further process"
 # got the holding line while "And what are the documents needed" one
 # message later was answered in full. Two misses at once - the classifier
 # returned intent=home_leave (a SERVICE, not a question type, so
 # _answerable's KB_QUESTION_INTENTS test failed) and the deterministic net
 # wanted "what is (the) process" with nothing in between. An adjective
 # defeated it, exactly as a missing "the" did on 2026-09-08.
 ("an adjective before the noun does not hide the question",
  [m for m in ("Ok but tell what is the further process",
               "what is the further process", "what is the next process",
               "what is the whole process", "what is the entire process")
   if not btr.asks_general_info(m)], []),
 ("nor does asking for it as an instruction",
  [m for m in ("tell me the process", "just tell the steps",
               "what are the next steps")
   if not btr.asks_general_info(m)], []),
 ("and the 2026-09-08 cost phrasings still pass",
  [m for m in ("Ok what is cost", "what is the cost", "what documents do I need",
               "how long does it take")
   if not btr.asks_general_info(m)], []),
 # The widening must not swallow a chase - that is what parking a topic is
 # FOR, and answering it with records instead of a human is the opposite
 # failure.
 ("a chase is still a chase",
  [m for m in ("any update on my case", "what is the status", "how far is it",
               "still waiting", "any news", "is it done")
   if btr.asks_general_info(m)], []),
 # ...and this is the one that actually exercises the _CHASING_STATUS guard.
 # The six above never reach _GENERAL_INFO at all, so they stay False whether
 # the guard is there or not - a check that passes for a reason unrelated to
 # what it is checking, which is the trap caught earlier the same day. A
 # COMPOUND hits both patterns, so removing the guard flips it.
 ("a chase carrying a question with it is still held",
  btr.asks_general_info("any update? and what is the cost"), False),
 # The home-leave process was NOT missing when the agency reported it - the
 # row already carried all six of the steps they sent. Asserted so nobody
 # "fixes" this by loading a second copy (section 9.8).
 ("the home leave process row covers all six steps",
  [w for w in ("nationality", "embassy appointments", "documents",
               "endorsement forms", "levy waiver", "six-monthly medical",
               "flights")
   if w not in "".join(r["answer"] for r in lsn.ROWS
                       if r["service_type"] == "home_leave")], []),
 # --- home leave, 2026-09-08 ------------------------------------------
 # The nationality decides the documents, the lead time AND the price - PH
 # needs her ORIGINAL passport plus a ticket itinerary, 4 weeks, $400; ID
 # needs copies, 2 weeks, $250. Quote the wrong route and the client has
 # budgeted the wrong amount against the wrong deadline. The flow asked
 # only her name and the travel dates, so there was nothing to route on.
 ("home leave asks which country she is from",
  [f.key for f in t.SERVICE_FIELDS["home_leave"]],
  ["full_name", "helper_name", "nationality", "leave_dates"]),
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

 # --- the small-ticket overview, 2026-09-09 ---------------------------
 # It had been DEAD since 2026-09-04 and nothing caught it, because a
 # briefing that never happens looks exactly like one working quietly: the
 # client gets a perfectly reasonable question either way. The old
 # condition was
 #     brief_on_turn = 1 if _is_first_contact(state) else 0
 #     ... and sum(asked.values()) == brief_on_turn
 # and _is_first_contact is only true while nothing has been asked, so the
 # two sides could never be equal. Asserted across a whole turn sequence
 # rather than as one call, which is the only shape that would have caught
 # it.
 ("the overview turn can actually happen, exactly once",
  [n for n in range(6) if ico.briefs_on_this_turn("renewal", {"f": n})], [1]),
 ("it never lands on the introduction turn",
  ico.briefs_on_this_turn("renewal", {}), False),
 ("a service that briefs at the END does not brief at the start too",
  ico.briefs_on_this_turn("passport_renewal", {"f": 1}), False),
 ("...and that holds for every service with a closing briefing",
  [k for k in t.BRIEFING_AFTER if ico.briefs_on_this_turn(k, {"f": 1})], []),
 ("an ordinary flow never gives an overview",
  ico.briefs_on_this_turn("new_hiring", {"f": 1}), False),
 # The old expression still appears verbatim - inside the docstring that
 # explains why it could never be true, which is an incident note and
 # stays (section 0.4). So assert the call site uses the PREDICATE
 # instead; that plus the turn-sequence check above is what makes the
 # dead condition unable to come back.
 ("the collector decides via the predicate, not an inline condition",
  "if briefs_on_this_turn(service_type, asked):" in _COLLECTOR_SRC, True),
 # That turn's incoming message is the answer to the first question - a
 # NAME, usually - so the query built from it retrieved NOTHING: renewal
 # measured 0.000 on that turn. The records have to be searched for by
 # SERVICE, the same way the closing briefing already does it.
 ("the overview turn searches for the service, not the client's answer",
  "OVERVIEW_QUERY" in _RAG_SRC and "briefs_on_this_turn" in _RAG_SRC, True),
 ("and its query avoids the word that pulls up our own processing",
  "process" in rr.OVERVIEW_QUERY.lower(), False),
 # A general instruction beats a specific one unless the specific one names
 # the rule it is overriding - the 2026-09-04 introduction defect, and the
 # reason this note was silent even once it fired and had records.
 ("the overview note names the rule it overrides",
  "THIS MESSAGE IS THE ONE" in _COLLECTOR_SRC, True),

 # --- a name we already hold is USED, 2026-09-09 ----------------------
 # Live, conversation 3766: an employer whose name is on file was opened
 # with "Hi, I'm Claire ... May I know your helper's name?" - no name at
 # all. The skip was correct (the agency's own rule is ask when it is not
 # in the database, greet when it is); the greeting half never happened,
 # and from the client's side those two are the same bot.
 ("a name on file fills the field, so it is never asked for",
  ico._known_fields({"record_name": "tunaktun"},
                    "passport_renewal").get("full_name"), "tunaktun"),
 ("a number with nothing on file still gets the question",
  "full_name" in ico._known_fields({"record_name": ""}, "passport_renewal"), False),
 # `replacement` joined 2026-09-10 - same complaint, same words, a third time.
 # Spelled out rather than derived, on purpose: this is the tripwire, and it
 # has already caught one field-set change it was meant to (2026-09-10).
 ("and the WhatsApp push name is not evidence on these flows",
  sorted(t.NAME_FROM_RECORD_ONLY),
  ["direct_hiring", "home_leave", "insurance", "passport_renewal",
   "renewal", "replacement", "transfer_employer"]),
 ("a name we hold is greeted with, not just filed",
  "CARRIES the name" in ico.RECORD_NAME_NOTE, True),
 ("and it is still never re-asked",
  "ask for a name we are already holding" in ico.RECORD_NAME_NOTE, True),
 ("and the bare name with a comma is not a greeting",
  "is a form calling out a row" in ico.RECORD_NAME_NOTE, True),
 ("nor tidied up on the client's behalf",
  "never change the spelling" in ico.RECORD_NAME_NOTE.lower(), True),
 # 2026-09-10. It used to be gated behind returning_note and recognised_note,
 # which silenced it for every RETURNING client - most of them - so an existing
 # client got "Welcome back" with the name on their file never used, and asked
 # why they had not been asked for it. It is not a competing opener: those two
 # decide what the message opens WITH, this decides that it carries the name.
 ("the greeting is not gated behind the other two notes",
  "not returning_note" in _COLLECTOR_SRC and "not recognised_note" in _COLLECTOR_SRC,
  False),
 # And the other end of the same defect: a NEW client who had just typed their
 # name got the next question with no greeting at all.
 ("it fires on the turn the name becomes known, however we learned it",
  "already_greeted" in _COLLECTOR_SRC and "not already_greeted" in _COLLECTOR_SRC, True),

 # --- a numbered list always says what it is, 2026-09-10 --------------
 # "the bot is directly listing the documents and process like 1 2 3 so on so
 # it should firstly write the heading in same message." Already true of the
 # passport briefing since 2026-09-09; this is the same rule on the two
 # answering paths, so it holds for every service.
 ("a process answer says what the list is before writing it",
  "SAY WHAT THE LIST IS BEFORE YOU WRITE IT" in tpl.PROCESS_INSTRUCTION, True),
 ("so does one given while a topic is parked",
  "saying what the list is" in tpl.PROCESS_ADDENDUM, True),
 ("and it closes on a sentence, not on step 8",
  "CLOSE IT PROPERLY" in tpl.PROCESS_INSTRUCTION, True),
 ("the parked path closes properly too, without reopening the topic",
  ("End on a SENTENCE" in tpl.PROCESS_ADDENDUM
   and "does NOT reopen the topic" in tpl.PROCESS_ADDENDUM), True),

 # --- Case ID resolution, 2026-09-09 ----------------------------------
 # The agency's hard constraint was that this layer is purely additive and
 # read-only. Both halves are asserted rather than intended.
 ("case tables are never written by the bot", _case_writes, []),
 ("nor is one so much as named outside contact.py",
  sorted(f.name for f, src in _APP_SRC.items()
         if _re.search(rf'"(?:{_TABLE_RE})"', src)), ["contact.py"]),
 # Three columns point at a case and which one is set depends on how the
 # office actioned the enquiry. Reading only the structural one means a case
 # created by either conversion path is invisible until a placement exists.
 ("a case is resolved down all three paths",
  [k for k in ("converted_employer_id", "employer_service_requests",
               'in_("placement_id"') if k not in _CONTACT_SRC], []),
 # The old filter was `status = 'active'`. Probed against the live CHECK on
 # 2026-09-09 the column takes active|completed|cancelled|on_hold, so that
 # filter hid three quarters of the vocabulary - including on_hold, whose
 # client is the likeliest of all of them to be chasing us.
 ("a case is no longer excluded by its status",
  'eq("status", "active")' in _CONTACT_SRC, False),
 ("and a case on hold is not treated as finished",
  "on_hold" in _contact._CLOSED_LOOKING, False),
 ("but a completed one sorts below a live one",
  _contact._case_sort_key({"status": "completed"})[0]
  > _contact._case_sort_key({"status": "active"})[0], True),
 ("the number of cases put in front of the model is bounded",
  _contact.MAX_CASES <= 5, True),
 # No user-facing change: the case is context the model may USE, never a
 # line it reads out. Same rule as RETURNING_NOTE - referring to what we
 # last spoke about is warmth, reciting their file is not.
 ("a case number is never recited unprompted",
  "Do NOT read a" in _cases_block({"matched_cases": [_CASE_A]}), True),
 ("the case detail does reach the prompt",
  "CS-2026-0007" in _cases_block({"matched_cases": [_CASE_A]}), True),
 ("and it names the helper the case is about",
  "Liza Fernandez" in _cases_block({"matched_cases": [_CASE_A, _CASE_B]}), True),
 # Nothing that used to be in the prompt may go missing: an id that resolved
 # against a row we could not read still means there IS a case.
 ("an unreadable case falls back to the line that was there before",
  _cases_block({"matched_case_id": "abc", "matched_cases": []}),
  "- They have an active case with us."),
 ("and a client with no case adds nothing at all",
  _cases_block({}), ""),
 # The ban is on ASKING a client for a case reference, not on putting the one
 # we already hold in front of the agent who picks the ticket up.
 ("the ticket carries the case, and no flow asks for it",
  ("case_number" in t._DETAIL_LABELS,
   [s for s, fl in t.SERVICE_FIELDS.items() if any(f.key == "case_id" for f in fl)]),
  (True, [])),
]
bad = 0
for label, got, want in rows:
    ok = got == want
    bad += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {label:40} {got!r}")
print("\nALL PASS" if not bad else f"\n{bad} FAILED")

raise SystemExit(1 if bad else 0)
