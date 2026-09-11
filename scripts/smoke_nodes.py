"""Actually RUN each graph node once, with the LLM and the database stubbed.

The point is to catch errors that only appear when the code executes —
UnboundLocalError, a bad f-string, a renamed helper. selfcheck_flows.py reads
data structures and passed cleanly on 2026-09-04 while info_collector raised
UnboundLocalError on every single turn: the intro note read `first_contact`
eighty lines above the line that assigned it. The graph caught it as
"bot_confused" and handed each message to a human, so from the client's side
the bot had simply stopped replying.

2026-09-08: extended to response_generator and blocked_topic_responder.
Until then this file ran ONE node, so the two reply-writing paths had no
execution cover at all — and the change that
day introduced a `process_question` flag read in three places, which is the
same shape as the bug above. A node that is never executed is a node whose
next typo reaches a client.

Nothing here touches the network or the database — every outbound call is
replaced before the nodes are imported. Safe to run anywhere.

    python scripts/smoke_nodes.py
    docker compose exec chatbot python /app/scripts/smoke_nodes.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = {
    "conversation_id": 1,
    "phone": "+6591234567",
    "customer_name": "Thomas",
    "incoming_text": "Hi I want passport renewal for my helper",
    "history_text": "",
    "contact_type": "employer",
    "intent": "passport_renewal",
    "service_type": "passport_renewal",
    "confidence": 0.99,
    "collected_info": {},
    "asked_field_counts": {},
    "missing_field_keys": [],
    "rag_matches": [{"similarity": 0.65, "question": "How long does passport renewal take?",
                     "answer": "Roughly 6 to 8 weeks.", "chunk_type": "qa_pair"}],
    "rag_context": "Based on our records:\n1. Q: How long...\n   A: Roughly 6 to 8 weeks.",
    "rag_best_score": 0.65,
    "blocked_topics": {},
    "prior_hires": 0,
    "placed_helper": None,
    "recent_tickets": [],
    "matched_lead": None,
    "lead_kind": None,
}

# A stepped reply, so the widened clamp and the list-marker masking are
# exercised on the way out rather than only in a unit assertion.
STEPPED_REPLY = (
    "Here is how it runs:\n"
    "1. We go through what your household needs.\n"
    "2. We shortlist helpers and you interview them.\n"
    "3. We apply to MOM for her work pass.\n"
    "4. Her embassy paperwork and medical are done.\n"
    "5. She arrives and we hand her over.\n"
    "Happy to go into any of those in more detail."
)

CASES = [
    ("info_collector", "first contact", {}),
    ("info_collector", "mid-conversation", {"history_text": "client: hi\nbot: Hello",
                                            "collected_info": {"helper_name": "Koko"},
                                            "asked_field_counts": {"helper_name": 1}}),
    ("info_collector", "new_hiring opening", {"service_type": "new_hiring", "intent": "new_hiring"}),
    ("info_collector", "recognised client",
     {"prior_hires": 1, "placed_helper": {"helper_name": "Liza Fernandez",
                                          "nationality": "PH",
                                          "passport_expiry": "27 September 2033"}}),
    ("info_collector", "volunteered requirement",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "She shouldn't smoke and no drinking please",
      "history_text": "bot: How many people live at home?"}),
    ("info_collector", "transfer take-on",
     {"service_type": "transfer_employer", "intent": "transfer",
      "incoming_text": "I'm looking for a transfer helper"}),
    ("info_collector", "insurance", {"service_type": "insurance", "intent": "insurance"}),
    # Exercises nationality_note: passport renewal, a route-dependent question,
    # nationality NOT yet collected. The branch selfcheck can only inspect.
    ("info_collector", "passport, no nationality",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "what documents are needed",
      "collected_info": {"helper_name": "Shushi"},
      "asked_field_counts": {"helper_name": 1},
      "history_text": "bot: May I know your helper's name?"}),
    ("info_collector", "passport, nationality known",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "what documents are needed",
      "collected_info": {"helper_name": "Shushi", "nationality": "Myanmar"},
      "asked_field_counts": {"helper_name": 1, "nationality": 1},
      "history_text": "bot: Which country is her passport from?"}),
    # Home leave, the two halves of the route branch. A cost question with the
    # nationality unknown must raise the caveat; once she is Filipino it must
    # not, because there is then one answer and it is $400.
    ("info_collector", "home leave, no nationality",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "how much does home leave cost",
      "collected_info": {"helper_name": "Liza"},
      "asked_field_counts": {"helper_name": 1},
      "history_text": "You: May I know your helper's name?"}),
    ("info_collector", "home leave, nationality known",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "how much does home leave cost",
      "collected_info": {"helper_name": "Liza", "nationality": "Filipino"},
      "asked_field_counts": {"helper_name": 1, "nationality": 1},
      "history_text": "You: Which country is she from?"}),
    # The 2026-09-08 regression, executed rather than inspected: a requirement
    # of "general housework" opens neither of requirement's two gates, and the
    # undecidable-gate rule blanked it and re-asked three times.
    ("info_collector", "hiring, general housework",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Only general housework",
      "collected_info": {"requirement": "general housework"},
      "asked_field_counts": {"requirement": 2},
      "history_text": "You: What would you mainly need help with?"}),
    # A bare "Yes" to an open question, which must be asked once more.
    ("info_collector", "hiring, bare yes on age/experience",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Yes",
      "collected_info": {"requirement": "general housework",
                         "helper_profile": "Yes"},
      "asked_field_counts": {"requirement": 1, "helper_profile": 1},
      "history_text": "You: Any preference on her age or how much experience "
                      "she should have?"}),
    # The 2026-09-08 passport-renewal redesign: the turn after the nationality
    # lands must EXECUTE the briefing branch, not just satisfy a data check.
    # This is the shape of the 2026-09-04 UnboundLocalError - a new flag read
    # in three places - and only running the node catches it.
    ("info_collector", "passport, the briefing turn",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "Indonesian",
      "collected_info": {"full_name": "Kapil", "helper_name": "Michan",
                         "nationality": "Indonesian"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Indonesian passport renewal costs $450 and takes about 3 working days.",
      "history_text": "You: Which country is her passport from?"}),
    ("info_collector", "passport, already briefed",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "In 2 weeks",
      "collected_info": {"full_name": "Kapil", "helper_name": "Michan",
                         "nationality": "Indonesian", "passport_expiry": "2 weeks"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1,
                             "passport_expiry": 1},
      "briefed_services": ["passport_renewal"],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "history_text": "You: When does her current passport expire?"}),
    # The name rule, executed. The suppression flag is set ~100 lines above
    # where the prompt state is built, which is the UnboundLocalError shape
    # this file exists to catch - and it did, on seven states.
    ("info_collector", "passport, new number, push name only",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "i want to renew my helper passport",
      "customer_name": "Vaidik", "record_name": "",
      "collected_info": {}, "asked_field_counts": {}}),
    # The CANDIDATE flow, 2026-09-10. Live it opened "Hi Vaidik Dubey, I'm
    # Claire ... Which country are you from?" - the push name used as her own
    # and no name question at all. A helper has no employer record, so
    # record_name is always empty here: the question must always be asked, and
    # the push name must not reach the prompt.
    ("info_collector", "candidate, new number, push name only",
     {"service_type": "candidate_new_hiring", "intent": "candidate_registration",
      "contact_type": "candidate",
      "incoming_text": "hi i want job",
      "customer_name": "Vaidik Dubey", "record_name": "",
      "collected_info": {}, "asked_field_counts": {}}),
    # ...and the turn after she types it, which is the half the agency asked
    # for in the same sentence: "then it should greet after taking name with
    # followup question".
    ("info_collector", "candidate, greets on the name she just gave",
     {"service_type": "candidate_new_hiring", "intent": "candidate_registration",
      "contact_type": "candidate",
      "incoming_text": "Siti",
      "customer_name": "Vaidik Dubey", "record_name": "",
      "collected_info": {"full_name": "Siti"},
      "asked_field_counts": {"full_name": 1},
      "history_text": "You: May I know your name?"}),
    # Deep into the matching half - the questions that did not exist before
    # 2026-09-10, so the ticket reached a consultant with her country, her
    # scope and her years and nothing to match an employer's ticket against.
    ("info_collector", "candidate, the matching questions",
     {"service_type": "candidate_new_hiring", "intent": "candidate_registration",
      "contact_type": "candidate",
      "incoming_text": "from next month",
      "collected_info": {"full_name": "Siti", "nationality": "Indonesia",
                         "age": "32", "work_scope": "childcare and eldercare",
                         "experience": "3 years childcare, 2 years eldercare",
                         "experience_field": "childcare and eldercare",
                         "current_location": "overseas",
                         "availability": "next month"},
      "asked_field_counts": {"full_name": 1, "nationality": 1, "age": 1,
                             "work_scope": 1, "experience": 1,
                             "experience_field": 1, "current_location": 1,
                             "availability": 1},
      "history_text": "You: When would you be able to start?"}),
    # The closing briefing on a registration, executed. This is the turn that
    # quoted the passport renewal's $450 as the price of applying for work
    # before the candidate flow got its own note - the guard binned the reply,
    # so what the client actually saw was the bare handover line.
    ("info_collector", "candidate, the closing briefing",
     {"service_type": "candidate_new_hiring", "intent": "candidate_registration",
      "contact_type": "candidate",
      "incoming_text": "on whatsapp",
      "collected_info": {"full_name": "Siti", "nationality": "Indonesia",
                         "age": "32", "work_scope": "childcare and eldercare",
                         "experience": "5 years",
                         "experience_field": "childcare and eldercare",
                         "current_location": "overseas", "availability": "next month",
                         "languages_spoken": "Bahasa, English",
                         "duties_willing": "gardening", "pet_comfort": "yes",
                         "room_sharing": "can share", "rest_day_preference": "weekly",
                         "expected_salary": "not sure",
                         "candidate_notes": "prefers older children"},
      "asked_field_counts": {k: 1 for k in (
          "full_name", "nationality", "age", "work_scope", "experience",
          "experience_field", "current_location", "availability",
          "languages_spoken", "duties_willing", "pet_comfort", "room_sharing",
          "rest_day_preference", "expected_salary", "candidate_notes")},
      "briefed_services": [],
      "rag_matches": [{"question": "What happens after I register?",
                       "answer": "We match you and arrange an interview.",
                       "similarity": 0.7}],
      "rag_context": "Based on our records: Q: What happens after I register?",
      "history_text": "You: Would you prefer updates by email, or here on WhatsApp?"}),
    ("info_collector", "passport, name on our file",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "i want to renew my helper passport",
      "customer_name": "Vaidik", "record_name": "Vaidik Dubey",
      "contact_type": "employer",
      "collected_info": {}, "asked_field_counts": {}}),
    # The closing turn: every field answered, so this is the one that explains
    # the whole service. Myanmar deliberately - we hold no fee for her, and the
    # briefing must say so rather than borrow the $450 beside it.
    ("info_collector", "passport, the closing briefing",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "in 2 months",
      "collected_info": {"full_name": "Vaidik", "helper_name": "Holabhola",
                         "nationality": "Myanmar", "passport_expiry": "2 months",
                         "helper_location": "in Singapore",
                         "permit_expiry": "2 months"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1,
                             "passport_expiry": 1, "helper_location": 1,
                             "permit_expiry": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "A consultant will confirm the cost for her embassy.",
      "history_text": "You: Thanks, and when does her Work Permit expire?"}),
    ("info_collector", "direct hire",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "I already found a helper, can you process her",
      "collected_info": {"employment_status": "currently employed"}}),
    ("info_collector", "direct hire, location unknown",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "how long does it take",
      "collected_info": {"helper_name": "Ruru"},
      "asked_field_counts": {"helper_name": 1},
      "history_text": "bot: May I know the full name of the helper?"}),
    ("info_collector", "direct hire, location known",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "how long does it take",
      "collected_info": {"helper_name": "Ruru", "helper_location": "already in Singapore"},
      "asked_field_counts": {"helper_name": 1},
      "history_text": "bot: Where is she at the moment?"}),
    ("info_collector", "transfer, direction opens no branch",
     {"service_type": "transfer_employer", "intent": "transfer",
      "incoming_text": "Hi I'm looking for a transfer helper",
      "collected_info": {"transfer_direction": "transfer"},
      "history_text": ""}),
    ("info_collector", "transfer, direction answered",
     {"service_type": "transfer_employer", "intent": "transfer",
      "incoming_text": "taking one on",
      "collected_info": {"full_name": "Thomas",
                         "transfer_direction": "taking on a transfer helper"},
      "asked_field_counts": {"transfer_direction": 1},
      "history_text": "You: Are you taking on a transfer helper, or releasing yours?"}),
    ("info_collector", "transfer take-on, NEW client",
     {"service_type": "transfer_employer", "intent": "transfer",
      "incoming_text": "hi i am looking for a transfer helper",
      "collected_info": {"full_name": "Vaidik",
                         "transfer_direction": "looking for a transfer helper"},
      "history_text": ""}),
    ("info_collector", "transfer take-on, EXISTING client",
     {"service_type": "transfer_employer", "intent": "transfer",
      "incoming_text": "hi i am looking for a transfer helper",
      "prior_hires": 2,
      "collected_info": {"full_name": "Vaidik",
                         "transfer_direction": "looking for a transfer helper"},
      "history_text": ""}),
    # --- response_generator ------------------------------------------------
    # The stepped-answer path: both halves of the trigger, then each half on
    # its own, then neither. A process question with NO records must stay on
    # the ordinary two-sentence path, because a long reply improvised from
    # nothing is the worst of the three outcomes.
    ("response_generator", "process question, records",
     {"intent": "process_question", "service_type": "new_hiring",
      "incoming_text": "What is the full process for hiring a helper?",
      "history_text": "client: hi\nbot: Hello"}),
    ("response_generator", "process question, NO records",
     {"intent": "process_question", "service_type": "new_hiring",
      "incoming_text": "What is the full process for hiring a helper?",
      "rag_context": "", "rag_matches": [], "rag_best_score": 0.0,
      "history_text": "client: hi\nbot: Hello"}),
    ("response_generator", "documents question",
     {"intent": "document_question", "service_type": "new_hiring",
      "incoming_text": "what documents do I need to provide",
      "history_text": "client: hi\nbot: Hello"}),
    ("response_generator", "ordinary question",
     {"intent": "general_question", "service_type": "new_hiring",
      "incoming_text": "which nationality is cheapest",
      "history_text": "client: hi\nbot: Hello"}),
    ("response_generator", "first message", {"history_text": ""}),
    # A job seeker from a country Ming Hwee does not recruit from. She is told
    # so and her follow-ups are answered; she is NOT collected from and NOT
    # handed to a human, because "we do not recruit from your country" is an
    # answer we hold. Agency instruction, 2026-09-11.
    ("info_collector", "candidate, a country we do not place from",
     {"intent": "candidate_registration", "service_type": "candidate_new_hiring",
      "contact_type": "candidate",
      "incoming_text": "i am from india",
      "history_text": "client: i need a job\nbot: Which country are you from?",
      "collected_info": {"full_name": "Asha", "nationality": "India"},
      "_expect_state": {"info_complete": False, "needs_handover": False,
                        "missing_field_keys": []}}),
    # ...and the follow-up two turns later still lands here rather than
    # restarting the collection, which is what makes "why?" answerable.
    ("info_collector", "candidate, still unplaceable, asking why",
     {"intent": "candidate_registration", "service_type": "candidate_new_hiring",
      "contact_type": "candidate",
      "incoming_text": "why? i really want a job",
      "history_text": "client: i am from india\nbot: We are only able to place "
                      "helpers from the Philippines, Indonesia and Myanmar.",
      "collected_info": {"full_name": "Asha", "nationality": "India"},
      "_expect_state": {"info_complete": False, "missing_field_keys": []}}),
    # The control: one of the three is collected from exactly as before, and
    # the flow is NOT finished after her country.
    ("info_collector", "candidate, a country we do place from",
     {"intent": "candidate_registration", "service_type": "candidate_new_hiring",
      "contact_type": "candidate",
      "incoming_text": "i am from indonesia",
      "history_text": "client: i need a job\nbot: Which country are you from?",
      "collected_info": {"full_name": "Siti", "nationality": "Indonesia"},
      "_expect_state": {"info_complete": False}}),
    # --- blocked_topic_responder -------------------------------------------
    # A question asked while a topic sits with an agent. The stepped branch is
    # allowed here too, and the holding branch must stay two sentences.
    ("blocked_topic_responder", "process question, parked",
     {"intent": "process_question", "service_type": "new_hiring",
      "incoming_text": "what is the full process for hiring a helper",
      "history_text": "client: hi\nbot: I've passed this to our team.",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0001"}}}),
    # A job seeker asking for HER documents after the handover. Live
    # 2026-09-10 this exact sentence got "I'll check with the team and come
    # back to you shortly." while the answer sat in the records; the phrasing
    # is kept verbatim so the state names the turn that failed.
    ("blocked_topic_responder", "candidate, her documents while parked",
     {"intent": "document_question", "service_type": "candidate_new_hiring",
      "contact_type": "employer",
      "incoming_text": "Tell me the documents I needed",
      "history_text": "client: Here\nbot: Here is what happens next, Ruru:",
      # Not the holding line. STEPPED_REPLY is what the stub returns, so this
      # asserts the turn was ANSWERED rather than passed to a human - which is
      # the whole complaint, and which asks_general_info alone decides.
      "_expect_reply": "1.",
      "blocked_topics": {"candidate_new_hiring": {"ticket_id": 1,
                                                  "ticket_number": "CB-2026-0009"}}}),
    # ...and asking about money. She pays us nothing, and the reply must not
    # reach for a figure that belongs to the employer's price list.
    ("blocked_topic_responder", "candidate, asking about fees while parked",
     {"intent": "fee_enquiry", "service_type": "candidate_new_hiring",
      "contact_type": "employer",
      "incoming_text": "Is there any fees I need to pay",
      "history_text": "client: hi\nbot: I've passed this to our team.",
      "blocked_topics": {"candidate_new_hiring": {"ticket_id": 1,
                                                  "ticket_number": "CB-2026-0009"}}}),
    # The hiring-cost guard on the path that never had it. The stub is the
    # reply that actually went out on 2026-09-10 with a hiring ticket parked -
    # every figure in it is genuinely in Form A, which is why every other guard
    # passed it.
    ("blocked_topic_responder", "employer, a parked reply that prices the hire",
     {"intent": "fee_enquiry", "service_type": "new_hiring",
      "incoming_text": "Is there any fees I need to pay",
      "history_text": "client: hi\nbot: I've passed this to our team.",
      "_stub_reply": ("The approximate total service fee and third-party costs "
                      "are $4,225, with a combined total of about $4,285."),
      # The deferral, not the figure and not the bare holding line: the client
      # is told WHY they are getting a person instead of a number.
      "_expect_reply": "would rather one of our consultants",
      "blocked_topics": {"new_hiring": {"ticket_id": 1,
                                        "ticket_number": "CB-2026-0001"}}}),
    ("blocked_topic_responder", "ordinary message, parked",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "ok noted thanks",
      "history_text": "client: hi\nbot: I've passed this to our team.",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0001"}}}),
]


async def _run_info_collector(state, stub=None):
    with patch("app.graph.nodes.info_collector.complete",
               new=AsyncMock(return_value=stub or "When does her passport expire?"),
               create=True), \
         patch("app.graph.nodes.info_collector.complete_json",
               new=AsyncMock(return_value={}), create=True), \
         patch("app.graph.nodes.info_collector._open_lead_early",
               new=AsyncMock(return_value={})):
        from app.graph.nodes.info_collector import info_collector
        return await info_collector(state)


async def _run_response_generator(state, stub=None):
    # Returns a stepped reply so the widened clamp and the list-marker masking
    # are exercised end to end, not just in a unit assertion.
    with patch("app.graph.nodes.response_generator.complete",
               new=AsyncMock(return_value=stub or STEPPED_REPLY), create=True):
        from app.graph.nodes.response_generator import response_generator
        return await response_generator(state)


async def _run_blocked_topic_responder(state, stub=None):
    with patch("app.graph.nodes.blocked_topic_responder.complete",
               new=AsyncMock(return_value=stub or STEPPED_REPLY), create=True):
        from app.graph.nodes.blocked_topic_responder import blocked_topic_responder
        return await blocked_topic_responder(state)


RUNNERS = {
    "info_collector": _run_info_collector,
    "response_generator": _run_response_generator,
    "blocked_topic_responder": _run_blocked_topic_responder,
}


async def _lid_checks() -> list[tuple[str, bool]]:
    """The webhook's LID resolution, with Whapi stubbed.

    Live 2026-09-10: a migrated sender's payload carried no phone number at all
    (both `from` and `chat_id` were "116909177569373@lid"), normalize_phone
    turned that into "+116909177569373", and an allowlisted client was stood
    down on every message. Everything downstream is keyed on the phone, so the
    resolution has to happen before any of it - which is why this exercises the
    webhook rather than the parser.
    """
    from app.api.webhook import _resolve_lid
    from app.whapi.parser import parse_webhook

    def payload(**over):
        msg = {"id": "lid-1", "type": "text", "from_me": False,
               "chat_id": "116909177569373@lid", "from": "116909177569373@lid",
               "text": {"body": "hi"}}
        msg.update(over)
        return {"messages": [msg]}

    results = []

    # Resolved: the real number replaces the LID.
    m = parse_webhook(payload())[0]
    with patch("app.api.webhook.whapi.resolve_lid",
               new=AsyncMock(return_value="+917970027379")):
        await _resolve_lid(m)
    results.append(("a resolved LID becomes the client's number",
                    m.customer_number == "+917970027379"))

    # Unresolvable: unchanged, so it stands down exactly as before - never a
    # guess at whose number it might be.
    m = parse_webhook(payload())[0]
    with patch("app.api.webhook.whapi.resolve_lid", new=AsyncMock(return_value=None)):
        await _resolve_lid(m)
    results.append(("an unresolvable LID is left alone, not guessed",
                    m.customer_number == "+116909177569373"))

    # And the wiring, not just the function. Removing the call from
    # handle_payload left the three checks above green, because they call
    # _resolve_lid directly - a check that passes while the thing it is about
    # is broken, which is the failure mode this whole script exists for. This
    # one goes through handle_payload and reads what the inbound handler was
    # actually handed.
    from app.api import webhook as _wh
    seen: list[str] = []

    async def _capture(message):
        seen.append(message.customer_number)

    with patch("app.api.webhook.whapi.resolve_lid",
               new=AsyncMock(return_value="+917970027379")),          patch.object(_wh, "handle_inbound", new=_capture):
        await _wh.handle_payload(payload())
    results.append(("handle_payload resolves before it dispatches",
                    seen == ["+917970027379"]))

    # An ordinary message never calls Whapi at all.
    m = parse_webhook(payload(**{"from": "917970027379@s.whatsapp.net",
                                 "chat_id": "917970027379@s.whatsapp.net"}))[0]
    stub = AsyncMock(return_value="+000")
    with patch("app.api.webhook.whapi.resolve_lid", new=stub):
        await _resolve_lid(m)
    results.append(("an ordinary message never asks Whapi anything",
                    m.customer_number == "+917970027379" and not stub.called))
    return results


async def main() -> int:
    failures = 0
    for label, ok in await _lid_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  webhook  {label:44}")
        failures += not ok
    for node, label, overrides in CASES:
        state = {**BASE, **overrides}
        # A state may name the reply the model would have written, so a guard
        # that only fires on particular WORDS can be executed rather than only
        # unit-tested. Added 2026-09-10 for the hiring-cost guard, which had
        # never run on this path at all.
        stub = state.pop("_stub_reply", None)
        # What the reply must CONTAIN. Optional, and most states do not use it -
        # this file's first job is execution cover. But a state that exists to
        # prove a guard fires proves nothing while the only test is that a dict
        # came back: on 2026-09-10 the hiring-cost guard was disabled outright
        # (`if False:`) and every check in both scripts stayed green, because
        # selfcheck asserts the module IMPORTS the guard and this one asserted
        # only the shape of the return. Imported and never called is precisely
        # the state that guard was in for two days.
        expect = state.pop("_expect_reply", None)
        # ...and what the returned STATE must say. A reply-writing branch also
        # decides whether the turn hands over and whether the collection is
        # finished, and neither shows up in the text. Added 2026-09-11 for the
        # branch that declines a job seeker we cannot place: the assertion that
        # it does NOT hand her to a human was, on its first attempt, a string
        # search of the source that matched the import line instead.
        expect_state = state.pop("_expect_state", None)
        full = f"{node}  {label}"
        with patch("app.graph.llm.complete",
                   new=AsyncMock(return_value=stub or STEPPED_REPLY)), \
             patch("app.graph.llm.complete_json", new=AsyncMock(return_value={})):
            try:
                out = await RUNNERS[node](state, stub)
                ok = isinstance(out, dict)
                detail = sorted(out)[:4] if ok else out
                if ok and expect:
                    got = out.get("reply") or out.get("reply_text") or ""
                    ok = expect.lower() in got.lower()
                    detail = f"expected {expect!r} in {got[:80]!r}"
                if ok and expect_state:
                    wrong = {k: out.get(k) for k, v in expect_state.items()
                             if out.get(k) != v}
                    ok = not wrong
                    detail = f"state {expect_state} -> " + ("as expected" if ok
                                                            else f"got {wrong}")
                print(f"  {'PASS' if ok else 'FAIL'}  {full:44} -> {detail}")
                failures += not ok
            except Exception as exc:  # noqa: BLE001 - reporting is the whole job
                print(f"  FAIL  {full:44} -> {type(exc).__name__}: {exc}")
                failures += 1
    print("\nALL PASS" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
