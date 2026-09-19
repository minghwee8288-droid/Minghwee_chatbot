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
import app.graph.guards as _guards
from app.graph.guards import (
    COST_WITHHELD_SERVICES as _WITHHELD,
    asks_for_documents as _docs_q,
    asks_for_process as _proc_q,
)
ic = importlib.import_module("app.graph.nodes.intent_classifier")
ico = importlib.import_module("app.graph.nodes.info_collector")
S = ico._SMALL_TICKET_SERVICES
P = ico._COLLECTION_PURPOSE
from app.graph.prompts.system import RULES
from app.graph.prompts.system import IDENTITY as _IDENTITY
from app.graph.prompts.templates import AGENCY_INFO_INSTRUCTION as _AGENCY_INFO
_rr = importlib.import_module("app.graph.nodes.rag_retriever")
D = chr(36)

# The agency's seven services, in their words. Used by the bracket check below,
# which is written as a SET on purpose: the row it replaced was written about
# the two fields they happened to name and was silent on every other flow.
_ICO_SRC = (Path(__file__).resolve().parents[1]
            / "app/graph/nodes/info_collector.py").read_text(encoding="utf-8")
_BTR_SRC = (Path(__file__).resolve().parents[1]
            / "app/graph/nodes/blocked_topic_responder.py").read_text(encoding="utf-8")
_asks_general = importlib.import_module(
    "app.graph.nodes.blocked_topic_responder").asks_general_info

# Every node in the graph, as an imported MODULE. Whether a node applies a
# guard is decided by whether it imported the symbol - not by whether the name
# appears in the file, which on the first attempt matched rag_retriever purely
# on four comments about ungrounded_figures and made the check red on correct
# code. A check that fails on correct code is noise (2026-09-09 C).
_NODES = {
    n: importlib.import_module(f"app.graph.nodes.{n}")
    for n in ("info_collector", "response_generator", "blocked_topic_responder",
              "rag_retriever", "intent_classifier", "ticket_creator",
              "handover_executor")
}

def _flat(text: str) -> str:
    """The note as one line. These templates are hard-wrapped, so a phrase this
    file asserts on is routinely split across two lines by a re-wrap that
    changed nothing."""
    return " ".join((text or "").split())


_LOADER_SRC = (Path(__file__).resolve().parents[1]
               / "scripts/load_service_notes.py").read_text(encoding="utf-8")


def _row_by_question(fragment: str) -> dict:
    """One ROWS entry from the loader, found by a fragment of its question."""
    import ast
    for node in ast.walk(ast.parse(_LOADER_SRC)):
        if not isinstance(node, ast.Dict):
            continue
        try:
            d = ast.literal_eval(node)
        except Exception:
            continue
        if isinstance(d, dict) and fragment in str(d.get("question") or ""):
            return d
    return {}


_FEE_ROW = _row_by_question("pay any fee to Ming Hwee")

def _text_replacements() -> list[dict]:
    """The loader's TEXT_REPLACEMENTS, read from source rather than imported.

    Importing the loader builds a live Supabase client; this file must run with
    no network (it runs in the container, and on a laptop with no .env).
    """
    import ast
    for node in ast.walk(ast.parse(_LOADER_SRC)):
        if (isinstance(node, ast.AnnAssign)
                and getattr(node.target, "id", "") == "TEXT_REPLACEMENTS"):
            return ast.literal_eval(node.value)
    return []


_REPLACEMENTS = _text_replacements()


def _was_called(first_letter: str) -> str:
    """What the person who follows up used to be called, read from the loader.

    Never typed out here. The sweep further down asserts that no file except
    the loader carries a replaced string, so a check that spells one out fails
    it - which is exactly what the first draft of the 2026-09-11 phone-number
    check did to itself, and what the first draft of this one did on
    2026-09-19. Returning a placeholder rather than raising keeps a missing
    needle a RED line naming the assertion instead of a traceback (2026-09-10).
    """
    for rule in _REPLACEMENTS:
        if (rule["old"].startswith(first_letter)
                and rule["old"].lower().endswith("consultant will")):
            return rule["old"]
    return "<the replaced word is gone from the loader>"

# Every file that could name a phone number, minus the loader, which names the
# old one on purpose - it is the needle it searches for.
_NUMBER_SWEEP = {
    str(f.relative_to(Path(__file__).resolve().parents[1])): f.read_text(
        encoding="utf-8", errors="replace")
    for f in list((Path(__file__).resolve().parents[1] / "app").rglob("*.py"))
    + list((Path(__file__).resolve().parents[1] / "scripts").glob("*.py"))
    + list((Path(__file__).resolve().parents[1] / "scripts").glob("*.md"))
    if f.name != "load_service_notes.py"
}



SEVEN_SERVICES = ("new_hiring", "direct_hiring", "transfer_employer", "renewal",
                  "passport_renewal", "home_leave", "replacement")

# Every option set on those seven was read on 2026-09-10. These three carry
# digits DELIBERATELY and are named here so a new one cannot arrive unnoticed:
#   budget          - the bands are salary bands, and they are also the
#                     grounding `ungrounded_figures` reads (2026-09-09 D), so
#                     removing them silently reintroduces that defect
#   home_type       - REMOVED 2026-09-17. It carried "HDB 1-3 room" / "HDB 4-5
#                     room", and those blocked the fix the agency asked for:
#                     "option of asking landed property is missing" needs the
#                     question to name its options, and naming them with the
#                     room counts in would read brackets at the client - the
#                     very thing they objected to on 2026-09-10. The counts
#                     were redundant anyway: home_size has asked bedrooms and
#                     bathrooms outright since 2026-09-07. One fewer exemption.
#   start_timeline  - a timeframe is not a count
#   expected_salary - the candidate half of `budget`, and it carries the SAME
#                     bands for the same two reasons (2026-09-10)
_DIGITS_ON_PURPOSE = {"budget", "start_timeline", "expected_salary"}
_BRACKET = re.compile(r"\d+\s*(?:-|to|\u2013)\s*\d+|\bat least \d+|"
                      r"\b\d+\s*(?:and\s+)?(?:above|or more)")


# The two halves of the matching form. The EMPLOYER is asked the left-hand
# question about the helper they want; the HELPER is asked the right-hand one
# about herself. A consultant shortlisting reads both tickets side by side.
#
# Written here rather than in ticket.py because it is an assertion about two
# independently maintained field lists: the point is to fail when one side
# gains a question and the other does not.
_MATCHED_PAIRS = {
    "requirement": "work_scope",
    "preferred_nationality": "nationality",
    # 2026-09-17, at the agency's instruction and IN PLACE OF the pork/beef
    # question on both sides. The second pairing whose halves cannot share one
    # option list, for the same reason as the one above it: the employer's list
    # ends in "no preference", which is an answer to his question and not a
    # thing she can be. Derived from `_RELIGIONS` rather than retyped, so the
    # two still cannot drift.
    "helper_religion": "religion",
    "helper_profile": "age",
    "languages": "languages_spoken",
    # `cooking_ability` is still the counterpart - an employer IS asked whether
    # she must handle pork or beef, and that has to be matchable - but since
    # 2026-09-10 it is GATED. A helper who said she does childcare and
    # eldercare was asked what cooking she can do and objected, rightly: the
    # question presumed an answer she never gave. Cooking is now one of the
    # duties she is asked whether she is WILLING to take on, and only if she
    # says yes is she asked which cuisines. The pets -> pet_detail shape.
    "cooking": "cooking_ability",
    "special_duties": "duties_willing",
    "pets": "pet_comfort",
    "helper_room": "room_sharing",
    "rest_day": "rest_day_preference",
    "budget": "expected_salary",
    # `additional_notes` -> `candidate_notes` was here until 2026-09-11, when
    # the agency had the candidate half removed by name: "Remove the
    # unnecessary final question ... The bot should not ask the candidate for
    # additional information at this point." So the employer is still asked
    # what else we should know and the helper is not, and that asymmetry is a
    # DECISION rather than the gap this table exists to catch - which is why it
    # is written here instead of quietly deleted. The cost is real and is
    # recorded at the field list: this question is what produced "I do smoke
    # and i can't leave that" in their own test the day before.
}


def _question_of(service: str, key: str) -> str:
    field = next((f for f in t.SERVICE_FIELDS[service] if f.key == key), None)
    return field.question if field else ""


def _options_of(service: str, key: str) -> tuple:
    field = next((f for f in t.SERVICE_FIELDS[service] if f.key == key), None)
    return tuple(field.options or ()) if field else ()


def _guidance_for(service: str, key: str) -> str:
    """The instruction the model is actually handed for this field.

    Through the real builder, because the question the client reads is the
    field's own wording PLUS this - and the trailing clause of this is what put
    "or another country?" on the end of a question whose own text named exactly
    three (2026-09-11).
    """
    field = next(f for f in t.SERVICE_FIELDS[service] if f.key == key)
    return ico._field_guidance(service, {}, field)


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
dh_emp  = [f.key for f in t.applicable_fields("direct_hiring", {"helper_transfer_case": "yes"})]
dh_free = [f.key for f in t.applicable_fields("direct_hiring", {"helper_transfer_case": "no"})]
hire_src = next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "hire_source")
_budget_field = next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "budget")
import app.graph.prompts.system as _sys
_ROOT = Path(__file__).resolve().parents[1]
# Looked up by dict rather than by `next(...)`, so a field that has been
# renamed or removed turns an assertion RED naming itself instead of raising
# StopIteration and printing no FAIL line at all. A crash tells you less than
# a red - 2026-09-16, where removing `requirement` outright took the whole
# harness down and the run read as success.
_dh = {f.key: f for f in t.SERVICE_FIELDS["direct_hiring"]}
# The five record shapes a client can arrive in, for the 2026-09-17 fill of
# `helper_from_us`. Built here rather than inside the assertion list so the
# same five are read by every check below - a check that tries one shape is
# how "written for the case that was reported" keeps happening.
_RECORD_SHAPES = {
    "a number we have never matched": {},
    "an employer on file with no placement": {"record_name": "Tolo"},
    "one placement, and it names her": {
        "prior_hires": 1,
        "placed_helper": {"helper_name": "Liza Fernandez", "nationality": "PH"}},
    "four placements, none matchable": {"prior_hires": 4},
    "a placement row naming nobody": {"prior_hires": 1},
}
_SHAPE_BASE = {
    "record_name": "", "customer_name": "", "matched_lead": None,
    "lead_kind": "", "prior_hires": 0, "placed_helper": None,
}


def _from_us(shape: dict, service: str = "renewal") -> str:
    """What `helper_from_us` is filled with, for one record shape."""
    return ico._known_fields({**_SHAPE_BASE, **shape}, service).get(
        "helper_from_us", ""
    )


# Every flow that asks where the helper came from, derived rather than listed,
# so a flow added tomorrow is covered or fails by name.
_ASKS_FROM_US = sorted(
    svc for svc, fields in t.SERVICE_FIELDS.items()
    if any(f.key == "helper_from_us" for f in fields)
)

_rn = {f.key: f for f in t.SERVICE_FIELDS["renewal"]}
_rp = {f.key: f for f in t.SERVICE_FIELDS["replacement"]}
_dh_keys = [f.key for f in t.SERVICE_FIELDS["direct_hiring"]]
_dh_at = lambda k: _dh_keys.index(k) if k in _dh_keys else -1
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
import httpx as _httpx
from app.whapi import client as _whapi_client
_whapi_src = _pathlib.Path(_whapi_client.__file__).read_text(encoding='utf-8')
_msg_src = _pathlib.Path(ms.__file__).read_text(encoding='utf-8')
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
# Every word of the office rows, so a fact can be asserted present without
# naming which of the six rows carries it.

def _home_type():
    return next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "home_type")


def _workload_note() -> str:
    """The one-helper warning, as the model is handed it."""
    src = (Path(__file__).resolve().parents[1]
           / "app/graph/nodes/info_collector.py").read_text(encoding="utf-8")
    start = src.find("workload_note = (")
    return _flat(src[start:src.find("# They stated a requirement", start)])


def _hh():
    return next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "household")


def _purpose_note() -> str:
    """The opening-turn reason, as the model is handed it."""
    src = (Path(__file__).resolve().parents[1]
           / "app/graph/nodes/info_collector.py").read_text(encoding="utf-8")
    start = src.find("purpose_note = (")
    return _flat(src[start:src.find("# They stated a requirement", start)])


# The live transcript, plus the five shapes that must NOT fire.
_WORKLOAD_CASES = [
    ({"requirement": "I will need a combination of the childcare and cleaning",
      "household": "6", "home_size": "12 bedrooms and 10 toilets"}, True,
     "the live transcript"),
    ({"requirement": "childcare", "household": "6"}, False,
     "one kind of work, big household"),
    ({"requirement": "childcare and cleaning", "household": "3",
      "home_size": "2 bedrooms"}, False, "two kinds, small household"),
    ({"requirement": "childcare and cleaning", "household": "3",
      "home_size": "8 bedrooms 6 bathrooms"}, True, "two kinds, big HOME"),
    ({"requirement": "all of the above", "household": "5"}, True, "all of the above"),
    ({"requirement": "general housework and cooking", "household": "7"}, False,
     "housework and cooking is ONE kind of work"),
    ({"requirement": "eldercare", "household": "2"}, False, "an ordinary placement"),
    ({}, False, "nothing collected yet"),
]

_STATE_SRC = (Path(__file__).resolve().parents[1]
              / "app/graph/state.py").read_text(encoding="utf-8")
graph_mod = importlib.import_module("app.graph.graph")
import app.graph.prompts.templates as tmpl


# A process answer with one step stating our own published turnaround, and one
# that promises somebody will ring them. The guard must treat those differently.
_STEPS_OK = (
    "Here is the process from here:\n"
    "1. Consultation - you share what your household needs.\n"
    "2. Shortlist - you receive 3 to 5 matched profiles within 48 hours.\n"
    "3. Interview - by video or in person."
)
_STEPS_PROMISE = (
    "Here is what happens:\n"
    "1. We shortlist helpers for you.\n"
    "2. A live agent will call you within 2 hours.\n"
    "3. She arrives."
)

_OFFICE_TEXT = " ".join(
    f'{r["question"]} {r["answer"]}' for r in lsn.ROWS
    if r["section_heading"].startswith("Office - "))
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
 # ...and the phrasing an employer actually opens with, which the lookahead
 # did not cover until 2026-09-18. "hi i want transfer helper" matched "i
 # want transfer" and was read as the helper speaking, so the first turn ran
 # the CANDIDATE flow and asked for HER name - and the switch to
 # transfer_employer a turn later wiped what had been collected. Swept as a
 # SET, both ways, because the cost of getting this wrong is symmetrical: an
 # employer in her questionnaire, or a helper in his.
 ("...and every way an employer asks for one reads as an employer",
  [m for m in ("hi i want transfer helper", "i want transfer maid",
               "i need transfer helper urgently", "i want a transfer helper",
               "i am looking for a transfer helper",
               "transfer my maid to another employer")
   if ic._detected_contact_type("transfer", None, m) != "employer"], []),
 ("...and every way SHE asks still reads as the helper",
  [m for m in ("transfer me to another employer", "i want to be transferred",
               "i want transfer to a new employer", "please find me a new employer",
               "my employer is not paying me i want transfer",
               "i am looking for a new employer")
   if ic._detected_contact_type("transfer", None, m) != "candidate"], []),
 # The whole point of the two rows above is which QUESTIONNAIRE they land in.
 ("...and that is what decides whose flow they get",
  [t.resolve_service("transfer", ic._detected_contact_type("transfer", None, m))
   for m in ("hi i want transfer helper", "i want to be transferred")],
  ["transfer_employer", "transfer"]),
 ("insurance is a service", "insurance" in t.SERVICE_FIELDS, True),
 ("small-ticket services", sorted(S), ["insurance", "passport_renewal", "renewal"]),
 ("blocks the hiring total", q(f"The total first-year cost is S{D}14,000-17,500."), True),
 ("still quotes salary", q(f"Salaries range from {D}600 to {D}800."), False),
 # 26 since 2026-09-17: `helper_religion`, asked at the agency's instruction
 # and in place of the pork/beef half of `cooking`.
 ("new_hiring field count", len(t.SERVICE_FIELDS["new_hiring"]), 26),
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
 # --- home leave tells them to book the ticket, 2026-09-17 ------------------
 # Agency: "the bot should advise the client to purchase the air ticket and
 # send a copy of the ticket to us", so the agent who picks the case up can
 # submit the embassy paperwork against confirmed dates. Their screenshot shows
 # the bot answering it correctly - but only because the client asked.
 ("home leave closes with a briefing, keyed on her nationality",
  t.BRIEFING_AFTER.get("home_leave"), "nationality"),
 # 2026-09-18, the agency's own ask. Keyed on the SECOND-TO-LAST field, not the
 # last: the retriever runs before the collector, so on the turn that completes
 # the collection the state it reads does not yet hold the final answer.
 ("replacement closes with a briefing, keyed on the field before the last",
  (t.BRIEFING_AFTER.get("replacement"),
   [f.key for f in t.SERVICE_FIELDS["replacement"]][-2:]),
  ("timeline", ["timeline", "replacement_preferences"])),
 # The replacement timeline asks ONE thing, and it is about the NEW helper.
 # It used to ask two ("when are you planning to replace her, AND when would
 # you ideally want the new helper to start?") and was closed by an answer to
 # the first half. Split on 2026-09-18; the half about the CURRENT helper's
 # departure was removed the same day on the agency's instruction - asked when
 # she planned to leave, a client answered "she is not planning to leave but
 # want her to leave my home", because a replacement is the employer ending it
 # and she has no plan of her own to report. Asserted as its ABSENCE, so it
 # cannot come back by accident.
 ("the replacement timeline asks about the NEW helper and nothing else",
  sorted(k for k in ("current_helper_exit_date", "timeline")
         if k in {f.key for f in t.SERVICE_FIELDS["replacement"]}),
  ["timeline"]),
 ("...and no flow anywhere asks when the current helper plans to leave",
  sorted(svc for svc, fields in t.SERVICE_FIELDS.items()
         for f in fields
         if "planning to leave" in f.question.lower()), []),
 # ", and " is the join, not the word "and": the start-date question opens
 # "And when would you ideally like...", which is one ask reading as a
 # follow-on. The old field was "...to leave, and when would you ideally want
 # the new helper to start?" - two asks, one comma, one answer.
 # The client's own name standing alone in front of the next question. Four
 # transcripts across two services, and the prompt rule alone left it in 1 run
 # of 3 - so it is a guard as well. Ordering matters and is asserted: the
 # opener guard runs FIRST and exposes the bare name by removing the filler in
 # front of it, so a name guard running before it sees nothing to do.
 ("a bare name in front of the question is stripped",
  gd.strip_leading_name("Amir. Are you sending Farhana home?", "amir khan"),
  "Are you sending Farhana home?"),
 ("...including with a comma",
  gd.strip_leading_name("Amir, how many people live with you?", "Amir"),
  "How many people live with you?"),
 ("...but a greeting that CARRIES the name is left alone",
  [r for r in ("Thanks, Amir. May I know her name?", "Hi Amir, I'm Claire.",
               "Got it - how long has she been with you?")
   if gd.strip_leading_name(r, "amir khan") != r], []),
 ("...and the opener guard runs before it, or it has nothing to catch",
  _COLLECTOR_SRC.index("strip_leading_name(\n        strip_repeated_opener(") > 0, True),
 # ", and " is the join that made the old field two questions in one:
 # "...to leave, and when would you ideally want the new helper to start?"
 ("...and it does not join a second ask onto itself",
  [f.key for f in t.SERVICE_FIELDS["replacement"]
   if f.key == "timeline" and ", and " in f.question.lower()], []),
 # The closing briefing must always END by saying the enquiry is with a person.
 # The agency asked for that sentence by name; the note says it twice and the
 # model still dropped it in 1 run of 3, so the collector appends it when it is
 # missing. Appended, never substituted.
 ("a briefing that already announced the handover is left alone",
  [r for r in ("Thank you, Ranbir. I've passed everything to our team, and a "
               "live agent will connect with you shortly.",
               "Our team will be in touch shortly.",
               "I've passed this to our team and a live agent will connect "
               "with you shortly.")
   if not ico._ANNOUNCES_HANDOVER.search(r)], []),
 ("...and one that only listed the steps is not",
  bool(ico._ANNOUNCES_HANDOVER.search(
      "Here is the process from here:\n1. Sign the form\n2. Collect her")), False),
 ("...and the line that gets appended survives the guards that run on it",
  (gd.strip_handover_talk(ico.BRIEFING_CLOSING_LINE) == ico.BRIEFING_CLOSING_LINE
   and not gd.looks_like_document(ico.BRIEFING_CLOSING_LINE)), True),
 # A briefing keyed on a field its own flow never asks can never come due, and
 # the flow would close on the bare handover line with nothing to show for it.
 # Derived, so a fourth service cannot reopen it.
 ("every briefing is keyed on a field that flow actually collects",
  sorted({f"{svc}:{key}" for svc, key in t.BRIEFING_AFTER.items()
          if key not in {f.key for f in t.SERVICE_FIELDS.get(svc, ())}}), []),
 ("the ticket note tells them to book it and send a copy",
  ("book her air ticket" in _flat(tmpl.HOME_LEAVE_TICKET_NOTE)
   and "send us a copy" in _flat(tmpl.HOME_LEAVE_TICKET_NOTE)), True),
 # ...and says WHY, which is the whole of the agency's reasoning: confirmed
 # dates are what let the embassy paperwork go in immediately.
 ("...and says why, so it does not read as an instruction out of nowhere",
  "confirmed travel dates" in _flat(tmpl.HOME_LEAVE_TICKET_NOTE), True),
 # The itinerary is a FILIPINO embassy document and the records say so; for an
 # Indonesian helper they do not list it. Asking for it as a document she needs
 # would contradict the document list in the same message. Same rule as
 # FEE_BY_NATIONALITY, applied to a document instead of a price.
 ("the itinerary is a document for PH and only confirmation of dates for ID",
  ("FILIPINO" in _flat(tmpl.HOME_LEAVE_TICKET_NOTE)
   and "INDONESIAN" in _flat(tmpl.HOME_LEAVE_TICKET_NOTE)
   and "not among them" in _flat(tmpl.HOME_LEAVE_TICKET_NOTE)), True),
 # No figure of any kind: ungrounded_figures bins the whole briefing and they
 # lose the advice with it - the 2026-09-17 workload note, same reasoning.
 ("the ticket note quotes nothing and sets no deadline",
  (not re.search(r"\d", _flat(tmpl.HOME_LEAVE_TICKET_NOTE))
   and "never say when she must fly by" in _flat(tmpl.HOME_LEAVE_TICKET_NOTE)), True),
 # home_leave already has a fee for PH and ID only, so a Myanmar helper defers
 # rather than being quoted somebody else's price.
 ("home leave still defers the cost for a nationality we have no price for",
  (t.fee_is_known_for("home_leave", "PH"), t.fee_is_known_for("home_leave", "MM")),
  (True, False)),

 # --- ...and "both" is a third answer, not a way of saying no, 2026-09-17 ---
 # Agency test on direct hire: the client answered "both", was never asked for
 # an address, and said so. `excludes` is checked first and held the names of
 # the OTHER channel, so naming WhatsApp alongside email closed the gate - even
 # when the answer said "email" outright. Asserted through applicable_fields on
 # every flow that asks the question, not on the gate alone: a correct predicate
 # nobody calls is the hole this file has recorded three times.
 ("every way of saying BOTH gets asked for an address, on every flow that asks",
  sorted({f"{svc}:{ans}"
          for svc, fl in t.SERVICE_FIELDS.items()
          if any(f.key == "update_channel" for f in fl)
          for ans in ("both", "Both", "both please", "both email and whatsapp",
                      "email and whatsapp", "whatsapp and email",
                      "email as well as whatsapp", "send to both my email and here",
                      "either", "any", "both is fine")
          if not any(f.key == "email"
                     for f in t.applicable_fields(svc, {"update_channel": ans}))}),
  []),
 # The half that must NOT move: picking WhatsApp alone is still taken as an
 # answer and no address is asked for. That is the 2026-09-04 defect this gate
 # was built for - the bot chose the channel, then asked for the one detail
 # that channel needs.
 ("choosing WhatsApp alone is still never asked for an address",
  sorted({f"{svc}:{ans}"
          for svc, fl in t.SERVICE_FIELDS.items()
          if any(f.key == "update_channel" for f in fl)
          for ans in ("whatsapp", "here on whatsapp", "this number", "here is fine",
                      "phone", "whatsapp only", "just whatsapp", "neither",
                      "whatsapp is enough")
          if any(f.key == "email"
                 for f in t.applicable_fields(svc, {"update_channel": ans}))}),
  []),
 # A refusal that names email is a refusal. This is what `excludes` is FOR -
 # the same shape as "no, I don't have pets" containing "have".
 ("a refusal of email closes the gate even though it says the word",
  sorted({a for a in ("no email", "not email", "i dont have an email",
                      "do not have email", "only whatsapp not email", "no e-mail address")
          if t._WANTS_EMAIL.state({"update_channel": a}) != "closed"}), []),
 # Still undecided while unanswered, so the extractor keeps listening for an
 # address volunteered before we get there.
 ("an unanswered channel question leaves the gate undecided",
  t._WANTS_EMAIL.state({}), "undecided"),
 # The cause, as a tripwire: the moment the other channel's NAME goes back into
 # excludes, "both email and whatsapp" closes again.
 ("the email gate never excludes on the name of the other channel",
  sorted(x for x in t._WANTS_EMAIL.excludes
         if any(w in x for w in ("whatsapp", "whats app", "here", "number",
                                 "phone", "text", "chat"))), []),
 # The question offers it, so a client does not have to volunteer a word the
 # question never showed them. Three named literally, which is what puts
 # _field_guidance on its "name them all" branch.
 ("the channel question names all three answers",
  sum(1 for o in t._UPDATE_CHANNEL.options
      if o.lower() in t._UPDATE_CHANNEL.question.lower()) >= 3, True),
 ("no field mentions swimming",
  [f.key for fl in t.SERVICE_FIELDS.values() for f in fl
   if "swim" in (f.label + f.question).lower()], []),
 ("first message is told to introduce Claire",
  "introduction is NOT optional" in ico.COLLECTOR_INTRO_NOTE, True),
 # --- the agency process table, 2026-09-07 ---------------------------
 # direct_hiring was an empty list, so it raised a ticket that said only
 # "wants us to process a helper they have already chosen". These six are
 # the agency's own list; if the flow is ever emptied again this fails.
 ("direct_hiring collects what the agency asked for",
  [k for k in ("helper_name", "helper_contact", "helper_nationality",
               "helper_transfer_case", "helper_availability")
   if k not in dh_emp], []),
 # 2026-09-17: `helper_location` and `employment_status` were collapsed into
 # one yes/no, because only one of the four answers they produced between them
 # changes what we do. Asserted as ABSENCE, not just as the presence of the
 # new field - a flow that asks the new question AND still asks the old two
 # has not been simplified, it has been made longer.
 ("the two questions the agency collapsed are gone",
  [k for k in ("helper_location", "employment_status")
   if k in [f.key for f in t.SERVICE_FIELDS["direct_hiring"]]], []),
 ("...and the one that replaced them is a yes/no, with no options to read out",
  getattr(_dh.get("helper_transfer_case"), "options", "FIELD MISSING"), ()),
 # ...and it must READ as a yes/no, or `_BARE_YES_NO` re-asks a client who
 # correctly answered "no" (2026-09-08).
 ("...and reads as one, so a bare yes/no closes it",
  ico._yes_no_question(
      getattr(_dh.get("helper_transfer_case"), "question", "")), True),
 ("on a permit under another employer -> asked about notice / clearance",
  "notice_clearance" in dh_emp, True),
 ("not a transfer case -> NOT asked about notice",
  "notice_clearance" in dh_free, False),
 # The agency, 2026-09-17: "The bot should not request personal contact
 # details at the beginning of the conversation." Their own transcript has the
 # helper's number asked THIRD and answered "I'm not comfortable to provide
 # this information now". Asserted as a position rather than by naming an
 # index, so reordering the qualifying questions cannot silently undo it.
 # `_dh_at` rather than list.index(): a field that has been renamed away
 # raises ValueError from index() and takes the whole harness down without
 # printing a FAIL line, which reads as success. Returns -1 instead, so the
 # comparison is False and the assertion names itself.
 ("the helper's number is not asked before her nationality",
  _dh_at("helper_contact") > _dh_at("helper_nationality") >= 0, True),
 ("...nor before we know whether it is a transfer case",
  _dh_at("helper_contact") > _dh_at("helper_transfer_case") >= 0, True),
 ("...and a client who declines it is not blocked",
  getattr(_dh.get("helper_contact"), "optional", "FIELD MISSING"), True),
 # "For Direct Hire, the bot should avoid using the word paperwork."
 ("the direct-hire purpose note does not say 'paperwork'",
  "paperwork" in ico._COLLECTION_PURPOSE["direct_hiring"], False),
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
 # --- 2026-09-18: ...and a cost question that NAMES a service ------------
 # The door the 2026-09-07 fix left open. That one keyed on another service
 # being ESTABLISHED; here the service is named in the very message asking the
 # price, so nothing was established and fee_enquiry collected its own two
 # fields. Live 23:05: "what is the fees for work permit renewal?" ->
 # "Which nationality are you looking at?" -> "What kind of care would this be
 # for?", the care question asked three times, the last AFTER "i am not asking
 # for any new hiring". Neither field means anything for a renewal.
 ("a fee question naming a service is answered, not qualified",
  g.route_after_rag({"intent": "fee_enquiry", "service_type": "renewal",
                     "collected_service": None, "blocked_topics": {}}),
  "response_generator"),
 # The SERVICE moves, the INTENT does not. Promoting the intent routes to the
 # collector and opens the named service's own intake - a price question
 # turned into a form, which is the defect being fixed, one service along.
 ("...and promoting the intent instead would open an intake",
  g.route_after_rag({"intent": "renewal", "service_type": "renewal",
                     "collected_service": None, "blocked_topics": {}}),
  "info_collector"),
 # fee_enquiry's two fields are HIRING-shaped, which is why they cannot be put
 # to a renewal client. Asserted so that "just add care_type to renewal" is
 # never the fix somebody reaches for.
 ("the money enquiries ask a care type and the renewals never do",
  ([f.key for f in t.SERVICE_FIELDS["fee_enquiry"]] == ["nationality", "care_type"]
   and not any(f.key == "care_type"
               for s in ("renewal", "passport_renewal", "home_leave")
               for f in t.SERVICE_FIELDS[s])), True),
 # A passport renewal is $450 and a work permit renewal is $695, and
 # FEE_STATED_SERVICES holds both - so resolving a passport phrasing to
 # `renewal` states the wrong price rather than merely picking the wrong flow.
 # `\brenew` was tested before `\bpassport`, so all four phrasings did.
 # ungrounded_figures cannot catch it: $695 is genuinely in the records.
 ("a passport phrasing names the passport service, not the work permit",
  [_named_svc(m) for m in ("how much for passport renewal",
                           "what is the cost to renew my helper passport",
                           "renew my helper passport please",
                           "how much does it cost to renew her passport?")],
  ["passport_renewal"] * 4),
 ("...and a work permit phrasing still names the work permit service",
  [_named_svc(m) for m in ("what is the fees for work permit renewal?",
                           "I want to renew my helper work permit")],
  ["renewal"] * 2),
 ("...and renewing an insurance is still the insurance",
  _named_svc("cost of insurance renewal"), "insurance"),
 # The control: a price question naming nothing must still reach the two
 # fields that qualify it, or an opening "how much do you charge?" cannot be
 # answered at all.
 ("a price question naming no service names none",
  [_named_svc(m) for m in ("how much do you charge",
                           "how much does it cost to hire a helper")],
  [None, None]),
 # --- 2026-09-18: the SAME fee question, asked twice, answered neither time --
 # The 12:05 transcript. A renewal collected four fields, raised a ticket and
 # closed on the handover line without ever stating the fee the client opened
 # with; they wrote "i have asked for the fees" and were acknowledged again.
 #
 # A price question that names nothing still has a subject when the
 # conversation has been about one. "The whole conversation" holds for an
 # opening "how much do you charge?" and is false for a repeat after a
 # handover, which is how the query went out bare.
 ("a repeated fee question recovers the parked service as its subject",
  rr._subject_service({"intent": "fee_enquiry", "service_type": "fee_enquiry",
                       "blocked_topics": {"renewal": {"ticket_id": 1}}}), "renewal"),
 ("...and the flow that ran is preferred to the parked topic",
  rr._subject_service({"intent": "fee_enquiry", "service_type": "fee_enquiry",
                       "collected_service": "passport_renewal",
                       "blocked_topics": {"renewal": {"ticket_id": 1}}}),
  "passport_renewal"),
 # THE CONTROL: with nothing else in hand there is genuinely no other subject,
 # and tagging it "(fee enquiry)" tags it with itself.
 ("...but an opening fee question still has no subject",
  rr._subject_service({"intent": "fee_enquiry", "service_type": "fee_enquiry",
                       "blocked_topics": {}}), "fee_enquiry"),
 # salary_enquiry stays out, for the reason it is not in _SUBJECTLESS_INTENTS:
 # what a helper earns is about the helper, and tagging it measured WORSE.
 ("...and a salary question is never given one",
  rr._subject_service({"intent": "salary_enquiry", "service_type": "salary_enquiry",
                       "blocked_topics": {"renewal": {"ticket_id": 1}}}),
  "salary_enquiry"),
 # `renewal` is the one service key that does not read as itself - "renewal",
 # of what? - while every row it must match says "work permit renewal". As
 # "(cost of renewal)" the repeated fee question scored 0.399, four
 # thousandths UNDER the soft floor, so weak_retrieval discarded the reply and
 # handed over with $695 sitting in the retrieved set. As "(cost of work
 # permit renewal)" it scores 0.558.
 ("the subject is tagged in the words the records use",
  rr._readable_service("renewal"), "work permit renewal"),
 ("...and every other key still reads as itself",
  [rr._readable_service(k) for k in ("passport_renewal", "home_leave",
                                     "direct_hiring", "new_hiring")],
  ["passport renewal", "home leave", "direct hiring", "new hiring"]),
 # A client restating a question is asking it, not chasing a case - and their
 # phrasing is usually not interrogative, which is why every existing detector
 # missed all six live phrasings.
 ("a restated question is recognised as one",
  [gd.asks_again(m) for m in ("i have asked for the fees",
                              "you never told me the fee",
                              "i already asked about the cost",
                              "as i said i need the fees",
                              "you didnt answer my question")],
  [True] * 5),
 # ...and does not fire on somebody else doing the asking, or on an ordinary
 # question, which is already handled.
 ("...and an ordinary message is not a restatement",
  [gd.asks_again(m) for m in ("what is the cost",
                              "my employer asked me to come back",
                              "she asked for a day off",
                              "i will ask my husband",
                              "ok thanks")],
  [False] * 5),
 # The parked path has to see it too, or a repeat while a human owns the topic
 # gets the holding line for the second time - which is the live transcript.
 ("a parked topic answers a restated question",
  btr.asks_general_info("i have asked for the fees"), True),
 # ...but a genuine chase is still held, and "still waiting" overlaps both. It
 # is tested first on purpose: a parked topic silences chasing.
 ("...and a chase is still a chase",
  [btr.asks_general_info(m) for m in ("any update on my case",
                                      "i am still waiting for the fees")],
  [False, False]),
 # The note is gated on having something to answer WITH, so it can never turn
 # an honest "I don't know" into an invented figure.
 ("the restated-question note refuses the three non-answers",
  all(p in tpl.ASKED_AGAIN_NOTE.lower()
      for p in ("noted", "come back", "live agent")), True),
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
 ("route unknown -> caveat needed",
  ico._known_transfer_case({"collected_info": {}}), None),
 ("route known -> no caveat needed",
  ico._known_transfer_case(
      {"collected_info": {"helper_transfer_case": "yes, on a permit here"}}),
  "yes, on a permit here"),
 # The guard is keyed on a field the flow actually collects. Keyed on anything
 # else it can never be satisfied, and the caveat would attach to every
 # direct-hire timing question forever - which looks exactly like the guard
 # working. Derived from the flow, not from the field's name.
 ("the route guard is keyed on a field direct_hiring collects",
  "helper_transfer_case" in [f.key for f in t.SERVICE_FIELDS["direct_hiring"]], True),
 # --- 2026-09-17: a passport that runs out before we could renew it ----
 # Live: "in 5 days" answered with "It takes approximately 6 to 8 weeks", the
 # two numbers one line apart and nothing connecting them. Reproduced 2 runs of
 # 2. The expiry has been collected since the flow was built and put on the
 # ticket; nothing ever read it.
 ("a clearly short expiry is read as short",
  [m for m in ("in 5 days", "5 days", "in 2 weeks", "in 4 weeks", "next month",
               "in 2 months", "tomorrow", "this week", "in 1 month")
   if not ico._expires_before_we_finish({"passport_expiry": m})], []),
 # 60 days covers the slowest route we hold - a Filipino renewal is 6 to 8
 # weeks, which is 42 to 56 days - so anything past it finishes comfortably
 # whatever her nationality.
 ("...and a comfortable one is not",
  [m for m in ("in 6 months", "in 2 years", "in 1 year", "in 90 days")
   if ico._expires_before_we_finish({"passport_expiry": m})], []),
 # FAILS TOWARDS SILENCE, which is the whole safety of it: anything this cannot
 # read plainly leaves the briefing exactly as it is today. Guessing at a date
 # and then calling somebody's passport urgent on the strength of it is worse
 # than the omission being fixed. "27 September 2033" is in that set on
 # purpose - it is the format `contact._passport_expiry` produces off
 # `biodata.passportExpiry`, and a 2033 date is not urgent anyway.
 ("an answer it cannot read plainly says nothing at all",
  [m for m in ("next March", "when the contract ends", "not sure", "",
               "soon", "27 September 2033")
   if ico._expires_before_we_finish({"passport_expiry": m})], []),
 # The note may restate the two figures it was given and invent no third.
 ("the note forbids inventing a date or a deadline",
  "Do NOT invent a new date" in tpl.EXPIRING_SOON_NOTE, True),
 ("...and forbids promising it can be rushed",
  "rushed" in tpl.EXPIRING_SOON_NOTE, True),
 # We hold no record of what MOM does when a passport lapses, and frightening
 # somebody with a consequence nobody has checked is worse than silence.
 ("...and forbids threatening them with a consequence we have not checked",
  "what MOM will do" in tpl.EXPIRING_SOON_NOTE, True),
 ("...and carries no figure of its own",
  bool(re.search(r"\d{2,}", tpl.EXPIRING_SOON_NOTE)), False),
 # --- 2026-09-17: whose passport is it? --------------------------------
 # Live on two numbers. The serious one: "I want to renew my passport" ->
 # "There isn't any helper here. I want to renew my passport" -> four questions
 # later, a full closing briefing quoting "approximately 3 working days",
 # "approximately $450" and asking for "a copy of your NRIC" AND "a copy of
 # your Work Permit" - a contradiction on its face, and a price for a service
 # Ming Hwee does not offer to that person at all. Every passport_renewal row
 # and every one of its questions is about a HELPER, but none of that was a
 # TEST, so the model just reworded the questions.
 ("an explicit denial of a helper is caught",
  [m for m in ("There isn't any helper here. I want to renew my passport",
               "I am talking about my passport renewal not my helper",
               "I want to renew my own passport",
               "I dont have a helper, I need my passport renewed",
               "no helper, its for me",
               "passport for myself please")
   if not ico._asks_about_own_passport({"incoming_text": m, "flagged_once": []}, {})],
  []),
 # The other side, and the one that decides whether this is safe: a client
 # asking about HER passport must be untouched, including the sloppy phrasings
 # employers actually use.
 ("a helper's passport is never mistaken for the client's",
  [m for m in ("I want to renew my helper passport",
               "I want to renew my helper's passport",
               "my maid passport is expiring",
               "renew passport for my helper",
               "her passport expires in 3 weeks",
               "Polo", "Indonesia", "in 3 weeks",
               # Bare and genuinely ambiguous. Deliberately NOT caught on the
               # first message: plenty of employers say this meaning their
               # maid's, and the flow's own first question is what surfaces it.
               "I want to renew my passport")
   if ico._asks_about_own_passport({"incoming_text": m, "flagged_once": []}, {})],
  []),
 # ...and the same bare sentence IS caught once we already hold a helper for
 # this conversation, because then "my passport" cannot be hers. That is the
 # first transcript: "Also I want to renew my passport also", said after Polo's
 # renewal had completed.
 ("...but it is caught once we already know the helper",
  ico._asks_about_own_passport(
      {"incoming_text": "Also I want to renew my passport also", "flagged_once": []},
      {"helper_name": "Polo"}), True),
 ("...and not before we do",
  ico._asks_about_own_passport(
      {"incoming_text": "Also I want to renew my passport also", "flagged_once": []},
      {}), False),
 # Once established it stays established, so "why not?" lands in the same
 # branch instead of being met with "May I know your helper's name?" - the
 # 2026-09-11 rule that a refusal has to be a conversation.
 ("a follow-up still lands in the branch",
  ico._asks_about_own_passport(
      {"incoming_text": "why not? can you still help me",
       "flagged_once": ["own_passport"]}, {}), True),
 # ...and naming a helper takes the turn straight back to collecting, so a
 # correction costs nothing and needs no special case.
 ("...and naming a helper releases it",
  ico._asks_about_own_passport(
      {"incoming_text": "ok then my helper passport",
       "flagged_once": ["own_passport"]}, {}), False),
 # Both services it lands in, measured rather than assumed: a bare "renew my
 # passport" classifies as passport_renewal, and "Also I want to renew my
 # passport also" classifies as `renewal`, because the word carrying the intent
 # is "renew".
 ("the branch covers both services the request lands in",
  sorted(ico._PASSPORT_SERVICES), ["passport_renewal", "renewal"]),
 # The note must not price it. Every figure on that turn - the fee, the working
 # days, the NRIC and Work Permit copies - belongs to a HELPER's embassy
 # renewal, and `ungrounded_figures` would pass them because they really are in
 # our records. Grounded is not the same as wanted.
 ("the note forbids the fee, the timing and the document list",
  [w for w in ("fee", "timeline", "document list")
   if w not in tpl.OWN_PASSPORT_NOTE.lower()], []),
 ("...and forbids sending them somewhere we have no record of",
  "Do NOT tell them where to go" in tpl.OWN_PASSPORT_NOTE, True),
 # Not a handover, for the reason the nationality refusal is not one: this is
 # an answer we hold, and a consultant repeating it is spent time.
 ("...and does not hand it to a consultant",
  "do not hand them to a colleague" in tpl.OWN_PASSPORT_NOTE, True),
 ("...and carries no figure of its own",
  bool(re.search(r"\d{3}", tpl.OWN_PASSPORT_NOTE)), False),
 # --- 2026-09-19: the HELPER renewing her own passport -----------------
 # The agency tested it as a new user and the 2026-09-17 branch refused her
 # twice: "Ming Hwee handles passport renewal for domestic helpers only, not
 # clients' own passports." She had written "i want to renew my passport then
 # why you are asking about my helper name i dont have any helper".
 #
 # The first answer to that was a disambiguating question, and the agency took
 # it back the same day: "the intent is clear naa, it means the user is helper
 # and wants to renew their passport ... this question does not make any
 # sense." So "my passport" with no helper named IS the sender's, decided on
 # the message that says it, and what catches the 2026-09-17 employer instead
 # is the nationality question the flow already asks.
 ("'my passport' is the passport of the person writing it",
  [m for m in ("i want to renew my passport",
               "I want to renew my passport",
               "hi i need to renew my own passport",
               "my passport is expiring, can you renew it")
   if ico._whose_passport({"incoming_text": m, "flagged_once": []},
                          {}) != "sender"], []),
 # Decided on the FIRST message, before we have asked their name - because it
 # is the helper-NAME question, not the refusal, that the agency objected to,
 # and a decision that waits for the name arrives one question too late.
 ("...and decided before their own name has been asked for",
  ico._whose_passport(
      {"incoming_text": "i want to renew my passport", "flagged_once": []},
      {}), "sender"),
 # A denial is the same statement written the other way round, and it names a
 # helper in order to say there is not one - so it must not be read as "my
 # helper's". This is the 2026-09-17 employer's own sentence.
 ("a denial beats the helper word inside it",
  [m for m in ("There isn't any helper here. I want to renew my passport",
               "i want to renew my passport then why you are asking about my "
               "helper name i dont have any helper",
               "i dont have a helper, its my passport",
               "no helper, its for me")
   if ico._whose_passport({"incoming_text": m, "flagged_once": []},
                          {"full_name": "Rats"}) != "sender"], []),
 # She says so herself, and then nothing is asked at all.
 ("a helper who says she is one is believed straight away",
  [m for m in ("i am a helper, i want to renew my passport",
               "im a maid and i need my passport renewed",
               "my employer said to message you about my passport")
   if ico._whose_passport({"incoming_text": m, "flagged_once": []},
                          {"full_name": "kareena"}) != "sender"], []),
 # Settled answers stick, so the decision is not re-taken from a later message
 # that no longer says it - "kareena", "indonesia" and a date all say nothing
 # about whose passport this is.
 ("a settled answer is not re-decided from a later message",
  [m for m in ("my self kareena", "indonesia", "next year march", "ok")
   if ico._whose_passport(
       {"incoming_text": m,
        "flagged_once": ["passport_holder_is_the_sender"]}, {}) != "sender"],
  []),
 # ...and naming a helper takes it back, so a client who corrects us costs
 # nothing and needs no special case - the 2026-09-11 rule.
 ("...and naming a helper releases it",
  ico._whose_passport(
      {"incoming_text": "sorry i meant my helper's passport",
       "flagged_once": ["passport_holder_is_the_sender"]}, {}), None),
 # ...but a denial and her saying outright that she is a helper both BEAT that
 # release, for the reason above: each contains the word that would otherwise
 # undo what she has just told us.
 ("...but not on the two sentences that only she writes",
  [m for m in ("i dont have any helper", "i am a helper",
               "there isnt any helper, it is mine")
   if ico._whose_passport(
       {"incoming_text": m,
        "flagged_once": ["passport_holder_is_the_sender"]}, {}) != "sender"],
  []),

 # THE EMPLOYER FLOW IS UNTOUCHED, which is what the agency asked for by name.
 # None of these reaches the branch at all: they name a helper, so "my
 # passport" never matches without one beside it.
 ("the employer flow never reaches any of this",
  [m for m in ("Hey i want to Renew my Helper Passport",
               "i want to renew my helper's passport",
               "my maid passport is expiring",
               "renew passport for my helper",
               "Sallu", "Lily", "indonesia", "On 26/10/2026")
   if ico._whose_passport({"incoming_text": m, "flagged_once": []},
                          {"full_name": "Sallu"}) is not None], []),
 # The one case where "my passport" is genuinely NOT the sender's: we already
 # hold a helper on this conversation, so we know they employ one and the
 # passport they have just asked about is their own. That needs no nationality
 # test - a named helper is the evidence the test stands in for elsewhere.
 ("...and an employer who has named his helper is refused, not served",
  ico._whose_passport(
      {"incoming_text": "Also I want to renew my passport also",
       "flagged_once": []},
      {"full_name": "Vaidik", "helper_name": "Polo"}), "own"),
 # ...and her collection note stops the model talking to her about "your
 # helper", which is what it did on home leave when nothing told it otherwise.
 ("her questions are about her, not about somebody she employs",
  ("IS the passport holder" in tpl.HELPER_OWN_PASSPORT_NOTE,
   "never \"your\nhelper's\"" in tpl.HELPER_OWN_PASSPORT_NOTE
   or "never \"your" in tpl.HELPER_OWN_PASSPORT_NOTE),
  (True, True)),
 # The briefing is built from rows written to the EMPLOYER, so "a copy of your
 # NRIC" in them means HIS. Sent to her unchanged, the list asks a Work Permit
 # holder for an NRIC - the 2026-09-17 contradiction arriving from the other
 # direction.
 ("the briefing assigns the documents to the right person",
  [w for w in ("employer's NRIC", "Work Permit")
   if w not in tpl.HELPER_PASSPORT_BRIEFING_NOTE], []),
 # ...and it does not quietly downgrade her. Same timing, same fee, same steps
 # - the agency asked for "process, documents, timeline, fees accordingly".
 ("...and gives her the same timing, fee and steps as anybody else",
  "same timing, the same fee" in _flat(tpl.HELPER_PASSPORT_BRIEFING_NOTE), True),
 # The refusal did not go away, it moved onto evidence. Its three prohibitions
 # are unchanged and are asserted above; what changed is WHEN it fires.
 ("the refusal now names the three embassies it is measured against",
  [c for c in ("Philippines", "Indonesia", "Myanmar")
   if c not in tpl.OWN_PASSPORT_NOTE], []),

 # --- 2026-09-17: the WhatsApp profile name, everywhere ----------------
 # Live, on the first two messages of a conversation:
 #   client: Hello
 #   Claire: Hi Vaidik, I'm Claire, Ming Hwee's AI assistant. How can I help?
 #   client: I want to hire a helper
 #   Claire: I'll ask a few details ... May I know your name?
 # The suppression existed, but inside `info_collector` and gated on
 # `service_type in NAME_FROM_RECORD_ONLY`. A greeting turn has no service and
 # does not reach that node, so neither test could fire. Moved to the one place
 # the name ENTERS the prompt, and asserted there.
 ("a number we hold no name for puts no name in the prompt",
  "Vaidik" in _sys._contact_block(
      {"contact_type": "unknown", "customer_name": "Vaidik", "record_name": ""}),
  False),
 ("...and the profile label is not mentioned in order to forbid it either",
  [w for w in ("WhatsApp name", "profile")
   if w in _sys._contact_block(
       {"contact_type": "unknown", "customer_name": "Vaidik", "record_name": ""})],
  []),
 # The other half, which is the 2026-09-09 warmth fix and must not regress: a
 # client whose name we DO hold is greeted by it.
 ("a name we hold IS in the prompt",
  "Ratna Choukade" in _sys._contact_block(
      {"contact_type": "employer", "customer_name": "Vaidik",
       "record_name": "Ratna Choukade"}),
  True),
 ("...and it is the record name, never the profile label, that gets used",
  "Vaidik" in _sys._contact_block(
      {"contact_type": "employer", "customer_name": "Vaidik",
       "record_name": "Ratna Choukade"}),
  False),
 # ONE decision, not one per node. The whole defect was two nodes disagreeing
 # about whether the push name is the client's name (§9.8, applied to a policy
 # rather than a constant), so no reply-writing node may carry its own copy.
 ("no node blanks the push name for itself any more",
  sorted(f.name for f in (_ROOT / "app" / "graph" / "nodes").glob("*.py")
         if 'customer_name"] = ""' in f.read_text(encoding="utf-8")), []),
 # --- 2026-09-17: the salary floor -----------------------------------
 # Circled in the agency's own screenshot: a client who had said they wanted a
 # FILIPINO helper was asked "Do you have a monthly salary budget in mind, such
 # as SGD 500-600 or SGD 600-700?" - two bands at or below the S$650 a Filipino
 # helper cannot be placed under. The question invited a budget no placement
 # could be made at.
 ("nothing known -> every band is still offered",
  ico._effective_options(_budget_field, {}), _budget_field.options),
 ("a Filipino helper -> no band below the S$650 floor",
  [o for o in ico._effective_options(_budget_field,
                                     {"preferred_nationality": "Filipino"})
   if (lambda b: b[1] is not None and b[1] <= 650)(ico._band_bounds(o))], []),
 ("...and the band that straddles the floor starts AT it, not above it",
  ico._effective_options(_budget_field, {"preferred_nationality": "Filipino"})[0],
  "SGD 650-700"),
 ("...and 'not sure yet' carries no figure, so it always survives",
  "not sure yet" in ico._effective_options(
      _budget_field, {"preferred_nationality": "Filipino"}), True),
 # The helper's OWN nationality narrows it too - a direct hire states her
 # nationality rather than a preference, and it is the same fact.
 ("her own nationality narrows it the same way",
  ico._effective_options(_budget_field, {"nationality": "Philippines"}),
  ico._effective_options(_budget_field, {"preferred_nationality": "Filipino"})),
 # We hold a floor for the Philippines and for nothing else. Indonesia and
 # Myanmar keep every band, because the agency gave figures for one country
 # and inferring the other two from it is the mistake section 9 records twice
 # for Myanmar already.
 ("a nationality we hold no floor for is untouched",
  [n for n in ("Indonesian", "Myanmar", "no preference")
   if ico._effective_options(_budget_field, {"preferred_nationality": n})
   != _budget_field.options], []),
 ("...and the floor table names exactly one country, on purpose",
  sorted(ico._SALARY_FLOOR_BY_NATIONALITY), ["PH"]),
 # Only `budget` is filtered. A filter that reached any field with digits in
 # its options would quietly edit the languages list or the home types.
 ("no other field's options are touched",
  [f.key for svc in t.SERVICE_FIELDS for f in t.SERVICE_FIELDS[svc]
   if f.key != "budget" and f.options
   and ico._effective_options(f, {"preferred_nationality": "Filipino"}) != f.options],
  []),
 # THE important one. `_field_guidance` builds the question and
 # `grounded_options` tells ungrounded_figures which figures the reply may
 # contain. If those two read different lists the model is instructed to offer
 # a figure and then binned for offering it, which is the 2026-09-09 (D)
 # defect. Asserted on the guidance TEXT, so the two cannot drift apart
 # without this going red.
 ("the question a Filipino client is asked names no sub-floor figure",
  [d for d in ("500", "600-700", "below")
   if d in ico._field_guidance("new_hiring",
                               {"preferred_nationality": "Filipino"},
                               _budget_field)], []),
 ("...and it does name the floor",
  "650" in ico._field_guidance("new_hiring",
                               {"preferred_nationality": "Filipino"},
                               _budget_field), True),
 ("...while a client with no preference still sees the full range",
  "below SGD 500" in ico._field_guidance("new_hiring", {}, _budget_field), True),
 # The helper's side of the pairing is NOT narrowed. Her `expected_salary`
 # takes the employer's bands through _matched_options, which reads the static
 # options, so a Filipino applicant is still asked across the whole range.
 ("the helper's own salary question keeps every band",
  t._matched_options("budget"), _budget_field.options),

 # --- 2026-09-17: the Filipino salary figures --------------------------
 # The agency gave them; three live rows contradicted them in BOTH directions,
 # saying a Filipino helper "starts at S$570-650" (below the floor) and that an
 # experienced one is "S$700-850+" (above where she starts from).
 ("the corrected sentence states the agency's two figures",
  [d for d in ("S$650", "S$670")
   if not any(d in r["new"] for r in _REPLACEMENTS)], []),
 ("...and no longer states the figure below the floor",
  [r["new"] for r in _REPLACEMENTS if "S$570" in r["new"]], []),
 # TWO needles for ONE fact, because the sentence is written two ways in the
 # live rows. Measured: one needle corrects two rows of three and leaves the
 # third contradicting the pair.
 # TWO needles for ONE fact, because the live rows write the same sentence two
 # different ways. Counted through the CORRECTED text rather than by naming
 # the old strings: retyping a needle here is what the sweep below is for, and
 # the first version of this block spelled one out and was caught by it - the
 # same way the phone-number sweep caught itself on 2026-09-11.
 # THREE needles for one fact, because the live rows write the Filipino
 # salary three different ways - two phrasings of the embassy-minimums
 # sentence, and a third inside a nationality comparison filed under
 # new_hiring. Found by SWEEPING for the old figure after the first two were
 # corrected, which is the only reason the third is here at all.
 ("every way the old floor is written is corrected",
  sum(1 for r in _REPLACEMENTS if "S$650" in r["new"]), 3),
 # ...and each of those rules is actually LOOKING for the old floor. The first
 # version of this check counted the corrected text instead, so blanking a
 # needle outright left it green - the rule still installed S$650, at a string
 # that no longer existed anywhere. Counted through `old`, which is the half
 # that has to match something.
 ("...and each of them is looking for the figure it replaces",
  [r["new"][:45] for r in _REPLACEMENTS
   if "S$650" in r["new"] and "S$570" not in r["old"]], []),
 ("...and they are distinct needles, not one rule written three times",
  len({r["old"] for r in _REPLACEMENTS if "S$650" in r["new"]}), 3),
 ("Indonesia and Myanmar are left alone in the same sentence",
  [n for n in ("Indonesian", "Myanmar")
   if any(n in r["old"] for r in _REPLACEMENTS)], []),
 ("the salary row states the floor and the experienced starting point",
  [d for d in ("S$650", "S$670")
   if not any(d in r["answer"] for r in lsn.ROWS
              if r["section_heading"] == "Employer - Filipino helper salary")], []),
 # It must not read as a price for an experienced helper. The agency was
 # explicit that figure "varies based on experience".
 ("...and says the experienced figure is not fixed",
  any("rather than fixed in advance" in r["answer"] for r in lsn.ROWS
      if r["section_heading"] == "Employer - Filipino helper salary"), True),

 # --- 2026-09-17: the overview says a different thing per service ------
 # "a short, well-defined job we handle end to end" is true of a permit
 # renewal and false of a 25-question first-time hire.
 ("the two hiring flows are not called a short, well-defined job",
  ico._SMALL_TICKET_SERVICES & {"new_hiring", "direct_hiring"}, set()),
 ("...but they are in the set that explains itself up front",
  {"new_hiring", "direct_hiring"} <= ico._OVERVIEW_AT_START, True),
 # The agency's flow puts Cost/Fee straight after Process & Timeline. On these
 # two their own 2026-09-04 instruction forbids a price outright, and
 # quotes_hiring_package_cost enforces it whatever the prompt says - so the
 # overview is told not to spend its one sentence on a figure that is about to
 # be swapped for the deferral line.
 ("a cost-withheld overview is told not to quote a price",
  [k for k in ("new_hiring", "direct_hiring") if k not in _guards.COST_WITHHELD_SERVICES],
  []),
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
 # Widened on 2026-09-10 from the seven to EVERY service there is. The seven
 # are the agency's employer-facing list, so a candidate flow was outside the
 # sweep entirely - the same "written for the set that was reported" shape this
 # row was created to fix, one level up. Measured before widening: only
 # expected_salary is new, and it is named above.
 ("no field on any service reads a bracket out",
  sorted({f.key for fs in t.SERVICE_FIELDS.values() for f in fs
          if f.key not in _DIGITS_ON_PURPOSE and _reads_a_bracket(f)}), []),
 ("and the two that carry digits still do so deliberately",
  sorted({f.key for svc in SEVEN_SERVICES for f in t.SERVICE_FIELDS.get(svc, [])
          if f.key in _DIGITS_ON_PURPOSE}),
  ["budget", "start_timeline"]),
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
 # The VALUE here was corrected on 2026-09-18 and the claim was not. This
 # asserts that naming another service is still a real switch - any truthy
 # answer satisfies that - and it pinned "renewal", which is what
 # `_named_service` returned rather than what is true: "renew my helper
 # PASSPORT" is a passport renewal. The ordering bug was recorded here as the
 # expected value, so the tripwire held the defect in place instead of
 # catching it. It goes red on the fix, which is what it is for.
 ("naming another service is still a real switch",
  _named_svc("i also want to renew my helper passport"), "passport_renewal"),
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
 # `helper_religion` and `religion` joined on 2026-09-17. Religion is the
 # most personal thing either side is asked for, so it explains itself for
 # exactly the reason the other three do.
 ("the intrusive questions say why they are asked",
  sorted(ico._WHY_WE_ASK),
  ["additional_notes", "helper_religion", "pets", "religion", "rest_day"]),
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
 # Was "every other flow still uses the push name", asserting Vaidik.
 # new_hiring was the last flow that did, and on 2026-09-16 the agency
 # asked for it to stop, so the control is now the same both ways.
 ("new hiring asks a new number for it, rather than reading the profile",
  ico._known_fields({"customer_name": "Vaidik"},
                    "new_hiring").get("full_name"), None),
 ("...and greets a client whose name is on our file",
  ico._known_fields({"customer_name": "Vaidik", "record_name": "Vaidik Dubey"},
                    "new_hiring").get("full_name"), "Vaidik Dubey"),
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
 # Four since 2026-09-17. The agency: "For services such as Work Permit
 # Renewal, the service is not limited to existing agency clients ... the bot
 # should identify whether the enquiry is for an existing client/helper, or a
 # new client/helper, in which case the bot should collect the required
 # information and determine whether a new case/file needs to be opened."
 ("a work permit renewal is four questions now",
  [f.key for f in t.SERVICE_FIELDS["renewal"]],
  ["full_name", "helper_name", "helper_from_us", "permit_expiry"]),
 # ...and it is the SAME object `replacement` asks, not a second copy of the
 # same question that can be reworded in one place and not the other (9.8).
 ("...asking the question replacement already asks, not a copy of it",
  _rn.get("helper_from_us") is not None
  and _rn.get("helper_from_us") is _rp.get("helper_from_us"), True),
 # And it is NOT the banned question. "Have you hired with us before?" is
 # answered from `placements` and never put to anyone (2026-09-04); this asks
 # where one particular helper came from, which no record answers.
 ("...and it does not ask whether they have hired with us before",
  any("hired with us" in (f.question or "").lower()
      or "hired a helper before" in (f.question or "").lower()
      for f in t.SERVICE_FIELDS["renewal"]), False),
 # --- 2026-09-17: ...and then read from the records instead of asked ---
 # The agency watched it go out live and said it should never be asked at all:
 # "if the user exists then this question didn't come, and if the user is new
 # then also this message should not, because if the user is new it means the
 # work permit is not from Ming Hwee."
 #
 # A zero placement count is not an absence of evidence, it IS the answer - we
 # cannot have placed this helper with an employer we have never placed anyone
 # with. Same reading `first_time_hire` has taken since 2026-09-04, and the
 # same accepted cost: somebody who hired through us on a different number
 # reads as "no placement on record", which is a statement about our RECORDS
 # rather than about them.
 ("a client with no placement is never asked where the helper came from",
  [k for k, v in _RECORD_SHAPES.items()
   if not v.get("prior_hires") and "hired elsewhere" not in _from_us(v)], []),
 # ...and one whose single placement names her is not asked either.
 ("...nor one whose placement we can actually name",
  _from_us(_RECORD_SHAPES["one placement, and it names her"]),
  "from Ming Hwee - placed by us"),
 # The middle case, and the half of the old note that is still true:
 # `get_placed_helper` returns nothing unless there is exactly ONE live
 # placement naming a candidate, and live only 2 of 6 rows did. So this one
 # reports what our records hold and claims nothing - putting a guess about
 # which of four helpers she is onto a ticket is the failure `get_placed_helper`
 # was made cautious to avoid.
 ("...and one we cannot match says so rather than claiming her",
  [k for k, v in _RECORD_SHAPES.items()
   if v.get("prior_hires", 0) > 0 and not v.get("placed_helper")
   and "not matched on file" not in _from_us(v)], []),
 # The one that matters most, and it is derived over the flows rather than
 # written about `renewal`: the question can never come due on ANY record
 # shape, on any flow that defines it. A branch that returns "" here puts the
 # question back in front of a client.
 ("no record shape leaves the question to be asked, on any flow that has it",
  [f"{svc}: {k}" for svc in _ASKS_FROM_US for k, v in _RECORD_SHAPES.items()
   if not _from_us(v, svc)], []),
 ("...and both flows that ask it are still covered",
  _ASKS_FROM_US, ["renewal", "replacement"]),
 # These values are grounding for `ungrounded_figures` (they reach the prompt
 # as collected_info), so a count in one of them is a number the model may
 # quote back at the client - the 2026-09-09 (D) shape. `first_time_hire` says
 # "2 placements on record" and gets away with it; this one carries no digit.
 ("...and no branch of it carries a figure",
  [k for k, v in _RECORD_SHAPES.items() if re.search(r"\d", _from_us(v))], []),
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
  all(any("agent will confirm" in r["answer"] for r in lsn.ROWS
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
 # And the default is still 'all', so a row only ever narrows on purpose.
 # Stated as a RULE now rather than as "everything except the transfer
 # checklist": the helper's own journey rows added on 2026-09-10 are the
 # second set to narrow, and a list of the exceptions would have to be
 # edited every time - which is how a tripwire stops being read. Every
 # narrowed row must declare its audience in its own section heading, so
 # the label and the text cannot drift apart.
 ("a row that narrows its audience says so in its heading",
  sorted({r["section_heading"] for r in lsn.ROWS
          if r.get("contact_type", "all") != "all"
          and not r["section_heading"].startswith(
              ("Transfer - ", "Helper - ", "Employer - "))}),
  []),
 ("the transfer checklist is for employers, the journey rows for helpers",
  {(r["section_heading"].split(" - ")[0], r.get("contact_type", "all"))
   for r in lsn.ROWS if r.get("contact_type", "all") != "all"},
  {("Transfer", "employer"), ("Helper", "candidate"),
   ("Employer", "employer")}),
 # The helper's journey is filed `general` DELIBERATELY, and that is what
 # makes a routing change unnecessary: candidate_new_hiring is not a
 # service_type any row uses, so _labelled_filter narrows her to 'general'
 # forever (the section 9.15 shape) - and 'general' is exactly where these
 # are. Filed under new_hiring instead, she would never see them.
 ("the helper's journey is reachable from a service the KB never labels",
  sorted({r["service_type"] for r in lsn.ROWS
          if r["section_heading"].startswith("Helper - ")}), ["general"]),
 # ...and it never tells her what she pays. There is no helper-side fee
 # policy in the knowledge base, and a placement fee is the one figure a
 # job seeker acts on, so it is Ming Hwee's to give rather than something
 # to reason out of the employer's price list (CLAUDE.md section 9).
 ("and it quotes her no fee",
  [r["question"] for r in lsn.ROWS
   if r["section_heading"].startswith("Helper - ")
   and any(w in r["answer"].lower()
           for w in ("agency fee", "placement fee", "you pay us",
                     "service fee", "deducted from your salary"))], []),

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
  [j for j in ("917970027379@s.whatsapp.net", "6565342277@c.us", "917970027379")
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
                    "text": {"body": "hi"}, "from": "6565342277@s.whatsapp.net",
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
                    "text": {"body": "hi"}, "from": "6565342277@s.whatsapp.net",
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
 # REVERSED 2026-09-16. new_hiring was the last employer flow reading the
 # name off WhatsApp, and the agency asked for it to stop: "why chatbot
 # is not asking the user name like before it is again picking name
 # automatically". The old assertion asserted the opposite and is kept
 # inverted rather than deleted, because it was right for its day.
 ("new_hiring asks for the name too, as of 2026-09-16",
  "new_hiring" in t.NAME_FROM_RECORD_ONLY, True),
 # ...and with it the set is now every flow that collects a name at all,
 # so this stops being a list and becomes the rule. A flow added
 # tomorrow with a full_name field and no entry here fails by name.
 ("no flow anywhere takes the client's own name off their WhatsApp profile",
  sorted(svc for svc, fl in t.SERVICE_FIELDS.items()
         if any(f.key == "full_name" for f in fl)
         and svc not in t.NAME_FROM_RECORD_ONLY), []),

 # --- the candidate half of the matching form, 2026-09-10 -------------
 # Tested as a job seeker: "bot didnt ask the name at first like all services
 # then it should greet after taking name ... also it didnt ask for the age
 # and any other question that are needed it end the conversation by taking
 # few details".
 #
 # The name half is the SAME defect as the four employer complaints, and the
 # derived rule above could not catch it because it sweeps
 # EMPLOYER_LEAD_SERVICES - a candidate flow is not in that set. So the rule
 # is stated from the other side too: a lead-producing intake never puts a
 # WhatsApp profile label on the record it is opening.
 ("a candidate is asked her name rather than read off WhatsApp",
  sorted(svc for svc in _lead.CANDIDATE_LEAD_SERVICES
         if svc not in t.NAME_FROM_RECORD_ONLY), []),
 ("and it is still the first thing asked",
  [f.key for f in t.SERVICE_FIELDS["candidate_new_hiring"]][:1], ["full_name"]),
 # The complaint named age outright. It is a `candidates` column and the
 # employer is asked an age preference on every hiring enquiry.
 ("and her age is asked",
  "age" in [f.key for f in t.SERVICE_FIELDS["candidate_new_hiring"]], True),

 # THE RULE, not a list of the fields that were missing: every question the
 # employer is asked ABOUT a helper has a counterpart the helper is asked
 # about herself, or a consultant holding both tickets matches them by eye.
 # Nine of these eleven had no counterpart at all before 2026-09-10, which is
 # why a nine-question registration reached the desk saying her country, her
 # work scope and her years.
 ("every employer question about a helper has a candidate counterpart",
  sorted(emp for emp, cand in _MATCHED_PAIRS.items()
         if cand not in {f.key for f in t.SERVICE_FIELDS["candidate_new_hiring"]}),
  []),
 # ...and both halves offer the SAME words. "eldercare" against "caring for
 # the elderly", or "$600-700" against "600 to 700 dollars", is a match made
 # by eye - which is what the note on work_scope has said since it was
 # written. Taken from the employer Field rather than retyped, so this cannot
 # drift the way a copied constant does (section 9.8).
 # `preferred_nationality` is the one pairing whose two halves CANNOT share an
 # option list, and it is named rather than quietly skipped. The employer picks
 # from the three nationalities we place plus "no preference"; a helper states
 # the country she is actually from, and constraining her to those three would
 # turn a Sri Lankan or Cambodian applicant away at the first question - while
 # "no preference" is not a thing anyone can be.
 # `duties_willing` answers TWO employer questions, so it carries cooking on
 # top of the extra duties rather than the same list - the test is that the
 # employer's options are all present, not that the two lists are identical.
 ("and both halves of a pairing offer the same options",
  sorted(emp for emp, cand in _MATCHED_PAIRS.items()
         if emp not in ("preferred_nationality", "helper_religion")
         and _options_of("new_hiring", emp)
         and not set(_options_of("new_hiring", emp))
                 <= set(_options_of("candidate_new_hiring", cand))),
  []),
 # `helper_religion` is the SECOND pairing whose halves cannot share one list,
 # and it is held to a tighter rule than a bare exception rather than simply
 # skipped: her list must be his list with "no preference" removed, and nothing
 # else. So a religion added to one side and not the other still fails here,
 # which is the whole job of this table.
 ("...and the religion pair differs by exactly 'no preference'",
  set(_options_of("new_hiring", "helper_religion")) - {"no preference"},
  set(_options_of("candidate_new_hiring", "religion"))),
 # A question only one answer can fit is not offered "or more than one".
 # Live 2026-09-17: "may I know your religion, such as Muslim, Christian,
 # Catholic, Hindu, Buddhist, another faith, OR MORE THAN ONE". That clause is
 # `_field_guidance` doing as it is told, and it is right for `languages` and
 # `requirement` and wrong for a person's own faith.
 ("her religion is the one field a single answer has to fit",
  sorted(f.key for svc in t.SERVICE_FIELDS for f in t.SERVICE_FIELDS[svc]
         if not f.multiple_answers), ["religion"]),
 # Checked through the guidance the model is HANDED, not on the flag, and both
 # halves separately: the "more than one" invitation goes, the "something not
 # on the list" invitation stays. Dropping both would be
 # `options_are_exhaustive`, which is still only ever her country - a helper
 # whose faith is not one of the five still has to be able to say so.
 ("...so hers does not invite more than one",
  "may give more than one" in _guidance_for("candidate_new_hiring", "religion"),
  False),
 ("...but still lets her give one that is not listed",
  "not on the list" in _guidance_for("candidate_new_hiring", "religion"), True),
 # His DOES take more than one - "Muslim or Christian is fine" is a real
 # preference - and so do the option sets this clause was written for.
 ("the employer's preference still takes more than one",
  "may give more than one" in _guidance_for("new_hiring", "helper_religion"), True),
 ("...and so does languages, which is why the clause exists",
  "may give more than one" in _guidance_for("new_hiring", "languages"), True),
 ("...with 'no preference' on his side only, since nobody can BE it",
  ("no preference" in _options_of("new_hiring", "helper_religion"),
   "no preference" in _options_of("candidate_new_hiring", "religion")),
  (True, False)),
 # Both halves name their options IN the written question, so `_field_guidance`
 # takes its "name them all" branch. With six options and none of them named, a
 # Hindu household shown "Muslim or Christian" as examples reads that as the
 # whole of what we place - the 2026-09-07 languages defect.
 ("the religion question names every option it offers",
  [o for o in _options_of("new_hiring", "helper_religion")
   if o.lower() not in _question_of("new_hiring", "helper_religion").lower()], []),
 ("...and so does hers",
  [o for o in _options_of("candidate_new_hiring", "religion")
   if o.lower() not in _question_of("candidate_new_hiring", "religion").lower()], []),
 # It replaced the pork/beef question rather than joining it, which is what the
 # agency asked for once the trade was put to them. Asserted on BOTH sides and
 # on the OPTIONS as well as the question: `no pork` and `no beef` were in the
 # employer's option list, and `_field_guidance` reads options into the spoken
 # question, so leaving them would have half-asked the question that was
 # removed.
 ("neither cooking question asks about pork or beef any more",
  [svc for svc, key in (("new_hiring", "cooking"),
                        ("transfer_employer", "cooking"),
                        ("candidate_new_hiring", "cooking_ability"))
   if "pork" in _question_of(svc, key).lower()
   or "beef" in _question_of(svc, key).lower()], []),
 ("...nor offers it as an option",
  sorted({o for svc, key in (("new_hiring", "cooking"),
                             ("candidate_new_hiring", "cooking_ability"))
          for o in _options_of(svc, key)
          if "pork" in o.lower() or "beef" in o.lower()}), []),
 # `halal kitchen` and `vegetarian` deliberately STAY. Those describe the
 # client's own kitchen, which is a cooking requirement; they are not a
 # question about anybody's faith.
 ("...while the kitchen requirements stay, because they are about the kitchen",
  [o for o in ("halal kitchen", "vegetarian")
   if o not in _options_of("new_hiring", "cooking")], []),
 # And the employer's religion question is asked on both flows that choose a
 # helper. A direct hire is deliberately out: the employer has already chosen
 # her, so a preference is not a question anyone can act on.
 ("the religion preference is asked wherever a helper is still being chosen",
  [svc for svc in ("new_hiring", "transfer_employer")
   if "helper_religion" not in {f.key for f in t.SERVICE_FIELDS[svc]}], []),
 ("...and not where the helper is already chosen",
  "helper_religion" in {f.key for f in t.SERVICE_FIELDS["direct_hiring"]}, False),
 # ...and cooking is on that list by name, which is the agency's own wording:
 # "Would you be willing to do other duties such as cooking, high-rise window
 # cleaning, car washing, gardening, grocery shopping, or hand-washing
 # laundry?"
 ("cooking is one of the duties she is asked about, not a question of its own",
  "cooking" in _question_of("candidate_new_hiring", "duties_willing").lower(),
  True),
 # The detail question still exists for the helpers it applies to - an
 # employer IS asked whether she must handle pork or beef, and that has to be
 # matchable - but it is gated, so a helper who never mentions cooking is
 # never asked it. That is the whole complaint.
 # Gated on `work_scope`, whose options are a controlled vocabulary, and NOT
 # on the duties answer - that was tried and measured: Gate matches on
 # substrings, so "no i dont want to cook, only the window cleaning" contains
 # "cook" and opened it, which is the complaint again in writing.
 ("and the cooking detail is gated on the work she says she does",
  (lambda f: f is not None and f.gate is not None
   and f.gate.field == "work_scope")(
      next((f for f in t.SERVICE_FIELDS["candidate_new_hiring"]
            if f.key == "cooking_ability"), None)), True),
 ("so a childcare-and-eldercare helper is never asked about cooking",
  [f.key for f in t.missing_fields(
      "candidate_new_hiring", {"work_scope": "childcare and eldercare"})
   if f.key == "cooking_ability"], []),
 ("...nor a childcare-only one",
  [f.key for f in t.missing_fields(
      "candidate_new_hiring", {"work_scope": "childcare"})
   if f.key == "cooking_ability"], []),
 ("but a helper who says she cooks still is",
  sorted({w for w in ("general housework and cooking", "all of the above")
          if "cooking_ability" not in [f.key for f in t.missing_fields(
              "candidate_new_hiring", {"work_scope": w})]}), []),

 # --- the candidate's closing briefing, 2026-09-10 ---------------------
 # "the bot did not explain the next steps/process to the candidate." An
 # employer finishing a passport renewal is told what happens next; a helper
 # who had just answered seventeen questions was thanked and handed over.
 ("a registration explains what happens next before handing over",
  t.BRIEFING_AFTER.get("candidate_new_hiring"), "availability"),
 # .get() rather than [] on purpose: with the entry removed this has to FAIL
 # and name itself, not raise a KeyError and take the whole run down - a check
 # that crashes tells you less than one that goes red.
 ("keyed on a field that is always answered",
  next((f.optional for f in t.SERVICE_FIELDS["candidate_new_hiring"]
        if f.key == t.BRIEFING_AFTER.get("candidate_new_hiring")), None), False),
 # SERVICE_BRIEFING_NOTE is written for somebody BUYING a service and
 # REQUIRES a cost section. Pointed at a registration it did as it was told
 # and quoted the passport renewal's $450 as the price of applying for work;
 # ungrounded_figures binned the reply and it was logged as a lost briefing,
 # so she got the bare handover line. A job seeker gets her own note.
 ("a job seeker's closing message is not the one written for a buyer",
  tpl.CANDIDATE_BRIEFING_NOTE != tpl.SERVICE_BRIEFING_NOTE, True),
 ("and it forbids quoting her any figure at all",
  all(p in tpl.CANDIDATE_BRIEFING_NOTE
      for p in ("NEVER quote her a fee", "not what she might earn")), True),
 ("nor promising her a job or a date",
  "Do not promise her a job" in tpl.CANDIDATE_BRIEFING_NOTE, True),
 # "heading line" until 2026-09-10, when that line was given a second job -
 # saying her registration is finished - and renamed; "closing sentence" until
 # 2026-09-11, when the agency asked for a second closing sentence offering
 # further help and it became "TWO short sentences". The tripwire has now fired
 # twice for the right reason; the three things it guards are unchanged.
 ("and it still says what the list is, one step per line",
  all(p in tpl.CANDIDATE_BRIEFING_NOTE
      for p in ("opening line", "REAL LINE BREAK", "TWO short sentences")), True),
 # The second of those two, asked for by name: "If you need any further help or
 # have any questions, please let us know." It is rule 2's standing offer and
 # this message IS a handover, so it belongs - but it must not reopen the
 # collection, which is the 2026-09-09 "would you like to go ahead?" defect.
 ("and it offers further help without reopening anything",
  ("glad to help" in _flat(tpl.CANDIDATE_BRIEFING_NOTE),
   "do not ask her for any more details" in _flat(tpl.CANDIDATE_BRIEFING_NOTE)),
  (True, True)),
 ("the buyer's note still requires the cost it was written for",
  "WHAT IT COSTS" in tpl.SERVICE_BRIEFING_NOTE, True),
 # The query is the other half, and the word `cost` is absent from it on
 # purpose: "how much does it cost" matches _PRICE_QUESTION, which DROPS the
 # service filter - which is how the passport renewal's $450 reached a
 # registration in the first place.
 ("her briefing query does not go looking for a price",
  [w for w in ("cost", "price", "fee", "how much")
   if w in rr.CANDIDATE_BRIEFING_QUERY.lower()], []),
 ("and it asks for her journey instead",
  all(w in rr.CANDIDATE_BRIEFING_QUERY
      for w in ("after I register", "interview", "arrive")), True),
 ("the buyer's query still asks the price",
  "how much does it cost" in rr.BRIEFING_QUERY, True),

 # --- what she is told AFTER the handover, 2026-09-10 ------------------
 # Live, a job seeker whose registration was parked: "Tell me the documents I
 # needed" -> "I'll check with the team and come back to you shortly.", and two
 # messages later "No i ask for what are the documents I required" -> the full
 # correct answer, from records that had been there the whole time. Her words:
 # "if the bot knows the documents required, then why didn't it tell me when I
 # said tell me the documents I needed".
 #
 # Measured: BOTH phrasings were False on BOTH detectors. The one turn that
 # worked was the classifier happening to return document_question, and the
 # deterministic net underneath it - the entire reason that net exists - caught
 # neither. Asserted as the PAIR, because the two disagreeing about the same
 # sentence is what let this through: one decides whether a parked topic
 # answers at all, the other whether the answer may be a numbered list, and she
 # needed both.
 ("the way she actually asked for her documents reaches both detectors",
  [m for m in ("Tell me the documents I needed",
               "No i ask for what are the documents I required",
               "what documents are required",
               "which forms do i sign",
               "what is the paperwork",
               "papers I have to provide")
   if not (_docs_q(m) and _proc_q(m) and _asks_general(m))], []),
 # ...and the two callers read ONE definition rather than a copy each (9.8).
 ("one definition, read by both paths",
  ("asks_for_documents" in _BTR_SRC, "_DOCUMENTS_QUESTION" in _BTR_SRC),
  (True, False)),
 # A statement about documents is not a question about them. A false positive
 # here only widens a sentence budget, but a rule that fires on everything has
 # stopped being a rule.
 ("but telling us about documents is not asking for them",
  [m for m in ("I will send the documents tomorrow",
               "the documents are ready",
               "I already gave you my passport",
               "ok noted thanks")
   if _docs_q(m) or _asks_general(m)], []),
 # The PLURAL. "(?:fee|cost|charge)\b" cannot match "fees" - there is no word
 # boundary inside it - so the singular was answered and the plural was handed
 # to a human. Third time a one-word gap in this pattern has cost a client an
 # answer: "what is THE cost" (2026-09-08), "what is the FURTHER process"
 # (2026-09-10), and now this.
 ("a fee question survives being asked in the plural",
  [m for m in ("Is there any fees I need to pay",
               "is there any fee i need to pay",
               "do i have to pay any fees",
               "what fees do i need to pay",
               "will i have to pay anything",
               "Ok what is cost")
   if not _asks_general(m)], []),
 # ...and a chase is still a chase, which is what gates the whole thing.
 ("chasing us about money is still chasing, not a question",
  [m for m in ("any update on my payment",
               "any update on my application")
   if _asks_general(m)], []),

 # --- the third path that writes a reply, 2026-09-10 -------------------
 # A new hire's price is never quoted before a salesperson has spoken to the
 # client. That held on info_collector and response_generator and NOT on
 # blocked_topic_responder - the path used once a topic is parked, i.e. exactly
 # when a consultant already has it. Live, with a hiring ticket parked, "Is
 # there any fees I need to pay" returned "The approximate total service fee
 # and third-party costs are $4,225, with a combined total of about $4,285".
 # Every other guard passed it, correctly: those figures ARE in Form A, so
 # ungrounded_figures waves them through. Grounded is not sanctioned.
 #
 # Derived, not a list of three: a node that runs ungrounded_figures over a
 # generated reply is by definition a node that sends one, so it must apply
 # this guard too. A fourth reply-writing path cannot reopen the gap.
 ("every path that grounds a reply also refuses to price the hire",
  sorted(n for n, m in _NODES.items()
         if hasattr(m, "ungrounded_figures")
         and not hasattr(m, "quotes_hiring_package_cost")), []),
 ("and there are three such paths, not two",
  sorted(n for n, m in _NODES.items() if hasattr(m, "ungrounded_figures")),
  ["blocked_topic_responder", "info_collector", "response_generator"]),
 ("the guard still fires on the figure that reached a client",
  q("The approximate total service fee and third-party costs are "
    f"{D}4,225, with a combined total of about {D}4,285."), True),
 ("and new hiring is a service that withholds it",
  "new_hiring" in _WITHHELD, True),
 # Not withheld, deliberately: passport renewal states its own $450 and
 # quoting it is the point of that flow.
 ("a service that states its own fee still states it",
  "passport_renewal" in _WITHHELD, False),

 # --- what a HELPER pays us, answered by the agency 2026-09-10 ---------
 # This was an open item in section 9 ("What a HELPER pays us, if anything")
 # and the agency closed it: "there is no fees, the candidate does not have to
 # pay any fees for this". Before the row, a job seeker asking it retrieved the
 # EMPLOYER's direct-hire cost comparison at 0.488; after _retrieval_audience
 # shut that shelf she got a handover instead.
 ("a job seeker is told outright that she pays us nothing",
  bool(_FEE_ROW), True),
 ("and she is told it on her own shelf, where an employer cannot read it",
  (_FEE_ROW.get("contact_type"), _FEE_ROW.get("service_type")),
  ("candidate", "general")),
 # No figure, for the reason CANDIDATE_BRIEFING_NOTE forbids one: there is no
 # helper-side amount anywhere, so every number in reach belongs to somebody
 # else's price list.
 ("and no figure appears in it",
  re.findall(r"\d", _FEE_ROW.get("answer", "")), []),
 # The care that makes this row safe. "27.1a Your Placement Loan" is
 # contact_type='candidate' and tells her money is often taken from her salary
 # by an agency in her home country - and lists "Ming Hwee?" among the possible
 # creditors. A flat "there are no fees at all" would contradict a row she can
 # also retrieve, which is 9.14 in a new place. This row says what the agency
 # said - she pays US nothing - and sends a loan question to a consultant,
 # which is what the loan row itself instructs.
 ("it does not deny the placement loan it cannot speak for",
  ("loan" in _FEE_ROW.get("answer", "").lower(),
   "agent" in _FEE_ROW.get("answer", "").lower()),
  (True, True)),

 # --- the closing message says the registration is finished, 2026-09-10 -
 # The last question was "Would you prefer updates by email or here on
 # WhatsApp?", she answered "Here", and the next thing she read was a numbered
 # list of the hiring process. She wrote back "Here I mean WhatsApp why did you
 # tell the process" - nothing in the message said her registration was done,
 # so a list of steps on the back of a one-word answer read as the bot having
 # misunderstood her. It then APOLOGISED and disowned its own correct closing
 # message, which is the half a prompt cannot be trusted to fix; saying the
 # registration is complete stops the question being asked at all.
 ("her closing message says that is everything we need",
  "everything we need from her" in _flat(tpl.CANDIDATE_BRIEFING_NOTE), True),
 ("and it still leads the list with a line saying what it is",
  "say what this message is" in _flat(tpl.CANDIDATE_BRIEFING_NOTE), True),

 # --- only three countries, 2026-09-11 ---------------------------------
 # Agency: "The bot should only proceed with the hiring flow if the candidate
 # is from one of these three countries: Myanmar, Indonesia, Philippines. If
 # the candidate provides any other country, the bot should clearly respond
 # that we only help candidates from these three countries."
 ("a helper from a country we place is taken through the flow",
  [v for v in ("Indonesia", "indonesian", "i am from indonesia", "Indo",
               "Philippines", "philippines", "filipina", "Filipino",
               "Pilipinas", "Myanmar", "burmese", "from Burma",
               "Chinese Indonesian")
   if t.nationality_state(v) != "supported"], []),
 ("a helper from one we do not is told so",
  [v for v in ("India", "i am from india", "Indian", "Sri Lanka", "sri lankan",
               "Bangladesh", "Nepal", "Cambodia", "Vietnam", "Thailand",
               "Malaysia", "China", "Pakistan", "Ethiopia", "Kenya")
   if t.nationality_state(v) != "unsupported"], []),
 # THE SAFETY OF THE WHOLE THING. The unplaceable list is positive, never
 # "did not match the three", so an answer nobody recognises is UNDECIDED and
 # the question is simply asked again. A wrong decline is a woman told to go
 # away who should not have been; a missed one is a conversation a consultant
 # closes. Singapore and Hong Kong are the ones that would bite: a helper
 # already working here can easily answer "which country are you from" with
 # where she IS.
 ("an answer nobody recognises is asked again, not turned away",
  [v for v in ("Java", "Cebu", "Singapore", "in singapore", "Hong Kong",
               "from my village", "idk", "", "   ")
   if t.nationality_state(v) != "undecided"], []),
 # The question names them, which constrains the answer space so the extractor
 # has something to map onto - half the fix on its own.
 ("and the question itself names the three",
  all(c in _question_of("candidate_new_hiring", "nationality")
      for c in ("Philippines", "Indonesia", "Myanmar")), True),
 # ...and it does not then offer a fourth. Live on the agency's new number,
 # 2026-09-11, the very first candidate conversation: "the Philippines,
 # Indonesia, Myanmar, or another country?" - the bot inviting the one answer
 # it would have to refuse on the next turn. Not the model improvising:
 # _field_guidance told it to say the client may give "something not on the
 # list", which is correct for every other option set in this codebase and
 # exactly wrong for this one. Checked through the REAL guidance builder rather
 # than by grepping the source, because what matters is the sentence the model
 # is handed.
 ("the country question does not offer a fourth country",
  [w for w in ("not on the list", "more than one")
   if w in _guidance_for("candidate_new_hiring", "nationality")], []),
 ("and it says outright not to add one",
  all(w in _guidance_for("candidate_new_hiring", "nationality") for w in (
      "whole of it", "another country")), True),
 # The control, and the reason the clause exists at all: `languages` was
 # rewritten on 2026-09-04 because the bot was hiding four of its seven options
 # from a Tamil-speaking household. An option set that is a set of EXAMPLES
 # must keep saying so.
 ("an ordinary option list still invites what it does not name",
  all("not on the list" in _guidance_for(svc, key) for svc, key in (
      ("candidate_new_hiring", "languages_spoken"),
      ("new_hiring", "languages"),
      ("new_hiring", "requirement"))), True),
 # Tripwire, not a rule: a closed answer space is a decision to make once, with
 # somewhere for the answers it excludes to go. Her country has that - the
 # refusal branch in info_collector. Nothing else does, so a second field
 # turning up here should be read rather than assumed.
 ("hers is the only closed option set anywhere",
  sorted((svc, f.key) for svc, fields in t.SERVICE_FIELDS.items()
         for f in fields if f.options_are_exhaustive),
  [("candidate_new_hiring", "nationality")]),

 # What the refusal may not do. She has just been turned down, which is the
 # worst possible audience for a promise we cannot keep - there is no waiting
 # list, no file we keep her on and no fee anywhere in our records.
 ("the refusal promises her nothing we do not have",
  all(p in _flat(tpl.UNPLACEABLE_NATIONALITY_NOTE) for p in (
      "do not promise to keep her details",
      "do not ask her any more questions about herself",
      "do not quote a fee")), True),
 # ...and it is a conversation, not a canned line: it applies on every turn
 # while her country reads as unplaceable, so "why?" is answered against the
 # history instead of drawing the refusal a second time.
 ("and it answers her follow-up instead of repeating itself",
  ("ANSWER IT" in tpl.UNPLACEABLE_NATIONALITY_NOTE,
   "Do not repeat the refusal" in _flat(tpl.UNPLACEABLE_NATIONALITY_NOTE)),
  (True, True)),
 # A correction costs nothing: the extractor updates the field, the state reads
 # "supported" next turn, and the registration carries on with no special case.
 ("a helper who corrects herself is simply taken at her word",
  "take her at her word" in _flat(tpl.UNPLACEABLE_NATIONALITY_NOTE), True),
 # Not a handover: "we do not recruit from your country" is an answer we hold,
 # and putting it in front of a consultant spends their time saying it again.
 # Proved by RUNNING the node - see smoke_nodes.py, "candidate, a country we
 # do not place from". The first version of this check read the source and
 # split it on the template's name, which matched the IMPORT line rather than
 # the branch and was green for a reason unrelated to what it was checking.
 ("the branch that declines her is wired to the note",
  "UNPLACEABLE_NATIONALITY_NOTE," in _ICO_SRC, True),

 # --- the currency, 2026-09-11 -----------------------------------------
 # "The bot should consistently use SGD, not USD/dollars." Only on her side:
 # an employer reading "$600-700" is in Singapore and cannot mean anything
 # else, while a helper answering from Manila or Jakarta reasonably can.
 ("the salary she is asked for is in SGD",
  "SGD" in _question_of("candidate_new_hiring", "expected_salary"), True),
 ("and the bands are still the employer's own, so the two match",
  _options_of("candidate_new_hiring", "expected_salary")
  == _options_of("new_hiring", "budget"), True),

 # --- the process is the CLOSING message, 2026-09-11 -------------------
 # She asked "can you please tell me the further process" at the last question
 # and got a compressed, out-of-order version of the briefing with the next
 # question tacked on - then the real briefing one turn later. Told twice, and
 # the first telling was the wrong one.
 ("a process question mid-collection does not pre-empt the briefing",
  ("CANDIDATE_PROCESS_COMES_LAST_NOTE" in _ICO_SRC,
   "Do not number the steps" in _flat(tpl.CANDIDATE_PROCESS_COMES_LAST_NOTE)),
  (True, True)),
 # A DOCUMENTS question is excluded, because answering that one at any point is
 # its own agency instruction from the day before.
 ("but a documents question is still answered on the spot",
  "not asks_for_documents(message)" in _ICO_SRC, True),

 # --- the number the agency answers on, 2026-09-11 ---------------------
 # Ming Hwee's WhatsApp number changed - the same Whapi channel re-paired to a
 # new handset, so WHAPI_SENDER_PHONE was the only .env line that moved. The
 # part that was NOT config: 14 live rows named the old number and 13 were
 # contact_type='candidate', including "NOT EMERGENCY - Call Ming Hwee" and
 # "27.8a If Someone in the House Touches You or Pressures You". A helper
 # reporting abuse was being told to call a line the agency no longer answers.
 # The digits live in the loader's TEXT_REPLACEMENTS, with the full note; they
 # are deliberately not repeated here, because the sweep below reads them.
 #
 # All 14 are document_chunk rows with NO question, so UPDATES could not reach
 # them - section 9.15 predicted exactly this and said it would need a
 # chunk-level path. TEXT_REPLACEMENTS is that path.
 ("the loader can correct a chunk row, not just a Q&A row",
  bool(_REPLACEMENTS), True),
 # A replacement is a blunt instrument pointed at live client-facing text. Too
 # short a needle ("9456") would also match a postcode, a licence number or a
 # price, and nothing afterwards would show it had happened.
 ("no replacement needle is short enough to hit something else",
  [r["old"] for r in _REPLACEMENTS if len(r["old"]) < 8], []),
 ("and none of them is a no-op or unexplained",
  [r["old"] for r in _REPLACEMENTS
   if r["old"] == r["new"] or not r.get("reason", "").strip()], []),
 # ORDER. One row read "(WhatsApp: <old> / Tel: 6534 2277)" - which is how we
 # learnt 6534 2277 was already the office phone line and WhatsApp had simply
 # moved onto it. The specific rewrite of that sentence has to run BEFORE the
 # general digit swap, or the general one gets there first, the specific
 # needle no longer exists, and the row is left saying the same number twice
 # under two labels.
 #
 # The rule is directional and this check had it backwards on the first run:
 # it flagged the CORRECT arrangement. A later needle contained in an earlier
 # one is fine - that is specific-then-general. An EARLIER needle contained in
 # a later one is the dead case, because the earlier rule rewrites the text the
 # later rule was looking for. (2026-09-09 C: a check that fails on correct
 # code is noise, and noise is how a real failure gets ignored.)
 ("a specific replacement is never shadowed by a general one",
  [(a["old"], b["old"])
   for i, a in enumerate(_REPLACEMENTS)
   for b in _REPLACEMENTS[i + 1:]
   if a["old"] in b["old"]], []),
 # And it is gone from the repo itself, not just the database: TEST_SCRIPT.md
 # told a tester to message it, and three parser fixtures used it to stand for
 # "us". Swept rather than listed, so it cannot come back in a file nobody
 # thought to check, and the needles are taken FROM the loader rather than
 # retyped here - which is also why no digit of the old number appears in this
 # file. The first version spelled it out in the comment above and the sweep
 # caught itself, which is the right answer to the wrong question.
 ("a replaced string survives nowhere but the loader that replaces it",
  sorted(f for f, src in _NUMBER_SWEEP.items()
         if any(r["old"] in src for r in _REPLACEMENTS)), []),

 # --- one name for the person who picks it up, 2026-09-19 --------------
 # The agency read the closing briefing of a passport renewal and objected to
 # one line of it: the cost section deferred to a consultant. Their
 # instruction was the word, not the sentence - "it should not be (Consultant)
 # it should be (Our agent)". The old phrasing is not written out anywhere in
 # this file; see _was_called.
 #
 # Applied to every service rather than to the one they tested, and the reason
 # is in the same message: four lines below that sentence it closed with "a
 # live agent will connect with you shortly". One message, two names, one
 # person. The word is not a fact about passport renewal - it is what the
 # agency calls its own staff.
 #
 # Swept over the ROWS rather than listed, so a row written tomorrow that
 # reaches for the old word fails by name. The repo half is already covered
 # by the needle sweep below, and the old phrasing is deliberately NOT
 # written out here: it is one of the loader's replacement needles, and
 # spelling it out makes this file fail its own sweep - which is what the
 # first draft did, exactly as the phone number did on 2026-09-11.
 ("no knowledge-base row calls them a consultant",
  sorted({r["question"] for r in lsn.ROWS
          if "consultant" in _flat(str(r.get("answer") or "")).lower()}), []),
 # ...and the instruction that writes the closing briefing names the word
 # outright rather than leaving it to an example. An example is what the
 # briefing had, and the model followed the rest of the note and not it.
 ("...and the briefing note says so in as many words",
  ("our agent" in tpl.SERVICE_BRIEFING_NOTE,
   "never \"a consultant\"" in tpl.SERVICE_BRIEFING_NOTE),
  (True, True)),
 # The two guards that were keyed on the old word, and would have gone quiet
 # on the new one. Neither is about vocabulary: the first decides whether a
 # time inside a numbered step is a callback nobody promised, the second
 # whether a closing briefing announced the handover at all. A rename that
 # left these behind would have disabled both silently, which is the shape
 # this file has recorded four times.
 # The sentences deliberately carry NO other trigger. The first draft used
 # "our agent will call you within 2 hours" and stayed GREEN under the fault,
 # because "call you" is a trigger of its own - so it proved the guard works
 # and said nothing at all about the word this commit changed.
 ("a callback time is still caught when the person is called an agent",
  bool(gd._CONTACT_PROMISE.search(
      "our agent will confirm that within 2 hours")), True),
 ("...and the old word still is, for a reply that has not caught up",
  bool(gd._CONTACT_PROMISE.search(
      _was_called("a") + " confirm that within 2 hours")), True),
 ("...while a step that promises nobody anything is left alone",
  bool(gd._CONTACT_PROMISE.search(
      "you receive 3 to 5 matched profiles within 48 hours")), False),
 ("a briefing that ends on our agent counts as announcing the handover",
  bool(ico._ANNOUNCES_HANDOVER.search("Our agent will be in touch shortly.")), True),
 ("...and so does one that ends on the old word",
  bool(ico._ANNOUNCES_HANDOVER.search(
      _was_called("A") + " be in touch shortly.")), True),

 # --- the Indonesian passport timeline, 2026-09-19 ---------------------
 # "Renewal of Indo passport may take more than 3 working days" - the agency,
 # having tested it. Three rows stated "3 working days" flat, and it was the
 # only one of the three nationalities claiming a hard number with nothing
 # about the wait in front of it: PH says 6 to 8 weeks, MM says outright that
 # the appointment slot is the unpredictable part.
 #
 # They gave no replacement span, so none is invented - the figure is stated
 # as the floor it is. Asserted over the UPDATES that own those rows, because
 # a fourth row reintroducing the flat figure is the way this comes back.
 ("no passport row states 3 working days as the whole answer",
  sorted({u["where"]["question"] for u in lsn.UPDATES
          if u["where"].get("service_type") == "passport_renewal"
          and "3 working days" in _flat(str(u["set"].get("answer") or "")).lower()
          and "more than 3 working days"
              not in _flat(str(u["set"].get("answer") or "")).lower()}),
  []),
 # ...and all three of them still say it, so the floor was not simply deleted.
 ("...and all three that carry it say more than",
  len([u for u in lsn.UPDATES
       if u["where"].get("service_type") == "passport_renewal"
       and "more than 3 working days"
           in _flat(str(u["set"].get("answer") or "")).lower()]), 3),

 # --- who the ROWS are written for, 2026-09-10 -------------------------
 # effective_contact_type puts a master record above one message, rightly.
 # For retrieval that has a hole: a job seeker messaging from a number this
 # database holds an EMPLOYER record for could not see one of the 27
 # helper-facing rows, and "what is the process" came back at 0.397 - four
 # thousandths under the floor - so she was handed to a human for something
 # the knowledge base answers. As a candidate the same question scores 0.437.
 ("a candidate flow searches the candidate's shelf, whatever the record says",
  rr._retrieval_audience({"service_type": "candidate_new_hiring",
                          "contact_type": "employer",
                          "matched_employer_id": "e1"}), "candidate"),
 ("a helper's own transfer too",
  rr._retrieval_audience({"service_type": "transfer",
                          "contact_type": "employer",
                          "matched_employer_id": "e1"}), "candidate"),
 ("and an employer flow is untouched",
  rr._retrieval_audience({"service_type": "new_hiring",
                          "contact_type": "employer",
                          "matched_employer_id": "e1"}), "employer"),
 ("as is an employer's transfer, which is a different service key",
  rr._retrieval_audience({"service_type": "transfer_employer",
                          "contact_type": "employer",
                          "matched_employer_id": "e1"}), "employer"),
 # This one was inverted on 2026-09-11 and the tripwire caught it, which is
 # what these counts are for. Until then the candidate's `nationality` had NO
 # options, and the note here said constraining her to three countries "would
 # turn a Sri Lankan applicant away at the first question". The agency has
 # since said that IS the intended behaviour - so she is now offered the three,
 # and turned away kindly with her questions answered if she names another.
 # The two halves still cannot share a LIST: the employer picks a nationality
 # ("Filipino") and plus "no preference", the helper names a country ("the
 # Philippines"). Same three, different words, so the shared-options rule
 # excludes the pair and this asserts the overlap instead.
 ("the nationality pair now offers the same three countries",
  (len(_options_of("candidate_new_hiring", "nationality")),
   len(_options_of("new_hiring", "preferred_nationality"))), (3, 4)),
 ("and a helper is offered countries, not nationalities",
  _options_of("candidate_new_hiring", "nationality"),
  t.PLACEABLE_NATIONALITIES),
 # The keys are deliberately NEW rather than the employer's own. `languages`
 # and `budget` are portable across services, so reusing them would carry an
 # employer's "Mandarin spoken at home" into a helper's file as a language she
 # speaks - the direct-hire flow avoided the same trap with its helper_ prefix.
 ("no candidate key collides with a portable employer key",
  sorted({f.key for f in t.SERVICE_FIELDS["candidate_new_hiring"]}
         & (ico._PORTABLE_ACROSS_SERVICES - {"full_name", "email",
                                             "contact_number", "nationality"})),
  []),
 # _WHY_WE_ASK is keyed on the field key with no idea which flow is asking,
 # and every reason in it is written from the employer's side ("so anything
 # that matters to them is agreed with THE HELPER up front"). Said to the
 # helper herself that is a sentence about somebody else, which is why
 # candidate_notes is its own key and not `additional_notes`.
 # Restated 2026-09-17. The hazard is a SHARED key: `_WHY_WE_ASK` is keyed on
 # the field key with no idea which flow is asking, so a reason written for an
 # employer is read out to the helper when both flows use that key. A key only
 # the candidate flow has cannot inherit anything - there is no employer
 # question wearing it - so `religion` is fine while `additional_notes` would
 # not be. Derived from the employer flows rather than from a list, which is
 # also what makes it survive a new flow.
 ("no candidate field inherits a reason written for an employer",
  sorted({f.key for f in t.SERVICE_FIELDS["candidate_new_hiring"]}
         & set(ico._WHY_WE_ASK)
         & {f.key for svc in _lead.EMPLOYER_LEAD_SERVICES
            if svc in t.SERVICE_FIELDS
            for f in t.SERVICE_FIELDS[svc]}), []),
 # ...and her reason is written TO her. The employer's says "your household";
 # hers says "you". A reason that talks about the client in the third person to
 # the client is the defect this pair of rules exists to stop.
 ("her reason is addressed to her, not about her",
  ("your household" in ico._WHY_WE_ASK.get("helper_religion", ""),
   "your household" in ico._WHY_WE_ASK.get("religion", "")), (True, False)),
 # Until 2026-09-11 this ended on update_channel + email, "the order every
 # other flow uses". It no longer asks either: she is messaging us ON WhatsApp,
 # so the channel is not a question, and the agency asked for the collection to
 # end and go straight into the next-steps message. Asserted as what it must
 # NOT contain rather than as the new last two keys, because the point is that
 # nothing administrative follows the last real question.
 ("nothing is asked after the last question about her",
  sorted({"candidate_notes", "update_channel", "email"}
         & {f.key for f in t.SERVICE_FIELDS["candidate_new_hiring"]}), []),
 # ...and the flows that DO ask it still do. A hard list, because it is a
 # tripwire and not a rule: "every employer flow asks how to reach them" was
 # written here first and was simply false - five of them never have, and the
 # check went red on correct code the moment it ran (2026-09-09 C).
 ("the flows that ask how to reach them are unchanged",
  sorted(svc for svc, fl in t.SERVICE_FIELDS.items()
         if any(f.key == "update_channel" for f in fl)),
  ["direct_hiring", "new_hiring", "transfer_employer"]),
 # A job seeker has less patience than an employer (the note on `transfer`
 # says so), so the matching half is optional throughout: asked once each,
 # and a collection completes without them.
 ("the matching questions are asked once and are optional",
  sorted(f.key for f in t.SERVICE_FIELDS["candidate_new_hiring"]
         if f.key in set(_MATCHED_PAIRS.values())
         - {"work_scope", "nationality", "age"}
         and not (f.optional and f.max_asks == 1)), []),
 # Three of the pairings are NOT optional, and that is the decision rather
 # than an oversight: her country, what she can take on and her age are what a
 # consultant filters on before reading anything else, and age is the one the
 # agency named outright.
 ("but her country, her scope and her age are asked properly",
  sorted(f.key for f in t.SERVICE_FIELDS["candidate_new_hiring"]
         if f.key in ("nationality", "work_scope", "age") and not f.optional),
  ["age", "nationality", "work_scope"]),

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
 # Measured on `insurance` rather than on `renewal` since 2026-09-18: `renewal`
 # gained a CLOSING briefing that day, and a service that briefs at the end
 # does not also brief at the start - so the exemplar had stopped exercising
 # the thing it was written for and simply went red. `insurance` is the
 # small-ticket service that still opens with an overview and has no closing
 # briefing, which is exactly the shape this pair is about.
 ("the overview turn can actually happen, exactly once",
  [n for n in range(6) if ico.briefs_on_this_turn("insurance", {"f": n})], [1]),
 ("it never lands on the introduction turn",
  ico.briefs_on_this_turn("insurance", {}), False),
 ("a service that briefs at the END does not brief at the start too",
  ico.briefs_on_this_turn("passport_renewal", {"f": 1}), False),
 ("...and that holds for every service with a closing briefing",
  [k for k in t.BRIEFING_AFTER if ico.briefs_on_this_turn(k, {"f": 1})], []),
 # Reversed 2026-09-17, on the agency's instruction: "Once the service or
 # intent has been identified, the bot should proactively explain the relevant
 # process and expected timeline/lead time, without waiting for the user to
 # ask." The two hiring flows are the longest in the codebase and were the
 # only ones that explained themselves at neither end.
 # ...and HALVED again on 2026-09-18, which is the correction this tripwire
 # exists to force. `direct_hiring` gained a CLOSING briefing that day, on the
 # agency's instruction after testing it: "this is the process related and
 # documents related things we should tell this at last with the process,
 # requirements, timeline, cost/fees ... without waiting for the user to ask
 # for". Live, the overview had arrived welded onto the second question -
 # "Thanks, john. Direct hire involves processing the MOM application,
 # documents, insurance and bond, and getting the helper here and settled; may
 # I know the full name of the helper you would like to hire?" - which is
 # process and document material in front of a client who has just given his
 # name. So the set is now `new_hiring` alone, and the OTHER half of that is
 # asserted two entries down: a flow that leaves the overview must have a
 # closing briefing instead, never neither.
 ("the flow that still explains itself up front is new_hiring",
  [k for k in ("new_hiring", "direct_hiring")
   if not ico.briefs_on_this_turn(k, {"f": 1})], ["direct_hiring"]),
 ("...on exactly one turn, like every other service that does",
  [n for n in range(6) if ico.briefs_on_this_turn("new_hiring", {"f": n})], [1]),
 ("...and never on the introduction turn",
  ico.briefs_on_this_turn("new_hiring", {}), False),
 # A flow that briefs at neither end is a flow whose client is told nothing
 # unless they think to ask. Derived over every service that collects, so a
 # flow added tomorrow has to make that choice deliberately rather than by
 # omission. `transfer` and the money enquiries are out: the first is the
 # helper's own six questions, the others are a question and not an intake.
 ("every employer intake explains itself at one end or the other",
  sorted(k for k in t.SERVICE_FIELDS
         if k in _lead.EMPLOYER_LEAD_SERVICES
         and k not in ("fee_enquiry", "salary_enquiry")
         and k not in t.BRIEFING_AFTER
         and not ico.briefs_on_this_turn(k, {"f": 1})),
  # EMPTY since 2026-09-18, and it took two instructions on the same day to
  # get there. `replacement` left first ("after getting all the required
  # details the bot should reply the process, required documents, timeline and
  # the cost"), and `transfer_employer` left hours later on the same
  # complaint about its own transcript: "after getting all the required
  # details bot didnt message the process, documents, timline, cost/fees".
  #
  # Both were recorded HERE as decisions rather than omissions while they were
  # open, which is what made each of them one line to close. The section 9
  # entry that asked the agency to choose is now answered in full.
  #
  # An employer RELEASING their helper is the one branch still told nothing -
  # see the note on BRIEFING_AFTER[TRANSFER_EMPLOYER]. It is not visible to
  # this check, which reads the service and not the branch, and that is worth
  # knowing rather than working around: a check derived over services cannot
  # see a gate.
  []),
 # A withheld price is said as what WE will do, never as a gap in our files.
 # Live 2026-09-18: "The transfer fee is not stated in our records, so a
 # consultant will confirm the exact amount" - true, and it tells the client
 # about our filing and reads as though we do not know our own prices.
 ("a withheld cost never remarks on our own records",
  "never as what our records" in tpl.SERVICE_BRIEFING_NOTE, True),
 ("...and the cost section is still required",
  "Never leave this out" in tpl.SERVICE_BRIEFING_NOTE, True),

 # An employer transfer briefs at the END, 2026-09-18, and the KEY is the
 # whole of the care here. `referral_source` is the true second-to-last
 # question and is the wrong answer: `_known_fields` fills it from the
 # records for any returning client, so keyed there the briefing would be
 # due from turn ONE and the retriever would spend a twenty-question intake
 # searching for a briefing instead of for what the client just said.
 ("an employer transfer explains itself at the end",
  t.BRIEFING_AFTER.get(t.TRANSFER_EMPLOYER), "rest_day"),
 # `.get()` and not `[...]`, and that is not tidiness: removing the entry
 # outright made this assertion raise KeyError, so the harness printed a
 # traceback and no FAIL line - which is the third time a check has crashed
 # where it should have failed by name (2026-09-10, 2026-09-17, here).
 ("...on a field only the client can fill",
  t.BRIEFING_AFTER.get(t.TRANSFER_EMPLOYER, "") in ico._PORTABLE_ACROSS_SERVICES
  or t.BRIEFING_AFTER.get(t.TRANSFER_EMPLOYER, "") in ico._known_fields({}), False),
 # ...and far enough from the end that the RETRIEVER, which runs a node
 # earlier, has the records before the collection completes. Measured as a
 # position rather than an index, the way the direct-hire contact question
 # is (2026-09-17).
 ("...with questions left after it, because the retriever runs first",
  [f.key for f in t.SERVICE_FIELDS[t.TRANSFER_EMPLOYER]].index(
      t.BRIEFING_AFTER.get(t.TRANSFER_EMPLOYER, "email"))
  < len(t.SERVICE_FIELDS[t.TRANSFER_EMPLOYER]) - 1, True),

 # --- direct hire briefs at the END, 2026-09-18 -----------------------
 # Agency, testing it as an employer: "this is the process related and
 # documents related things we should tell this at last with the process,
 # requirements, timeline, cost/fees when all the requirements are gathered
 # bot have to message these things in single message without waiting for the
 # user to ask for". Both halves are asserted, because they are one change:
 # adding the entry is what REMOVES the opening overview.
 ("a direct hire explains itself at the end",
  t.BRIEFING_AFTER.get("direct_hiring"), "helper_availability"),
 # `.get()` throughout, never `[...]`: removing an entry outright used to make
 # these raise KeyError, so the harness printed a traceback and no FAIL line
 # (2026-09-10, 2026-09-17, 2026-09-18).
 ("...on a field only the client can fill",
  t.BRIEFING_AFTER.get("direct_hiring", "") in ico._PORTABLE_ACROSS_SERVICES
  or t.BRIEFING_AFTER.get("direct_hiring", "") in ico._known_fields({}), False),
 # It is the LAST REQUIRED field, and that is the reason it was chosen rather
 # than an accident of ordering. Everything after it is optional, so keying
 # the briefing on one of THOSE would lose it entirely whenever a client
 # declined that question - and declining `helper_contact` is not
 # hypothetical, it is what the agency's own 2026-09-17 transcript did.
 ("...which is the last field the client must answer",
  [f.key for f in t.SERVICE_FIELDS["direct_hiring"] if not f.optional][-1],
  t.BRIEFING_AFTER.get("direct_hiring")),
 ("...with questions left after it, because the retriever runs first",
  [f.key for f in t.SERVICE_FIELDS["direct_hiring"]].index(
      t.BRIEFING_AFTER.get("direct_hiring", "email"))
  < len(t.SERVICE_FIELDS["direct_hiring"]) - 1, True),
 # ...and the overview is gone from the front, which is the half they
 # objected to by quoting it back at us.
 ("...and it no longer explains itself at the start as well",
  ico.briefs_on_this_turn("direct_hiring", {"f": 1}), False),
 # The cost section defers to a consultant rather than quoting a package
 # total - and it has to, because quotes_hiring_package_cost would otherwise
 # swap the whole briefing for the deferral line and the client would lose
 # the process and the documents with it.
 ("...and its cost section is a deferral, not a package price",
  "direct_hiring" in _guards.COST_WITHHELD_SERVICES, True),

 # --- a question about WHEN is not answered by a sentence about WHERE ---
 # Live 2026-09-18, reproduced 3 runs of 3. Asked whether she was in
 # Singapore under another employer's Work Permit, the client answered "No
 # she is on Myanmar right Now" - and `helper_availability` was filled with
 # "Myanmar right now", so "When would she be available to start?" was never
 # asked and ticket CB-2026-0009 carried a country where a start date belongs.
 #
 # Derived from the QUESTION rather than written as a list of keys, because a
 # rule written over the flow that was reported is false on the other eight
 # until somebody checks - which this file has now had to correct four times.
 ("every field that asks when is covered, on every service",
  sorted({f.key for fs in t.SERVICE_FIELDS.values() for f in fs
          if ico._ASKS_WHEN.search(f.question)}),
  ["availability", "helper_availability", "leave_dates", "passport_expiry",
   "permit_expiry", "policy_expiry", "start_timeline", "timeline"]),
 # The test is an ECHO of another answer, not "does this state a time": the
 # live value genuinely contains one - "right now" - and it is attached to
 # the country rather than to her availability.
 ("the live value repeats an answer we already hold",
  ico._echoes_another_answer(
      "Myanmar right now",
      {"helper_nationality": "Myanmar", "helper_name": "chowchow"},
      "helper_availability"), True),
 ("...and so does the run that phrased it differently",
  ico._echoes_another_answer(
      "in Myanmar right now", {"helper_nationality": "Myanmar"},
      "helper_availability"), True),
 # The controls. A real answer to "when" repeats nothing, and the rule must
 # not start dropping those - it fails towards asking, so every one of these
 # still lands.
 ("...but a real date repeats nothing",
  [v for v in ("next month", "as soon as possible", "right now", "2 weeks",
               "she is free from 1 January", "immediately")
   if ico._echoes_another_answer(
       v, {"helper_nationality": "Myanmar", "helper_name": "chowchow"},
       "helper_availability")], []),
 # A two-letter answer cannot make every sentence containing those letters
 # look like a restatement. "no idea, maybe next month" contains the
 # transfer-case answer "no", and dropping that would be worse than the
 # defect being fixed.
 ("...and a yes/no answer elsewhere cannot poison a real one",
  ico._echoes_another_answer(
      "no idea, maybe next month",
      {"helper_transfer_case": "no"}, "helper_availability"), False),
 # A field never compares against ITSELF, or correcting an answer would look
 # like a restatement of it.
 ("...and a field is never an echo of itself",
  ico._echoes_another_answer(
      "Myanmar", {"helper_availability": "Myanmar"}, "helper_availability"),
  False),

 # --- the enquiry is not an answer to the enquiry's own questions -------
 # Found by REPLAYING the direct-hire transcript, not by reading the code,
 # and reproduced 4 runs of 4 against the real extractor: the opening message
 # "i want to do direct hire" filled `helper_transfer_case` with the value
 # "direct hire". A filled field is never asked, so the one question that
 # decides the ROUTE - 2-3 weeks for a helper already here against 4-6 from
 # overseas, and whether the notice-period question applies at all - was
 # never put to him.
 ("the service's own name does not answer its own question",
  ico._restates_the_service("direct hire", "direct_hiring"), True),
 ("...however it is spelled",
  ico._restates_the_service("direct hiring", "direct_hiring"), True),
 # It fires only on the service IN HAND, and these two controls are why.
 # `current_helper_exit` offers "going home", which _named_service reads as
 # `home_leave` - a real option on a flow the agency signed off hours before
 # this, and dropping it would have been a silent regression.
 ("...but an option that happens to name ANOTHER service still lands",
  ico._restates_the_service("going home", "replacement"), False),
 # ...and `transfer_direction`'s junk value resolves to `transfer`, not to
 # `transfer_employer`, so the machinery built for it earlier today is
 # untouched rather than quietly duplicated.
 ("...and the transfer direction fix is left to its own rule",
  ico._restates_the_service("transfer", t.TRANSFER_EMPLOYER), False),
 ("...and a real answer is never a service name",
  [v for v in ("no", "yes", "Myanmar", "next month", "2 dogs and 3 cats",
               "she has one month notice")
   if ico._restates_the_service(v, "direct_hiring")], []),
 # The length guard, and it is load-bearing rather than tidiness: a real
 # answer may MENTION the service without being a restatement of it, and
 # _named_service matches anywhere in the text. "as soon as the direct hire
 # is approved" is a genuine answer to "when would she be available to
 # start?", and without the guard it is dropped and asked again.
 ("...nor is an answer that merely mentions the service in passing",
  [v for v in ("as soon as the direct hire is approved",
               "she can start once the direct hire paperwork is done",
               "whenever the direct hire goes through")
   if ico._restates_the_service(v, "direct_hiring")], []),

 # --- a send that may already have arrived is never sent again ----------
 # Live 2026-09-18, conversation 3766: the same reply went out twice 1.9
 # seconds apart - this loop's own backoff - because _post retried a request
 # that had already been delivered. The duplicate came back under a message
 # id we had never seen, so handle_outbound read our own sentence as a human
 # agent, the bot stood down, and the client's next question got no reply at
 # all. The cost of a retry here is not a duplicate message, it is the
 # conversation.
 ("a lost RESPONSE is not a reason to send the message again",
  [e.__name__ for e in (_httpx.ReadTimeout, _httpx.ReadError,
                        _httpx.RemoteProtocolError, _httpx.WriteError)
   if issubclass(e, _whapi_client.WhapiClient._SAFE_TO_RESEND)], []),
 # ...and a connection that was never made genuinely sent nothing, so that
 # one still retries. Without this the rule would just be "never retry".
 ("...but a connection that was never made did not send one",
  issubclass(_httpx.ConnectError, _whapi_client.WhapiClient._SAFE_TO_RESEND), True),
 # Every _post in this client is a message to a client's phone, which is what
 # makes the rule total rather than per-caller. A third caller that is NOT a
 # send would need this decision taken again.
 ("...and every POST in the client is a message being sent",
  sorted(set(_re.findall(r"self\._post\(\s*f?\"([^\"]+)", _whapi_src))),
  ["/messages/text", "/messages/{media_type}"]),

 # --- our own words are never a human agent, 2026-09-18 -----------------
 # The second lock on the same door, and the one that holds if any other
 # route ever produces a copy of our own reply: the failure it prevents is
 # not a duplicate message, it is permanent silence.
 ("a reply we just sent is recognised as ours",
  (ms.mark_body_sent_by_bot("+917970027379", "Usually about 4 to 6 weeks."),
   ms.echoes_our_own_send("917970027379", "Usually about 4 to 6 weeks."))[1],
  True),
 ("...whatever the spacing and case",
  ms.echoes_our_own_send("917970027379", "  usually about 4 to 6 WEEKS.  "), True),
 # The recipient is part of the key, so an identical line legitimately sent
 # to two clients cannot mask a real agent on one of them.
 ("...but only on the thread we sent it to",
  ms.echoes_our_own_send("6591234567", "Usually about 4 to 6 weeks."), False),
 ("...and a sentence we never said is still an agent",
  ms.echoes_our_own_send("917970027379", "Hi John, Grace here."), False),
 ("...and an empty body is never ours",
  ms.echoes_our_own_send("917970027379", "   "), False),
 # Recorded as OURS when it does arrive, not as an agent's: a row saying
 # sent_by='agent' is a human on the transcript who was never there, and it
 # is what `last_agent_message_at` would later measure an idle window
 # against. The live row is still on the database in exactly that state.
 ("a duplicate of ours is stored as ours",
  [n for n in ('"is_bot": True', '"sent_by": "bot"')
   if n not in _msg_src.split("async def store_own_echo")[1][:1600]], []),

 # The direction an employer means by "transfer" is read off their own words.
 # Agency, 2026-09-18: "if someone is coming and telling that i want transfer
 # helper it means user intent is clear". The asymmetry between the two
 # patterns is the accuracy - a release carries a possessive ("transfer MY
 # helper"), taking one on does not ("a transfer helper") - so both
 # directions are asserted, not just the reported one.
 ("'i want transfer helper' is taking one on",
  ico._transfer_direction_said(
      {"incoming_text": "hi i want transfer helper"}, t.TRANSFER_EMPLOYER, {}),
  ico._TAKING_ON_VALUE),
 ("...and so is a phrasing no verb of ours appears in",
  ico._transfer_direction_said(
      {"incoming_text": "can you find me a transfer helper"},
      t.TRANSFER_EMPLOYER, {}),
  ico._TAKING_ON_VALUE),
 ("...and 'transfer my helper' is releasing their own",
  ico._transfer_direction_said(
      {"incoming_text": "i want to transfer my helper"}, t.TRANSFER_EMPLOYER, {}),
  ico._RELEASING_VALUE),
 ("...and the junk value the extractor really returns is replaced",
  ico._transfer_direction_said(
      {"incoming_text": "hi i want transfer helper"}, t.TRANSFER_EMPLOYER,
      {"transfer_direction": "transfer"}),
  ico._TAKING_ON_VALUE),
 # Fails towards ASKING. A message that decides nothing leaves the
 # disambiguating question exactly where it is.
 ("a message that says neither still asks",
  [m for m in ("hi", "myself sanjay dutt", "i want childcare", "transfer",
               "i want to hire a helper")
   if ico._transfer_direction_said({"incoming_text": m}, t.TRANSFER_EMPLOYER, {})],
  []),
 # ...and it never flips a direction that is already decided, which would
 # strand every field gated behind it mid-collection.
 ("a decided direction is never overwritten",
  ico._transfer_direction_said(
      {"incoming_text": "i want to transfer my helper"}, t.TRANSFER_EMPLOYER,
      {"transfer_direction": ico._TAKING_ON_VALUE}),
  ""),
 # The half found by REPLAYING the transcript rather than by reading the
 # code, and the one that makes the rest hold: `collected` is
 # {**previous, **extracted}, so the extractor handing back the same
 # undecidable word on a later turn overwrites a direction already settled
 # and the question comes back. A value that opens no branch may not
 # replace one that does.
 ("an undecidable value never overwrites a settled direction",
  ico._settled_transfer_direction(
      {"incoming_text": "myself sanjay dutt"}, t.TRANSFER_EMPLOYER,
      {"transfer_direction": ico._TAKING_ON_VALUE},
      {"transfer_direction": "transfer"}),
  ico._TAKING_ON_VALUE),
 ("...but a real correction still wins, because it decides something",
  ico._settled_transfer_direction(
      {"incoming_text": "actually i want to release my own helper"},
      t.TRANSFER_EMPLOYER, {"transfer_direction": ico._TAKING_ON_VALUE},
      {"transfer_direction": ico._RELEASING_VALUE}),
  ""),
 ("...and nothing is restored when nothing was ever settled",
  ico._settled_transfer_direction(
      {"incoming_text": "hello"}, t.TRANSFER_EMPLOYER, {},
      {"transfer_direction": "transfer"}),
  ""),
 ("...and no other service is touched by it",
  ico._transfer_direction_said(
      {"incoming_text": "hi i want transfer helper"}, "new_hiring", {}), ""),
 # The values written are the field's OWN options, so the gates recognise
 # them. A value outside that pair opens neither gate, which is the exact
 # deadlock (section 9.12) this exists to end.
 ("the values it writes are the ones the gates know",
  sorted((ico._TAKING_ON_VALUE, ico._RELEASING_VALUE)),
  sorted(next(f for f in t.SERVICE_FIELDS[t.TRANSFER_EMPLOYER]
              if f.key == "transfer_direction").options)),
 ("...and each opens exactly one branch",
  [len(t.applicable_fields(t.TRANSFER_EMPLOYER, {"transfer_direction": v}))
   for v in (ico._TAKING_ON_VALUE, ico._RELEASING_VALUE)],
  [19, 4]),

 # The household question does not ask again about the children it has
 # already been told about. Live 2026-09-18: "i have 4 childrens and all are
 # under 15" -> "How many people live in your household, and who are they,
 # such as adults, elderly parents or children?" -> "AS I TOLD THEN WHY
 # ASKED ME AGAIN".
 ("the household question is told what it already has",
  "ALREADY told you this much" in ico._field_guidance(
      t.TRANSFER_EMPLOYER,
      {"requirement": "childcare", "children_detail": "4 children under 15"},
      next(f for f in t.SERVICE_FIELDS[t.TRANSFER_EMPLOYER] if f.key == "household")),
  True),
 ("...and asks the whole question when it has nothing",
  "ALREADY told you this much" in ico._field_guidance(
      t.TRANSFER_EMPLOYER, {"requirement": "childcare"},
      next(f for f in t.SERVICE_FIELDS[t.TRANSFER_EMPLOYER] if f.key == "household")),
  False),
 # A general instruction beats a specific one unless the specific one says so
 # (2026-09-07, languages). The note above it says to ask for everything the
 # written question asks for, which is the opposite of this.
 ("...and it names the rule it overrides",
  "OVERRIDES" in ico._field_guidance(
      t.TRANSFER_EMPLOYER,
      {"requirement": "childcare", "children_detail": "4 children under 15"},
      next(f for f in t.SERVICE_FIELDS[t.TRANSFER_EMPLOYER] if f.key == "household")),
  True),
 # Derived from the gates on `requirement`, not from a list of two keys.
 ("what it already has is derived from the care details themselves",
  ico._care_details_already_told(
      t.TRANSFER_EMPLOYER,
      {"children_detail": "4 under 15", "elderly_detail": "2 parents"}),
  ["the children: 4 under 15", "the elderly family member: 2 parents"]),

 # The cooking question stops taking it as read that she will be cooking.
 # Agency, 2026-09-18, mid-transfer, having said childcare: "i want childcare
 # then why you are asking the cooking related question". Reworded rather
 # than gated - a gate on `requirement` would make `_gates_are_exhaustive`
 # true for that field and reinstate the 2026-09-08 blank-and-re-ask defect.
 ("the cooking question asks IF, not only which kind",
  next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "cooking").question,
  "Would she need to do any cooking, and if so, any particular kind?"),
 ("...and a bare no closes it instead of being re-asked",
  ico._yes_no_question(
      next(f for f in t.SERVICE_FIELDS["new_hiring"] if f.key == "cooking").question),
  True),
 ("...and `requirement` is still NOT gate-exhaustive",
  ico._gates_are_exhaustive(
      t.TRANSFER_EMPLOYER, "requirement",
      [f.gate for f in t.SERVICE_FIELDS[t.TRANSFER_EMPLOYER]
       if f.gate and f.gate.field == "requirement"]),
  False),
 ("...so a plain housework answer is never blanked and re-asked",
  ico._undecidable_gate_keys(
      t.TRANSFER_EMPLOYER,
      {"transfer_direction": ico._TAKING_ON_VALUE, "requirement": "general house work"}),
  []),

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
  ["candidate_new_hiring", "direct_hiring", "home_leave", "insurance",
   "new_hiring", "passport_renewal", "renewal", "replacement",
   "transfer_employer"]),
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

 # --- where we are, when we are open, how to get here (2026-09-16) ------
 # A client asked for the office address three times and was handed to a
 # live agent three times, and only ONE of the three failures was a missing
 # row. The bot had volunteered "Jurong (HQ), Tampines and Woodlands" out of
 # the IDENTITY block, so the client asked for the TAMPINES address - and
 # there is no Tampines. Checked against three independent sources: the
 # platform's `branches` table holds one row, "CHINA TOWN"; the Client
 # Service Agreement names one Registered Business Address; and the agency's
 # own brief names one outlet. The prompt was the only place the other two
 # ever existed, which is why this is asserted on the prompt and not on a row.
 ("the prompt never names a branch we do not have",
  sorted({b for b in ("Jurong", "Tampines", "Woodlands")
          if b in _IDENTITY or b in _AGENCY_INFO}), []),
 ("...and says outright that there is one office",
  "ONE office" in _IDENTITY and "Chinatown" in _IDENTITY, True),
 # The address, hours and MRT must live in the RECORDS, not in the prompt:
 # ungrounded_figures grounds a reply on the retrieved records and on what
 # the client said, never on the identity block, so a postal code stated
 # from the prompt is binned and the client gets the holding line instead.
 ("the address is never stated from the prompt, where it would be ungrounded",
  [b for b in ("058357", "Upper Cross", "9:30", "Exit D")
   if b in _IDENTITY or b in _AGENCY_INFO], []),
 ("...and every part of it IS in the records",
  [f for f in ("101 Upper Cross Street", "#03-54", "People's Park Centre",
               "058357", "Chinatown MRT", "Exit D", "9:30am", "6:30pm",
               "10:30am", "5:00pm", "Sundays", "public holidays")
   if f not in _OFFICE_TEXT], []),
 ("the office rows are filed where every service can reach them",
  {r["service_type"] for r in lsn.ROWS
   if r["section_heading"].startswith("Office - ")}, {"general"}),
 ("...and are shown to helpers as well as employers",
  {r.get("contact_type", "all") for r in lsn.ROWS
   if r["section_heading"].startswith("Office - ")}, {"all"}),
 # The parked path is where this was reported: the client already had a
 # hiring topic with an agent. Fifth gap of this shape in _GENERAL_INFO -
 # "what is THE cost", "the FURTHER process", the plural "fees" and the two
 # documents phrasings - so it is asserted over the phrasings the agency
 # listed rather than the one that was screenshotted.
 ("a parked topic still says where we are and when we are open",
  [m for m in ("can i have the office location ? for tampines",
               "Can you please provide the address of the Tampines branch?",
               "i would like to visit the outlets",
               "what is your office address", "where is your office",
               "where are you located", "what are your opening hours",
               "are you open on sunday", "what time do you open",
               "when can i visit", "which mrt station is nearby",
               "what is the nearest mrt", "is it near an mrt station",
               "how can i get there", "how do i come to your office",
               "can you give me directions")
   if not _asks_general(m)], []),
 # ...and the chase it must NOT swallow. A parked topic silences chasing,
 # not curiosity, and "where is my helper" is neither.
 ("...without answering a chase or an ordinary answer",
  [m for m in ("any update on my case?", "what is the status of my application now?",
               "where is my helper now", "where is she from", "please update me",
               "when will the agent call me", "ok", "Myanmar", "3-4")
   if _asks_general(m)], []),
 # agency_info named the SHAPE of the question and was tagging the query
 # with itself - the 2026-09-07 process_question defect, one intent along.
 # Measured 2026-09-16: tagged "(agency info)", "how can i get there" and
 # "where are you located" put the office rows OUTSIDE the top 5 and handed
 # the model clause 6 of the service agreement at 0.425, above the floor.
 ("an agency question is searched for what it asks, not for its own label",
  [m for m in ("where are you located", "how can i get there",
               "what are your opening hours")
   if _rr._search_query({"incoming_text": m, "intent": "agency_info",
                         "service_type": "passport_renewal",
                         "collected_info": {}, "asked_field_counts": {}}) != m], []),
 # ...and the reason it is its own branch rather than another entry in
 # _SUBJECTLESS_INTENTS: that set makes the in-flight service the subject,
 # and "where is your office" during a passport renewal is not about the
 # passport renewal.
 ("...and is not tagged with the service in flight either",
  "agency_info" in _rr._SUBJECTLESS_INTENTS, False),
 # The placeholder is what actually reached the client: "Our Tampines branch
 # is at [address not available in my records]." No guard catches that -
 # strip_meta_commentary cuts a bracket only when it reads as commentary,
 # and cutting this one leaves "Our Tampines branch is at ." - so the
 # instruction is the control and it is asserted.
 ("the model is told not to send a placeholder where a detail should be",
  "placeholder" in _IDENTITY and "placeholder" in _AGENCY_INFO, True),
 ("...and that where we are is answered, never handed over",
  "never hand this to a live agent" in _flat(_AGENCY_INFO), True),

 # --- what kind of help, and whose name (2026-09-16) -------------------
 # Live new-hiring test. The opening message was "hey i want to hire a
 # helper", `requirement` came back "general housework" - never said - so the
 # field looked answered, was NEVER ASKED, and closed `children_detail` and
 # `elderly_detail` with it. Seventeen questions later: "one thing to flag i
 # didnt mention what type of service i need then how you move forward".
 #
 # The guard for this has existed since 2026-09-07 and was subtractive on the
 # MESSAGE - "does this contain a word that is not hiring filler" - which is
 # true of almost anything. Measured against that transcript it passed all
 # EIGHTEEN client messages, "10" and "google" among them. It is additive now.
 ("a message that names no care type cannot fill one",
  [m for m in ("hey i want to hire a helper", "i want to hire a helper",
               "10", "HDB", "3 and 3", "600", "google", "western food",
               "car washing", "weekly off", "ASAP", "myanmad",
               "english and tamil", "no i dont have any pets",
               "on whatsapp onlyn", "yes she have there own room",
               "she should be 35 year old and atleast 5 years experiences")
   if ico._mentions_care(m)], []),
 # ...and the half that must still work: a care type the client volunteers is
 # taken, so they are not asked for something they have just said. A miss here
 # costs one question; a false positive costs a helper matched against a
 # requirement nobody gave, which is why this test fails towards asking.
 ("a care type the client DOES name is still taken",
  [m for m in ("i need someone for my mum who is bedridden",
               "looking for childcare for my 2 year old",
               "i need a helper to take care of my elderly father",
               "someone to do housework and cooking",
               "need help with cleaning and laundry",
               "she will look after my grandmother",
               "help for my disabled son", "post natal confinement help",
               # depends on the WORK half of the vocabulary alone, so
               # removing it cannot stay green on the people half
               "someone to look after the house while we are at work",
               "i just need help with the daily chores",
               "my baby is due next month")
   if not ico._mentions_care(m)], []),
 # The greeting that defeated the value test. "i want to hire a helper"
 # subtracts to "" and is blocked; "hey i want to hire a helper" subtracted to
 # "hey" and passed. Same one-word gap as "what is THE cost" (2026-09-08).
 ("a greeting in front of the enquiry does not make it a care type",
  [m for m in ("hey i want to hire a helper", "hi i want to hire a maid",
               "hello i need a helper", "good morning i want to hire a helper",
               "ok i need a maid")
   if ico._states_a_care_type(m)], []),
 # The question the client never got. With nothing known, what kind of help
 # they need comes SECOND - after their name and before the household - and
 # this is the ordering that was silently skipped.
 ("what kind of help they need is asked, and asked early",
  [f.key for f in t.applicable_fields("new_hiring", {})
   if f.key in ("full_name", "requirement", "household")],
  ["full_name", "requirement", "household"]),
 # SGD on BOTH halves of the pairing, 2026-09-16. The bands carry it rather
 # than the question alone, because _field_guidance reads the options into the
 # spoken question - live, that produced "such as below $500, $500-600, or
 # $600-700?" with no currency named anywhere.
 ("the salary bands say SGD on both sides of the desk",
  [f"{svc}.{key}" for svc, key in (("new_hiring", "budget"),
                                   ("candidate_new_hiring", "expected_salary"))
   if not all("SGD" in o for o in
              next(f for f in t.SERVICE_FIELDS[svc] if f.key == key).options
              if o != "not sure yet")], []),
 # ...and the DIGITS are untouched, because they are what ungrounded_figures
 # grounds the reply on - the 2026-09-09 defect where every budget turn was
 # binned and the client got the bare question.
 ("...and the figures behind them are unchanged",
  [o for o in next(f for f in t.SERVICE_FIELDS["new_hiring"]
                   if f.key == "budget").options
   if not any(n in o for n in ("500", "600", "700", "800"))], ["not sure yet"]),

 # --- the agency's team test, 2026-09-17 --------------------------------
 # "Option of asking landed property is missing." It was in the OPTIONS and
 # never in the question, so _field_guidance picked two as examples and the
 # client was never shown the one that described their home. Naming them all
 # is what `languages` got on 2026-09-07 and `nationality` on 2026-09-11.
 ("the home question offers landed property, and offers it by name",
  ("landed property" in _flat(_home_type().question)
   and "landed property" in _home_type().options), True),
 # ...and it can only name them all because the room counts went. With "HDB
 # 1-3 room" in the list, spelling the options out reads a bracket at the
 # client, which is the 2026-09-10 complaint.
 ("...which is possible because the home question carries no bracket",
  _reads_a_bracket(_home_type()), False),
 ("...and naming them is what the guidance now tells the model to do",
  "name them all" in _flat(ico._field_guidance("new_hiring", {}, _home_type())), True),
 # "Ask how many people living in household but doesn't ask the people
 # staying and ages." The count alone cannot be matched: six people is two
 # adults and four children, or four adults and two elderly parents.
 ("the household question asks who lives there, not just how many",
  all(w in _flat(_hh().question).lower()
      for w in ("how many", "who are they", "elderly", "children")), True),
 # "Why will knowing my name help you in recommending a helper that suits my
 # household?" - the reason for the RUN of questions welded onto the NAME
 # question, producing a claim that is not true.
 ("the collection reason is never bolted onto the question as its reason",
  ("BELONGS TO THE QUESTIONS AS A WHOLE" in _purpose_note()
   and "May I know your name so we can recommend" in _purpose_note()), True),
 # "bot should highlight that one helper cannot manage all the duties
 # assigned ... consider limited scope to focus rather than move on."
 # BOTH halves required, and the thresholds are high on purpose: firing
 # wrongly tells a client their job is too big when it is not.
 ("more work than one helper can carry is recognised",
  [label for c, want, label in _WORKLOAD_CASES if ico._heavy_workload(c) != want], []),
 # It is advice, not a refusal, and it must not reach for a figure - a
 # number here gets the whole reply binned and they lose the advice with it.
 ("...and the note refuses nobody and quotes nothing",
  all(s in _workload_note() for s in
      ("not a rejection", "Quote NO figure", "Say it once")), True),
 # Said once, like briefed_services - and for the same reason it must not be
 # recorded when a guard threw the reply away (2026-09-08 briefing_lost).
 ("the once-only notes survive the turn reset",
  "flagged_once" in _STATE_SRC and "flagged_once" not in graph_mod._TURN_RESET, True),
 # "it does not introduce it as a chatbot but gives this reply" - a first
 # message that was a PROCESS question, so PROCESS_INSTRUCTION replaced the
 # whole instruction and said nothing about introducing yourself.
 ("Claire introduces herself whichever instruction wins the turn",
  ("AI assistant" in tmpl.FIRST_CONTACT_INTRO_NOTE
   and "FIRST" in tmpl.FIRST_CONTACT_INTRO_NOTE), True),

 # --- a guard that was breaking the reply it was protecting, 2026-09-17 ---
 # Found while verifying the first-contact introduction, on the agency's own
 # process question. strip_handover_talk re-joined on " ", so a seven-step
 # answer came back as "1. Consultation ... 2. 3. Interview ..." with every
 # line break gone - the same defect clamp_reply had until 2026-09-08, on the
 # same replies.
 ("stripping a handover promise does not flatten a list",
  _guards.strip_handover_talk(_STEPS_OK).count(chr(10)), 3),
 # ...and the step it deleted was the agency's own published turnaround,
 # grounded in the records and passed by ungrounded_figures. This guard is for
 # "Grace will call you back at 3pm", not for how long our own service takes.
 ("a step may state how long OUR OWN service takes",
  "48 hours" in _guards.strip_handover_talk(_STEPS_OK), True),
 ("...and the list comes back untouched",
  _guards.strip_handover_talk(_STEPS_OK), _STEPS_OK),
 # A step that really does promise somebody will ring them still goes, and it
 # goes whole - a bare "2." left behind reads worse than the missing step.
 ("a step promising a callback still goes, and leaves no orphan marker",
  ("call you" not in _guards.strip_handover_talk(_STEPS_PROMISE)
   and "\n2.\n" not in _guards.strip_handover_talk(_STEPS_PROMISE)
   and "3. She arrives." in _guards.strip_handover_talk(_STEPS_PROMISE)), True),
 # Prose is unchanged: this is the case the guard was built for.
 ("a named colleague and a time in prose are still stripped",
  _guards.strip_handover_talk("I have passed this on. Grace will call you back at 3pm."),
  "I have passed this on."),
 ("...and an announced handover is still left alone",
  _guards.strip_handover_talk(
      "I have passed this to our team and a live agent will connect with you shortly."),
  "I have passed this to our team and a live agent will connect with you shortly."),
]
bad = 0
for label, got, want in rows:
    ok = got == want
    bad += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {label:40} {got!r}")
print("\nALL PASS" if not bad else f"\n{bad} FAILED")

raise SystemExit(1 if bad else 0)
