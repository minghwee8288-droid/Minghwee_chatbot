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

# Step 2c: what a renewal turn retrieves before the extraction (no fee row),
# what the briefing search returns, and a briefing quoting the fee.
_RENEWAL_GENERAL_ROWS = (
    "Based on our records:\n1. Q: How do I renew my helper's work permit?\n"
    "   A: Ming Hwee handles all of it. It usually takes about a week."
)
_RENEWAL_BRIEFING_ROWS = (
    "Based on our records:\n1. Q: How much does it cost to renew my helper's work permit?\n"
    "   A: The agency fee is approximately $695.\n"
    "2. Q: How long does a work permit renewal take?\n   A: It usually takes about a week."
)
_RENEWAL_BRIEFING_REPLY = (
    "Here is everything for Devi's Work Permit renewal:\n\n"
    "It usually takes about a week.\n\nThe cost is approximately $695.\n\n"
    "Thank you. I've passed everything to our team, and a live agent will connect "
    "with you shortly. In the meantime, is there anything else I can help you with?"
)

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

# The helper-rights summary row's own text: ten numbered rights and the
# contact line (scripts/load_service_notes.py).
from scripts.load_service_notes import _HELPER_RIGHTS_ANSWER as _HELPER_RIGHTS  # noqa: E402
from app.graph.guards import NATIONALITY_FEE_DEFERRAL  # noqa: E402
# Where we hold no fee for her (Step 5 item 17): names her and the service.
_MM_PASSPORT_DEFERRAL = "Our agent will confirm the cost for a Myanmar helper's passport renewal."

# Step 4: the fee rows as the KB states them - the passport fee for two of the
# three nationalities, and the new-hire agency fee for all three.
_PASSPORT_FEE_ROW = (
    "Based on our records:\n1. Q: How much does it cost to renew my helper's passport?\n"
    "   A: It is $450 for a Filipino helper and $450 for an Indonesian helper. If your "
    "helper is of another nationality, tell us and our agent will confirm the cost."
)
# The knowledge base row the rest-day rule is quoted from (Batch C item 7).
_REST_DAY_RULE_ROW = (
    "Your helper is entitled to one rest day a week. If she agrees, a weekly rest day can "
    "be worked and compensated with at least one day's wage on top of her salary. However, "
    "since 1 January 2023, at least one rest day a month must actually be taken and cannot "
    "be compensated away."
)
_RENEWAL_FEE_ROW = (
    "Based on our records:\n1. Q: How much does it cost to renew my helper's work permit?\n"
    "   A: Work permit renewal is $695. That covers us handling the whole thing for you - "
    "the authorisation, the declaration, the insurance arrangement and the submission to MOM."
)
_NEW_HIRE_FEE_ROWS = (
    "Based on our records:\n1. The agency fee for a new hire from the Philippines is $1,428.\n"
    "2. The agency fee for a new hire from Indonesia is $1,188.\n"
    "3. The agency fee for a new hire from Myanmar is $1,168."
)
_MM_PASSPORT_BRIEFING_REPLY = (
    "Here is everything for Holabhola's passport renewal:\n\n"
    "The appointment wait can take several weeks.\n\n"
    "The cost is approximately $450.\n\n"
    "Here is what we will need from you:\n1. A copy of your NRIC\n2. A copy of her passport\n\n"
    "Thank you. I've passed everything to our team, and a live agent will connect "
    "with you shortly."
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
    # 2026-10-08, the agency: a helper we know the client has is CONFIRMED by
    # name, not asked for; home leave asks the reason; "welcome back" only
    # after a real break.
    ("info_collector", "a known helper is offered, not asked for",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "my helper wants to go home",
      "collected_info": {"full_name": "VD"}, "asked_field_counts": {},
      "known_helpers": [{"helper_name": "Yoyo", "nationality": "Indonesian"}],
      "_expect_prompt": 'Is this home leave for Yoyo, or for another helper?'}),
    # Step 5 item 11: named once, inside the question - never stated and
    # then asked about.
    ("info_collector", "a known helper is named once, inside the confirmation question",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "my helper wants to go home",
      "collected_info": {"full_name": "VD"}, "asked_field_counts": {},
      "known_helpers": [{"helper_name": "Yoyo", "nationality": "Indonesian"}],
      "_stub_extraction": {},
      "_stub_reply": "We have Yoyo's details on record. Is this home leave for Yoyo, or for another helper?",
      "_expect_prompt": "Name her ONLY inside this one question",
      "_expect_reply": "Is this home leave for Yoyo, or for another helper?",
      "_forbid_reply": "details on record"}),
    # Step 5 Batch C item 7: no rest day - acknowledged, the MOM rule from the
    # KB in one sentence, then the next question.
    ("info_collector", "C7: 'no rest day' gets the MOM rule from the KB, then carries on",
     {"service_type": "new_hiring", "intent": "new_hiring", "contact_type": "employer",
      "incoming_text": "no rest day, she can work every day",
      "history_text": "You: How would you want to handle her rest days?",
      "collected_info": {"full_name": "VD", "requirement": "childcare", "budget": "SGD 600-700"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "budget": 1, "rest_day": 1},
      "_stub_extraction": {"rest_day": "no rest day"},
      "_stub_reply": "Noted. Under MOM rules she is entitled to one rest day a week, and since "
                     "1 January 2023 at least one a month must actually be taken. Any other "
                     "requirements, house rules or preferences we should note?",
      "_expect_prompt": "state the MOM rest-day rule in ONE short sentence",
      "_expect_reply": "1 January 2023"}),
    ("info_collector", "C7: ...a weekly day off gets no rule sentence",
     {"service_type": "new_hiring", "intent": "new_hiring", "contact_type": "employer",
      "incoming_text": "a weekly day off on Sundays",
      "history_text": "You: How would you want to handle her rest days?",
      "collected_info": {"full_name": "VD", "requirement": "childcare", "budget": "SGD 600-700"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "budget": 1, "rest_day": 1},
      "_stub_extraction": {"rest_day": "a weekly day off"},
      "_forbid_prompt": "MOM rest-day rule"}),
    # Step 5 Batch C item 6: the opening asks the question it was meant to ask.
    # The e2e direct hire asked whether she was in Singapore instead of the
    # client's name.
    ("info_collector", "C6: a reply asking about something else gets its own question",
     {"service_type": "direct_hiring", "intent": "direct_hiring", "contact_type": "employer",
      "incoming_text": "I want to do a direct hire", "history_text": "",
      "collected_info": {}, "asked_field_counts": {},
      "_stub_extraction": {},
      "_stub_reply": "Hi, I'm Claire, Ming Hwee's AI assistant. Is she already in Singapore, "
                     "working for another employer?",
      "_expect_reply": "May I know your name?",
      "_forbid_reply": "already in Singapore"}),
    ("info_collector", "C6: ...and a reply asking nothing gets it added",
     {"service_type": "transfer_employer", "intent": "transfer", "contact_type": "employer",
      "incoming_text": "I am looking for a transfer helper", "history_text": "",
      "collected_info": {}, "asked_field_counts": {},
      "_stub_extraction": {},
      "_stub_reply": "Hi, I'm Claire, Ming Hwee's AI assistant. I'll ask a few details so we "
                     "can find the right helper.",
      "_expect_reply": "May I know your name?"}),
    # Step 5 B2 item 7: a phone number that is not one is never filed, and the
    # question is asked again with the reason - never read back.
    ("info_collector", "B2: '+9612+654+941' is not filed, asked again with the reason",
     {"service_type": "home_leave", "intent": "home_leave", "contact_type": "employer",
      "incoming_text": "+9612+654+941",
      "history_text": "You: What is your employer's phone number, so our agent can contact "
                      "them to arrange your leave?",
      "flagged_once": ["helper_home_leave"],
      "collected_info": {"full_name": "Siti", "helper_name": "Siti", "requested_by": "helper",
                         "nationality": "Indonesian", "leave_dates": "1 to 30 November",
                         "leave_reason": "regular home leave", "employer_name": "Mr Tan"},
      "asked_field_counts": {"full_name": 1, "nationality": 1, "leave_dates": 1,
                             "leave_reason": 1, "employer_name": 1, "employer_contact": 1},
      "_stub_extraction": {"employer_contact": "+9612+654+941"},
      "_stub_reply": "Thanks. That number does not look complete - could you send your "
                     "employer's phone number again?",
      "_expect_prompt": "does not look like a phone number",
      "_expect_not_collected": ["employer_contact"],
      "_forbid_reply": "+9612"}),
    ("info_collector", "B2: ...while a real number is filed",
     {"service_type": "home_leave", "intent": "home_leave", "contact_type": "employer",
      "incoming_text": "+65 9123 4567",
      "history_text": "You: What is your employer's phone number, so our agent can contact "
                      "them to arrange your leave?",
      "flagged_once": ["helper_home_leave"],
      "collected_info": {"full_name": "Siti", "helper_name": "Siti", "requested_by": "helper",
                         "nationality": "Indonesian", "leave_dates": "1 to 30 November",
                         "leave_reason": "regular home leave", "employer_name": "Mr Tan"},
      "asked_field_counts": {"full_name": 1, "nationality": 1, "leave_dates": 1,
                             "leave_reason": 1, "employer_name": 1, "employer_contact": 1},
      "_stub_extraction": {"employer_contact": "+65 9123 4567"},
      "_expect_collected": {"employer_contact": "+65 9123 4567"}}),
    # Step 5 B2 item 4: a helper name typed in capitals is filed in title case.
    ("info_collector", "B2: 'DEVI' is filed as 'Devi'",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "DEVI",
      "history_text": "You: May I know your helper's name?",
      "collected_info": {"full_name": "VD"}, "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "_stub_extraction": {"helper_name": "DEVI"},
      "_expect_collected": {"helper_name": "Devi"}}),
    ("info_collector", "...and a yes confirms her, with what we hold about her",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "yes",
      "history_text": "You: Is this home leave for Yoyo, or for another helper?",
      "collected_info": {"full_name": "VD"}, "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "known_helpers": [{"helper_name": "Yoyo", "nationality": "Indonesian"}],
      "_stub_extraction": {"helper_name": "yes"},
      "_expect_collected": {"helper_name": "Yoyo", "nationality": "Indonesian"}}),
    # Conversation 4551, 2026-10-08: a HELPER asking for her own home leave.
    ("info_collector", "a helper asking for her own leave is asked about herself, not 'your helper'",
     {"service_type": "home_leave", "intent": "home_leave", "contact_type": "employer",
      "incoming_text": "Hey I need leave for 1 month. Could you please help me?",
      "history_text": "Client: Hello\nYou: Good evening, I'm Claire, Ming Hwee's AI assistant. How can I help you?",
      "_stub_extraction": {},
      "_expect_prompt": "HELPER herself, asking for her OWN home leave",
      "_expect_state": {"flagged_once": ["helper_home_leave"]}}),
    ("info_collector", "...her own name is the helper's name, and her employer's number is asked",
     {"service_type": "home_leave", "intent": "home_leave", "contact_type": "employer",
      "incoming_text": "it is for an emergency at home",
      "history_text": "You: What is the reason for your leave?",
      "flagged_once": ["helper_home_leave"],
      "collected_info": {"full_name": "Ratna", "nationality": "Indonesian",
                         "leave_dates": "next week, back in a month"},
      "asked_field_counts": {"full_name": 1, "nationality": 1, "leave_dates": 1, "leave_reason": 1},
      "_stub_extraction": {"leave_reason": "family emergency"},
      "_expect_collected": {"helper_name": "Ratna", "requested_by": "helper"},
      "_expect_prompt": "employer's name"}),
    ("info_collector", "...while an employer asking for their helper's leave runs as before",
     {"service_type": "home_leave", "intent": "home_leave", "contact_type": "employer",
      "incoming_text": "my helper needs home leave next month",
      "history_text": "Client: hi\nYou: Hi, how can I help?",
      "_stub_extraction": {},
      "_forbid_prompt": "HELPER herself, asking for her OWN home leave",
      "_expect_collected": {"requested_by": "employer"}}),
    # Step 5 Batch E item 1: the request restated is not the reason for it.
    # Phone test 14 Oct: the employer flow never asked the reason, because
    # "go back to her home country" was filed as leave_reason.
    ("info_collector", "E1: 'go back to her home country' is not filed as the leave reason",
     {"service_type": "home_leave", "intent": "home_leave", "contact_type": "employer",
      "incoming_text": "Hi my helper wants to go back to her home country for leave",
      "history_text": "",
      "_stub_extraction": {"leave_reason": "return to her home country"},
      "_expect_not_collected": ["leave_reason"]}),
    ("info_collector", "E1: ...while a reason the client volunteers is kept",
     {"service_type": "home_leave", "intent": "home_leave", "contact_type": "employer",
      "incoming_text": "my helper wants to go home for her yearly leave",
      "history_text": "",
      "_stub_extraction": {"leave_reason": "yearly leave"},
      "_expect_collected": {"leave_reason": "yearly leave"}}),
    ("info_collector", "'Singapore' is not a home leave's home country - asked again, with why",
     {"service_type": "home_leave", "intent": "home_leave", "contact_type": "employer",
      "incoming_text": "she is from Singapore",
      "history_text": "You: Which country is she from?",
      "collected_info": {"full_name": "Ratna", "helper_name": "Yoyo"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1},
      "_stub_extraction": {"nationality": "Singapore"},
      "_expect_not_collected": ["nationality"],
      "_expect_prompt": "Singapore - where she works"}),
    # Conversation 4551, 2026-10-08: asked about rest days, the client answered
    # the budget question before it, and the extractor filed rest_day "no
    # preference" too - a preference nobody gave, on the ticket.
    ("info_collector", "a 'no preference' the message never says is not filed",
     {"service_type": "new_hiring", "intent": "new_hiring", "contact_type": "employer",
      "incoming_text": "You can find someone between 500 and 700",
      "history_text": "You: How would you want to handle her rest days?",
      "collected_info": {"full_name": "Faizal Khan", "budget": "no preference"},
      "asked_field_counts": {"full_name": 1, "budget": 1, "rest_day": 1},
      "_stub_extraction": {"budget": "$500-700", "rest_day": "no preference"},
      "_expect_collected": {"budget": "$500-700"},
      "_expect_not_collected": ["rest_day"]}),
    ("info_collector", "...while one the client did say is kept",
     {"service_type": "new_hiring", "intent": "new_hiring", "contact_type": "employer",
      "incoming_text": "no preference on rest days",
      "history_text": "You: How would you want to handle her rest days?",
      "collected_info": {"full_name": "Faizal Khan"},
      "asked_field_counts": {"full_name": 1, "rest_day": 1},
      "_stub_extraction": {"rest_day": "no preference"},
      "_expect_collected": {"rest_day": "no preference"}}),
    ("info_collector", "...while 'no, another helper' does not",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "no, it is for another helper",
      "history_text": "You: Is this home leave for Yoyo, or for another helper?",
      "collected_info": {"full_name": "VD"}, "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "known_helpers": [{"helper_name": "Yoyo", "nationality": "Indonesian"}],
      "_stub_extraction": {},
      "_expect_not_collected": ["helper_name", "nationality"],
      "_forbid_prompt": "Do NOT ask for the helper's name"}),
    ("info_collector", "...and a name we do not know is that other helper",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "it is for maria",
      "history_text": "You: Is this home leave for Yoyo, or for another helper?",
      "collected_info": {"full_name": "VD"}, "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "known_helpers": [{"helper_name": "Yoyo", "nationality": "Indonesian"}],
      "_stub_extraction": {"helper_name": "Maria"},
      "_expect_collected": {"helper_name": "Maria"},
      "_expect_not_collected": ["nationality"]}),
    # Step 2b, 2026-10-13, conversation 3766: a renewal had collected the
    # helper's name, a home-leave intake started, and asked for it again -
    # helper_name is dropped on a switch and known_helpers was empty (no
    # employer record). A helper named earlier in THIS conversation is
    # offered for confirmation like a known one, on the switch turn...
    ("info_collector", "a helper named earlier in the conversation is offered after a switch",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "I also want to arrange home leave for her",
      "collected_service": "renewal",
      "collected_info": {"full_name": "VD", "helper_name": "devi lal singh",
                         "permit_expiry": "in 3 weeks"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "permit_expiry": 1},
      "known_helpers": [],
      "_stub_extraction": {},
      "_expect_prompt": "Is this home leave for devi lal singh, or for another helper?",
      "_expect_state": {"helpers_named": ["devi lal singh"]}}),
    # ...and on the next turn, from state.helpers_named, a yes confirms her.
    ("info_collector", "...and a yes on the next turn confirms her",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "yes",
      "history_text": "You: Is this home leave for devi lal singh, or for another helper?",
      "collected_service": "home_leave",
      "collected_info": {"full_name": "VD"}, "asked_field_counts": {"helper_name": 1},
      "known_helpers": [], "helpers_named": ["devi lal singh"],
      "_stub_extraction": {"helper_name": "yes"},
      # Filed in title case since Step 5 B2 item 4 (was "devi lal singh").
      "_expect_collected": {"helper_name": "Devi Lal Singh"}}),
    # ...but a helper's OWN name (her own home leave) is never offered back
    # to her as "your helper".
    # Two states, one per test, because a state meeting both proved neither
    # (both faults stayed green on the first run).
    ("info_collector", "...and a helper's own home leave is never remembered as 'her helper'",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "next week",
      "collected_service": "home_leave",
      "collected_info": {"full_name": "Siti", "helper_name": "Siti Rahayu", "requested_by": "helper"},
      "asked_field_counts": {"helper_name": 0},
      "_stub_extraction": {},
      "_expect_state": {"helpers_named": None}}),
    ("info_collector", "...nor a helper renewing her own passport",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "Indonesia",
      "collected_service": "passport_renewal",
      "collected_info": {"full_name": "Kareena", "helper_name": "Kareena"},
      "asked_field_counts": {"full_name": 1},
      "_stub_extraction": {},
      "_expect_state": {"helpers_named": None}}),
    ("info_collector", "...and a direct hire, whose helper is a NEW one, is not offered any",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "i want to do direct hire",
      "collected_info": {"full_name": "VD"}, "asked_field_counts": {"full_name": 1},
      "known_helpers": [{"helper_name": "Yoyo", "nationality": "Indonesian"}],
      "_forbid_prompt": "Do NOT ask for the helper's name"}),
    ("info_collector", "an existing client back after a break is welcomed back",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "my helper wants to go home",
      "recent_tickets": [{"service_type": ["passport_renewal"], "status": "open"}],
      "hours_since_last_message": 20.0,
      "_expect_prompt": "welcoming them back"}),
    ("info_collector", "...but not ten minutes into the same conversation",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "my helper wants to go home",
      "recent_tickets": [{"service_type": ["passport_renewal"], "status": "open"}],
      "hours_since_last_message": 0.2,
      "_forbid_prompt": "welcoming them back"}),
    ("info_collector", "home leave asks the reason for the leave",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "3 to 17 December",
      "collected_info": {"full_name": "VD", "helper_name": "Yoyo", "nationality": "Indonesian",
                         "leave_dates": "3 to 17 December"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1, "leave_dates": 1},
      "_expect_prompt": "reason for her leave"}),
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
                         "permit_expiry": "2 months"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1,
                             "passport_expiry": 1, "permit_expiry": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Our agent will confirm the cost for her embassy.",
      "history_text": "You: Thanks, and when does her Work Permit expire?"}),
    ("info_collector", "direct hire",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "I already found a helper, can you process her",
      "collected_info": {"helper_transfer_case": "yes, with another employer"}}),
    ("info_collector", "direct hire, route unknown",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "how long does it take",
      "collected_info": {"helper_name": "Ruru"},
      "asked_field_counts": {"helper_name": 1},
      "history_text": "bot: May I know the full name of the helper?"}),
    ("info_collector", "direct hire, route known",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "how long does it take",
      "collected_info": {"helper_name": "Ruru",
                         "helper_transfer_case": "yes, she is on a permit here"},
      "asked_field_counts": {"helper_name": 1},
      "history_text": "bot: Is she in Singapore on a Work Permit with another employer?"}),
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
    # --- whose flow an employer transfer lands in, 2026-09-18 --------------
    # The first turn of the agency's own transcript. "hi i want transfer
    # helper" matched _HELPER_SPEAKING's "i want transfer", so the contact was
    # read as the HELPER and the first question asked for her own name.
    #
    # Run on the CLASSIFIER and not on the collector, and the first draft of
    # this state had it the wrong way round: the collector never runs the
    # classifier, so with contact_type=None it resolved to transfer_employer
    # whatever the message said - the state passed for a reason that had
    # nothing to do with the fix, and its own control caught that by failing.
    ("intent_classifier", "an employer asking for a transfer helper reads as an employer",
     {"incoming_text": "hi i want transfer helper",
      "intent": None, "service_type": None, "history_text": "",
      "_stub_intent": {"intent": "transfer", "service_type": "transfer",
                       "contact_type": None, "confidence": 0.9},
      "_expect_state": {"detected_contact_type": "employer",
                        "service_type": "transfer"}}),
    # ...and the control, which is the half that must not be traded away: a
    # helper asking for herself keeps her own flow (2026-09-04).
    ("intent_classifier", "...and a helper asking for herself still reads as the helper",
     {"incoming_text": "i want to be transferred to a new employer",
      "intent": None, "service_type": None, "history_text": "",
      "_stub_intent": {"intent": "transfer", "service_type": "transfer",
                       "contact_type": None, "confidence": 0.9},
      "_expect_state": {"detected_contact_type": "candidate"}}),
    # Step 2b, 2026-10-13, conversation 3766: with renewal CB-2026-0033 open,
    # "also please note she prefers to fly on a weekend" was read as home_leave
    # (the model's verdict, stubbed here as it came back live) and a new
    # home-leave intake began. A detail stays on the parked topic...
    ("intent_classifier", "a detail about a parked topic stays on it",
     {"incoming_text": "also please note she prefers to fly on a weekend",
      "intent": "case_enquiry", "service_type": "renewal",
      "history_text": "Client: are you there\nYou: It is with a live agent. Anything else?",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_stub_intent": {"intent": "home_leave", "service_type": "home_leave",
                       "contact_type": "employer", "confidence": 0.8},
      "_expect_state": {"service_type": "renewal", "intent": "renewal"}}),
    # ...but a client who clearly asks for another service still gets it:
    # naming it, asking for something, or asking a question.
    ("intent_classifier", "...a named new service still switches",
     {"incoming_text": "I also want to arrange home leave for her",
      "intent": "case_enquiry", "service_type": "renewal",
      "history_text": "Client: are you there\nYou: It is with a live agent. Anything else?",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_stub_intent": {"intent": "home_leave", "service_type": "home_leave",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "home_leave"}}),
    ("intent_classifier", "...even a 'please note' that names another service",
     {"incoming_text": "please note she also has home leave next month",
      "intent": "case_enquiry", "service_type": "renewal",
      "history_text": "Client: are you there\nYou: It is with a live agent. Anything else?",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_stub_intent": {"intent": "home_leave", "service_type": "home_leave",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "home_leave"}}),
    ("intent_classifier", "...a request with no service named still switches",
     {"incoming_text": "my helper needs to go back to her country next month",
      "intent": "case_enquiry", "service_type": "renewal",
      "history_text": "Client: are you there\nYou: It is with a live agent. Anything else?",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_stub_intent": {"intent": "home_leave", "service_type": "home_leave",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "home_leave"}}),
    ("intent_classifier", "...and so does a question",
     {"incoming_text": "can she fly on a weekend?",
      "intent": "case_enquiry", "service_type": "renewal",
      "history_text": "Client: are you there\nYou: It is with a live agent. Anything else?",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_stub_intent": {"intent": "home_leave", "service_type": "home_leave",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "home_leave"}}),

    # --- direct hire: a question about WHEN, answered with a WHERE ---------
    # 2026-09-18, reproduced 3 runs of 3 against the real extractor before
    # anything was touched. Asked "Is she currently in Singapore, working
    # under a Work Permit with another employer?", the client answered "No she
    # is on Myanmar right Now" - and the extractor returned the transfer-case
    # answer AND `helper_availability: 'Myanmar right now'` out of the same
    # seven words. A filled field is never put to anyone, so "When would she
    # be available to start?" was never asked, and ticket CB-2026-0009 reached
    # a consultant reading "helper availability: Myanmar right now".
    #
    # `_stub_extraction` is what the model really returned. Asserted on what
    # reaches `collected_info` AND on the next question, because a rule that
    # drops the value and then fails to ask for it has fixed nothing.
    ("info_collector", "a location is not an answer to when she can start",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "No she is on Myanmar right Now",
      "history_text": "Client: chowchow\nYou: Is she currently in Singapore, "
                      "working under a Work Permit with another employer?",
      "collected_info": {"full_name": "john", "helper_name": "chowchow",
                         "helper_nationality": "Myanmar"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1, "helper_transfer_case": 1},
      "_stub_extraction": {"helper_transfer_case": "no",
                           "helper_availability": "Myanmar right now"},
      "_expect_collected": {"helper_transfer_case": "no"},
      "_expect_not_collected": ["helper_availability"],
      "_expect_prompt": "When would she be available to start?"}),
    # ...and once the question HAS been put, their answer is their answer -
    # even one that mentions where she is. The rule fails towards asking and
    # this is the control that proves it stopped there: without it, a guard
    # wide enough to drop every availability containing a place name would
    # pass, and the client would be asked the same question forever.
    ("info_collector", "...but once asked, an answer naming a place still lands",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "she is in Myanmar right now and can start next month",
      "history_text": "You: When would she be available to start?",
      "collected_info": {"full_name": "john", "helper_name": "chowchow",
                         "helper_nationality": "Myanmar",
                         "helper_transfer_case": "no"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1, "helper_transfer_case": 1,
                             "helper_availability": 1},
      "_stub_extraction": {"helper_availability": "Myanmar right now, from next month"},
      "_expect_collected": {"helper_availability": "Myanmar right now, from next month"}}),
    # ...and a volunteered date that repeats nothing is still taken, so the
    # rule has not simply stopped the field being filled early.
    ("info_collector", "...and a plain volunteered date is still taken",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "no she is not working for anyone, she can start next month",
      "history_text": "You: Is she currently in Singapore, working under a Work "
                      "Permit with another employer?",
      "collected_info": {"full_name": "john", "helper_name": "chowchow",
                         "helper_nationality": "Myanmar"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1, "helper_transfer_case": 1},
      "_stub_extraction": {"helper_transfer_case": "no",
                           "helper_availability": "next month"},
      "_expect_collected": {"helper_availability": "next month"}}),

    # ...and the neighbouring field, found by the same replay. "i want to do
    # direct hire" filled `helper_transfer_case` with "direct hire" - 4 runs
    # of 4 - so "Is she currently in Singapore, working under a Work Permit
    # with another employer?" was never asked, and the route it establishes
    # was never known. Asserted on what reaches `collected_info` AND on the
    # question still being put, because dropping the value without asking the
    # question would leave the flow exactly as blind.
    ("info_collector", "the words they opened with do not answer the route question",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "i want to do direct hire", "history_text": "",
      "collected_info": {"full_name": "john", "helper_name": "chowchow",
                         "helper_nationality": "Myanmar"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1},
      "_stub_extraction": {"helper_transfer_case": "direct hire"},
      "_expect_not_collected": ["helper_transfer_case"],
      "_expect_prompt": "working under a Work Permit with another employer"}),
    # ...and once the question HAS been put, their answer is their answer -
    # even if they answer it with the name of the service. Dropping it there
    # re-asks a question they have answered until `max_asks` runs out and
    # then files nothing at all, which is worse than the defect. Same gate,
    # same reasoning and same control as the when-field rule above.
    ("info_collector", "...but once asked, even that answer lands",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "it is a direct hire", "history_text":
          "You: Is she currently in Singapore, working under a Work Permit "
          "with another employer?",
      "collected_info": {"full_name": "john", "helper_name": "chowchow",
                         "helper_nationality": "Myanmar"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1, "helper_transfer_case": 1},
      "_stub_extraction": {"helper_transfer_case": "direct hire"},
      "_expect_collected": {"helper_transfer_case": "direct hire"}}),

    # --- direct hire: the process arrives at the END, not on question two --
    # "this is the process related and documents related things we should tell
    # this at last with the process, requirements, timeline, cost/fees when
    # all the requirements are gathered ... without waiting for the user to
    # ask for". Live, it arrived welded onto the name question instead:
    # "Thanks, john. Direct hire involves processing the MOM application,
    # documents, insurance and bond, and getting the helper here and settled;
    # may I know the full name of the helper you would like to hire?"
    #
    # Both halves are asserted on the PROMPT, because the stubbed model writes
    # the same reply whichever instruction won - reading the reply could not
    # tell "was told to brief" from "happened to say something similar".
    ("info_collector", "a direct hire no longer opens with the process",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "my self john", "history_text": "You: may I know your name?",
      "collected_info": {"full_name": "john"},
      "asked_field_counts": {"full_name": 1},
      "_stub_extraction": {},
      "_forbid_prompt": "what the job involves",
      "_expect_prompt": "full name of the helper"}),
    # ...and the closing turn is the one that lays it all out. The briefing is
    # keyed on `helper_availability`, so the turn that answers the LAST
    # question finds it already filled and briefs.
    ("info_collector", "...and the closing message is the whole briefing",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "John@gmail.com",
      "history_text": "You: what email address should I use for the updates?",
      "collected_info": {"full_name": "john", "helper_name": "chowchow",
                         "helper_nationality": "Myanmar",
                         "helper_transfer_case": "no",
                         "helper_availability": "next month",
                         "helper_contact": "+6575968464",
                         "update_channel": "email, WhatsApp"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1, "helper_transfer_case": 1,
                             "helper_availability": 1, "helper_contact": 1,
                             "update_channel": 1, "email": 1},
      "_stub_extraction": {"email": "John@gmail.com"},
      "_expect_prompt": "WHAT IT COSTS",
      "_expect_state": {"briefed_services": ["direct_hiring"]}}),

    # --- the direction an employer means by "transfer", 2026-09-18 ---------
    # "why is bot asking that are you looking to take on transfer helper
    # already in singapore or you want to release your current helper ... if
    # someone is coming and telling that i want transfer helper it means user
    # intent is clear". RUN rather than unit-tested, and deliberately: the
    # predicate can be perfect and still never be called, which is the state
    # `quotes_hiring_package_cost` was in for two days (2026-09-10) and the
    # hole three separate fixes have fallen into since.
    #
    # `_stub_extraction` is what the extractor really returns for this
    # sentence - the bare word "transfer" (section 9.12), which opens neither
    # gate. So this also proves the fill beats the extraction rather than
    # losing to it, which is what `_known_fields` would have done.
    ("info_collector", "an employer who said which transfer they mean is not asked",
     {"intent": "transfer", "service_type": "transfer_employer",
      "incoming_text": "hi i want transfer helper", "history_text": "",
      "collected_info": {"full_name": "sanjay"}, "asked_field_counts": {"full_name": 1},
      "_stub_extraction": {"transfer_direction": "transfer"},
      "_expect_collected": {"transfer_direction": "taking on a transfer helper"},
      "_forbid_prompt": "releasing your current helper",
      "_expect_prompt": "What would you mainly need help with"}),
    # ...and the other direction, which is the half a take-on-only fix would
    # have broken silently: "transfer MY helper" is a release, and the
    # possessive is the only thing that says so.
    ("info_collector", "...and an employer releasing their own is not asked either",
     {"intent": "transfer", "service_type": "transfer_employer",
      "incoming_text": "i want to transfer my helper to another employer",
      "history_text": "",
      "collected_info": {"full_name": "sanjay"}, "asked_field_counts": {"full_name": 1},
      "_stub_extraction": {"transfer_direction": "transfer"},
      "_expect_collected": {"transfer_direction": "releasing your current helper"},
      "_forbid_prompt": "take on a transfer helper",
      "_expect_prompt": "May I know the helper's name?"}),
    # ...and a message that decides nothing still gets the question. The rule
    # fails towards asking, so the control is the half that proves it has not
    # simply started guessing.
    ("info_collector", "...but a transfer that says neither is still asked which",
     {"intent": "transfer", "service_type": "transfer_employer",
      "incoming_text": "sanjay dutt", "history_text": "bot: may I know your name?",
      "collected_info": {"full_name": "sanjay"}, "asked_field_counts": {"full_name": 1},
      "_stub_extraction": {},
      "_expect_prompt": "releasing your current helper"}),

    # ...and the turn AFTER it, which is where replaying the live transcript
    # found the hole: the extractor returns the same undecidable "transfer" on
    # every turn, `collected` is {**previous, **extracted}, so a direction
    # settled on turn one was overwritten on turn two and the question came
    # back. Unit-testing the predicate cannot see this - it needs the two
    # dictionaries the node builds.
    ("info_collector", "...and the extractor cannot un-settle it a turn later",
     {"intent": "transfer", "service_type": "transfer_employer",
      "incoming_text": "myself sanjay dutt",
      "history_text": "bot: may I know your name?",
      "collected_info": {"transfer_direction": "taking on a transfer helper"},
      "asked_field_counts": {"full_name": 1},
      "_stub_extraction": {"full_name": "sanjay dutt",
                           "transfer_direction": "transfer"},
      "_expect_collected": {"transfer_direction": "taking on a transfer helper"},
      "_forbid_prompt": "releasing your current helper"}),

    # --- the household question, 2026-09-18 --------------------------------
    # "i have 12 peoples in my family 8 are adults and 4 are childrens AS I
    # TOLD THEN WHY ASKED ME AGAIN" - the children's half had been answered one
    # question earlier. RUN, because the note lives in the guidance the model
    # is handed and a predicate cannot tell you whether it got there.
    ("info_collector", "the household question does not ask again about the children",
     {"intent": "transfer", "service_type": "transfer_employer",
      "incoming_text": "i have 4 childrens and all are under 15",
      "history_text": "bot: how many children, and how old are they?",
      "collected_info": {"full_name": "sanjay",
                         "transfer_direction": "taking on a transfer helper",
                         "requirement": "childcare",
                         "children_detail": "4 children, all under 15"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "children_detail": 1},
      "_stub_extraction": {},
      "_expect_prompt": "ALREADY told you this much"}),
    # ...and the control: with nothing on file it is the whole question again.
    ("info_collector", "...and asks it in full when it has been told nothing",
     {"intent": "transfer", "service_type": "transfer_employer",
      "incoming_text": "general housework",
      "history_text": "bot: what would you mainly need help with?",
      "collected_info": {"full_name": "sanjay",
                         "transfer_direction": "taking on a transfer helper",
                         "requirement": "general housework and cooking"},
      "asked_field_counts": {"full_name": 1, "requirement": 1},
      "_stub_extraction": {},
      "_forbid_prompt": "ALREADY told you this much"}),

    # --- what kind of help they need, 2026-09-16 ---------------------------
    # Live: the opening message was "hey i want to hire a helper" and the
    # extractor returned requirement="general housework". Nobody said it. The
    # field looked answered so it was never asked, the collection opened on
    # "how many people live in your household?", and the client noticed
    # seventeen questions later. RUN rather than unit-tested, because the
    # predicate was correct the whole time and the call site was not.
    ("info_collector", "an invented care type is dropped, not filed",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "hey i want to hire a helper", "history_text": "",
      "collected_info": {}, "asked_field_counts": {},
      "_stub_extraction": {"requirement": "general housework"},
      "_expect_not_collected": ["requirement"]}),
    # The turn that separates the additive test from the subtractive one it
    # replaced. A bare "10" answering the household question is not hiring
    # filler, so the old test read it as a stated care type - which is how
    # every one of the eighteen messages in the live transcript passed.
    ("info_collector", "a bare number answering something else names no care type",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "10", "history_text": "bot: how many people live in your household?",
      "collected_info": {"full_name": "Vaidik"}, "asked_field_counts": {"household": 1},
      "_stub_extraction": {"requirement": "general housework", "household": "10"},
      "_expect_not_collected": ["requirement"]}),
    # ...and the half that must still work, or the client is asked for
    # something they have just told us.
    ("info_collector", "a care type the client DID name is kept",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "i need childcare for my two kids", "history_text": "",
      "collected_info": {}, "asked_field_counts": {},
      "_stub_extraction": {"requirement": "childcare"},
      "_expect_collected": {"requirement": "childcare"}}),

    # --- the agency's team test, 2026-09-17 ---------------------------------
    # "bot should highlight that one helper cannot manage all the duties
    # assigned." Six people, twelve bedrooms, childcare AND cleaning - the bot
    # collected all of it and moved on without a word. RUN, not unit-tested:
    # the predicate was only ever half the fix, and a note nobody appends to
    # the instruction is the hole this file exists to close.
    ("info_collector", "more work than one helper can carry is flagged once",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "12 bedrooms and 10 toilets", "history_text": "bot: how many bedrooms?",
      "collected_info": {"full_name": "Thomas", "requirement": "a combination of childcare and cleaning",
                         "household": "6", "home_size": "12 bedrooms and 10 toilets"},
      "asked_field_counts": {"household": 1, "home_size": 1},
      "_expect_state": {"flagged_once": ["workload"]},
      "_expect_prompt": "MORE WORK THAN ONE HELPER"}),
    # ...and never twice. Telling a client their job is too big a second time
    # reads as an argument rather than as advice.
    ("info_collector", "...and never raised a second time",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "12 bedrooms and 10 toilets", "history_text": "bot: how many bedrooms?",
      "collected_info": {"full_name": "Thomas", "requirement": "a combination of childcare and cleaning",
                         "household": "6", "home_size": "12 bedrooms and 10 toilets"},
      "asked_field_counts": {"household": 1, "home_size": 1},
      "flagged_once": ["workload"],
      "_expect_state": {"flagged_once": []}}),
    # An ordinary placement is left alone - a big family with one clear job is
    # not a problem, and saying it is talks them out of a hire we could make.
    ("info_collector", "an ordinary placement is not lectured",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "6", "history_text": "bot: how many people live with you?",
      "collected_info": {"full_name": "Thomas", "requirement": "childcare", "household": "6"},
      "asked_field_counts": {"household": 1},
      "_expect_state": {"flagged_once": []}}),
    # "it does not introduce it as a chatbot but gives this reply" - a FIRST
    # message that was a process question, so PROCESS_INSTRUCTION replaced the
    # instruction wholesale and said nothing about introducing yourself.
    # --- 2026-10-08, conversation 3766: finished work and unsent questions ---
    ("response_generator", "a status question on a COMPLETED request is answered as completed",
     {"intent": "case_enquiry", "service_type": "home_leave",
      "incoming_text": "is there any update on yoyos home leave",
      "history_text": "Client: hi\nYou: Hi, how can I help?",
      "rag_best_score": 0.0, "rag_matches": [], "rag_context": "",
      "completed_topics": {"home_leave": {"ticket_number": "CB-2026-0026", "status": "resolved"}},
      "_stub_reply": "Our team has completed Yoyo's home leave request. If anything is still outstanding, tell me and I'll pass it on.",
      "_expect_prompt": "marked that request as completed",
      "_expect_reply": "Our team has completed"}),
    # Live after that fix went out: "hello" came back case_enquiry and got the
    # completed line; replayed with that removed, the status instruction wrote
    # a repeated "checking the latest update" and the repeat guard raised a ticket.
    ("response_generator", "a greeting misread as a status question is greeted back, not handed over",
     {"intent": "case_enquiry", "service_type": "home_leave", "record_name": "VD",
      "incoming_text": "hello",
      "history_text": "Client: any update on yoyo home leave\nYou: I'll check the latest update with our team.",
      "completed_topics": {"home_leave": {"ticket_number": "CB-2026-0027", "status": "resolved"}},
      "_stub_reply": "I'll check the latest update with our team.",
      "_forbid_reply": "update",
      "_expect_reply": "VD",
      "_expect_state": {"needs_handover": False}}),
    ("response_generator", "what the client told us about a helper is in front of the model",
     {"intent": "general_question", "service_type": "renewal", "contact_type": "employer",
      "incoming_text": "can you tell me the nationality of choral",
      "history_text": "Client: hi\nYou: Hi, how can I help?",
      "known_helpers": [{"helper_name": "choral", "nationality": "Filipino"}],
      "rag_best_score": 0.5, "rag_matches": [{"question": "x", "similarity": 0.5}], "rag_context": "x",
      "_stub_reply": "Choral is Filipino.",
      "_expect_prompt": "choral (Filipino)"}),
    # 2026-10-12, conversation 4443: her rights went out as one sentence of
    # seven. All ten, as a list, and none cut off by the clamp.
    # 2026-10-13: "i want detailed rights message for helper in listed format
    # properly" - a known helper gets Module 27 in detail, word for word.
    ("response_generator", "a helper asking her rights gets the detailed message, word for word",
     {"intent": "general_question", "contact_type": "candidate",
      "incoming_text": "i am helper i want to know my rights",
      "history_text": "Client: hello\nYou: Hi, I'm Claire, Ming Hwee's AI assistant. How can I help you?",
      "rag_best_score": 0.56, "rag_matches": [{"question": "x", "similarity": 0.56}],
      "rag_context": _HELPER_RIGHTS,
      "_stub_reply": "SHOULD NOT BE SENT",
      "_expect_reply": "7. Your placement loan",
      "_forbid_reply": "SHOULD NOT BE SENT",
      "_expect_state": {"needs_handover": False}}),
    ("response_generator", "...and asked again right after it, she is pointed to it rather than sent nothing",
     {"intent": "general_question", "contact_type": "candidate",
      "incoming_text": "please provide me with my rights",
      "history_text": "Client: i am helper i want to know my rights\n"
                      "You: Here are your rights as a domestic helper in Singapore:",
      "rag_best_score": 0.56, "rag_matches": [{"question": "x", "similarity": 0.56}],
      "rag_context": _HELPER_RIGHTS,
      "_stub_reply": "SHOULD NOT BE SENT",
      "_expect_reply": "sent your full list of rights just above",
      "_expect_state": {"needs_handover": False}}),
    ("response_generator", "...while an unknown sender asking her rights gets the list from the records",
     {"intent": "general_question", "contact_type": "unknown",
      "incoming_text": "what are my rights",
      "history_text": "Client: hello\nYou: Hi, I'm Claire, Ming Hwee's AI assistant. How can I help you?",
      "rag_best_score": 0.56, "rag_matches": [{"question": "x", "similarity": 0.56}],
      "rag_context": _HELPER_RIGHTS,
      "_stub_reply": "These are your rights as a helper in Singapore:\n" + _HELPER_RIGHTS,
      "_expect_prompt": "EVERY right our records list",
      "_expect_reply": "10. Transfer",
      "_expect_state": {"needs_handover": False}}),
    ("response_generator", "...while one right asked about is not given the whole list",
     {"intent": "general_question", "contact_type": "candidate",
      "incoming_text": "can my employer keep my passport",
      "history_text": "Client: hello\nYou: Hi, I'm Claire, Ming Hwee's AI assistant. How can I help you?",
      "rag_best_score": 0.64, "rag_matches": [{"question": "x", "similarity": 0.64}],
      "rag_context": "Your passport belongs to YOU. You should keep it.",
      "_stub_reply": "No, your passport belongs to you and you keep it.",
      "_forbid_prompt": "EVERY right our records list"}),
    ("response_generator", "...while an ordinary status question is not told that",
     {"intent": "case_enquiry", "service_type": "home_leave",
      "incoming_text": "is there any update on yoyos home leave",
      "history_text": "Client: hi\nYou: Hi, how can I help?",
      "completed_topics": {},
      "_forbid_prompt": "marked that request as completed"}),
    ("info_collector", "a question held while the client was typing does not count as asked",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "sorry from myanmar",
      "history_text": "You: Which country is Yoyo from?\nClient: from indonasia",
      "collected_info": {"full_name": "VD", "helper_name": "Yoyo", "nationality": "Indonesia",
                         "leave_dates": "in 2 weeks"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1, "leave_dates": 1,
                             "leave_reason": 1},
      "last_question": {"key": "leave_reason",
                        "text": "Got it - is Yoyo's leave for her regular home leave, or because of a family matter?"},
      "_stub_extraction": {"nationality": "Myanmar"},
      "_expect_not_collected": ["leave_reason"],
      "_expect_prompt": "reason for her leave"}),
    ("info_collector", "...while one that WAS sent and went unanswered is filed as unanswered",
     {"service_type": "home_leave", "intent": "home_leave",
      "incoming_text": "sorry from myanmar",
      "history_text": "You: Got it - is Yoyo's leave for her regular home leave, or because of a family matter?\nClient: from indonasia",
      "collected_info": {"full_name": "VD", "helper_name": "Yoyo", "nationality": "Indonesia",
                         "leave_dates": "in 2 weeks"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1, "leave_dates": 1,
                             "leave_reason": 1},
      "last_question": {"key": "leave_reason",
                        "text": "Got it - is Yoyo's leave for her regular home leave, or because of a family matter?"},
      "_stub_extraction": {"nationality": "Myanmar"},
      "_expect_collected": {"leave_reason": "not provided"}}),
    ("response_generator", "Claire introduces herself on a first process question",
     {"intent": "process_question", "service_type": "new_hiring",
      "incoming_text": "hi i would like to hire a helper. what is the process to go about it?",
      "history_text": "",
      "_expect_prompt": "introduce yourself in one short sentence"}),
    # ...and on no other turn, or she says it every time. Written in the
    # webhook's own rendering ("Client:" / "You:"): since 2026-10-07 only a
    # "You:" line counts as Claire having spoken (guards.claire_has_spoken).
    ("response_generator", "...and only on the first message",
     {"intent": "process_question", "service_type": "new_hiring",
      "incoming_text": "what is the process to go about it?",
      "history_text": "Client: hi\nYou: Hi, I'm Claire, Ming Hwee's AI assistant.",
      "_forbid_prompt": "introduce yourself in one short sentence"}),

    # --- home leave tells them to book the ticket, 2026-09-17 --------------
    # Agency: advise the client to buy the air ticket and send us a copy, so the
    # agent picking the case up can submit the embassy paperwork against
    # confirmed dates. home_leave had no BRIEFING_AFTER entry at all, so it
    # closed on the bare handover line and the client had to ask.
    # RUN, not read: briefing_due is one condition and the note reaching the
    # prompt is another, and only the node joins them.
    ("info_collector", "home leave closes by telling them to book the ticket",
     {"intent": "home_leave", "service_type": "home_leave",
      "incoming_text": "12 December to 8 January",
      "history_text": "bot: When is she planning to travel, and when would she be back?",
      "collected_info": {"full_name": "Vincent", "helper_name": "Jenny Rose Ann",
                         "nationality": "Filipino",
                         "leave_dates": "12 December to 8 January",
                         "leave_reason": "regular home leave"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "leave_dates": 1, "leave_reason": 1},
      "briefed_services": [],
      "_expect_prompt": "book her air ticket"}),
    # --- a renewal briefing carries its fee, Step 2c, 2026-10-13 -----------
    # Conversation 3766: name, helper and expiry in ONE message. The retriever
    # ran before they were extracted, searched the client's words, and the
    # briefing deferred "the exact fee" where the answer is $695. The stub
    # quotes $695, so if the briefing's own records are not fetched,
    # ungrounded_figures bins it and the reply is the fallback question.
    ("info_collector", "renewal: all fields in one message -> the briefing quotes $695",
     {"intent": "renewal", "service_type": "renewal", "contact_type": "employer",
      "incoming_text": "myself VD and my helper is Devi and her permit expires in 3 weeks",
      "history_text": "You: Hi, I'm Claire, Ming Hwee's AI assistant. May I know your name?",
      "collected_info": {}, "asked_field_counts": {"full_name": 1},
      "briefed_services": [],
      "rag_context": _RENEWAL_GENERAL_ROWS,
      "_briefing_records": {"rag_matches": [{"similarity": 0.69}],
                            "rag_context": _RENEWAL_BRIEFING_ROWS,
                            "rag_best_score": 0.69},
      "_stub_extraction": {"full_name": "VD", "helper_name": "Devi",
                           "permit_expiry": "in 3 weeks"},
      "_stub_reply": _RENEWAL_BRIEFING_REPLY,
      "_expect_prompt": "$695",
      "_expect_reply": "$695"}),
    # ...and the ordinary way, one answer per turn: the retriever already
    # searched for the briefing, so its records are used as they are.
    ("info_collector", "renewal: one field per turn -> the briefing quotes $695",
     {"intent": "renewal", "service_type": "renewal", "contact_type": "employer",
      "incoming_text": "in 3 weeks",
      "history_text": "You: When does her work permit expire?",
      "collected_info": {"full_name": "VD", "helper_name": "Devi",
                         "helper_from_us": "hired elsewhere - no placement on record"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "permit_expiry": 1},
      "briefed_services": [],
      "rag_context": _RENEWAL_BRIEFING_ROWS,
      "_briefing_records": {"rag_context": "SHOULD NOT BE FETCHED"},
      "_stub_extraction": {"permit_expiry": "in 3 weeks"},
      "_stub_reply": _RENEWAL_BRIEFING_REPLY,
      "_expect_reply": "$695",
      "_forbid_prompt": "SHOULD NOT BE FETCHED"}),
    # ...and the HELPER's own closing message is written to her (4551,
    # 2026-10-08): no fee, her documents only, wait for her employer to
    # confirm before buying the ticket. Complete, so a briefing is built.
    ("info_collector", "a helper's own home leave closes with HER briefing",
     {"intent": "home_leave", "service_type": "home_leave", "contact_type": "employer",
      "incoming_text": "his number is 91234567",
      "history_text": "You: What is Mr Tan's phone number?",
      "flagged_once": ["helper_home_leave"],
      "collected_info": {"full_name": "Ratna", "helper_name": "Ratna", "requested_by": "helper",
                         "nationality": "Indonesian", "leave_dates": "next week for a month",
                         "leave_reason": "family emergency", "employer_name": "Mr Tan"},
      "asked_field_counts": {"full_name": 1, "nationality": 1, "leave_dates": 1,
                             "leave_reason": 1, "employer_name": 1, "employer_contact": 1},
      "briefed_services": [],
      "_stub_extraction": {"employer_contact": "91234567"},
      "_expect_prompt": "THIS CLOSING MESSAGE GOES TO THE HELPER HERSELF",
      "_forbid_prompt": "COST: no figure at all"}),
    # ...and it TELLS her the fee we hold for her nationality (agency,
    # 2026-10-09: "the fee is settled with your employer" went out with no
    # figure on an Indonesian leave we price at $250).
    ("info_collector", "a helper's own home leave briefing quotes her fee",
     {"intent": "home_leave", "service_type": "home_leave", "contact_type": "employer",
      "incoming_text": "his number is 91234567",
      "history_text": "You: What is Mr Tan's phone number?",
      "flagged_once": ["helper_home_leave"],
      "collected_info": {"full_name": "Ratna", "helper_name": "Ratna", "requested_by": "helper",
                         "nationality": "Indonesian", "leave_dates": "next week for a month",
                         "leave_reason": "family emergency", "employer_name": "Mr Tan"},
      "asked_field_counts": {"full_name": 1, "nationality": 1, "leave_dates": 1,
                             "leave_reason": 1, "employer_name": 1, "employer_contact": 1},
      "briefed_services": [],
      "_stub_extraction": {"employer_contact": "91234567"},
      "_expect_prompt": "the fee the records state for HER nationality"}),
    # A replacement of a helper WE placed less than 6 months ago is inside the
    # package's replacement period, and its replacement and documentation fee
    # is quoted (agency, 2026-10-09). The stub is the package wording the cost
    # guard catches, so this also proves the guard is lifted for it.
    ("info_collector", "a replacement inside the package period quotes its fee",
     {"intent": "replacement", "service_type": "replacement", "contact_type": "employer",
      "incoming_text": "someone more experienced with kids",
      "history_text": "You: What are you looking for in the replacement helper?",
      "prior_hires": 1,
      "placed_helper": {"helper_name": "Hungama", "nationality": "Indonesian"},
      "rag_context": "Q: What is the replacement fee for a new hire from Indonesia?\n"
                     "A: The package includes 2 replacements within 6 months, and the "
                     "replacement and documentation fee is $288.",
      "collected_info": {"full_name": "Pogo", "helper_name": "Hungama",
                         "helper_tenure": "4 months", "reason": "rude",
                         "current_helper_exit": "not decided", "timeline": "ASAP"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "helper_tenure": 1,
                             "reason": 1, "current_helper_exit": 1, "timeline": 1,
                             "replacement_preferences": 1},
      "briefed_services": [],
      "_stub_extraction": {"replacement_preferences": "more experienced with kids"},
      "_stub_reply": "Here is everything for replacing Hungama:\n\nThe package includes "
                     "2 replacements within 6 months, and the replacement and "
                     "documentation fee is $288.\n\nI've passed everything to our team, "
                     "and a live agent will connect with you shortly.",
      "_expect_reply": "$288",
      "_expect_prompt": "THE COST SECTION, FOR THIS REPLACEMENT",
      "_forbid_prompt": "--- The cost of a replacement ---"}),
    # ...and at 7 months it is outside the period: no package fee note.
    ("info_collector", "a replacement outside the package period defers its fee",
     {"intent": "replacement", "service_type": "replacement", "contact_type": "employer",
      "incoming_text": "someone more experienced with kids",
      "history_text": "You: What are you looking for in the replacement helper?",
      "prior_hires": 1,
      "placed_helper": {"helper_name": "Hungama", "nationality": "Indonesian"},
      "collected_info": {"full_name": "Pogo", "helper_name": "Hungama",
                         "helper_tenure": "its been since 7 months", "reason": "rude",
                         "current_helper_exit": "not decided", "timeline": "ASAP"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "helper_tenure": 1,
                             "reason": 1, "current_helper_exit": 1, "timeline": 1,
                             "replacement_preferences": 1},
      "briefed_services": [],
      "_stub_extraction": {"replacement_preferences": "more experienced with kids"},
      "_expect_prompt": "--- The cost of a replacement ---",
      "_forbid_prompt": "THE COST SECTION, FOR THIS REPLACEMENT"}),
    # ...and it is home leave's note, not every briefing's. A passport renewal
    # has no flight in it at all.
    #
    # This control must be COMPLETE, not merely past its nationality. The
    # briefing is the CLOSING message (2026-09-08), so a flow with a field still
    # outstanding never builds one - and the first version of this state left
    # `passport_expiry` unanswered, so it forbade a note that could not have
    # appeared either way and stayed green with the service gate removed.
    # A _forbid_ on a turn that has no briefing at all proves nothing.
    ("info_collector", "...and a passport renewal is never told to book a flight",
     {"intent": "passport_renewal", "service_type": "passport_renewal",
      "incoming_text": "27 September 2033",
      "history_text": "bot: When does her current passport expire?",
      "collected_info": {"full_name": "Vincent", "helper_name": "Michan",
                         "nationality": "Indonesian",
                         "passport_expiry": "27 September 2033"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "passport_expiry": 1},
      "briefed_services": [],
      "_expect_prompt": "tells them what they have signed up for",
      "_forbid_prompt": "book her air ticket"}),
    # Said once. The briefing is recorded as given, so a later turn does not
    # tell them to book a ticket they have already booked.
    ("info_collector", "...and not again once the briefing has been given",
     {"intent": "home_leave", "service_type": "home_leave",
      "incoming_text": "ok thanks",
      "history_text": "bot: Here is everything for Jenny Rose Ann's home leave:",
      "collected_info": {"full_name": "Vincent", "helper_name": "Jenny Rose Ann",
                         "nationality": "Filipino",
                         "leave_dates": "12 December to 8 January",
                         "leave_reason": "regular home leave"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "leave_dates": 1, "leave_reason": 1},
      "briefed_services": ["home_leave"],
      "_forbid_prompt": "book her air ticket"}),

    # --- "i said both then why you didnt ask for email", 2026-09-17 ---------
    # Direct hire, the agency's own test. The client answered "both" to the
    # channel question, the flow completed and handed over without ever asking
    # for an address. `excludes` is checked FIRST and held the names of the
    # OTHER channel, so naming WhatsApp beside email closed the gate.
    # Asserted by RUNNING the collector and reading the prompt it built: a gate
    # that returns "open" to nobody is the hole this file exists to close.
    ("info_collector", "answering BOTH is asked for an email address",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "both", "history_text": "bot: email or here on WhatsApp?",
      "collected_info": {"full_name": "VD", "helper_name": "Hululu",
                         "helper_contact": "+6599988553", "helper_nationality": "Myanmar",
                         "helper_transfer_case": "no, she is in Myanmar",
                         "helper_availability": "1 month", "update_channel": "both"},
      "asked_field_counts": {"update_channel": 1},
      "_expect_prompt": "what email should I send them to"}),
    # ...and the half that must not move. Picking WhatsApp alone is an answer,
    # and asking for an address anyway is the 2026-09-04 defect this gate was
    # built for - we chose the channel, then asked for what that channel needs.
    ("info_collector", "...and choosing WhatsApp alone still is not",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "whatsapp", "history_text": "bot: email or here on WhatsApp?",
      "collected_info": {"full_name": "VD", "helper_name": "Hululu",
                         "helper_contact": "+6599988553", "helper_nationality": "Myanmar",
                         "helper_transfer_case": "no, she is in Myanmar",
                         "helper_availability": "1 month", "update_channel": "whatsapp"},
      "asked_field_counts": {"update_channel": 1},
      "_forbid_prompt": "what email should I send them to"}),


    # --- 2026-09-17: where the helper came from is read, not asked ---------
    # The screenshot's own turn. A number we hold no record for was asked "is
    # Polo's current Work Permit from Ming Hwee, or was she hired elsewhere?"
    # - of a client we have never placed anyone with, so the answer was already
    # on file. RUN rather than unit-tested: the fill can be perfect and still
    # never reach `allowed_keys`, which is the "imported and never called" hole
    # this file exists to close.
    ("info_collector", "a new client is not asked where the helper came from",
     {"service_type": "renewal", "intent": "renewal",
      "incoming_text": "My helper name is polo",
      "collected_info": {"full_name": "tolo", "helper_name": "Polo"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "prior_hires": 0, "placed_helper": None,
      "history_text": "bot: may I know your helper's name?",
      "_forbid_prompt": "did you hire her elsewhere",
      "_expect_prompt": "When does her work permit expire?",
      "_expect_collected": {
          "helper_from_us": "hired elsewhere - no placement on record"}}),
    # ...and neither is a client whose one placement we can name. Her name is
    # already off the records here, so this turn is the whole renewal in one
    # question.
    ("info_collector", "...nor is a client we have placed a helper with",
     {"service_type": "renewal", "intent": "renewal",
      "incoming_text": "i want to renew my helpers work permit",
      "collected_info": {}, "asked_field_counts": {},
      "record_name": "Ratna Choukade", "prior_hires": 1,
      "placed_helper": {"helper_name": "Liza Fernandez", "nationality": "PH"},
      "history_text": "",
      "_forbid_prompt": "did you hire her elsewhere",
      "_expect_collected": {"helper_from_us": "from Ming Hwee - placed by us"}}),
    # The third branch, and the one with a guess available to it: placements on
    # file and no way to say which helper. It must report what we hold, because
    # claiming her is a guess on a ticket and asking is the question the agency
    # has just had removed.
    ("info_collector", "...and one we cannot match is not claimed as ours",
     {"service_type": "renewal", "intent": "renewal",
      "incoming_text": "My helper name is polo",
      "collected_info": {"full_name": "tolo", "helper_name": "Polo"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "prior_hires": 4, "placed_helper": None,
      "history_text": "bot: may I know your helper's name?",
      "_forbid_prompt": "did you hire her elsewhere",
      "_expect_collected": {
          "helper_from_us":
              "placed with us before - this helper not matched on file"}}),
    # And the half that keeps the fill honest: a client who tells us anyway
    # overrides it. `known` goes in UNDER the extraction, so their own words
    # win - without that a client correcting our records would be filed with
    # the guess, which is worse than the question ever was.
    ("info_collector", "...but the client's own words beat the record fill",
     {"service_type": "renewal", "intent": "renewal",
      "incoming_text": "no she is hired from somewhere else",
      "collected_info": {"full_name": "tolo", "helper_name": "Polo"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "prior_hires": 1,
      "placed_helper": {"helper_name": "Polo", "nationality": "PH"},
      "history_text": "bot: may I know your helper's name?",
      "_stub_extraction": {"helper_from_us": "hired elsewhere"},
      "_expect_collected": {"helper_from_us": "hired elsewhere"}}),

    # --- 2026-09-17: the expiry the briefing said nothing about -----------
    ("info_collector", "a passport expiring in days is flagged in the briefing",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "in 5 days",
      "collected_info": {"full_name": "Ellena", "helper_name": "Bella",
                         "nationality": "Philippines", "passport_expiry": "in 5 days"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "passport_expiry": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "A Filipino helper's passport renewal takes approximately "
                     "6 to 8 weeks and costs approximately $450.",
      "history_text": "bot: When does Bella's current passport expire?",
      "_expect_prompt": "HER PASSPORT EXPIRES SOON"}),
    # The control. A passport good for two more years needs no warning, and a
    # note that fires on every briefing is the 2026-09-17 workload defect.
    ("info_collector", "...and one with years left is not",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "in 2 years",
      "collected_info": {"full_name": "Ellena", "helper_name": "Bella",
                         "nationality": "Philippines", "passport_expiry": "in 2 years"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "passport_expiry": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "A Filipino helper's passport renewal takes approximately "
                     "6 to 8 weeks and costs approximately $450.",
      "history_text": "bot: When does Bella's current passport expire?",
      "_forbid_prompt": "HER PASSPORT EXPIRES SOON"}),
    # ...and an answer we cannot read plainly stays silent rather than guessing.
    ("info_collector", "...nor one we cannot read plainly",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "when her contract ends",
      "collected_info": {"full_name": "Ellena", "helper_name": "Bella",
                         "nationality": "Philippines", "passport_expiry": "when her contract ends"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "passport_expiry": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "A Filipino helper's passport renewal takes approximately "
                     "6 to 8 weeks and costs approximately $450.",
      "history_text": "bot: When does Bella's current passport expire?",
      "_forbid_prompt": "HER PASSPORT EXPIRES SOON"}),

    # --- 2026-09-17: whose passport is it? --------------------------------
    # Run, not predicated. The predicate returning True proves nothing if the
    # branch never fires - and this branch has to do three things the predicate
    # cannot show: answer instead of collecting, NOT hand over, and not finish
    # the collection.
    ("info_collector", "a denial is taken at face value and collected for THEM",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "There isn't any helper here. I want to renew my passport",
      "collected_info": {"full_name": "Rats"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "history_text": "bot: may I know your helper's name?",
      "_stub_extraction": {},
      "_expect_collected": {"helper_name": "Rats"},
      "_expect_prompt": "The person writing IS the passport holder",
      # `flagged_once` is the half that makes the NEXT turn work. Without it
      # the decision is re-taken from a later message that no longer says it,
      # and an assertion on the predicate cannot see that, because it supplies
      # the flag itself.
      "_expect_state": {"info_complete": False, "needs_handover": False,
                        "flagged_once": ["passport_holder_is_the_sender"]}}),
    # ...and the question it must NOT ask on that turn is the one that started
    # the complaint. The agency's words: she "should not be asked for the
    # helper's name as if she is providing someone else's details".
    ("info_collector", "...and not for a helper's name on that turn",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "There isn't any helper here. I want to renew my passport",
      "collected_info": {"full_name": "Rats"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "history_text": "bot: may I know your helper's name?",
      "_forbid_prompt": "May I know your helper's name?"}),
    # ...and the one client who IS refused on this branch rather than on the
    # nationality answer: he has already named his helper, so we know he
    # employs one and the passport he has just asked about is his own. The
    # records are stripped on that turn, which is the guard half rather than
    # the prompt half: the retrieved set holds "$450" and would ground it.
    ("info_collector", "an employer asking about HIS own passport beside hers is refused",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "and also I want to renew my passport",
      "collected_info": {"full_name": "Sallu", "helper_name": "Lily"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "rag_context": "A passport renewal costs approximately $450 and takes "
                     "approximately 3 working days.",
      "history_text": "bot: which country is Lily's passport from?",
      "_stub_extraction": {},
      "_expect_prompt": "not one we can renew",
      "_forbid_prompt": "450",
      "_expect_state": {"info_complete": False, "needs_handover": False,
                        "flagged_once": ["own_passport"]}}),
    # The same request on the service the words actually misroute into.
    ("info_collector", "...on the work-permit flow it lands in too",
     {"service_type": "renewal", "intent": "renewal",
      "incoming_text": "Also I want to renew my passport also",
      "collected_info": {"full_name": "Vaidik", "helper_name": "Polo"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "history_text": "bot: Here is everything for Polo's passport renewal.",
      "_stub_extraction": {},
      "_expect_prompt": "not one we can renew"}),

    # --- 2026-09-19: and the answer she gives -----------------------------
    # The agency tested passport renewal AS THE HELPER and the 2026-09-17
    # branch refused her twice. Their instruction: the service is available to
    # whoever approaches, and "since the passport being renewed will always be
    # the helper's passport, the flow should handle both scenarios".
    #
    # Run rather than predicated, because the predicate cannot show the two
    # things that matter: that `helper_name` is filled from her OWN name, so
    # the question is never put, and that the instruction stops talking about
    # "your helper".
    ("info_collector", "she says the passport is her own, and the flow collects for HER",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "my own passport",
      "collected_info": {"full_name": "kareena"},
      "asked_field_counts": {"full_name": 1},
      "history_text": "You: May I know your name?",
      "_stub_extraction": {},
      "_expect_collected": {"helper_name": "kareena"},
      "_expect_prompt": "The person writing IS the passport holder",
      "_expect_state": {"info_complete": False,
                        "flagged_once": ["passport_holder_is_the_sender"]}}),
    # ...and the turn the agency actually objected to, which is the FIRST one.
    # "i want to renew my passport" settles it there and then, so the
    # helper-name question is never even queued - a decision that waits for
    # her name arrives one question too late, which is the transcript they
    # sent: "May I know your name?" -> "my self kareena" -> "May I know your
    # HELPER's name?".
    ("info_collector", "...decided on the opening message, so no helper is ever asked for",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "i want to renew my passport",
      "collected_info": {},
      "asked_field_counts": {},
      "history_text": "",
      "_stub_extraction": {},
      "_forbid_prompt": "May I know your helper's name?",
      "_expect_state": {"info_complete": False,
                        "flagged_once": ["passport_holder_is_the_sender"]}}),
    # She says so herself on the very first turn she says it, with no question
    # from us at all - "i am helper" and "my employer" are the two things only
    # she writes.
    # ...and the question this branch removes is the HELPER's name, never the
    # client's own. Found by replaying the transcript after the branch started
    # firing on the opening message: the note said "her name is already on
    # file", which is false on the turn before she has given it, and the model
    # correctly did as it was told and skipped straight to the nationality -
    # 0 of 4. `full_name` is the field five separate complaints since
    # 2026-09-08 have been about NOT being asked.
    ("info_collector", "...and she is still asked for her own name",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "i want to renew my passport",
      "collected_info": {}, "asked_field_counts": {},
      "history_text": "",
      "_stub_extraction": {},
      "_expect_prompt": "May I know your name?",
      "_forbid_prompt": "May I know your helper's name?"}),
    ("info_collector", "...or says outright that she is the helper",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "i am a helper, i want to renew my passport",
      "collected_info": {"full_name": "kareena"},
      "asked_field_counts": {"full_name": 1},
      "history_text": "You: May I know your helper's name?",
      "_stub_extraction": {},
      "_expect_collected": {"helper_name": "kareena"},
      "_expect_prompt": "The person writing IS the passport holder"}),
    # The other answer, and the one that keeps the working flow working: an
    # employer who meant their maid's goes straight back to the collection.
    ("info_collector", "...and an employer who meant their helper's carries on",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "i want to renew my helper's passport",
      "collected_info": {"full_name": "Vaidik"},
      "asked_field_counts": {"full_name": 1},
      "history_text": "You: May I know your name?",
      "_stub_extraction": {},
      "_expect_prompt": "May I know your helper's name?",
      "_forbid_prompt": "The person writing IS the passport holder"}),
    # The refusal did not go away - it moved onto evidence. A Singaporean
    # employer renewing their OWN passport says the same sentence she does, so
    # what separates them is the country the passport is from: we renew through
    # the Philippine, Indonesian and Myanmar embassies and nowhere else.
    ("info_collector", "a passport from outside the three embassies is still refused",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "Singapore",
      "collected_info": {"full_name": "Rats", "helper_name": "Rats",
                         "nationality": "Singapore"},
      "asked_field_counts": {"full_name": 1, "nationality": 1},
      "flagged_once": ["passport_holder_is_the_sender"],
      "rag_context": "A passport renewal costs approximately $450.",
      "history_text": "You: Which country is your passport from?",
      "_stub_extraction": {},
      "_expect_prompt": "not one we can renew",
      "_forbid_prompt": "450",
      "_expect_state": {"info_complete": False, "needs_handover": False,
                        "flagged_once": ["own_passport"]}}),
    # HER CLOSING BRIEFING, which is the half a content assertion cannot see.
    # The note existed and was correct while nothing appended it - the
    # "imported and never called" hole (2026-09-10, -16, -17), and the only
    # injection of this round that came back green.
    ("info_collector", "...and her closing briefing is read to HER",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "next year march",
      "collected_info": {"full_name": "kareena", "helper_name": "kareena",
                         "nationality": "Indonesia",
                         "passport_expiry": "next year march"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "passport_expiry": 1},
      "flagged_once": ["passport_holder_is_the_sender"],
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "An Indonesian helper's passport renewal takes more than "
                     "3 working days and costs approximately $450. We need a "
                     "copy of your NRIC, her work permit and her passport.",
      "history_text": "You: When does your current passport expire?",
      "_stub_extraction": {},
      "_expect_prompt": "WHOSE DOCUMENTS ARE WHOSE"}),
    # ...and an EMPLOYER's briefing is untouched, which is the control that
    # makes the one above safe: his list really is "a copy of your NRIC".
    ("info_collector", "...while an employer's briefing is not",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "next year march",
      "collected_info": {"full_name": "Sallu", "helper_name": "Lily",
                         "nationality": "Indonesia",
                         "passport_expiry": "next year march"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "passport_expiry": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "An Indonesian helper's passport renewal takes more than "
                     "3 working days and costs approximately $450.",
      "history_text": "You: When does Lily's current passport expire?",
      "_stub_extraction": {},
      "_forbid_prompt": "WHOSE DOCUMENTS ARE WHOSE"}),
    # ...and the control that makes that safe. A helper from one of the three
    # is served exactly like anybody else - refusing her is the defect being
    # fixed, and a test that only proves the refusal fires would not see it.
    ("info_collector", "...while a helper from one of the three is served",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "Indonesia",
      "collected_info": {"full_name": "kareena", "helper_name": "kareena",
                         "nationality": "Indonesia"},
      "asked_field_counts": {"full_name": 1, "nationality": 1},
      "flagged_once": ["passport_holder_is_the_sender"],
      "history_text": "You: Which country is your passport from?",
      "_stub_extraction": {},
      "_forbid_prompt": "not one we can renew"}),
    # The control. A helper's passport renewal is untouched - this is the
    # service, and breaking it to fix the edge case would be the worse trade.
    ("info_collector", "...while a helper's passport renewal still collects",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "Indonesia",
      "collected_info": {"full_name": "Vaidik", "helper_name": "Polo",
                         "nationality": "Indonesia"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1},
      "history_text": "bot: which country is Polo's passport from?",
      "_forbid_prompt": "asking about THEIR OWN passport"}),

    # --- 2026-09-17: the WhatsApp profile name on a GREETING turn ---------
    # The turn the suppression could not reach: no service_type yet, and it
    # goes to response_generator rather than info_collector, so neither half of
    # the old test could be true. Asserted on the PROMPT, because the defect is
    # that the name was handed to the model at all - checking the reply would
    # pass on any run where the model simply chose not to use it.
    ("response_generator", "a greeting gets no name we do not hold",
     {"intent": "greeting", "service_type": None, "incoming_text": "Hello",
      "customer_name": "Vaidik", "record_name": "", "contact_type": "unknown",
      "history_text": "", "_forbid_prompt": "Vaidik"}),
    # ...and the half that must not regress: a name we DO hold still reaches
    # the model, which is the 2026-09-09 warmth fix.
    ("response_generator", "...but a name we do hold still does",
     {"intent": "greeting", "service_type": None, "incoming_text": "Hello",
      "customer_name": "Vaidik", "record_name": "Ratna Choukade",
      "contact_type": "employer", "history_text": "",
      "_expect_prompt": "Ratna Choukade"}),
    ("response_generator", "...and even then the profile label is not offered",
     {"intent": "greeting", "service_type": None, "incoming_text": "Hello",
      "customer_name": "Vaidik", "record_name": "Ratna Choukade",
      "contact_type": "employer", "history_text": "",
      "_forbid_prompt": "Vaidik"}),
    # A greeting from a RETURNING client - the 2026-09-22 report. Run rather
    # than asserted on the predicate, because `greeting_only` was correct from
    # the moment it was written and the defect was which string the call site
    # picked. The stub carries the handover token, which is what the live model
    # returned on that turn and what sends it down the nothing-asked branch.
    ("response_generator", "a returning client who says Hi is greeted back",
     {"intent": "greeting", "service_type": None, "incoming_text": "Hi",
      "customer_name": "Vaidik", "record_name": "Ratna Choukade",
      "contact_type": "employer",
      "history_text": "Client: i want insurance\nYou: Thanks, passed on.",
      "_stub_reply": "[[NEEDS_HUMAN]]",
      "_expect_reply": "Hi Ratna, good to hear from you",
      "_forbid_reply": "go ahead"}),
    ("response_generator", "...and is not introduced to Claire a second time",
     {"intent": "greeting", "service_type": None, "incoming_text": "Hi",
      "customer_name": "Vaidik", "record_name": "Ratna Choukade",
      "contact_type": "employer",
      "history_text": "Client: i want insurance\nYou: Thanks, passed on.",
      "_stub_reply": "[[NEEDS_HUMAN]]",
      "_forbid_reply": "I'm Claire"}),
    ("response_generator", "...while an ANNOUNCED question still gets go ahead",
     {"intent": "greeting", "service_type": None,
      "incoming_text": "can i ask you something",
      "record_name": "Ratna Choukade", "contact_type": "employer",
      "history_text": "Client: i want insurance\nYou: Thanks, passed on.",
      "_stub_reply": "[[NEEDS_HUMAN]]",
      "_expect_reply": "Sure, go ahead"}),
    ("response_generator", "...and a first-time greeting still introduces her",
     {"intent": "greeting", "service_type": None, "incoming_text": "Hi",
      "record_name": "", "contact_type": "unknown", "history_text": "",
      "_stub_reply": "[[NEEDS_HUMAN]]",
      "_expect_reply": "I'm Claire, Ming Hwee's AI assistant"}),
    # ...and the half the canned reply cannot cover: most greeting turns get a
    # reply the MODEL writes, so the instruction has to say the same thing. The
    # prompt said "do NOT greet again" on every turn with history, which is
    # what made not-greeting-back the instructed behaviour rather than a slip.
    ("response_generator", "the prompt lets the model greet a greeting back",
     {"intent": "greeting", "service_type": None, "incoming_text": "Hi",
      "record_name": "Ratna Choukade", "contact_type": "employer",
      "history_text": "Client: i want insurance\nYou: Thanks, passed on.",
      "_expect_prompt": "The client has GREETED you and asked for nothing else"}),
    ("response_generator", "...and still forbids it mid-conversation",
     {"intent": "renewal", "service_type": "renewal",
      "incoming_text": "how long does the renewal take",
      "record_name": "Ratna Choukade", "contact_type": "employer",
      "history_text": "Client: i want insurance\nYou: Thanks, passed on.",
      "_forbid_prompt": "The client has GREETED you"}),
    # The collector end of the same rule, which used to be the only end.
    ("info_collector", "a hiring intake gets no name we do not hold",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "I want to hire a helper",
      "customer_name": "Vaidik", "record_name": "", "contact_type": "unknown",
      "history_text": "", "_forbid_prompt": "Vaidik"}),

    # --- 2026-09-17: religion, in place of the pork/beef question ---------
    # The agency's decision, taken after the trade was put to them: "in place
    # of this 'are you able to handle pork or beef' and this 'would she need to
    # handle pork or beef?' ask the religion question because that is priority."
    ("info_collector", "the employer is asked her religion, all options named",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Filipino",
      "collected_info": {"full_name": "Thomas", "requirement": "childcare",
                         "children_detail": "two, aged 3 and 7",
                         "household": "4 adults and 2 children",
                         "home_type": "condo", "helper_room": "own room",
                         "first_time_hire": "yes", "home_size": "3 bed 2 bath",
                         "pets": "no pets", "languages": "English",
                         "preferred_nationality": "Filipino"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "home_type": 1, "helper_room": 1, "pets": 1,
                             "languages": 1, "preferred_nationality": 1},
      "history_text": "bot: Do you have a preferred nationality?",
      "_expect_prompt": "Buddhist"}),
    # ...and it says WHY. Religion is the most personal thing either side is
    # asked for, and a question like that arriving bare is the complaint that
    # created _WHY_WE_ASK in the first place.
    ("info_collector", "...and it explains why it is asking",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Filipino",
      "collected_info": {"full_name": "Thomas", "requirement": "childcare",
                         "children_detail": "two, aged 3 and 7",
                         "household": "4 adults and 2 children",
                         "home_type": "condo", "helper_room": "own room",
                         "first_time_hire": "yes", "home_size": "3 bed 2 bath",
                         "pets": "no pets", "languages": "English",
                         "preferred_nationality": "Filipino"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "home_type": 1, "helper_room": 1, "pets": 1,
                             "languages": 1, "preferred_nationality": 1},
      "history_text": "bot: Do you have a preferred nationality?",
      "_expect_prompt": "say WHY you are asking before you ask it"}),
    # Her side of the pairing, asked of her about her.
    ("info_collector", "the helper is asked her own religion",
     {"service_type": "candidate_new_hiring", "intent": "candidate_registration",
      "contact_type": "candidate",
      "incoming_text": "28",
      "collected_info": {"full_name": "Siti", "nationality": "Indonesia",
                         "age": "28"},
      "asked_field_counts": {"full_name": 1, "nationality": 1, "age": 1},
      "history_text": "bot: May I know your age?",
      "_expect_prompt": "May I know your religion"}),
    # ...and her reason is addressed to HER. _WHY_WE_ASK is keyed on the field
    # key with no idea which flow is asking, which is why the two sides have
    # different keys and different wording.
    ("info_collector", "...and her reason is written to her, not about her",
     {"service_type": "candidate_new_hiring", "intent": "candidate_registration",
      "contact_type": "candidate",
      "incoming_text": "28",
      "collected_info": {"full_name": "Siti", "nationality": "Indonesia",
                         "age": "28"},
      "asked_field_counts": {"full_name": 1, "nationality": 1, "age": 1},
      "history_text": "bot: May I know your age?",
      "_forbid_prompt": "your household's practices"}),
    # The half that was REPLACED, not added to. Both the question and the
    # options had to lose it: `_field_guidance` reads options into the spoken
    # question, so "no pork" left in the list would have gone on asking it.
    ("info_collector", "the cooking question no longer asks about pork or beef",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "no preference on age",
      "collected_info": {"full_name": "Thomas", "requirement": "childcare",
                         "children_detail": "two, aged 3 and 7",
                         "household": "4 adults and 2 children",
                         "home_type": "condo", "home_size": "3 bed 2 bath",
                         "helper_room": "own room", "pets": "no pets",
                         "languages": "English",
                         "preferred_nationality": "Filipino",
                         "helper_religion": "Catholic",
                         "helper_profile": "no preference",
                         "hire_source": "worked abroad"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "home_type": 1, "helper_room": 1, "pets": 1,
                             "languages": 1, "preferred_nationality": 1,
                             "helper_religion": 1, "helper_profile": 1,
                             "hire_source": 1},
      "history_text": "bot: any preference on her age or experience?",
      "_forbid_prompt": "pork"}),

    # --- 2026-09-17: the salary floor ------------------------------------
    # Circled in the agency's screenshot. This client has said FILIPINO, and a
    # Filipino helper cannot be placed below S$650 - so "such as SGD 500-600 or
    # SGD 600-700?" invited a budget no placement could be made at.
    #
    # Run rather than predicated, because `_effective_options` returning the
    # right tuple proves nothing if the call site still passes the raw options
    # - which is the "imported and never called" hole this file has recorded
    # four times. This reads the prompt the model was actually handed.
    ("info_collector", "the budget question respects the Filipino floor",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "no gardening",
      "collected_info": {"full_name": "Thomas", "first_time_hire": "yes",
                         "requirement": "childcare",
                         "children_detail": "two, aged 3 and 7",
                         "household": "4 adults and 2 children",
                         "home_type": "condo",
                         "home_size": "3 bedrooms and 2 bathrooms",
                         "helper_room": "own room", "pets": "no pets",
                         "languages": "English",
                         "helper_religion": "no preference", "preferred_nationality": "Filipino",
                         "helper_profile": "32 to 36, Middle East experience",
                         "hire_source": "worked abroad",
                         "cooking": "any cuisine, willing to learn",
                         "special_duties": "no gardening"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "home_type": 1, "helper_room": 1, "pets": 1,
                             "languages": 1, "preferred_nationality": 1,
                             "helper_religion": 1,
                             "helper_profile": 1, "special_duties": 1},
      "history_text": "bot: would she need to handle gardening?",
      "_expect_prompt": "SGD 650-700",
      "_forbid_prompt": "SGD 500-600"}),
    # The control. With no nationality stated there is no floor to apply, and
    # narrowing the bands anyway would tell a client their cheapest option is
    # S$650 when it is not.
    ("info_collector", "...and with no nationality stated, every band stands",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "no gardening",
      "collected_info": {"full_name": "Thomas", "first_time_hire": "yes",
                         "requirement": "childcare",
                         "children_detail": "two, aged 3 and 7",
                         "household": "4 adults and 2 children",
                         "home_type": "condo",
                         "home_size": "3 bedrooms and 2 bathrooms",
                         "helper_room": "own room", "pets": "no pets",
                         "languages": "English",
                         "helper_religion": "no preference", "preferred_nationality": "no preference",
                         "helper_profile": "32 to 36, Middle East experience",
                         "hire_source": "worked abroad",
                         "cooking": "any cuisine, willing to learn",
                         "special_duties": "no gardening"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "home_type": 1, "helper_room": 1, "pets": 1,
                             "languages": 1, "preferred_nationality": 1,
                             "helper_religion": 1,
                             "helper_profile": 1, "special_duties": 1},
      "history_text": "bot: would she need to handle gardening?",
      "_expect_prompt": "below SGD 500"}),

    # ...and the half NOTHING else reaches. Every other check here reads the
    # QUESTION - the prompt built from `_effective_options`. If the call site
    # goes back to passing the raw options as grounding, the question still
    # says "SGD 650-700" and every one of those checks stays green, while
    # `ungrounded_figures` sees 650 in a reply and is not holding it as a
    # grounded figure, so it bins the whole message and the client gets the
    # bare fallback question instead. That is the 2026-09-09 (D) defect
    # exactly, where every budget turn was being discarded and a guard falling
    # back to a correct question looked like nothing going wrong.
    #
    # There is no rag_context here on purpose: the option list is the only
    # thing that can ground 650, which is what makes this state decisive.
    ("info_collector", "...and the floor is GROUNDED, not merely offered",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "no gardening",
      "collected_info": {"full_name": "Thomas", "requirement": "childcare",
                         "children_detail": "two, aged 3 and 7",
                         "household": "4 adults and 2 children",
                         "home_type": "condo",
                         "home_size": "3 bedrooms and 2 bathrooms",
                         "helper_room": "own room", "pets": "no pets",
                         "languages": "English",
                         "helper_religion": "no preference",
                         "preferred_nationality": "Filipino",
                         "helper_profile": "32 to 36, Middle East experience",
                         "hire_source": "worked abroad",
                         "cooking": "any cuisine, willing to learn",
                         "special_duties": "no gardening"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "home_type": 1, "helper_room": 1, "pets": 1,
                             "languages": 1, "preferred_nationality": 1,
                             "helper_religion": 1,
                             "helper_profile": 1, "special_duties": 1},
      "history_text": "bot: would she need to handle gardening?",
      "_stub_reply": "Do you have a monthly salary budget in mind, such as "
                     "SGD 650-700 or SGD 700-800?",
      "_expect_reply": "SGD 650-700"}),

    # --- 2026-09-17: the process and timeline come before the questions ---
    # "Once the service or intent has been identified, the bot should
    # proactively explain the relevant process and expected timeline/lead time,
    # without waiting for the user to ask."
    # ...and REVERSED on 2026-09-19, by the agency's own end-to-end test. New
    # hiring gained a CLOSING briefing, and adding that entry is what removes
    # the opening overview - one change, not two, the same trade direct hire
    # made the day before. The client had asked the process, the documents,
    # the timeline and the charges in four consecutive messages after 22
    # questions, so the end is where the explaining was missing.
    ("info_collector", "new hiring no longer explains itself before the questions",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Thomas",
      "collected_info": {"full_name": "Thomas"},
      "asked_field_counts": {"full_name": 1},
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Hiring from overseas usually takes 4 to 6 weeks.",
      "history_text": "bot: May I know your name?",
      "_forbid_prompt": "roughly how long it takes"}),
    # ...and it is not told it is a short job we handle end to end, which is
    # true of a permit renewal and false of a 25-question hire.
    # ...and the half of that pair that has to hold instead: the CLOSING
    # briefing fires. Run, not predicated - `briefing_due` returning True
    # proves nothing if nothing appends the note, which is the "imported and
    # never called" hole this file has now recorded five times.
    # Step 5 Batch C items 10 and 8: "worked in Singapore before" is not a
    # transfer, and a start sooner than the hire timeline is said out loud.
    ("info_collector", "C10/C8: not a transfer, and 'within 2 weeks' is too soon",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "on whatsapp only",
      "collected_info": {
          "full_name": "Sanju", "requirement": "childcare",
          "children_detail": "3 children under 18", "household": "4",
          "home_type": "condo", "home_size": "3 bedrooms, 2 bathrooms",
          "helper_room": "own room", "pets": "no",
          "languages": "English", "preferred_nationality": "Filipino",
          "helper_religion": "no preference", "helper_profile": "4+ years",
          "hire_source": "someone who has worked in Singapore before", "cooking": "Chinese",
          "special_duties": "no", "budget": "SGD 650-700",
          "rest_day": "weekly days off", "start_timeline": "within 2 weeks",
          "additional_notes": "no smoking", "referral_source": "Google",
          "update_channel": "WhatsApp"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "start_timeline": 1, "update_channel": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Hiring a helper takes about 4 to 6 weeks from signing. A transfer "
                     "helper already in Singapore takes 1 to 2 weeks from the interview.",
      "history_text": "You: Would you prefer updates by email or WhatsApp?",
      "_stub_extraction": {},
      "_expect_prompt": "She is NOT a transfer helper"}),
    ("info_collector", "C8: ...the start date sooner than the timeline is in the note",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "on whatsapp only",
      "collected_info": {
          "full_name": "Sanju", "requirement": "childcare",
          "children_detail": "3 children under 18", "household": "4",
          "home_type": "condo", "home_size": "3 bedrooms, 2 bathrooms",
          "helper_room": "own room", "pets": "no",
          "languages": "English", "preferred_nationality": "Filipino",
          "helper_religion": "no preference", "helper_profile": "4+ years",
          "hire_source": "first-timer", "cooking": "Chinese",
          "special_duties": "no", "budget": "SGD 650-700",
          "rest_day": "weekly days off", "start_timeline": "within 2 weeks",
          "additional_notes": "no smoking", "referral_source": "Google",
          "update_channel": "WhatsApp"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "start_timeline": 1, "update_channel": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Hiring a helper takes about 4 to 6 weeks from signing.",
      "history_text": "You: Would you prefer updates by email or WhatsApp?",
      "_stub_extraction": {},
      "_expect_prompt": "They want her to start within 2 weeks, sooner than"}),
    ("info_collector", "C10/C8: ...a transfer chosen: neither note",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "on whatsapp only",
      "collected_info": {
          "full_name": "Sanju", "requirement": "childcare",
          "children_detail": "3 children under 18", "household": "4",
          "home_type": "condo", "home_size": "3 bedrooms, 2 bathrooms",
          "helper_room": "own room", "pets": "no",
          "languages": "English", "preferred_nationality": "Filipino",
          "helper_religion": "no preference", "helper_profile": "4+ years",
          "hire_source": "transfer helper already in Singapore", "cooking": "Chinese",
          "special_duties": "no", "budget": "SGD 650-700",
          "rest_day": "weekly days off", "start_timeline": "within 2 weeks",
          "additional_notes": "no smoking", "referral_source": "Google",
          "update_channel": "WhatsApp"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "start_timeline": 1, "update_channel": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "A transfer helper already in Singapore takes 1 to 2 weeks.",
      "history_text": "You: Would you prefer updates by email or WhatsApp?",
      "_stub_extraction": {},
      "_forbid_prompt": "NOT a transfer helper"}),
    ("info_collector", "C10: 'Singapore experience' is filed as experience, not a transfer",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Singapore experience",
      "history_text": "You: Are you open to a first-timer, or would you rather someone with "
                      "experience?",
      "collected_info": {"full_name": "VD", "requirement": "childcare"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "hire_source": 1},
      "_stub_extraction": {"hire_source": "Singapore experience"},
      "_expect_collected": {"hire_source": "worked in Singapore before (to be hired from "
                                           "overseas)"}}),
    ("info_collector", "new hiring explains itself at the END instead",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "on whatsapp only",
      "collected_info": {
          "full_name": "Sanju", "requirement": "childcare",
          "children_detail": "3 children under 18", "household": "8",
          "home_type": "condo", "home_size": "10 bedrooms, 5 bathrooms",
          "helper_room": "own room", "pets": "yes", "pet_detail": "2 dogs",
          "languages": "English", "preferred_nationality": "Indonesian",
          "helper_religion": "Buddhist", "helper_profile": "4+ years",
          "hire_source": "worked in Singapore", "cooking": "Chinese",
          "special_duties": "no", "budget": "$650",
          "rest_day": "weekly days off",
          "start_timeline": "as soon as possible",
          "additional_notes": "no smoking", "referral_source": "Google",
          "update_channel": "WhatsApp"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "start_timeline": 1, "update_channel": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Hiring a helper takes about 4 to 6 weeks from signing. "
                     "From you we need your NRIC and proof of income.",
      "history_text": "You: Would you prefer updates by email or WhatsApp?",
      "_stub_extraction": {},
      "_expect_prompt": "CLOSE IT ONCE"}),
    # ...and the cost is now IN it. This assertion said the opposite until
    # 2026-09-22 and was right to: the 2026-09-04 rule was that a new hire's
    # price never reaches anyone before a salesperson has spoken to them, and
    # quotes_hiring_package_cost enforced it whatever the prompt said. The
    # agency reversed it for this service by sending the fee schedule and
    # asking that the bot answer with it. The state is unchanged; only what it
    # expects has moved, so the diff is the reversal and nothing else.
    #
    # `preferred_nationality` is Indonesian in the state above, so the figure
    # that may go out is $1,188 and the other two are somebody else's - which
    # is the whole of the agency's rule 8.
    ("info_collector", "...and the closing briefing now gives the package price",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "on whatsapp only",
      "collected_info": {
          "full_name": "Sanju", "requirement": "childcare",
          "children_detail": "3 children under 18", "household": "8",
          "home_type": "condo", "home_size": "10 bedrooms, 5 bathrooms",
          "helper_room": "own room", "pets": "yes", "pet_detail": "2 dogs",
          "languages": "English", "preferred_nationality": "Indonesian",
          "helper_religion": "Buddhist", "helper_profile": "4+ years",
          "hire_source": "worked in Singapore", "cooking": "Chinese",
          "special_duties": "no", "budget": "$650",
          "rest_day": "weekly days off",
          "start_timeline": "as soon as possible",
          "additional_notes": "no smoking", "referral_source": "Google",
          "update_channel": "WhatsApp"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1,
                             "start_timeline": 1, "update_channel": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "The agency fee for a new hire from Indonesia is $1,188.",
      "history_text": "You: Would you prefer updates by email or WhatsApp?",
      "_stub_extraction": {},
      "_stub_reply": "The agency fee for an Indonesian new hire is $1,188.",
      "_expect_reply": "$1,188"}),
    # ...while a service the schedule gives NO figure for still defers, which
    # is what keeps the guard covered rather than merely switched off. Same
    # shape, same node, same `_write(withhold_cost=)` call - `direct_hiring`
    # is in COST_WITHHELD_SERVICES and briefs at the end off helper_availability.
    ("info_collector", "...but a direct hire, which has no stated fee, still defers it",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "next month",
      "collected_info": {
          "full_name": "Thomas", "helper_name": "Lwin Lwin Nwe",
          "helper_nationality": "Myanmar", "helper_transfer_case": "no",
          "helper_availability": "next month", "helper_contact": "declined",
          "update_channel": "WhatsApp"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1, "helper_transfer_case": 1,
                             "helper_availability": 1, "helper_contact": 1,
                             "update_channel": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      # The figure has to be GROUNDED or ungrounded_figures bins the stub
      # first and the reply falls back to the bare closing line - which is a
      # green-looking red that says nothing about the cost guard. Exactly the
      # 2026-09-18 note: a state that expects a figure has to ground it.
      "rag_context": ("A direct hire takes about 4 to 6 weeks from overseas. "
                      "The total service fee and third-party costs are $4,225."),
      "history_text": "You: When would she be available to start?",
      "_stub_extraction": {},
      "_stub_reply": "The total package is approximately $4,225.",
      "_expect_reply": "rather one of our agents"}),

    # --- 2026-09-19: the care type an answer to another question rewrote ---
    # "In my family there are 8 peoples ... and 1 is elderly care" is an answer
    # to "who lives in your household". It rewrote `requirement` from
    # "childcare" to "childcare, eldercare", opened elderly_detail, and fired
    # the one-helper workload warning on a premise that was never true.
    #
    # Run rather than predicated: the pattern is only half of it, and what
    # matters is what reaches `collected_info`.
    ("info_collector", "an answer about who lives there does not rewrite the care type",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "In my family there are 8 peoples in these 8 peoples "
                       "there are 2 females and 2 are males and 1 is elderly care",
      "collected_info": {"full_name": "Sanju", "requirement": "childcare",
                         "children_detail": "3 children under 18"},
      "asked_field_counts": {"full_name": 1, "requirement": 1,
                             "children_detail": 1, "household": 1},
      "history_text": "You: And besides the 3 children, how many people live "
                      "in your household and who are they?",
      "_stub_extraction": {"requirement": "childcare, eldercare",
                           "household": "8"},
      "_expect_collected": {"household": "8"},
      "_expect_not_collected": ["requirement"]}),
    # ...and a client who actually asks for it still changes it, or a real
    # correction is lost and that is worse than the defect.
    ("info_collector", "...but a client who asks for it still changes it",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "i also need someone to look after my mother",
      "collected_info": {"full_name": "Sanju", "requirement": "childcare"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1},
      "history_text": "You: How many people live in your household?",
      "_stub_extraction": {"requirement": "childcare, eldercare"},
      "_expect_collected": {"requirement": "childcare, eldercare"}}),

    # --- 2026-09-19: the question that contradicted the answer before it ---
    ("info_collector", "an experienced-helper client is not offered a first-timer",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "she should be 4+ year experienced",
      "collected_info": {
          "full_name": "Sanju", "requirement": "childcare",
          "children_detail": "3 children under 18", "household": "8",
          "home_type": "condo", "home_size": "10 bedrooms, 5 bathrooms",
          "helper_room": "own room", "pets": "no", "languages": "English",
          "preferred_nationality": "Indonesian", "helper_religion": "Buddhist",
          "helper_profile": "4+ years experienced"},
      "asked_field_counts": {"full_name": 1, "requirement": 1,
                             "helper_profile": 1},
      "history_text": "You: Any preference on her age or experience?",
      "_stub_extraction": {},
      "_expect_prompt": "Do NOT offer them a first-timer"}),
    ("info_collector", "...while a client with no preference still is",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "no preference",
      "collected_info": {
          "full_name": "Sanju", "requirement": "childcare",
          "children_detail": "3 children under 18", "household": "8",
          "home_type": "condo", "home_size": "10 bedrooms, 5 bathrooms",
          "helper_room": "own room", "pets": "no", "languages": "English",
          "preferred_nationality": "Indonesian", "helper_religion": "Buddhist",
          "helper_profile": "no preference"},
      "asked_field_counts": {"full_name": 1, "requirement": 1,
                             "helper_profile": 1},
      "history_text": "You: Any preference on her age or experience?",
      "_stub_extraction": {},
      "_forbid_prompt": "Do NOT offer them a first-timer"}),
    # The control at the other end: a turn deeper into the same flow gets no
    # overview at all. A note that fires on every turn is the 2026-09-17
    # workload defect, and a _forbid_ on a turn that could never carry it
    # proves nothing (the home-leave green injection, same day).
    ("info_collector", "...but not on every turn of the same collection",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "condo",
      "collected_info": {"full_name": "Thomas", "requirement": "childcare",
                         "household": "4"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "household": 1},
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Hiring from overseas usually takes 4 to 6 weeks.",
      "history_text": "bot: how many people live in your household?",
      "_forbid_prompt": "roughly how long it takes"}),

    # --- 2026-09-17: one routing question, asked and answered -------------
    ("info_collector", "direct hire asks the one question that changes the route",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "Myanmar",
      "collected_info": {"full_name": "Thomas", "helper_name": "Lwin Lwin Nwe",
                         "helper_nationality": "Myanmar"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1},
      "history_text": "bot: Which country is she from?",
      "_expect_prompt": "working under a Work Permit with another employer"}),
    # ...and does not ask where she is, nor for her number, on the way there.
    ("info_collector", "...and not where she is, nor her number, before it",
     {"service_type": "direct_hiring", "intent": "direct_hiring",
      "incoming_text": "Myanmar",
      "collected_info": {"full_name": "Thomas", "helper_name": "Lwin Lwin Nwe",
                         "helper_nationality": "Myanmar"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "helper_nationality": 1},
      "history_text": "bot: Which country is she from?",
      "_forbid_prompt": "best number to reach her on"}),

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
    # Conversation 4551, 2026-10-08, after the new-hire briefing. An answer to
    # a question we just asked is information for the agent, not a chase.
    ("blocked_topic_responder", "an answer to our own question on a parked topic is noted, not held",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "ys",
      "history_text": "Client: what is that relaxation point?\nYou: Do you mean the helper's rest day "
                      "arrangement? In the meantime, is there anything else I can help you with?",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0031"}},
      "_expect_prompt": "answering a question you just asked"}),
    ("blocked_topic_responder", "...and a long detail about the parked hire is never asked 'which service'",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "Yes, I did not have any rest day arrangement as of now because I need help "
                       "with everyday life. When I am outside she can take rest",
      "history_text": "Client: ys\nYou: A live agent is handling your new helper enquiry and will "
                      "connect with you shortly. In the meantime, is there anything else I can help you with?",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0031"}},
      "_forbid_reply": "which service"}),
    # ...and a change to the request whose reply repeats the last holding line
    # is noted, not swallowed by the repeat guard (4551: no reply at all).
    ("blocked_topic_responder", "...and new information is never answered with silence",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "I need another child care rather than a house help so my budget for this is only 300 to 400",
      "history_text": "Client: all of this\nYou: A live agent is handling your new helper enquiry and will "
                      "connect with you shortly.",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0031",
                                        "captured_info": {"follow_ups": [{"message": "all of this"}]}}},
      "_stub_reply": "A live agent is handling your new helper enquiry and will connect with you shortly.",
      "_expect_reply": "Noted"}),
    # 2026-10-12: a chase and an "are you there?" on a parked topic carry that
    # ticket's reference (app/graph/reference.py), inside the existing sentence.
    # Step 5 item 4: pressed for why it costs that much, the client is told
    # our agent takes them through it - never "let me put together".
    ("blocked_topic_responder", "'why this much' is offered our agent, with the reference",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "why this much?",
      "history_text": "Client: how long does it take\nYou: Passport renewal takes roughly 6 to 8 weeks.",
      "blocked_topics": {"passport_renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_stub_reply": "Passport renewal takes roughly 6 to 8 weeks.",
      "_expect_reply": "Our agent will take you through exactly what it covers.",
      "_forbid_reply": "put together"}),
    # Step 5 B2 item 3: with records that say what the fee covers, "why this
    # much?" is told what it covers - and a reply that only repeats the price
    # is replaced by our agent's line, with the reference.
    ("blocked_topic_responder", "B2: 'why this much' repeating the price -> our agent",
     {"intent": "fee_enquiry", "service_type": "renewal",
      "incoming_text": "why this much?",
      "history_text": "Client: how much is the fee\nYou: The cost is $695.",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "rag_matches": [{"similarity": 0.6, "nationality": "all",
                       "question": "How much does it cost to renew my helper's work permit?",
                       "answer": _RENEWAL_FEE_ROW, "chunk_type": "qa_pair"}],
      "rag_context": _RENEWAL_FEE_ROW, "rag_best_score": 0.6,
      "_stub_reply": "The work permit renewal fee is $695, and the exact amount will be "
                     "confirmed by our team.",
      "_expect_reply": "Our agent will take you through exactly what it covers.",
      "_expect_prompt": "The client is asking WHY the fee is what it is",
      "_forbid_reply": "$695"}),
    ("blocked_topic_responder", "B2: ...and what it covers, from the records, is kept",
     {"intent": "fee_enquiry", "service_type": "renewal",
      "incoming_text": "why this much?",
      "history_text": "Client: how much is the fee\nYou: The cost is $695.",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "rag_matches": [{"similarity": 0.6, "nationality": "all",
                       "question": "How much does it cost to renew my helper's work permit?",
                       "answer": _RENEWAL_FEE_ROW, "chunk_type": "qa_pair"}],
      "rag_context": _RENEWAL_FEE_ROW, "rag_best_score": 0.6,
      "_stub_reply": "That covers us handling the whole renewal - the authorisation, the "
                     "declaration and the submission to MOM.",
      "_expect_reply": "That covers us handling the whole renewal",
      "_forbid_reply": "take you through"}),
    ("blocked_topic_responder", "B2: ...after a holding line, with no records -> our agent",
     {"intent": "fee_enquiry", "service_type": "renewal",
      "incoming_text": "why this much?",
      "history_text": "Client: how much is the fee\nYou: The cost is $695.\nClient: ok\n"
                      "You: Still checking on this with the team for you.",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "rag_matches": [], "rag_context": "", "rag_best_score": 0.0,
      "_expect_reply": "Our agent will take you through exactly what it covers."}),
    ("blocked_topic_responder", "a chase on a parked topic gives its ticket reference",
     {"intent": "case_enquiry", "service_type": "new_hiring",
      "incoming_text": "any update on my helper?",
      "history_text": "Client: I want to hire a helper\nYou: I've passed everything to our team, "
                      "and a live agent will connect with you shortly.\nClient: any update?\n"
                      "You: Still checking on this with the team for you.",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0031"}},
      "_expect_reply": "Your reference number is CB-2026-0031."}),
    ("blocked_topic_responder", "...and so does 'are you there?'",
     {"intent": "other", "service_type": "new_hiring",
      "incoming_text": "are you there?",
      "history_text": "Client: I want to hire a helper\nYou: I've passed everything to our team, "
                      "and a live agent will connect with you shortly.",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0031"}},
      "_expect_reply": "Your reference number is CB-2026-0031."}),
    # Step 2b, 2026-10-13, conversation 3766 (CB-2026-0033). The FIRST chase
    # after the handover went down the generated holding-line path, and the
    # model's "A live agent is handling this..." arrived with no reference.
    # Every parked reply that mentions the agent is also checked by the sweep
    # in _run_case, whichever path wrote it.
    ("blocked_topic_responder", "the first chase after a handover (model-written) gives the reference",
     {"intent": "case_enquiry", "service_type": "renewal",
      "incoming_text": "any update on this",
      "history_text": "Client: in 3 weels\nYou: Here is everything for Devi's Work Permit renewal:\n\n"
                      "Thank you. I've passed everything to our team (reference CB-2026-0033), "
                      "and a live agent will connect with you shortly.",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_stub_reply": "A live agent is handling this and will connect with you shortly. "
                     "In the meantime, is there anything else I can help you with?",
      "_expect_reply": "shortly. Your reference number is CB-2026-0033.",
      # Step 2e: our closing already gave the number, so the short form only.
      "_forbid_reply": "Please quote it"}),
    ("blocked_topic_responder", "...and the vetted fallback line gives it too",
     {"intent": "case_enquiry", "service_type": "renewal",
      "incoming_text": "any update on this",
      "history_text": "Client: in 3 weels\nYou: Thank you, I've passed everything to our team.",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_stub_reply": "We sent it all to the agency and they have passed it up to a live agent.",
      "_expect_reply": "Your reference number is CB-2026-0033."}),
    # The detail itself, after the chase reply had gone out: noted on the
    # ticket and acknowledged, never answered "I am chasing them" or silence.
    ("blocked_topic_responder", "a new detail after a chase is noted with the reference, not chased",
     {"intent": "renewal", "service_type": "renewal",
      "incoming_text": "also please note she prefers to fly on a weekend",
      "history_text": "Client: are you there\nYou: I hear you - I'll get back to you on this.",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033",
                                     "captured_info": {"follow_ups": [{"message": "any update on this"}]}}},
      "_stub_reply": "Noted, I've added that she prefers to fly on a weekend. A live agent will "
                     "connect with you shortly.",
      "_expect_reply": "shortly. Your reference number is CB-2026-0033.",
      # CHASE_REPLY also carries "...shortly. Your reference number...", so the reply alone
      # cannot tell the two paths apart; "has not been forgotten" is in both chase wordings.
      "_forbid_reply": "has not been forgotten"}),
    # Step 2d, 2026-10-13, conversation 3766 (CB-2026-0033), the live shape:
    # "any update?" got a model-written holding line, then "ok can you tell me
    # any updates on this" got "Noted, I've added that for our agent" - the
    # model repeated its holding line and the repeat guard filed the update
    # request as new information. It must get the status reply.
    ("blocked_topic_responder", "an update request after a holding line gets the status, not 'Noted'",
     {"intent": "case_enquiry", "service_type": "renewal",
      "incoming_text": "ok can you tell me any updates on this",
      "history_text": "Client: any update?\nYou: A live agent is already looking into Sharukkhan's "
                      "Work Permit renewal and will connect with you shortly (reference CB-2026-0033). "
                      "In the meantime, is there anything else I can help you with?",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033",
                                     "captured_info": {"follow_ups": [{"message": "any update?"}]}}},
      "_stub_reply": "A live agent is already looking into Sharukkhan's Work Permit renewal and will "
                     "connect with you shortly. In the meantime, is there anything else I can help you with?",
      "_expect_reply": "It is with a live agent, who will connect with you shortly",
      "_forbid_reply": "Noted"}),
    # ...and after a recognised holding line it is the chase branch, which a
    # detail used to skip.
    ("blocked_topic_responder", "...and after 'still checking' it is chased, not noted",
     {"intent": "case_enquiry", "service_type": "renewal",
      "incoming_text": "can you tell me any updates",
      "history_text": "Client: any update?\nYou: Still checking on this with the team for you.",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033",
                                     "captured_info": {"follow_ups": [{"message": "any update?"}]}}},
      "_stub_reply": "Noted, I've added that for our agent.",
      "_expect_reply": "It is with a live agent, who will connect with you shortly",
      "_forbid_reply": "Noted"}),
    # ...while a real detail in the same place is still noted.
    ("blocked_topic_responder", "...while a real detail there is still noted",
     {"intent": "renewal", "service_type": "renewal",
      "incoming_text": "she prefers window seat",
      "history_text": "Client: any update?\nYou: A live agent is already looking into Sharukkhan's "
                      "Work Permit renewal and will connect with you shortly (reference CB-2026-0033). "
                      "In the meantime, is there anything else I can help you with?",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033",
                                     "captured_info": {"follow_ups": [{"message": "any update?"}]}}},
      "_stub_reply": "A live agent is already looking into Sharukkhan's Work Permit renewal and will "
                     "connect with you shortly. In the meantime, is there anything else I can help you with?",
      "_expect_reply": "Noted",
      "_forbid_reply": "has not been forgotten"}),
    # A request for OTHER work handed to a person raises a NEW ticket, which
    # handover_executor numbers - so it must not carry the parked one's.
    ("blocked_topic_responder", "a handover for other work does not carry the parked topic's reference",
     {"intent": "other", "service_type": "renewal",
      "incoming_text": "I want another service",
      # We asked which service once already and our last line is not a
      # question, so this is the NEW_SERVICE_HANDOVER path.
      "history_text": "Client: I want 1 more service\nYou: Of course — which service can I help you with?\n"
                      "Client: hmm\nYou: Take your time.",
      "blocked_topics": {"renewal": {"ticket_id": 1, "ticket_number": "CB-2026-0033"}},
      "_expect_state": {"needs_handover": True},
      "_forbid_reply": "CB-2026-0033"}),
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
    # Re-pointed at `direct_hiring` on 2026-09-22. The reply below is the one
    # that actually went out with a HIRING ticket parked, and `new_hiring` may
    # now quote its own fee - so leaving this state on that service would have
    # deleted the only cover this path has ever had rather than moving it. The
    # defect it was written for is not about which service it was: it is that
    # this node had no cost guard at all until 2026-09-10, and `direct_hiring`
    # is withheld for the same reason `new_hiring` was.
    ("blocked_topic_responder", "employer, a parked reply that prices the hire",
     {"intent": "fee_enquiry", "service_type": "direct_hiring",
      "incoming_text": "Is there any fees I need to pay",
      "history_text": "client: hi\nbot: I've passed this to our team.",
      "_stub_reply": ("The approximate total service fee and third-party costs "
                      "are $4,225, with a combined total of about $4,285."),
      # The deferral, not the figure and not the bare holding line: the client
      # is told WHY they are getting a person instead of a number.
      "_expect_reply": "would rather one of our agents",
      "blocked_topics": {"direct_hiring": {"ticket_id": 1,
                                           "ticket_number": "CB-2026-0001"}}}),
    ("blocked_topic_responder", "ordinary message, parked",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "ok noted thanks",
      "history_text": "client: hi\nbot: I've passed this to our team.",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0001"}}}),

    # --- 2026-09-29: "Hi" the morning after a handover ---------------------
    # Conversation 36: the classifier stuck the parked direct-hire topic back
    # onto a bare greeting, and the reply was "I'm here! This one is with a
    # live agent ... is there anything else I can help you with?" - to a
    # client who had asked nothing that day. The wave emoji is part of the
    # state because it is what defeated greeting_only on the live turn.
    ("blocked_topic_responder", "a greeting on a parked topic is greeted back",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "Hi \U0001f44b", "record_name": "Geraldine",
      "history_text": "Client: How much is the cost\nYou: Our agent will confirm it.",
      "blocked_topics": {"direct_hiring": {"ticket_id": 1,
                                           "ticket_number": "CB-2026-0018"}},
      "_expect_reply": "Hi Geraldine, good to hear from you"}),
    ("blocked_topic_responder", "...and is not told something is with a live agent",
     {"intent": "direct_hiring", "service_type": "direct_hiring",
      "incoming_text": "Hi \U0001f44b", "record_name": "Geraldine",
      "history_text": "Client: How much is the cost\nYou: Our agent will confirm it.",
      "blocked_topics": {"direct_hiring": {"ticket_id": 1,
                                           "ticket_number": "CB-2026-0018"}},
      "_forbid_reply": "live agent"}),

    # --- 2026-09-29: a helper who has run away ------------------------------
    # Conversation 1687: forced to dispute_assault by the keyword "run away"
    # and answered with the 999 script. The model's own verdict is stubbed as
    # dispute_assault too, because that is the worst case the override has to
    # beat.
    ("intent_classifier", "a runaway helper is a missing-helper report, not an assault",
     {"incoming_text": "hi i need help as my helper has run away and is nowhere to be found",
      "intent": None, "service_type": None, "history_text": "",
      "_stub_intent": {"intent": "dispute_assault", "service_type": "dispute_assault",
                       "contact_type": None, "confidence": 0.9},
      "_expect_state": {"intent": "missing_helper", "service_type": "missing_helper"}}),
    # ...and the client's own answer to "have you made a police report?" is
    # not an assault either - "police report" used to force one.
    ("intent_classifier", "...and 'I made a police report' keeps the flow",
     {"incoming_text": "yes i already made a police report this morning",
      "intent": "missing_helper", "service_type": "missing_helper",
      "history_text": "You: Have you made a police report yet?",
      "_stub_intent": {"intent": "other", "service_type": None,
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "missing_helper"}}),
    # The control that must not be traded away: a helper describing violence
    # still gets the safety response, with no model involved.
    ("intent_classifier", "...while a helper who has been hit still gets the safety path",
     {"incoming_text": "my employer hit me and i want to run away",
      "intent": None, "service_type": None, "history_text": "",
      "_stub_intent": {"intent": "other", "service_type": None,
                       "contact_type": None, "confidence": 0.9},
      "_expect_state": {"intent": "dispute_assault"}}),
    # The opening turn says what to do NOW, from the records, before asking -
    # and the reporting deadline in it survives strip_handover_talk, which
    # deleted it as a "promised time" on the first replay.
    ("info_collector", "a missing-helper report gives the first steps before asking",
     {"service_type": "missing_helper", "intent": "missing_helper",
      "incoming_text": "my helper has run away and is not answering my calls",
      "history_text": "",
      "rag_context": ("Our records:\nWhat if my helper goes missing? Report to the police "
                      "and MOM within 24 hours, then contact Ming Hwee immediately."),
      "_stub_reply": ("Hi, I'm Claire, Ming Hwee's AI assistant. Please report it to the "
                      "police and MOM within 24 hours, and we will guide you through it. "
                      "May I know your helper's name?"),
      "_expect_prompt": "THEIR HELPER HAS RUN AWAY",
      "_expect_reply": "within 24 hours"}),
    ("info_collector", "...and only on the opening turn",
     {"service_type": "missing_helper", "intent": "missing_helper",
      "incoming_text": "Siti", "collected_info": {"helper_name": "Siti"},
      "asked_field_counts": {"helper_name": 1},
      "history_text": "Client: my helper has run away\nYou: May I know your helper's name?",
      "_forbid_prompt": "THEIR HELPER HAS RUN AWAY"}),

    # --- 2026-09-18: a money question that names a service -----------------
    # The agency's transcript, 23:05. "what is the fees for work permit
    # renewal?" opened the conversation, so nothing else was established, so
    # fee_enquiry collected its own two fields - "Which nationality are you
    # looking at?" then "What kind of care would this be for?" - and asked the
    # care question three times, the last of them AFTER the client wrote "i am
    # not asking for any new hiring".
    #
    # The service moves and the intent deliberately does not: that is what
    # sends the turn to response_generator, where the price is answered. A
    # state asserting only the service would stay green with the intent
    # promoted too, which routes to the collector and opens a 4-question
    # intake - the defect, one service along - so BOTH are asserted.
    ("intent_classifier", "a fee question naming a service resolves to it",
     {"incoming_text": "what is the fees for work permit renewal?",
      "intent": None, "service_type": None, "history_text": "",
      "_stub_intent": {"intent": "fee_enquiry", "service_type": "fee_enquiry",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "renewal", "intent": "fee_enquiry"}}),

    # The half that is a wrong PRICE rather than a wrong label. `_NAMED_SERVICE`
    # tested `\brenew` before `\bpassport`, so every passport phrasing resolved
    # to the WORK PERMIT service - $695 where the answer is $450, both of them
    # in FEE_STATED_SERVICES, so the figure goes out stated rather than
    # deferred.
    ("intent_classifier", "...and a passport fee is not the work permit's",
     {"incoming_text": "how much for passport renewal", "intent": None,
      "service_type": None, "history_text": "",
      "_stub_intent": {"intent": "fee_enquiry", "service_type": "fee_enquiry",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "passport_renewal",
                        "intent": "fee_enquiry"}}),

    # A service whose price we withhold still moves, so the turn is answered
    # with the deferral line a consultant stands behind rather than qualified.
    ("intent_classifier", "...and a withheld price moves service too",
     {"incoming_text": "what is the fee for a transfer", "intent": None,
      "service_type": None, "history_text": "",
      "_stub_intent": {"intent": "fee_enquiry", "service_type": "fee_enquiry",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "transfer", "intent": "fee_enquiry"}}),

    # THE CONTROL, and the reason this is a named-service rule and not a ban on
    # fee_enquiry collecting. A price question that names nothing cannot be
    # answered without knowing what for - which is what those two fields are
    # for, and what `_other_service_established` deliberately keeps collectible.
    ("intent_classifier", "...but a bare price question still qualifies",
     {"incoming_text": "how much do you charge", "intent": None,
      "service_type": None, "history_text": "",
      "_stub_intent": {"intent": "fee_enquiry", "service_type": "fee_enquiry",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "fee_enquiry", "intent": "fee_enquiry"}}),

    # THE SECOND CONTROL, and it exists because a fault injection found it
    # missing rather than because it was designed in. Widening the rule to fire
    # on every turn (`if True:` in place of the SOFT_SERVICES test) left the
    # four states above green and both suites passing - so they proved the rule
    # does the right thing on a money turn and said nothing about what it does
    # on any other one.
    #
    # Unwidened, this is the case `_WANTS_SERVICE` guards in the parked-topic
    # correction below: a helper's passport mentioned mid-hire is not a request
    # to renew it. The money rule skips it because new_hiring is not a SOFT
    # service, and that is the half being asserted.
    ("intent_classifier", "...and an ordinary turn naming a service is left alone",
     {"incoming_text": "her passport is expiring soon", "intent": "new_hiring",
      "service_type": "new_hiring",
      "history_text": "You: Any preference on her age or experience?",
      "_stub_intent": {"intent": "new_hiring", "service_type": "new_hiring",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "new_hiring"}}),

    # --- 2026-09-18: the same fee question asked twice, answered neither time -
    # The 12:05 transcript's last turn. Asserted on the PROMPT, not the reply:
    # the note tells the model to answer from the records, and reading the
    # reply would pass on any run where it happened to do so anyway - which it
    # did on 1 to 2 runs of 4 before the note existed. The instruction either
    # reaches the model or it does not.
    ("response_generator", "a restated question is answered, not acknowledged",
     {"intent": "fee_enquiry", "service_type": "fee_enquiry",
      "incoming_text": "i have asked for the fees",
      "history_text": "You: I've passed everything to our team, and a live agent "
                      "will connect with you shortly.",
      "_expect_prompt": "already asked"}),

    # Gated on having something to answer WITH. Without records the note would
    # be telling the model to state a figure it does not have, which is the one
    # way this fix could invent a price.
    ("response_generator", "...but not when there is nothing to answer with",
     {"intent": "fee_enquiry", "service_type": "fee_enquiry",
      "incoming_text": "i have asked for the fees",
      "history_text": "You: I've passed everything to our team.",
      "rag_matches": [], "rag_context": "", "rag_best_score": 0.0,
      "_forbid_prompt": "already asked"}),

    # ...and an ordinary question is untouched, so the note cannot leak onto
    # every turn that happens to mention a fee.
    ("response_generator", "...and an ordinary question does not get the note",
     {"intent": "fee_enquiry", "service_type": "renewal",
      "incoming_text": "what is the fee for work permit renewal",
      "history_text": "",
      "_forbid_prompt": "already asked"}),

    # The parked path is the one the live client was actually on: the topic had
    # a ticket, so this node wrote the reply that acknowledged the question for
    # the second time.
    # The records have to carry the figure the stub quotes, or ungrounded_figures
    # correctly bins the reply and the state proves nothing about answerability -
    # which is how this one failed on its first run.
    ("blocked_topic_responder", "a restated question is answerable while parked",
     {"intent": "renewal", "service_type": "renewal",
      "incoming_text": "i have asked for the fees",
      "history_text": "You: I've passed this to our team.",
      "rag_matches": [{"similarity": 0.56,
                       "question": "How much does it cost to renew my helper's work permit?",
                       "answer": "The agency fee is approximately $695.",
                       "chunk_type": "qa_pair"}],
      "rag_context": "Based on our records:\n1. Q: How much does it cost to renew "
                     "my helper's work permit?\n   A: The agency fee is approximately $695.",
      "rag_best_score": 0.56,
      "_stub_reply": "The work permit renewal fee is approximately $695.",
      "_expect_reply": "695",
      # Asserted on the INSTRUCTION, because the reply alone cannot tell the two
      # branches apart: the stubbed model hands back the same text whichever
      # template won, so this state was green with the fix removed until the
      # runner was taught to capture the prompt. `intent` is deliberately
      # `renewal`, which is NOT in KB_QUESTION_INTENTS, so `_answerable` has to
      # reach asks_general_info to get here.
      "_expect_prompt": "answer their question from",
      "blocked_topics": {"renewal": {"ticket_id": 1,
                                     "ticket_number": "CB-2026-0009"}}}),

    # --- 2026-09-19: the opening turn, and the question about US ----------
    # Run rather than predicated. A pattern that matches proves nothing if
    # nothing reads it - the "imported and never called" hole this file has
    # recorded four times - and the whole defect here was that the collector
    # never knew a question had been asked.
    ("info_collector", "a question about what we assumed reaches the answer instruction",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "how you know for what service i need helper",
      "collected_info": {}, "asked_field_counts": {"full_name": 1},
      "history_text": "You: So you're looking to hire a helper, and I'll ask a "
                      "few details. May I know your name?",
      "_stub_extraction": {},
      "_expect_prompt": "The client has also ASKED you something"}),
    # The control that makes it safe: an ordinary answer must NOT be read as a
    # question, or the collector promises to come back with an answer to
    # nothing (2026-09-08).
    ("info_collector", "...while an ordinary answer is not",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "3-4",
      "collected_info": {}, "asked_field_counts": {"household": 1},
      "history_text": "You: How many people live in your household?",
      "_stub_extraction": {},
      "_forbid_prompt": "The client has also ASKED you something"}),
    # The opening turn names the service it understood and states nothing else
    # about the client - and still ends with the question.
    ("info_collector", "the opening turn names what it understood, and asserts nothing else",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "hey i need helper",
      "collected_info": {}, "asked_field_counts": {},
      "history_text": "",
      "_stub_extraction": {},
      "_expect_prompt": "NAMING WHAT YOU HAVE UNDERSTOOD"}),
    ("info_collector", "...and is told the message still ends with the question",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "hey i need helper",
      "collected_info": {}, "asked_field_counts": {},
      "history_text": "",
      "_stub_extraction": {},
      "_expect_prompt": "ENDS WITH THE QUESTION"}),
    # ...and it is said ONCE. Said again at question nine it is the preamble
    # the note forbids by name.
    ("info_collector", "...but only on the opening turn",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "condo",
      "collected_info": {"full_name": "Sallu", "requirement": "childcare"},
      "asked_field_counts": {"full_name": 1, "requirement": 1, "home_type": 1},
      "history_text": "You: What type of home are you in?",
      "_stub_extraction": {},
      "_forbid_prompt": "NAMING WHAT YOU HAVE UNDERSTOOD"}),

    # --- a fee question on a nationality-priced service, 2026-09-22 ------
    # The agency gave the fee schedule and asked that the bot answer from it.
    # Six agency fees now sit in the knowledge base and three of them are
    # retrievable on any hiring turn - measured, "how much is the agency fee
    # for a new hire?" returns ID at 0.688 and PH and MM at 0.679, nine
    # thousandths apart. Whichever the model reaches for it is right one time
    # in three, and the client cannot tell which time it was.
    #
    # Run rather than predicated: fee_varies_by_nationality and
    # nationality_in_play were both correct the whole time, and what decides
    # the client's answer is whether anything APPENDS the note. That is the
    # "imported and never called" hole, recorded five times in this file.
    ("response_generator", "a hiring fee question with no nationality settled",
     {"intent": "fee_enquiry", "service_type": "new_hiring",
      "incoming_text": "how much is the agency fee for a new hire?",
      "collected_info": {},
      "history_text": "client: hi\nbot: Hello",
      "_expect_prompt": "DEPENDS ON THE HELPER'S NATIONALITY"}),
    # ...and once she is named it is an ordinary fee question again. Named via
    # `preferred_nationality`, which is the field the EMPLOYER flow actually
    # collects - the collector read `nationality` alone until 2026-09-22, so
    # this state fails against the old reader and is what proves the fix.
    ("response_generator", "...and not once the nationality is settled",
     {"intent": "fee_enquiry", "service_type": "new_hiring",
      "incoming_text": "how much is the agency fee for a new hire?",
      "collected_info": {"preferred_nationality": "Indonesian"},
      "history_text": "client: hi\nbot: Hello",
      "_forbid_prompt": "DEPENDS ON THE HELPER'S NATIONALITY"}),
    # A TIMING question is not a fee question and must not be told the answer
    # depends on her nationality. This is the gate that keeps the 2026-09-03
    # widening retry intact, read through the same predicate.
    ("response_generator", "...and a timing question is not a fee question",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "how long does it take to hire a helper?",
      "collected_info": {},
      "history_text": "client: hi\nbot: Hello",
      "_forbid_prompt": "DEPENDS ON THE HELPER'S NATIONALITY"}),
    # The other half: we know who she is and hold no figure for her. Asking
    # which nationality would be asking a question we have the answer to, so
    # this gets the deferral note instead.
    ("response_generator", "a Myanmar passport fee, which we do not hold",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "how much is the passport renewal?",
      "collected_info": {"nationality": "Myanmar"},
      "history_text": "client: hi\nbot: Hello",
      "_expect_prompt": "do NOT have a fee on record"}),
    ("response_generator", "...while a Filipino one is simply answered",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "how much is the passport renewal?",
      "collected_info": {"nationality": "Filipino"},
      "history_text": "client: hi\nbot: Hello",
      "_forbid_prompt": "do NOT have a fee on record"}),
    # ...and a service priced flat is never asked about a nationality at all.
    ("response_generator", "...and a work permit renewal is priced flat",
     {"intent": "fee_enquiry", "service_type": "renewal",
      "incoming_text": "what is the fee for work permit renewal?",
      "collected_info": {},
      "history_text": "client: hi\nbot: Hello",
      "_forbid_prompt": "DEPENDS ON THE HELPER'S NATIONALITY"}),
    # And the reply half: a transfer fee that IS established goes out as the
    # figure rather than the deferral, which is the guard change this commit
    # is about. `new_hiring` and both transfer keys left
    # COST_WITHHELD_SERVICES, so quotes_hiring_package_cost no longer swaps
    # this for COST_DEFERRAL_REPLY.
    ("response_generator", "an established transfer fee is given, not deferred",
     {"intent": "fee_enquiry", "service_type": "transfer_employer",
      "incoming_text": "how much does an Indonesian transfer helper cost?",
      "collected_info": {"preferred_nationality": "Indonesian"},
      "history_text": "client: hi\nbot: Hello",
      "rag_context": "The agency fee for a transfer helper from Indonesia is "
                     "$1,588. Insurance, MOM processing and lodging are extra.",
      "_stub_reply": "The agency fee for an Indonesian transfer helper is $1,588.",
      "_expect_reply": "$1,588"}),
    # ...while a direct hire, which the schedule leaves blank, still defers.
    # The do-not-add-them-up note. Asserted on the PROMPT, because the reply
    # half is already covered by the live run and what this proves is that
    # anything appends it at all - the fault that came back GREEN on the first
    # injection pass.
    ("response_generator", "a fee turn is told not to add the fees together",
     {"intent": "fee_enquiry", "service_type": "new_hiring",
      "incoming_text": "what are the fees for an Indonesian new hire?",
      "collected_info": {"preferred_nationality": "Indonesian"},
      "history_text": "client: hi\nbot: Hello",
      "_expect_prompt": "FEES ARE SEPARATE AMOUNTS, NOT A SUM"}),
    ("response_generator", "...and an ordinary turn is not",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "she should be good with children",
      "collected_info": {"preferred_nationality": "Indonesian"},
      "history_text": "client: hi\nbot: Hello",
      "_forbid_prompt": "FEES ARE SEPARATE AMOUNTS, NOT A SUM"}),
    ("response_generator", "...while a direct hire still defers its price",
     {"intent": "fee_enquiry", "service_type": "direct_hiring",
      "incoming_text": "how much does a direct hire cost?",
      "collected_info": {},
      "history_text": "client: hi\nbot: Hello",
      "rag_context": "The total service fee and third-party costs are $4,225.",
      "_stub_reply": "The total service fee and third-party costs are $4,225.",
      "_expect_reply": "rather one of our agents"}),

    # --- 2026-10-13: Phase 3 Step 4, one nationality's fee is not another's ---
    # Section 9.28: "How much is passport renewal for my Myanmar helper?" was
    # answered "$450" about one run in ten. $450 IS in the records - as the
    # Filipino and Indonesian price - so ungrounded_figures passes it and the
    # only protection was a prompt note. These run the reply the model actually
    # wrote through each of the three paths that write a reply.
    ("response_generator", "a Myanmar passport renewal never shows $450",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "How much is passport renewal for my Myanmar helper?",
      "collected_info": {},
      "history_text": "client: hi\nbot: Hello",
      "rag_context": _PASSPORT_FEE_ROW,
      "_stub_reply": "For a Myanmar helper, the passport renewal fee is approximately "
                     "$450. The appointment wait can take several weeks.",
      "_forbid_reply": "450",
      "_expect_reply": _MM_PASSPORT_DEFERRAL}),
    ("response_generator", "...while a Filipino passport renewal still shows $450",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "How much is passport renewal for my Filipino helper?",
      "collected_info": {},
      "history_text": "client: hi\nbot: Hello",
      "rag_context": _PASSPORT_FEE_ROW,
      "_stub_reply": "For a Filipino helper, the passport renewal fee is approximately $450.",
      "_expect_reply": "$450"}),
    # A nationality we DO price: the Philippines figure must not go to an
    # Indonesian new hire, even though all three sit in the records.
    ("response_generator", "an Indonesian new hire never shows the Philippines fee",
     {"intent": "fee_enquiry", "service_type": "new_hiring",
      "incoming_text": "What are the fees for Indonesian new hire?",
      "collected_info": {},
      "history_text": "client: hi\nbot: Hello",
      "rag_context": _NEW_HIRE_FEE_ROWS,
      "_stub_reply": "For an Indonesian new hire, the agency fee is $1,428. Insurance "
                     "and MOM processing are charged separately.",
      "_forbid_reply": "1,428",
      "_expect_reply": "charged separately"}),
    ("response_generator", "...while its own fee goes out",
     {"intent": "fee_enquiry", "service_type": "new_hiring",
      "incoming_text": "What are the fees for Indonesian new hire?",
      "collected_info": {},
      "history_text": "client: hi\nbot: Hello",
      "rag_context": _NEW_HIRE_FEE_ROWS,
      "_stub_reply": "For an Indonesian new hire, the agency fee is $1,188.",
      "_expect_reply": "$1,188"}),
    # --- Step 4b: the live 3766 turn, 2026-10-13 17:30 IST ---------------
    # Passport renewal for Lwin Lwin, "Myanmar" given as an answer, CB-2026-0033
    # raised, then "ok how much is the fee" -> "The fee is approximately $450
    # for a Myanmar helper." The stub is that reply, word for word.
    ("blocked_topic_responder", "3766: parked passport, Myanmar collected, 'how much is the fee' -> no figure",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "ok how much is the fee",
      "collected_info": {"full_name": "growwstacks", "helper_name": "lwin lwin",
                         "nationality": "Myanmar", "passport_expiry": "next year"},
      "history_text": "You: I've passed everything to our team, and a live agent will "
                      "connect with you shortly.",
      "rag_matches": [{"similarity": 0.55, "nationality": "all",
                       "question": "How much does it cost to renew my helper's passport?",
                       "answer": _PASSPORT_FEE_ROW, "chunk_type": "qa_pair"}],
      "rag_context": _PASSPORT_FEE_ROW, "rag_best_score": 0.55,
      "_stub_reply": "The fee is approximately $450 for a Myanmar helper. A live agent will "
                     "confirm the exact amount and connect with you shortly.",
      "_forbid_reply": "450",
      # Its second sentence already defers, so the cost sentence goes without
      # a second "confirm" in its place.
      "_expect_reply": "A live agent will confirm the exact amount",
      "blocked_topics": {"passport_renewal": {"ticket_number": "CB-2026-0033",
                                              "captured_info": {"details": {"nationality": "Myanmar"}}}}}),
    # ...and with the collection gone, the parked ticket still says whose fee it is.
    ("blocked_topic_responder", "...and with only the ticket knowing she is Myanmar, still no figure",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "ok how much is the fee",
      "collected_info": {},
      "history_text": "You: I've passed everything to our team.",
      "rag_matches": [{"similarity": 0.55, "nationality": "all",
                       "question": "How much does it cost to renew my helper's passport?",
                       "answer": _PASSPORT_FEE_ROW, "chunk_type": "qa_pair"}],
      "rag_context": _PASSPORT_FEE_ROW, "rag_best_score": 0.55,
      "_stub_reply": "The fee is approximately $450 for her. A live agent will confirm it.",
      "_forbid_reply": "450",
      "blocked_topics": {"passport_renewal": {"ticket_number": "CB-2026-0033",
                                              "captured_info": {"details": {"nationality": "Myanmar"}}}}}),
    # The Filipino control on the same path: $450 is hers and goes out.
    ("blocked_topic_responder", "...while a parked Filipino passport renewal still shows $450",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "ok how much is the fee",
      "collected_info": {"helper_name": "Ana", "nationality": "Filipino"},
      "history_text": "You: I've passed everything to our team.",
      "rag_matches": [{"similarity": 0.55, "nationality": "all",
                       "question": "How much does it cost to renew my helper's passport?",
                       "answer": _PASSPORT_FEE_ROW, "chunk_type": "qa_pair"}],
      "rag_context": _PASSPORT_FEE_ROW, "rag_best_score": 0.55,
      "_stub_reply": "The fee is approximately $450 for a Filipino helper.",
      "_expect_reply": "$450",
      "blocked_topics": {"passport_renewal": {"ticket_number": "CB-2026-0034",
                                              "captured_info": {"details": {"nationality": "Filipino"}}}}}),
    # The closing briefing, which states a cost section by construction.
    ("info_collector", "a Myanmar passport briefing never shows $450",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "in 2 months",
      "collected_info": {"full_name": "Vaidik", "helper_name": "Holabhola",
                         "nationality": "Myanmar", "passport_expiry": "2 months"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1,
                             "passport_expiry": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": _PASSPORT_FEE_ROW, "similarity": 0.6}],
      "rag_context": _PASSPORT_FEE_ROW,
      "history_text": "You: When does her current passport expire?",
      "_stub_reply": _MM_PASSPORT_BRIEFING_REPLY,
      "_forbid_reply": "450",
      "_expect_reply": _MM_PASSPORT_DEFERRAL}),
    ("blocked_topic_responder", "a parked Myanmar passport renewal never shows $450",
     {"intent": "fee_enquiry", "service_type": "passport_renewal",
      "incoming_text": "how much is the fee?",
      "collected_info": {"helper_name": "Lwin", "nationality": "Myanmar"},
      "history_text": "You: I've passed this to our team.",
      "rag_matches": [{"similarity": 0.56, "question": "How much does it cost to renew "
                       "my helper's passport?", "answer": _PASSPORT_FEE_ROW,
                       "chunk_type": "qa_pair"}],
      "rag_context": _PASSPORT_FEE_ROW,
      "rag_best_score": 0.56,
      "_stub_reply": "For Lwin, the passport renewal fee is approximately $450.",
      "_forbid_reply": "450",
      "_expect_reply": _MM_PASSPORT_DEFERRAL,
      "blocked_topics": {"passport_renewal": {"ticket_id": 1,
                                              "ticket_number": "CB-2026-0040"}}}),

    # --- 2026-10-07: conversation 26, a complaint and a direct-hire enquiry ---
    # Our TEAM had been messaging this client all week; Claire had not. The
    # history was not empty, so she never introduced herself. Agent lines are
    # "Agent:" now, and only a "You:" line counts as her having spoken.
    ("response_generator", "Claire introduces herself where only our team has spoken",
     {"intent": "general_question", "service_type": None,
      "incoming_text": "what are your opening hours?",
      "history_text": "Agent: Good afternoon Thomas, require to submit MOM application\nClient: Okay\nAgent: Kindly authorise",
      "_expect_prompt": "introduce yourself in one short sentence"}),
    ("info_collector", "...and so does the collector",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "i want to hire a helper",
      "history_text": "Agent: Good afternoon Thomas, require to submit MOM application\nClient: Okay\nAgent: Kindly authorise",
      "_expect_prompt": "your introduction is NOT optional"}),
    # "Hi / I want to lodge a complaint" was glued onto the direct-hire intake
    # by the model ("the active direct-hiring service remains in progress").
    ("intent_classifier", "a complaint is its own topic, not the intake in progress",
     {"incoming_text": "Hi\nI want to lodge a complaint",
      "intent": "direct_hiring", "service_type": "direct_hiring",
      "history_text": "Client: tell me about direct hire\nYou: May I know your name?",
      "_stub_intent": {"intent": "direct_hiring", "service_type": "direct_hiring",
                       "contact_type": "employer", "confidence": 0.96},
      "_expect_state": {"intent": "complaint", "service_type": "complaint"}}),
    # ...and what the client says next IS the complaint. Live, the description
    # was read as a direct-hire case enquiry and got the next intake question.
    ("intent_classifier", "...and what they say next belongs to the complaint",
     {"incoming_text": "it is about my helper Rowena, your agent did not reply for 3 days",
      "intent": "complaint", "service_type": "complaint",
      "history_text": "Client: I want to lodge a complaint\nYou: Please tell me here what happened.",
      "_stub_intent": {"intent": "case_enquiry", "service_type": "direct_hiring",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"intent": "complaint", "service_type": "complaint"}}),
    # 2026-10-08: the REASON for a replacement mentioned past complaints and
    # was filed as complaint CB-2026-0031, abandoning the replacement.
    ("intent_classifier", "an answer that mentions past complaints keeps the replacement",
     {"incoming_text": "He is not good at cooking, she always makes more salt in my food. "
                       "Many times I have complained about this to you also",
      "intent": "replacement", "service_type": "replacement",
      "history_text": "Client: It's about 4 months\nYou: Could you share the reason you would like to replace Ranchordas?",
      "_stub_intent": {"intent": "replacement", "service_type": "replacement",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"intent": "replacement", "service_type": "replacement"}}),
    # ...even when the model itself reads it as a complaint.
    ("intent_classifier", "...even when the model calls it a complaint",
     {"incoming_text": "Many times I have complained about this to you also",
      "intent": "replacement", "service_type": "replacement",
      "history_text": "Client: It's about 4 months\nYou: Could you share the reason you would like to replace her?",
      "_stub_intent": {"intent": "complaint", "service_type": "complaint",
                       "contact_type": "employer", "confidence": 0.8},
      "_expect_state": {"service_type": "replacement"}}),
    # ...while the same words with no question of ours pending still open one.
    ("intent_classifier", "...but the same words unprompted are a complaint",
     {"incoming_text": "Many times I have complained about this to you also",
      "intent": "replacement", "service_type": "replacement",
      "history_text": "Client: ok\nYou: A live agent will connect with you shortly.",
      "_stub_intent": {"intent": "other", "service_type": "replacement",
                       "contact_type": "employer", "confidence": 0.6},
      "_expect_state": {"service_type": "complaint"}}),
    # The controls: "no complaints" is not one, and a client can drop it.
    ("intent_classifier", "...while 'no complaints' is not a complaint",
     {"incoming_text": "no complaints so far, how long does it take?",
      "intent": "direct_hiring", "service_type": "direct_hiring",
      "history_text": "Client: hi\nYou: How can I help?",
      "_stub_intent": {"intent": "process_question", "service_type": "direct_hiring",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "direct_hiring"}}),
    ("intent_classifier", "...and 'never mind' ends the complaint",
     {"incoming_text": "never mind, forget it. I want to hire a helper",
      "intent": "complaint", "service_type": "complaint",
      "history_text": "Client: I want to complain\nYou: Please tell me here what happened.",
      "_stub_intent": {"intent": "new_hiring", "service_type": "new_hiring",
                       "contact_type": "employer", "confidence": 0.9},
      "_expect_state": {"service_type": "new_hiring"}}),
    # The agency's own wording: sorry, what we need, when, and who to call -
    # and never "anything else?" to somebody who has just complained.
    ("info_collector", "a complaint opens with the wait, the number and no 'anything else'",
     {"intent": "complaint", "service_type": "complaint",
      "incoming_text": "Hi\nI want to lodge a complaint",
      "history_text": "Agent: Good afternoon Thomas, require to submit MOM application\nClient: Okay\nAgent: Kindly authorise",
      "_stub_extraction": {},
      "_expect_reply": "within one working day",
      "_forbid_reply": "anything else"}),
    ("info_collector", "...introduces Claire where she has not spoken",
     {"intent": "complaint", "service_type": "complaint",
      "incoming_text": "I want to lodge a complaint",
      "history_text": "Agent: Good afternoon Thomas, require to submit MOM application\nClient: Okay\nAgent: Kindly authorise",
      "_stub_extraction": {},
      "_expect_reply": "I'm Claire, Ming Hwee's AI assistant"}),
    ("info_collector", "...and asks for all of it at once",
     {"intent": "complaint", "service_type": "complaint",
      "incoming_text": "I want to lodge a complaint",
      "history_text": "",
      "_stub_extraction": {},
      "_expect_reply": "your name, your case or helper details and what happened",
      "_expect_state": {"asked_field_counts": {"full_name": 1, "complaint_subject": 1,
                                               "complaint_detail": 1}}}),
    # "I want to lodge a complaint" is the request, not the complaint - filed as
    # it, the collection would close on the opening turn with nothing in it.
    ("info_collector", "...and the request is not filed as the complaint",
     {"intent": "complaint", "service_type": "complaint",
      "incoming_text": "I want to lodge a complaint",
      "history_text": "",
      "_stub_extraction": {"complaint_detail": "wants to lodge a complaint"},
      "_expect_not_collected": ["complaint_detail"],
      "_expect_state": {"info_complete": False}}),
    ("info_collector", "...while what happened closes it",
     {"intent": "complaint", "service_type": "complaint", "collected_service": "complaint",
      "incoming_text": "your agent did not reply to me for 3 days about the MOM application",
      "history_text": "Client: I want to complain\nYou: Please tell me here what happened.",
      "collected_info": {"full_name": "Thomas", "complaint_subject": "my helper Rowena"},
      "asked_field_counts": {"complaint_detail": 1},
      "_stub_extraction": {"complaint_detail": "agent did not reply for 3 days about the MOM application"},
      "_expect_state": {"info_complete": True},
      "_expect_reply": "has been logged"}),
    # The reference number only exists once the ticket does.
    ("ticket_creator", "a logged complaint is given its reference number",
     {"intent": "complaint", "service_type": "complaint",
      "incoming_text": "your agent did not reply to me",
      "collected_info": {"full_name": "Thomas", "complaint_detail": "no reply for 3 days"},
      "_expect_reply": "CB-2026-0099"}),
    # "find out more about direct hire process and costs" - answered in full,
    # then the first question, even though the classifier called it a fee turn.
    ("info_collector", "a client asking to hear about a service is told before asked",
     {"intent": "fee_enquiry", "service_type": "direct_hiring",
      "incoming_text": "Hi I will like to find out more about direct hire process and costs",
      "history_text": "Agent: Good afternoon Thomas, require to submit MOM application\nClient: Okay\nAgent: Kindly authorise",
      "_stub_extraction": {},
      "_expect_prompt": "THE CLIENT HAS ASKED TO KNOW ABOUT THIS SERVICE"}),
    ("info_collector", "...and not again once the collection is under way",
     {"intent": "process_question", "service_type": "direct_hiring",
      "collected_service": "direct_hiring",
      "incoming_text": "what is the process?",
      "history_text": "Client: hi\nYou: May I know your name?",
      "collected_info": {"full_name": "Thomas"}, "asked_field_counts": {"full_name": 1},
      "_stub_extraction": {},
      "_forbid_prompt": "THE CLIENT HAS ASKED TO KNOW ABOUT THIS SERVICE"}),
]


# What the EXTRACTOR returned on this turn. Set per state by the loop below.
#
# Added 2026-09-16. The guard that drops an invented care type was tested
# only by calling the predicate, so restoring the broken call site left every
# check green - the same "imported and never called" hole that let the
# hiring-cost guard sit disabled for two days (2026-09-10). A value the
# extractor made up is a thing only the NODE can drop, so the node has to run.
_EXTRACTION: dict = {}
_BRIEFING_RECORDS: dict = {}

# What the model was actually handed on the last run. A fix that lives in the
# INSTRUCTION cannot be proved by reading the reply: the note that raises the
# one-helper warning sets its own state flag whether or not anything appends
# it to the prompt, so without this an instruction nobody sends stays green -
# the 'imported and never called' hole, 2026-09-10 and again 2026-09-16.
_LAST_SYSTEM_PROMPT = ""


async def _run_info_collector(state, stub=None):
    async def _capture(system_prompt, *a, **kw):
        global _LAST_SYSTEM_PROMPT
        _LAST_SYSTEM_PROMPT = system_prompt or ""
        return stub or "When does her passport expire?"

    # The briefing's own search (Step 2c) never reaches a database here: {}
    # means "use this turn's records", which is what every older state expects.
    import importlib
    retriever = importlib.import_module("app.graph.nodes.rag_retriever")
    with patch("app.graph.nodes.info_collector.complete",
               new=_capture, create=True), \
         patch("app.graph.nodes.info_collector.complete_json",
               new=AsyncMock(return_value=dict(_EXTRACTION)), create=True), \
         patch.object(retriever, "briefing_records",
                      new=AsyncMock(return_value=dict(_BRIEFING_RECORDS))), \
         patch("app.graph.nodes.info_collector._open_lead_early",
               new=AsyncMock(return_value={})),          patch("app.graph.nodes.info_collector.rest_day_rule_record",
               new=AsyncMock(return_value=_REST_DAY_RULE_ROW)):
        from app.graph.nodes.info_collector import info_collector
        return await info_collector(state)


async def _run_response_generator(state, stub=None):
    # Returns a stepped reply so the widened clamp and the list-marker masking
    # are exercised end to end, not just in a unit assertion.
    async def _capture(system_prompt, *a, **kw):
        global _LAST_SYSTEM_PROMPT
        _LAST_SYSTEM_PROMPT = system_prompt or ""
        return stub or STEPPED_REPLY

    with patch("app.graph.nodes.response_generator.complete",
               new=_capture, create=True):
        from app.graph.nodes.response_generator import response_generator
        return await response_generator(state)


async def _run_blocked_topic_responder(state, stub=None):
    # Captures the prompt for the same reason info_collector does. The stubbed
    # model returns the same reply whichever instruction won, so a state that
    # only reads the reply cannot tell "decided to answer" from "decided to
    # acknowledge and happened not to bin my stub" - which is how the restated-
    # question state was green with the fix removed (2026-09-18).
    async def _capture(system_prompt, *a, **kw):
        global _LAST_SYSTEM_PROMPT
        _LAST_SYSTEM_PROMPT = system_prompt or ""
        return stub or STEPPED_REPLY

    with patch("app.graph.nodes.blocked_topic_responder.complete",
               new=_capture, create=True):
        from app.graph.nodes.blocked_topic_responder import blocked_topic_responder
        return await blocked_topic_responder(state)


async def _run_intent_classifier(state, stub=None):
    """The classifier, with the model's own verdict stubbed in.

    The state carries `_stub_intent` - what the LLM returned live - so what is
    exercised here is the POST-PROCESSING that runs on top of it: the
    stickiness rules, the named-service corrections and the money rule. Those
    are where every classifier fix in this file's history actually lives, and
    until 2026-09-18 this node had no execution cover at all.
    """
    verdict = state.pop("_stub_intent", None) or {
        "intent": state.get("intent"), "service_type": state.get("service_type"),
        "contact_type": state.get("contact_type"), "confidence": 0.9,
    }
    with patch("app.graph.nodes.intent_classifier.complete_json",
               new=AsyncMock(return_value=dict(verdict))):
        from app.graph.nodes.intent_classifier import intent_classifier
        return await intent_classifier(state)


async def _run_ticket_creator(state, stub=None):
    """ticket_creator with the ticket insert and assignment stubbed.

    Added 2026-10-07 for the complaint's closing message, which carries the
    ticket number and can only be written once the row exists - so the only
    proof that it is written at all is to run the node.
    """
    with patch("app.graph.nodes.ticket_creator.ticket_service.create",
               new=AsyncMock(return_value={"id": "t-1", "ticket_number": "CB-2026-0099"})), \
         patch("app.graph.nodes.ticket_creator.ticket_service.pick_ticket_to_update",
               new=AsyncMock(return_value=None)), \
         patch("app.graph.nodes.ticket_creator.assignment_service.resolve_agent",
               new=AsyncMock(return_value=(None, "unassigned"))):
        from app.graph.nodes.ticket_creator import ticket_creator
        return await ticket_creator(state)


RUNNERS = {
    "ticket_creator": _run_ticket_creator,
    "info_collector": _run_info_collector,
    "response_generator": _run_response_generator,
    "blocked_topic_responder": _run_blocked_topic_responder,
    "intent_classifier": _run_intent_classifier,
}


async def _reference_checks() -> list[tuple[str, bool]]:
    """The ticket reference in the handover sentence (app/graph/reference.py).

    Runs ticket_creator and then handover_executor, the order the graph runs
    them, with the insert, the merge lookup, the assignment and the handover
    log stubbed - so the number that reaches the reply is the one the real
    ticket_creator handed on, for a new ticket and for one it was folded into.
    """
    from app.config import settings as _settings
    from app.graph import guards as _guards
    import importlib
    hx = importlib.import_module("app.graph.nodes.handover_executor")
    tc = importlib.import_module("app.graph.nodes.ticket_creator")

    closing = ("Thank you, Vaidik. I've passed everything to our team, and a live agent "
               "will connect with you shortly. In the meantime, is there anything else I "
               "can help you with?")
    base = {"conversation_id": 77, "phone": "+6590000126", "intent": "renewal",
            "service_type": "renewal", "contact_type": "employer",
            "collected_info": {"full_name": "Vaidik", "helper_name": "Siti",
                               "permit_expiry": "next month"},
            "blocked_topics": {}, "reply": closing, "incoming_text": "next month"}

    async def run(state, merge_target=None):
        create = AsyncMock(return_value={"id": "t-1", "ticket_number": "CB-2026-0099"})
        with patch.object(tc.ticket_service, "create", new=create), \
             patch.object(tc.ticket_service, "pick_ticket_to_update",
                          new=AsyncMock(return_value=merge_target)), \
             patch.object(tc.ticket_service, "merge_into", new=AsyncMock()), \
             patch.object(tc.assignment_service, "resolve_agent",
                          new=AsyncMock(return_value=(None, "unassigned"))), \
             patch.object(tc, "_finish_lead", new=AsyncMock(return_value=None)), \
             patch.object(tc, "_maybe_create_lead", new=AsyncMock(return_value=None)), \
             patch.object(hx.handover_service, "log_escalation",
                          new=AsyncMock(return_value={})):
            state = dict(state)
            state.update(await tc.ticket_creator(state))
            out = await hx.handover_executor(state)
        return out.get("reply", state.get("reply")), create

    results = []
    reply, create = await run(base)
    # Step 2e: the agency's wording, a plain sentence after the handover one.
    _first = ("Your reference number is CB-2026-0099. Please quote it whenever you ask "
              "about this request.")
    results.append(("reference: a new ticket gives its number as the agency's sentence",
                    _first in reply and "(reference" not in reply))
    results.append(("reference: ...straight after the handover sentence, nothing else changed",
                    reply == closing.replace("shortly. ", "shortly. " + _first + " ")))

    reply, create = await run(base, merge_target={"id": "t-7", "ticket_number": "CB-2026-0007"})
    results.append(("reference: a message folded into a ticket shows THAT ticket's number",
                    "CB-2026-0007" in reply and "CB-2026-0099" not in reply
                    and not create.await_count))

    assault = dict(base, intent="dispute_assault", service_type="dispute_assault",
                   reply=hx.ASSAULT_FALLBACK_REPLY, ticket_id="t-9",
                   ticket_number="CB-2026-0009")
    with patch.object(hx.handover_service, "log_escalation", new=AsyncMock(return_value={})):
        out = await hx.handover_executor(assault)
    results.append(("reference: the safety reply shows none (SAFETY_REPLY_SHOWS_REFERENCE)",
                    "CB-2026" not in (out.get("reply") or assault["reply"])))

    with patch.object(_settings, "handover_reference", False):
        reply, _ = await run(base)
    results.append(("reference: HANDOVER_REFERENCE off -> the old wording", reply == closing))

    from app.graph.reference import add_reference
    # A closing that already carries its number AND a handover phrase, so the
    # duplicate check is the only thing stopping a second copy (the first
    # version had no phrase to insert at, and passed with the check removed).
    complaint = ("I've passed this to our team under reference CB-2026-0099, and a "
                 "live agent will connect with you shortly.")
    results.append(("reference: never added twice (a closing that already has it)",
                    add_reference(complaint, "CB-2026-0099") == complaint))

    with_ref = "I've passed this to our team (reference CB-2026-0142), and a live agent will connect with you shortly."
    results.append(("reference: ungrounded_figures does not read it as a figure",
                    _guards.ungrounded_figures(with_ref, "") == []))
    results.append(("reference: ...and still catches a real price beside it",
                    _guards.ungrounded_figures("It costs $450 (reference CB-2026-0142).", "") == ["450"]))
    results.append(("reference: strip_handover_talk keeps it",
                    "(reference CB-2026-0142)" in _guards.strip_handover_talk(with_ref)))

    # Step 2e: the sentence form - short once told, in the reply's language,
    # and the bracket back with the switch off.
    ref = importlib.import_module("app.graph.reference")
    held = "A live agent is handling this and will connect with you shortly."
    told = "You: ... Your reference number is CB-2026-0142. Please quote it whenever you ask about this request."
    later = ref.add_reference(held, "CB-2026-0142", told)
    results.append(("reference 2e: a later reply gives the short sentence only",
                    later == held + " Your reference number is CB-2026-0142."))
    results.append(("reference 2e: the first time it says to quote it",
                    "Please quote it whenever you ask about this request." in
                    ref.add_reference(held, "CB-2026-0142", "")))
    with_sentence = ref.add_reference(held, "CB-2026-0142", "")
    results.append(("reference 2e: ungrounded_figures does not read the sentence as a figure",
                    _guards.ungrounded_figures(with_sentence, "") == []))
    results.append(("reference 2e: strip_handover_talk keeps the sentence",
                    "Your reference number is CB-2026-0142." in _guards.strip_handover_talk(with_sentence)))
    for code, text, expect in (
        ("Indonesian", "Terima kasih. Saya sudah meneruskan semua ke tim kami, dan agen akan "
                       "segera menghubungi Anda.", "Nomor referensi Anda adalah CB-2026-0142. Mohon"),
        ("Malay", "Saya telah memaklumkan pasukan kami, dan ejen kami akan menghubungi anda "
                  "tidak lama lagi. Sila tunggu.", "Nombor rujukan anda ialah CB-2026-0142. Sila"),
        ("Tagalog", "Salamat po. Naipasa ko na ito sa aming team, at kokontakin ka ng live agent.",
                    "Ang iyong reference number ay CB-2026-0142."),
        ("Chinese", "好的，我已将所有资料转交给我们的团队，专员会尽快联系您。还有什么可以帮您？",
                    "联系您。您的参考编号是 CB-2026-0142。"),
        ("Burmese", "ကျေးဇူးတင်ပါတယ်။ live agent တစ်ဦး မကြာမီ ဆက်သွယ်ပါမယ်။ အခြားကူညီစရာ ရှိပါသလား။",
                    "ဆက်သွယ်ပါမယ်။ သင့်ရည်ညွှန်းနံပါတ်မှာ CB-2026-0142"),
        # Live replay: the model ended an Indonesian reply with stray Chinese.
        ("Indonesian with stray Chinese", "Agen kami sedang menangani perpanjangan Work Permit "
                                          "dan akan menghubungi Anda segera. 天天彩票中奖",
                    "segera. Nomor referensi Anda adalah CB-2026-0142."),
    ):
        got = ref.add_reference(text, "CB-2026-0142", "")
        results.append((f"reference 2e: in the reply's language ({code})",
                        expect in got and "Your reference number" not in got))
    results.append(("reference 2e: a reply that mentions no agent is left alone",
                    ref.add_reference("Noted, thanks.", "CB-2026-0142", "") == "Noted, thanks."))
    with patch.object(ref, "REFERENCE_AS_SENTENCE", False):
        results.append(("reference 2e: switch off -> the bracket, as before",
                        ref.add_reference(held, "CB-2026-0142", "")
                        == "A live agent is handling this and will connect with you shortly "
                           "(reference CB-2026-0142)."))
    return results


_UPDATE_REQUESTS = (
    "ok can you tell me any updates on this", "any updates", "any updates?", "any news",
    "any news?", "can you tell me any updates", "whats the status", "what is the status of this",
    "update me", "please update me", "can you update me on this", "is there any update",
    "what is the latest", "keep me updated", "tell me the status", "let me know the progress",
    "how is it going", "have you heard anything", "what is happening with my case",
    "status update please", "anything new?",
)
_REAL_NOTES = (
    "she prefers window seat", "please note she prefers a window seat",
    "also she prefers to fly on a weekend", "her flight is on 12 november",
    "i heard back from her she is fine", "her status is single",
)


def _chase_checks() -> list[tuple[str, bool]]:
    """Step 2d: an update request is a chase, never a detail to note (3766)."""
    import importlib
    ic = importlib.import_module("app.graph.nodes.intent_classifier")
    btr = importlib.import_module("app.graph.nodes.blocked_topic_responder")
    asked = "You: Do you mean her rest days?"
    out = []
    for m in _UPDATE_REQUESTS:
        out.append((f"update request: {m[:34]}",
                    ic.asks_for_an_update(m) and not ic.states_a_detail(m)
                    and not btr.adds_information(m)
                    and not btr.answers_our_question({"history_text": asked, "incoming_text": m})))
    for m in _REAL_NOTES:
        out.append((f"real note: {m[:34]}",
                    not ic.asks_for_an_update(m) and ic.states_a_detail(m)
                    and btr.adds_information(m)))
    with patch.object(ic, "CHASE_BEFORE_NOTE", False):
        out.append(("switch off: the plural is a detail again, as before",
                    ic.states_a_detail("ok can you tell me any updates on this")
                    and not ic.asks_for_an_update("any updates")
                    and ic.asks_for_an_update("any update?")))
    return out


# Step 3 (2026-10-13): the non-English harm net. The three messages the step
# names come first in each list; the meaning is beside each one.
_HARM_NON_ENGLISH = (
    ("Majikan saya memukul saya", "Bahasa: my employer hit me"),
    ("Sinaktan ako ng amo ko", "Tagalog: my employer hurt me"),
    ("ကျွန်မရဲ့ အလုပ်ရှင်က "
     "ကျွန်မကို ရိုက်တယ်",
     "Burmese: my employer hits me"),
)
_SAFE_NON_ENGLISH = (
    ("Majikan saya belum bayar gaji saya bulan ini", "Bahasa: salary not paid this month"),
    ("Ilang araw ng pahinga ang meron ako bawat buwan?", "Tagalog: how many rest days a month?"),
    ("Selamat pagi, apa kabar?", "Bahasa: good morning, how are you?"),
)
_ENGLISH = (
    "my employer hit me", "any update?", "ok", "renewal fee", "she prefers window seat",
    "Hi, I want to renew my helper's work permit", "Dan will call you tomorrow",
)


async def _harm_checks() -> list[tuple[str, bool]]:
    """Run the classifier and the out-of-hours override with the model and the
    harm check stubbed: who is checked, and what a confirmed check does."""
    import importlib
    from contextlib import nullcontext
    ic = importlib.import_module("app.graph.nodes.intent_classifier")
    lang = importlib.import_module("app.graph.language")
    _wh = importlib.import_module("app.api.webhook")
    out = []

    for m, gloss in _HARM_NON_ENGLISH + _SAFE_NON_ENGLISH:
        out.append((f"not mainly English: {gloss}", lang.mainly_non_english(m)))
    for m in _ENGLISH:
        out.append((f"English: {m[:30]}", not lang.mainly_non_english(m)))

    async def classify(message, verdict, harm, off=False):
        """The node's verdict and how often (and when) the harm check ran."""
        seen = {"harm_calls": 0, "harm_started_before_classify_returned": False}
        started = {"harm": False}

        async def _harm(_msg):
            started["harm"] = True
            seen["harm_calls"] += 1
            return harm

        async def _model(*_a, **_k):
            await asyncio.sleep(0)   # the real call yields while it waits
            seen["harm_started_before_classify_returned"] = started["harm"]
            return dict(verdict)

        state = {"incoming_text": message, "history_text": "Client: hi\nYou: Hi, how can I help?",
                 "conversation_id": "s3"}
        # Only the switch-off case patches the switch: the others read the
        # module's own value, so turning it off in the code turns these red.
        with patch.object(ic, "confirm_harm", new=_harm), \
             patch.object(ic, "complete_json", new=_model), \
             (patch.object(ic, "NON_ENGLISH_HARM_CHECK", False) if off else nullcontext()):
            result = await ic.intent_classifier(state)
        return result, seen

    missed = {"intent": "dispute_salary", "service_type": "dispute_salary",
              "contact_type": "candidate", "confidence": 0.8}
    for m, gloss in _HARM_NON_ENGLISH:
        r, seen = await classify(m, missed, harm=True)
        out.append((f"escalates although the model missed it: {gloss}",
                    r.get("intent") == "dispute_assault" and r.get("service_type") == "dispute_assault"
                    and seen["harm_calls"] == 1))
    r, seen = await classify(_HARM_NON_ENGLISH[0][0], missed, harm=True)
    out.append(("the harm check runs beside the classifier, not after it",
                seen["harm_started_before_classify_returned"]))
    for m, gloss in _SAFE_NON_ENGLISH:
        r, seen = await classify(m, missed, harm=False)
        out.append((f"not escalated when the check says no: {gloss}",
                    r.get("intent") == "dispute_salary" and seen["harm_calls"] == 1))
    said_assault = {"intent": "dispute_assault", "service_type": "dispute_assault",
                    "contact_type": "candidate", "confidence": 0.9}
    r, seen = await classify(_SAFE_NON_ENGLISH[0][0], said_assault, harm=False)
    out.append(("a model 'assault' the check rejected is not checked twice",
                r.get("intent") == "other" and seen["harm_calls"] == 1))
    r, seen = await classify("my employer has not paid my salary", missed, harm=True)
    out.append(("an English message costs no extra harm check",
                r.get("intent") == "dispute_salary" and seen["harm_calls"] == 0))
    r, seen = await classify(_HARM_NON_ENGLISH[0][0], missed, harm=True, off=True)
    out.append(("switch off: keywords only, as before",
                r.get("intent") == "dispute_salary" and seen["harm_calls"] == 0))

    # The out-of-hours override (a thread an agent holds) checks it too.
    async def override(message, off=False):
        harm = AsyncMock(return_value=False)
        with patch.object(_wh, "confirm_harm", new=harm), \
             (patch.object(ic, "NON_ENGLISH_HARM_CHECK", False) if off else nullcontext()):
            await _wh.emergency_override({"id": 1}, message)
        return harm.await_count

    for m, gloss in _HARM_NON_ENGLISH:
        out.append((f"override checks a non-English report: {gloss}", await override(m) == 1))
    out.append(("override: plain English with no keyword is not checked",
                await override("any update on my helper?") == 0))
    out.append(("override, switch off: keywords only, as before",
                await override(_HARM_NON_ENGLISH[0][0], off=True) == 0))
    return out


async def _tag_checks() -> list[tuple[str, bool]]:
    """Step 2c: "(MDW)" never in front of a possessive, through run_turn."""
    from app.graph import graph as graph_module
    from app.graph.guards import without_tag_before_possessive as tidy

    results = [
        ("tag: 'Devi lal singh (MDW)'s home leave' -> 'Devi lal singh's home leave'",
         tidy("Here is everything for Devi lal singh (MDW)'s home leave:")
         == "Here is everything for Devi lal singh's home leave:"),
        ("tag: ...with a curly apostrophe too",
         tidy("for Devi (MDW)’s passport") == "for Devi’s passport"),
        ("tag: a plain first mention keeps it",
         tidy("When would Usriyah (MDW) be available?")
         == "When would Usriyah (MDW) be available?"),
    ]

    # Step 2c, item 1: the collector's briefing search asks for the fee row.
    import importlib
    retriever = importlib.import_module("app.graph.nodes.rag_retriever")
    calls = []

    async def _search(query, **kw):
        calls.append((query, kw.get("match_count")))
        return []

    with patch.object(retriever.rag, "search", new=_search):
        await retriever.briefing_records({
            "service_type": "renewal", "intent": "renewal", "contact_type": "employer",
            "incoming_text": "myself VD, helper Devi, permit expires in 3 weeks",
            "collected_info": {"full_name": "VD", "helper_name": "Devi",
                               "permit_expiry": "in 3 weeks"},
        })
    results.append(("briefing: the collector's search is the briefing set (10 rows)",
                     any(n == retriever.BRIEFING_MATCH_COUNT for _, n in calls)))
    results.append(("briefing: ...plus the renewal's fee row",
                     any("cost of work permit renewal" in q for q, _ in calls)))

    class _Graph:
        async def ainvoke(self, *_a, **_kw):
            return {"reply": "Here is everything for Devi (MDW)'s home leave:"}

    with patch.object(graph_module, "get_graph", new=lambda: _Graph()), \
         patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()):
        out = await graph_module.run_turn("t", {})
        results.append(("tag: run_turn sends the reply without it",
                        out["reply"] == "Here is everything for Devi's home leave:"))
        with patch.object(graph_module, "NO_TAG_BEFORE_POSSESSIVE", False):
            out = await graph_module.run_turn("t", {})
        results.append(("tag: NO_TAG_BEFORE_POSSESSIVE off -> the reply as written",
                        "(MDW)'s" in out["reply"]))
    return results


async def _stray_script_checks() -> list[tuple[str, bool]]:
    """Step 5 item 15: a stray-script glitch never leaves run_turn."""
    from app.graph import graph as graph_module
    from app.graph import guards as guards_module

    glitch = "天天彩票中奖"
    written = ("Permintaan Anda sedang ditangani agen kami. Nomor referensi Anda "
               "adalah CB-2026-0033." + glitch)

    class _Graph:
        def __init__(self, reply):
            self.reply = reply

        async def ainvoke(self, *_a, **_kw):
            return {"reply": self.reply}

    payload = {"incoming_text": "ada update?",
               "history_text": "Client: saya mau perpanjang izin kerja\nYou: Baik.",
               "customer_name": "", "record_name": ""}
    out = []
    with patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()):
        with patch.object(graph_module, "get_graph", new=lambda: _Graph(written)):
            got = (await graph_module.run_turn("t", payload))["reply"]
            out.append(("stray script: run_turn cuts the Step 2e glitch",
                        glitch not in got and got.endswith("CB-2026-0033.")))
            with patch.object(guards_module, "STRAY_SCRIPT_GUARD", False):
                got = (await graph_module.run_turn("t", payload))["reply"]
            out.append(("stray script: STRAY_SCRIPT_GUARD off -> as written",
                        got == written))
        # A name the client wrote earlier, in Chinese, survives in an English reply.
        named = "Hi 王小明, your reference number is CB-2026-0033."
        with patch.object(graph_module, "get_graph", new=lambda: _Graph(named)):
            got = (await graph_module.run_turn("t", {
                "incoming_text": "any update?",
                "history_text": "Client: my name is 王小明\nYou: Thanks."}))["reply"]
            out.append(("stray script: the client's own Chinese name stays", got == named))
        # A Burmese reply keeps Burmese, Claire and the reference; loses the CJK.
        burmese = "မင်္ဂလာပါ Claire CB-2026-0033 " + glitch
        with patch.object(graph_module, "get_graph", new=lambda: _Graph(burmese)):
            got = (await graph_module.run_turn("t", {"incoming_text": "ဆရာ"}))["reply"]
            out.append(("stray script: a Burmese reply keeps Burmese, Claire, the reference",
                        got == burmese.replace(" " + glitch, "")))
    return out


async def _batch_b_checks() -> list[tuple[str, bool]]:
    """Step 5 items 9 and 17, through the real run_turn."""
    from app.graph import graph as graph_module
    from app.graph import guards as guards_module

    class _Graph:
        def __init__(self, reply):
            self.reply = reply

        async def ainvoke(self, *_a, **_kw):
            return {"reply": self.reply}

    first = "As this is your first helper, she must attend the Settling-In Programme."
    approx = "The direct hire fee is approximate and our agent will confirm it."
    out = []
    with patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()):
        async def turn(reply, payload=None):
            with patch.object(graph_module, "get_graph", new=lambda: _Graph(reply)):
                return (await graph_module.run_turn("t", payload or {}))["reply"]

        out.append(("first helper: said as a condition",
                    await turn(first, {"incoming_text": "what happens next?"})
                    == "If this is your first helper, she must attend the Settling-In Programme."))
        out.append(("first helper: kept where the client said so",
                    await turn(first, {"incoming_text": "ok",
                                       "history_text": "Client: this is my first helper\nYou: Noted."})
                    == first))
        with patch.object(guards_module, "FIRST_HELPER_CONDITIONAL", False):
            out.append(("first helper: FIRST_HELPER_CONDITIONAL off -> as written",
                        await turn(first) == first))
        out.append(("approximate: a figure-free cost is our agent confirming it",
                    await turn(approx) == guards_module.NATIONALITY_FEE_DEFERRAL))
        out.append(("approximate: a timing 'approximately' stays",
                    await turn("It takes approximately 4 to 6 weeks.")
                    == "It takes approximately 4 to 6 weeks."))
        with patch.object(guards_module, "NO_APPROXIMATE_COST_ANYWHERE", False):
            out.append(("approximate: NO_APPROXIMATE_COST_ANYWHERE off -> as written",
                        await turn(approx) == approx))
    return out


async def _batch_b2_checks() -> list[tuple[str, bool]]:
    """Step 5 Batch B2 (2026-10-14)."""
    from app.services import ticket as ticket_module

    out = []

    # Item 1: the reference comes from the database counter, never from the
    # highest ticket still on the table (which reset-ui deletes).
    class _Rows:
        def __init__(self, rows):
            self.data = rows

    class _Query:
        def __getattr__(self, _name):
            return lambda *a, **kw: self

    read_max = AsyncMock(return_value=_Rows([{"ticket_number": "CB-2026-0032"}]))
    with patch.object(ticket_module.db, "table", new=lambda *_: _Query()), \
            patch.object(ticket_module.db, "execute", new=read_max):
        with patch.object(ticket_module.db, "rpc", new=AsyncMock(return_value="CB-2026-0100")):
            number = await ticket_module.next_ticket_number()
            out.append(("ticket number: from the counter, never max+1",
                        number == "CB-2026-0100" and not read_max.await_count))
        with patch.object(ticket_module.db, "rpc",
                          new=AsyncMock(side_effect=RuntimeError("function missing"))):
            out.append(("ticket number: counter missing -> old path, logged",
                        (await ticket_module.next_ticket_number()).endswith("-0033")))
        with patch.object(ticket_module.db, "rpc", new=AsyncMock(return_value=None)):
            out.append(("ticket number: preview (rpc refused) -> old path",
                        (await ticket_module.next_ticket_number()).endswith("-0033")))
        with patch.object(ticket_module.db, "rpc", new=AsyncMock(return_value="junk")):
            out.append(("ticket number: a malformed counter answer is not used",
                        (await ticket_module.next_ticket_number()).endswith("-0033")))
        rpc = AsyncMock(return_value="CB-2026-0100")
        with patch.object(ticket_module.db, "rpc", new=rpc), \
                patch.object(ticket_module, "TICKET_NUMBER_COUNTER", False):
            out.append(("ticket number: TICKET_NUMBER_COUNTER off -> old path, no rpc",
                        (await ticket_module.next_ticket_number()).endswith("-0033")
                        and not rpc.await_count))

    # Item 2: through the real run_turn, a fee the records state exactly.
    from app.graph import graph as graph_module
    from app.graph import guards as guards_module

    class _Graph:
        def __init__(self, result):
            self.result = result

        async def ainvoke(self, *_a, **_kw):
            return dict(self.result)

    renewal_row = {"answer": "Work permit renewal is $695. That covers us handling the whole "
                             "thing for you."}
    passport_row = {"answer": "It is approximately $450 for a Filipino helper and "
                              "approximately $450 for an Indonesian helper."}

    async def turn(reply, rows, payload=None):
        with patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()), \
                patch.object(graph_module, "get_graph",
                             new=lambda: _Graph({"reply": reply, "rag_matches": rows})):
            return (await graph_module.run_turn("t", payload or {}))["reply"]

    live = "The approximate work permit renewal fee is $695."
    out.append(("fixed fee: 'The approximate fee is $695' -> stated plainly",
                await turn(live, [renewal_row]) == "The work permit renewal fee is $695."))
    out.append(("fixed fee: '...the exact amount will be confirmed' cut",
                await turn("The fee is $695, and the exact amount will be confirmed by our "
                           "team.", [renewal_row]) == "The fee is $695."))
    out.append(("fixed fee: a fee the KB calls approximate keeps the word",
                await turn("The cost is approximately $450, and our agent will confirm the "
                           "exact fee.", [passport_row])
                == "The cost is approximately $450, and our agent will confirm the exact fee."))
    out.append(("fixed fee: no records, nothing changed",
                await turn(live, []) == live))
    with patch.object(guards_module, "FIXED_FEE_STATED_PLAINLY", False):
        out.append(("fixed fee: FIXED_FEE_STATED_PLAINLY off -> as written",
                    await turn(live, [renewal_row]) == live))

    # Item 4: a helper name in capitals is read back in title case.
    async def named(reply, collected, payload=None):
        with patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()), \
                patch.object(graph_module, "get_graph",
                             new=lambda: _Graph({"reply": reply, "collected_info": collected})):
            return (await graph_module.run_turn("t", payload or {}))["reply"]

    caps = "Here is everything for DEVI's home leave. When is DEVI travelling?"
    out.append(("name case: 'DEVI's' -> 'Devi's'",
                await named(caps, {"helper_name": "DEVI"})
                == "Here is everything for Devi's home leave. When is Devi travelling?"))
    out.append(("name case: a known helper on file is tidied too",
                await named("Is this for YOYO?", {},
                            {"known_helpers": [{"helper_name": "yoyo"}]}) == "Is this for Yoyo?"))
    out.append(("name case: a mixed-case name is left as typed",
                await named("Is this for McDonald?", {"helper_name": "McDonald"})
                == "Is this for McDonald?"))
    with patch.object(guards_module, "TIDY_HELPER_NAME_CASE", False):
        out.append(("name case: TIDY_HELPER_NAME_CASE off -> as written",
                    await named(caps, {"helper_name": "DEVI"}) == caps))

    # Item 5: a family matter or an emergency gets one caring line.
    async def leave(reply, message, reason):
        result = {"reply": reply, "service_type": "home_leave",
                  "collected_info": {"leave_reason": reason}}
        with patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()), \
                patch.object(graph_module, "get_graph", new=lambda: _Graph(result)):
            return (await graph_module.run_turn("t", {"incoming_text": message}))["reply"]

    ask = "Which country is Devi from?"
    out.append(("caring line: a family emergency gets one",
                await leave(ask, "her mother is in hospital", "family emergency - mother in hospital")
                == "I hope everything is alright at home. " + ask))
    out.append(("caring line: a regular home leave does not",
                await leave(ask, "her regular home leave", "regular home leave") == ask))
    out.append(("caring line: not twice when the reply already cares",
                await leave("I'm sorry to hear that. " + ask, "her father is sick", "father sick")
                == "I'm sorry to hear that. " + ask))
    out.append(("caring line: only on the turn the reason is given",
                await leave(ask, "Philippines", "family emergency") == ask))
    with patch.object(guards_module, "CARING_LINE_FOR_FAMILY_MATTERS", False):
        out.append(("caring line: CARING_LINE_FOR_FAMILY_MATTERS off -> as written",
                    await leave(ask, "her mother is in hospital", "family emergency") == ask))

    # Item 6: no introduction once Claire has spoken on the thread.
    intro = "Hi, I'm Claire, Ming Hwee's AI assistant. Our agent will confirm the replacement cost."
    spoken = ("Client: How much does a direct hire cost?\nYou: Hi, I'm Claire, Ming Hwee's AI "
              "assistant. A direct hire costs less than a full recruitment.")

    async def intro_turn(reply, history, message="How much is a replacement?"):
        with patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()), \
                patch.object(graph_module, "get_graph", new=lambda: _Graph({"reply": reply})):
            return (await graph_module.run_turn(
                "t", {"history_text": history, "incoming_text": message}))["reply"]

    out.append(("no re-intro: taken out once Claire has spoken",
                await intro_turn(intro, spoken) == "Our agent will confirm the replacement cost."))
    out.append(("no re-intro: the first reply keeps it",
                await intro_turn(intro, "") == intro))
    out.append(("no re-intro: kept after only our team's lines",
                await intro_turn(intro, "Agent: Good morning, please send the form") == intro))
    out.append(("no re-intro: 'are you a bot?' keeps the answer",
                await intro_turn(intro, spoken, "are you a bot?") == intro))
    with patch.object(guards_module, "NO_REINTRODUCTION", False):
        out.append(("no re-intro: NO_REINTRODUCTION off -> as written",
                    await intro_turn(intro, spoken) == intro))

    # Batch C item 12: the $40 urgent fee only when they asked for urgent.
    closing = ("It costs $400 and takes about 4 weeks. An urgent request costs an additional "
               "$40.\n\nA live agent will connect with you shortly.")

    async def home_leave_turn(reply, message, history="", service="home_leave"):
        result = {"reply": reply, "service_type": service}
        with patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()), \
                patch.object(graph_module, "get_graph", new=lambda: _Graph(result)):
            return (await graph_module.run_turn(
                "t", {"incoming_text": message, "history_text": history}))["reply"]

    plain = "It costs $400 and takes about 4 weeks.\n\nA live agent will connect with you shortly."
    out.append(("conditional fee: $40 urgent dropped when nobody asked",
                await home_leave_turn(closing, "regular home leave") == plain))
    out.append(("conditional fee: kept when they said it is urgent",
                await home_leave_turn(closing, "Philippines",
                                      "Client: it is urgent, her mother is in hospital")
                == closing))
    out.append(("conditional fee: another service is left alone",
                await home_leave_turn(closing, "x", service="renewal") == closing))
    with patch.object(guards_module, "CONDITIONAL_FEE_ONLY_IF_ASKED", False):
        out.append(("conditional fee: CONDITIONAL_FEE_ONLY_IF_ASKED off -> as written",
                    await home_leave_turn(closing, "regular home leave") == closing))
    return out


async def _timing_checks() -> list[tuple[str, bool]]:
    """The per-turn timing line (app/timing.py), through the real webhook turn.

    Runs webhook._process_locked with the database, the graph and Whapi
    stubbed, and reads the TURN_TIMING line it logs. What has to hold: one line
    per turn that reached the graph, every phase and step labelled, no message
    text and no phone number in it, nothing at all when TURN_TIMING_LOG is
    off, and a failure inside the timing code never costs the client a reply.
    """
    import json as _json
    import logging as _logging
    import types
    from datetime import datetime, timezone

    from app import timing as _timing
    from app.api import webhook as _wh
    from app.config import settings as _settings
    from app.db.supabase import db as _db
    from app.graph import llm as _llm
    from app.whapi.parser import IncomingMessage

    phone, text = "+6590000123", "my secret words about Maria"
    lines: list[str] = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if msg.startswith(_timing.LOG_PREFIX):
                lines.append(msg)

    class _FakeChat:
        async def ainvoke(self, messages):
            return types.SimpleNamespace(content="ok")

    class _FakeQuery:
        http_method = "GET"
        path = "/rest/v1/cb_tickets"

        def execute(self):
            return types.SimpleNamespace(data=[], count=None)

    # An LLM call made from a function that looks, to the labeller, exactly
    # like the classifier node.
    node_globals = {"__name__": "app.graph.nodes.intent_classifier", "_llm": _llm}
    exec(
        "async def intent_classifier():\n"
        "    return await _llm.complete('system', 'user')\n",
        node_globals,
    )
    classify = node_globals["intent_classifier"]

    async def graph(thread_id, payload):
        await classify()
        await _db.execute(_FakeQuery())
        return {"reply": "Hello, how can I help?"}

    conversation = {"id": 4242, "customer_number": phone, "bot_status": "bot_active",
                    "langgraph_thread_id": "t-4242", "contact_type": "unknown"}
    message = IncomingMessage(
        whapi_message_id="timing-1", chat_id="", customer_number=phone, customer_name="",
        body=text, message_type="text", from_me=False,
        timestamp=datetime.now(timezone.utc),
    )
    sent: list[str] = []
    stops: list[bool] = []

    async def record_stop(phone, *, clear):
        stops.append(clear)

    # The REAL send_bot_reply runs; only Whapi and its own writes are stubbed,
    # so a send that is not timed inside message.py shows up here.
    async def send_text(to, body, **kwargs):
        sent.append(body)
        return {"message": {"id": "out-1"}}

    async def receive():
        """The real handle_inbound, up to the debouncer, so the receipt time is
        recorded the way production records it."""
        none = AsyncMock(return_value=None)
        with patch.object(_wh.conversation_service, "get_by_phone",
                          new=AsyncMock(return_value=None)), \
             patch.object(_wh, "may_engage", new=AsyncMock(return_value=(True, "ok"))), \
             patch.object(_wh.conversation_service, "get_or_create",
                          new=AsyncMock(return_value=(dict(conversation), False))), \
             patch.object(_wh.message_service, "store_incoming", new=none), \
             patch.object(_wh.conversation_service, "touch_inbound", new=none), \
             patch.object(_wh, "_schedule_read_receipt", new=lambda *a: None), \
             patch.object(_wh, "_should_identify", new=lambda *a: False), \
             patch.object(_wh, "_already_processed", new=lambda *a: False), \
             patch.object(_wh.typing_indicator, "start", new=none), \
             patch.object(_wh.debouncer, "add", new=none):
            # typing_indicator.start is stubbed: unstubbed it read production
            # and sent a REAL Whapi presence to this test's number on every
            # run (found 2026-10-12 via a flaky check it disturbed).
            _timing._received.pop(message.whapi_message_id, None)
            await _wh.handle_inbound(message)

    def stubs(run_turn=graph):
        none = AsyncMock(return_value=None)
        empty = AsyncMock(return_value=[])
        return [
            patch.object(_wh.conversation_service, "get_by_phone",
                         new=AsyncMock(return_value=dict(conversation))),
            patch.object(_wh.message_service, "get_history", new=empty),
            patch.object(_wh.ticket_service, "recent_for_conversation", new=empty),
            patch.object(_wh.lead_service, "find_by_phone", new=none),
            patch.object(_wh.contact_service, "count_prior_hires",
                         new=AsyncMock(return_value=0)),
            patch.object(_wh.contact_service, "get_placed_helper", new=none),
            patch.object(_wh.contact_service, "get_placement_helpers", new=empty),
            patch.object(_wh.ticket_service, "helpers_on_conversation", new=empty),
            patch.object(_wh.contact_service, "get_record_name", new=none),
            patch.object(_wh.contact_service, "get_cases", new=empty),
            patch.object(_wh.ticket_service, "open_topics_for_conversation", new=empty),
            patch.object(_wh.ticket_service, "completed_topics_for_conversation",
                         new=empty),
            patch.object(_wh, "run_turn", new=run_turn),
            patch.object(_wh.debouncer, "has_pending", new=AsyncMock(return_value=False)),
            patch.object(_wh.message_service.whapi, "send_text", new=send_text),
            patch.object(_wh.message_service, "_insert_ignoring_duplicates", new=none),
            patch.object(_wh.message_service.conversation_service, "touch_outbound",
                         new=none),
            patch.object(_wh.conversation_service, "update", new=none),
            patch.object(_llm, "get_llm", new=lambda *a, **k: _FakeChat()),
            patch.object(_wh.typing_indicator, "stop", new=record_stop),
        ]

    async def one_turn(run_turn=graph):
        lines.clear()
        sent.clear()
        handler = _Capture()
        timing_logger = _logging.getLogger("app.timing")
        level = timing_logger.level
        timing_logger.setLevel(_logging.INFO)
        timing_logger.addHandler(handler)
        try:
            from contextlib import ExitStack

            with ExitStack() as stack:
                for p in stubs(run_turn):
                    stack.enter_context(p)
                await receive()
                # The same wrapper process_batch puts around each pass.
                with _timing.turn(None):
                    await _wh._process_locked(phone, [message], allow_hold=False)
        finally:
            timing_logger.removeHandler(handler)
            timing_logger.setLevel(level)
        return lines[:], sent[:]

    results = []
    stops.clear()
    got, out = await one_turn()
    stopped_after_send = list(stops)
    line = got[0] if got else ""
    data = _json.loads(line[len(_timing.LOG_PREFIX):]) if line else {}
    steps = data.get("steps", {})
    results.append(("typing: the webhook stops it right after the reply is sent",
                    stopped_after_send == [False]))
    results.append(("timing: one line for a turn that reached the graph",
                    len(got) == 1 and bool(out)))
    results.append(("timing: setup, graph and send phases all timed",
                    set(data.get("phases", {})) >= {"setup", "graph", "send"}))
    results.append(("timing: the LLM call is labelled by node and purpose",
                    "llm.intent_classifier.classify" in steps))
    results.append(("timing: the database call is labelled by table",
                    "db.GET.cb_tickets" in steps))
    results.append(("timing: the WhatsApp send is timed",
                    "whapi.send" in steps))
    results.append(("timing: the client's wait is measured (debounce + whole)",
                    "debounce_ms" in data and "client_to_reply_ms" in data))
    results.append(("timing: no message text and no phone number in the line",
                    bool(line) and "secret" not in line.lower() and "Maria" not in line
                    and "90000123" not in line and data.get("conversation_id") == 4242))

    with patch.object(_settings, "turn_timing_log", False):
        got, out = await one_turn()
    results.append(("timing: TURN_TIMING_LOG off -> no line, reply still sent",
                    not got and bool(out)))

    # Contained means the turn neither loses its reply NOR raises: an escape
    # here would end process_batch's loop and skip the client's next pass.
    escaped = False
    with patch.object(_timing, "_emit", side_effect=RuntimeError("boom")):
        try:
            got, out = await one_turn()
        except Exception:  # noqa: BLE001
            escaped, out = True, sent[:]
    results.append(("timing: a failure in the timing code is contained",
                    bool(out) and not escaped))

    async def no_reply(thread_id, payload):
        return {"reply": ""}

    got, out = await one_turn(no_reply)
    results.append(("timing: a turn with nothing sent still logs, without 'sent'",
                    len(got) == 1 and '"sent"' not in got[0]))
    return results


async def _step1b_checks() -> list[tuple[str, bool]]:
    """The five 2026-10-12 latency changes, each through its real code path.

    Typing indicator (app/whapi/typing.py), smart debounce (webhook.
    debounce_delay + MessageDebouncer), the shorter pause at send
    (message.send_bot_reply), the embedding cache (rag.embed_query) and the
    per-call LLM timeout (llm.complete). Network and database stubbed.
    """
    import types
    from datetime import datetime, timezone

    from app.api import webhook as _wh
    from app.config import settings as _settings
    from app.graph import llm as _llm
    import importlib
    _rr = importlib.import_module("app.graph.nodes.rag_retriever")
    from app.services import message as _msg
    from app.services import rag as _rag
    from app.whapi import typing as _typing
    from app.whapi.debouncer import MessageDebouncer
    from app.whapi.parser import IncomingMessage

    results: list[tuple[str, bool]] = []

    async def send_text_1c(to, body, typing_time=0):
        return {"message": {"id": "out-1c"}}

    def msg(body, mid="m1", kind="text", transcript=None):
        m = IncomingMessage(
            whapi_message_id=mid, chat_id="", customer_number="+6590000124",
            customer_name="", body=body, message_type=kind, from_me=False,
            timestamp=datetime.now(timezone.utc),
        )
        m.transcript = transcript
        return m

    # --- 1. typing indicator -------------------------------------------------
    presences: list[str] = []

    async def fake_presence(to, presence):
        presences.append(presence)
        return True

    async def typing_case(*, history=(), open_topics=(), text="I need a helper",
                          wait=0.05, setting=True):
        presences.clear()
        _typing._keepers.clear()
        _typing._stopped_at.clear()
        with patch.object(_typing.whapi, "send_presence", new=fake_presence), \
             patch.object(_settings, "typing_indicator", setting), \
             patch.object(_settings, "typing_refresh_seconds", 0.02), \
             patch("app.services.ticket.open_topics_for_conversation",
                   new=AsyncMock(return_value=dict.fromkeys(open_topics, 1))), \
             patch("app.services.message.get_history",
                   new=AsyncMock(return_value=list(history))):
            await _typing.start("+6590000124", 77, text, "m1")
            await asyncio.sleep(wait)
            running = _typing.active("+6590000124")
            shown = list(presences)
            await _typing.stop("+6590000124", clear=True)
        return running, shown, list(presences)

    running, shown, after = await typing_case(wait=0.07)
    results.append(("typing: shown at once for a message that will be answered",
                    running and shown[:1] == ["typing"]))
    results.append(("typing: refreshed while the turn runs",
                    shown.count("typing") >= 2))
    results.append(("typing: a turn that sent nothing clears it ('pause')",
                    after[-1:] == ["pause"] and not _typing.active("+6590000124")))

    statement = [{"direction": "outbound", "is_bot": True,
                  "body": "Your helper's permit is renewed.", "created_at": "x"}]
    _, shown, _ = await typing_case(history=statement, text="ok thanks")
    results.append(("typing: not shown when the bot will stay silent", not shown))
    _, shown, _ = await typing_case(open_topics=("renewal",))
    results.append(("typing: not shown on a parked chat", not shown))
    _, shown, _ = await typing_case(setting=False)
    results.append(("typing: TYPING_INDICATOR off -> never shown", not shown))

    # Stopped after a reply: no 'pause' (the reply clears it), no keep-alive.
    presences.clear()
    with patch.object(_typing.whapi, "send_presence", new=fake_presence), \
         patch.object(_settings, "typing_refresh_seconds", 0.02):
        _typing._keepers["+6590000124"] = asyncio.create_task(
            _typing._keep_alive("+6590000124"))
        # Wait for the first "typing" rather than a fixed 10 ms: under load the
        # task can start late (flaked 1 run in 6 with a fixed sleep).
        for _ in range(100):
            if presences:
                break
            await asyncio.sleep(0.01)
        await _typing.stop("+6590000124", clear=False)
        at_stop = list(presences)
        await asyncio.sleep(0.1)   # five refresh intervals
    results.append(("typing: a sent reply stops it without a 'pause'",
                    at_stop[:1] == ["typing"] and presences == at_stop
                    and not _typing.active("+6590000124")))

    # A turn that finished while start() was still deciding never shows it.
    async def slow_topics(*a, **k):
        await _typing.stop("+6590000124", clear=False)   # the reply goes out
        return {}

    presences.clear()
    _typing._keepers.clear()
    with patch.object(_typing.whapi, "send_presence", new=fake_presence), \
         patch("app.services.ticket.open_topics_for_conversation", new=slow_topics), \
         patch("app.services.message.get_history", new=AsyncMock(return_value=[])):
        await _typing.start("+6590000124", 77, "I need a helper", "m1")
    results.append(("typing: never started after the reply already went out",
                    not presences and not _typing.active("+6590000124")))

    # The webhook starts it only past the gates: a paused (agent) chat never.
    started: list[str] = []

    async def record_start(phone, *a, **k):
        started.append(phone)

    conversation = {"id": 77, "customer_number": "+6590000124",
                    "bot_status": "human_active", "langgraph_thread_id": "t"}
    with patch.object(_wh.conversation_service, "get_by_phone",
                      new=AsyncMock(return_value=dict(conversation))), \
         patch.object(_wh, "may_engage", new=AsyncMock(return_value=(True, "ok"))), \
         patch.object(_wh, "maybe_return_to_bot",
                      new=AsyncMock(return_value=dict(conversation))), \
         patch.object(_wh, "_is_paused_for_agent", new=lambda c: True), \
         patch.object(_wh, "emergency_override", new=AsyncMock(return_value=False)), \
         patch.object(_wh, "acknowledge_direct_address", new=AsyncMock()), \
         patch.object(_wh, "_already_processed", new=lambda *a: False), \
         patch.object(_settings, "typing_indicator", True), \
         patch.object(_wh.typing_indicator, "start", new=record_start), \
         patch.object(_wh.debouncer, "add", new=AsyncMock()):
        await _wh.handle_inbound(msg("I need a helper please", mid="agent-1"))
        await asyncio.sleep(0)
    results.append(("typing: never started where a human agent is active",
                    not started))

    conversation["bot_status"] = "bot_active"
    with patch.object(_wh.conversation_service, "get_by_phone",
                      new=AsyncMock(return_value=None)), \
         patch.object(_wh, "may_engage", new=AsyncMock(return_value=(True, "ok"))), \
         patch.object(_wh.conversation_service, "get_or_create",
                      new=AsyncMock(return_value=(dict(conversation), False))), \
         patch.object(_wh.message_service, "store_incoming", new=AsyncMock()), \
         patch.object(_wh.conversation_service, "touch_inbound", new=AsyncMock()), \
         patch.object(_wh, "_schedule_read_receipt", new=lambda *a: None), \
         patch.object(_wh, "_should_identify", new=lambda *a: False), \
         patch.object(_wh, "_already_processed", new=lambda *a: False), \
         patch.object(_settings, "typing_indicator", True), \
         patch.object(_wh.typing_indicator, "start", new=record_start), \
         patch.object(_wh.debouncer, "add", new=AsyncMock()):
        await _wh.handle_inbound(msg("I need a helper please", mid="gate-1"))
        await asyncio.sleep(0)
    results.append(("typing: started by the webhook once the gates pass",
                    started == ["+6590000124"]))

    stops: list[bool] = []

    async def record_stop(phone, *, clear):
        stops.append(clear)

    async def turn_raises(*a, **k):
        raise RuntimeError("the turn broke")

    for body in (AsyncMock(), turn_raises):
        with patch.object(_wh, "_process_locked", new=body),              patch.object(_wh.debouncer, "drain", new=AsyncMock(return_value=[])),              patch.object(_wh.typing_indicator, "stop", new=record_stop):
            try:
                await _wh.process_batch("+6590000125", [msg("hi", "pb1")])
            except RuntimeError:
                pass
    results.append(("typing: cleared when a batch ends, even when the turn raises",
                    stops == [True, True]))

    # --- 2. smart debounce ---------------------------------------------------
    with patch.object(_settings, "smart_debounce", True), \
         patch.object(_settings, "debounce_seconds", 6.0), \
         patch.object(_settings, "debounce_short_seconds", 2.5):
        # "how much?" is the short question: two words, complete by its "?" alone.
        complete = [msg("how much?"), msg("how much is a passport renewal?"),
                    msg("I want to renew my helper permit"),
                    msg("", kind="voice", transcript="I need a helper for my mum")]
        unfinished = [msg("hi"), msg("hello"), msg("I need"), msg("for my"),
                      msg("", kind="image")]
        results.append(("debounce: a complete message gets the short wait",
                        all(_wh.debounce_delay(m) == 2.5 for m in complete)))
        results.append(("debounce: a greeting, fragment or bare media waits in full",
                        all(_wh.debounce_delay(m) == 6.0 for m in unfinished)))
    with patch.object(_settings, "smart_debounce", False), \
         patch.object(_settings, "debounce_seconds", 6.0):
        results.append(("debounce: SMART_DEBOUNCE off -> always the full wait",
                        _wh.debounce_delay(complete[0]) == 6.0))

    # Step 1c: a short answer to the question we just asked is complete.
    asked = "Thanks, Vaidik.\nWhich country is she from?"
    said = "Thanks, we have noted her country."
    answers = [msg("Filipino"), msg("condo"), msg("5 people"), msg("yes")]
    trailing = [msg("Filipino,"), msg("my mum and"), msg("yes but"),
                msg("also"), msg("not sure or"), msg("well…"), msg("hmm...")]
    with patch.object(_settings, "smart_debounce", True), \
         patch.object(_settings, "debounce_seconds", 6.0), \
         patch.object(_settings, "debounce_short_seconds", 2.5):
        _msg._LAST_BOT_REPLY.clear()
        with patch.object(_msg.whapi, "send_text", new=send_text_1c), \
             patch.object(_msg, "_insert_ignoring_duplicates", new=AsyncMock()), \
             patch.object(_msg.conversation_service, "touch_outbound", new=AsyncMock()):
            await _msg.send_bot_reply({"id": 77, "customer_number": "6590000124"}, asked)
        results.append(("debounce: the reply we sent is what is remembered",
                        _msg.last_bot_reply("+6590000124") == asked))
        results.append(("debounce: a short answer to our question gets the short wait",
                        all(_wh.debounce_delay(m) == 2.5 for m in answers)))
        results.append(("debounce: a bare greeting after our question waits in full",
                        _wh.debounce_delay(msg("hi")) == 6.0
                        and _wh.debounce_delay(msg("Hello 👋")) == 6.0))
        results.append(("debounce: a reply that trails off waits in full",
                        all(_wh.debounce_delay(m) == 6.0 for m in trailing)))
        _msg.remember_bot_reply("6590000124", said)
        results.append(("debounce: after a statement, a short reply still waits in full",
                        _wh.debounce_delay(msg("Filipino")) == 6.0))
        _msg.remember_bot_reply("6590000124", asked)
        with patch.object(_settings, "debounce_after_our_question", False):
            results.append(("debounce: DEBOUNCE_AFTER_OUR_QUESTION off -> full wait",
                            _wh.debounce_delay(msg("Filipino")) == 6.0))
        _msg._LAST_BOT_REPLY.clear()

    async def burst(messages, gaps, delay_for):
        flushed: list[tuple[float, int]] = []
        began = asyncio.get_running_loop().time()

        async def handler(phone, batch):
            flushed.append((asyncio.get_running_loop().time() - began, len(batch)))

        d = MessageDebouncer(0.30, handler, delay_for)
        for m, gap in zip(messages, gaps):
            await asyncio.sleep(gap)
            await d.add(m)
        await asyncio.sleep(0.6)
        return flushed

    def quick(m):  # the real rule, at test speed: 0.30 s full, 0.05 s short
        return 0.05 if _wh.looks_complete(m) else 0.30

    flushed = await burst([msg("hi", "b1"), msg("I need", "b2"),
                           msg("a helper for my mother please", "b3")],
                          [0, 0.1, 0.1], quick)
    results.append(("debounce: a burst of three quick messages -> one reply",
                    len(flushed) == 1 and flushed[0][1] == 3))
    results.append(("debounce: the timer restarts, and the last message's wait applies",
                    bool(flushed) and 0.22 <= flushed[0][0] < 0.40))
    flushed = await burst([msg("what does a transfer cost?", "c1"), msg("for my", "c2")],
                          [0, 0.02], quick)
    results.append(("debounce: a fragment after a question waits in full again",
                    len(flushed) == 1 and flushed[0][0] >= 0.30))

    # --- 3. the pause at send ------------------------------------------------
    sent_typing: list[float] = []

    async def send_text(to, body, typing_time=0):
        sent_typing.append(typing_time)
        return {"message": {"id": "out-9"}}

    conv = {"id": 77, "customer_number": "+6590000124"}
    long_reply = "x" * 120
    for setting in (True, False):
        with patch.object(_msg.whapi, "send_text", new=send_text), \
             patch.object(_msg, "_insert_ignoring_duplicates", new=AsyncMock()), \
             patch.object(_msg.conversation_service, "touch_outbound", new=AsyncMock()), \
             patch.object(_settings, "short_send_typing", setting), \
             patch.object(_settings, "send_typing_seconds", 0.5):
            await _msg.send_bot_reply(conv, long_reply)
    results.append(("send: the reply is held at most 0.5 s",
                    sent_typing[0] == 0.5))
    results.append(("send: SHORT_SEND_TYPING off -> the old 2 s for a long reply",
                    sent_typing[1] == 2))
    # Step 1d: with the typing indicator on, Whapi is asked for no typing_time.
    for omit in (True, False):
        with patch.object(_msg.whapi, "send_text", new=send_text), \
             patch.object(_msg, "_insert_ignoring_duplicates", new=AsyncMock()), \
             patch.object(_msg.conversation_service, "touch_outbound", new=AsyncMock()), \
             patch.object(_msg, "SEND_WITHOUT_TYPING_TIME", omit), \
             patch.object(_settings, "typing_indicator", True), \
             patch.object(_settings, "short_send_typing", True), \
             patch.object(_settings, "send_typing_seconds", 0.5):
            await _msg.send_bot_reply(conv, long_reply)
    results.append(("send: indicator on -> no typing_time asked of Whapi",
                    sent_typing[2] == 0))
    results.append(("send: SEND_WITHOUT_TYPING_TIME off -> the 0.5 s again",
                    sent_typing[3] == 0.5))
    posted: list[dict] = []

    async def _post(path, payload):
        posted.append(dict(payload))
        return {"message": {"id": "out-10"}}

    with patch.object(_msg.whapi, "_post", new=_post):
        await _msg.whapi.send_text("+6590000124", "hi", typing_time=0)
    results.append(("send: typing_time 0 is left out of the request entirely",
                    bool(posted) and "typing_time" not in posted[0]))

    # --- 4. the embedding cache ---------------------------------------------
    calls: list[str] = []

    class _Emb:
        class embeddings:  # noqa: N801
            @staticmethod
            async def create(model, input, dimensions):
                calls.append(input)
                return types.SimpleNamespace(
                    data=[types.SimpleNamespace(embedding=[0.1] * dimensions)])

    fixed = [
        f"{_rr.OVERVIEW_QUERY}\n({_rr._readable_service('insurance')})",
        f"{_rr.ENQUIRY_OVERVIEW_QUERY}\n({_rr._readable_service('direct_hiring')})",
        f"{_rr.BRIEFING_QUERY_BY_SERVICE.get('renewal', _rr.BRIEFING_QUERY)}"
        f"\n({_rr._readable_service('renewal')})",
        f"{_rr.BRIEFING_QUERY}\n({_rr._readable_service('home_leave')})",
    ]
    for size in (256, 0):
        calls.clear()
        _rag._EMBED_CACHE.clear()
        with patch.object(_rag, "get_embedding_client", new=lambda: _Emb), \
             patch.object(_settings, "embed_cache_size", size):
            for q in fixed + fixed:
                await _rag.embed_query(q)
        if size:
            results.append(("embed cache: the fixed overview and briefing queries hit it",
                            len(calls) == len(fixed)))
        else:
            results.append(("embed cache: EMBED_CACHE_SIZE=0 -> every query embedded",
                            len(calls) == 2 * len(fixed)))
    calls.clear()
    _rag._EMBED_CACHE.clear()
    with patch.object(_rag, "get_embedding_client", new=lambda: _Emb), \
         patch.object(_settings, "embed_cache_size", 2):
        for q in ("a", "b", "c", "a"):
            await _rag.embed_query(q)
    results.append(("embed cache: size-limited, oldest out first",
                    len(_rag._EMBED_CACHE) == 2 and calls == ["a", "b", "c", "a"]))
    _rag._EMBED_CACHE.clear()

    # --- 4b. the embedding started beside the classifier (Step 1d) ----------
    class _SlowEmb:
        class embeddings:  # noqa: N801
            @staticmethod
            async def create(model, input, dimensions):
                calls.append(input)
                await asyncio.sleep(0.05)
                if input == "boom":
                    raise RuntimeError("provider down")
                return types.SimpleNamespace(
                    data=[types.SimpleNamespace(embedding=[float(len(input))] * dimensions)])

    for cache in (256, 0):
        calls.clear()
        _rag._EMBED_CACHE.clear()
        _rag._PREFETCHED.clear()
        with patch.object(_rag, "get_embedding_client", new=lambda: _SlowEmb), \
             patch.object(_settings, "embed_cache_size", cache):
            _rag.prefetch_query("same query")
            await asyncio.sleep(0)
            vec = await _rag.embed_query("same query")   # still in flight
        results.append((f"prefetch: the same query, still running, is one call (cache {cache})",
                        calls == ["same query"] and vec[0] == float(len("same query"))))
    calls.clear()
    _rag._EMBED_CACHE.clear()
    _rag._PREFETCHED.clear()
    with patch.object(_rag, "get_embedding_client", new=lambda: _SlowEmb):
        _rag.prefetch_query("predicted query")
        vec = await _rag.embed_query("the real query")
    results.append(("prefetch: a different query is embedded itself, the prediction unused",
                    "the real query" in calls and vec[0] == float(len("the real query"))))
    calls.clear()
    _rag._EMBED_CACHE.clear()
    _rag._PREFETCHED.clear()
    with patch.object(_rag, "get_embedding_client", new=lambda: _SlowEmb), \
         patch.object(_settings, "embed_cache_size", 0):
        _rag.prefetch_query("boom")
        await asyncio.sleep(0.1)
        try:
            await _rag.embed_query("boom")
            failed_over = False
        except RuntimeError:
            failed_over = calls == ["boom", "boom"]
    results.append(("prefetch: a failed prefetch is retried by the search, never reused",
                    failed_over))
    _rag._PREFETCHED.clear()
    _rag._EMBED_CACHE.clear()

    # ...and the graph starts it with the retriever's own query, before the
    # classifier, only with the switch on and a previous intent to predict from.
    from app.graph import graph as _graph
    started: list[str] = []
    order: list[str] = []

    async def _classify(state):
        order.append("classify")
        return {}

    mid = {**BASE, "intent": "renewal", "service_type": "renewal",
           "incoming_text": "in 3 weeks",
           "history_text": "You: When does her work permit expire?",
           "collected_info": {"full_name": "VD", "helper_name": "Devi"},
           "asked_field_counts": {"full_name": 1, "helper_name": 1, "permit_expiry": 1}}
    for on in (True, False):
        started.clear()
        order.clear()
        with patch.object(_graph, "intent_classifier", new=_classify), \
             patch.object(_graph.rag, "prefetch_query",
                          new=lambda q: (started.append(q), order.append("prefetch"))), \
             patch.object(_graph, "PREFETCH_SEARCH_EMBEDDING", on):
            await _graph._classify_with_prefetch(mid)
        if on:
            results.append(("prefetch: started before the classifier, with the retriever's query",
                            order == ["prefetch", "classify"]
                            and started == [_rr._search_query(mid)]))
        else:
            results.append(("prefetch: PREFETCH_SEARCH_EMBEDDING off -> nothing started",
                            not started and order == ["classify"]))
    started.clear()
    with patch.object(_graph, "intent_classifier", new=_classify), \
         patch.object(_graph.rag, "prefetch_query", new=lambda q: started.append(q)):
        await _graph._classify_with_prefetch({**mid, "intent": None})
    results.append(("prefetch: nothing to predict from on a first message", not started))
    from langgraph.checkpoint.memory import MemorySaver
    node = _graph.build_graph(MemorySaver()).nodes["intent_classifier"]
    results.append(("prefetch: the graph's classifier step is the one that starts it",
                    getattr(node.bound, "afunc", None) is _graph._classify_with_prefetch))

    # --- 5. LLM timeout and one fast retry ----------------------------------
    attempts: list[str] = []
    llm_args: list[tuple] = []

    class _SlowThenFast:
        def __init__(self, behaviour):
            self.behaviour = behaviour

        async def ainvoke(self, messages):
            kind = self.behaviour[len(attempts)] if len(attempts) < len(self.behaviour) else "ok"
            attempts.append(kind)
            if kind == "slow":
                await asyncio.sleep(1.0)
            if kind == "bad":
                raise ValueError("a 400 - not worth retrying")
            return types.SimpleNamespace(content="done")

    async def run_llm(behaviour, setting=True):
        attempts.clear()
        llm_args.clear()

        def fake_get_llm(*args, **kwargs):
            llm_args.append(args)
            return _SlowThenFast(behaviour)

        with patch.object(_llm, "get_llm", new=fake_get_llm), \
             patch.object(_settings, "llm_call_timeouts", setting), \
             patch.dict(_llm.LLM_TIMEOUTS, {"reply": 0.05}):
            try:
                return await _llm.complete("s", "u"), None
            except Exception as exc:  # noqa: BLE001
                return None, exc

    # Called from this module, so its purpose is the function name; give it
    # the reply limit explicitly.
    from app import timing as _timing
    with _timing.purpose("reply"):
        out, err = await run_llm(["slow", "ok"])
        results.append(("llm: a stuck call is abandoned and retried once",
                        out == "done" and attempts == ["slow", "ok"]))
        out, err = await run_llm(["slow", "slow"])
        results.append(("llm: two stuck calls fail - never a third attempt",
                        out is None and attempts == ["slow", "slow"]))
        out, err = await run_llm(["bad"])
        results.append(("llm: a non-transient error is not retried",
                        isinstance(err, ValueError) and attempts == ["bad"]))
        out, err = await run_llm(["ok"])
        results.append(("llm: the client's own retries are off under the limit",
                        bool(llm_args) and llm_args[0][-2:] == (0.05, 0)))
        out, err = await run_llm(["ok"], setting=False)
        results.append(("llm: LLM_CALL_TIMEOUTS off -> the old 45 s, two retries",
                        out == "done" and llm_args and len(llm_args[0]) == 3))
    results.append(("llm: every limit is several times its measured p90",
                    all(_llm.LLM_TIMEOUTS[k] >= 4 * p90 for k, p90 in
                        {"classify": 2.0, "extract": 1.6, "reply": 2.7,
                         "rewrite": 1.8}.items())))
    return results


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
        ok = await _resolve_lid(m)
    results.append(("an unresolvable LID is left alone, not guessed",
                    m.customer_number == "+116909177569373" and ok is False))

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

    # And when Whapi cannot resolve it, the message is HANDLED AT ALL - which
    # is a different claim from "the bot stays quiet", and the one that was
    # false until 2026-09-14. Inbound the allowlist hid it; outbound there is
    # no allowlist, so handle_outbound called get_or_create on a pseudo-number
    # and minted a conversation row keyed on a LID. 145 of them on the live
    # database. Asserted on BOTH handlers, because the write was on the side
    # nobody was looking at.
    for direction, handler in (("inbound", "handle_inbound"),
                               ("outbound", "handle_outbound")):
        reached: list[str] = []

        async def _seen(message, _r=reached):
            _r.append(message.customer_number)

        with patch("app.api.webhook.whapi.resolve_lid",
                   new=AsyncMock(return_value=None)),                patch.object(_wh, handler, new=_seen):
            await _wh.handle_payload(payload(from_me=(direction == "outbound")))
        results.append((f"an unresolvable LID never reaches {handler}",
                        reached == []))

    # --- who a conversation row belongs to, 2026-09-14 -------------------
    # An allowlisted number messaged three times and got nothing, with the bot
    # logging "still with a human" on a thread no human had ever touched. The
    # portal creates the row for a brand-new client (its webhook usually wins
    # the race with ours) and writes bot_status "none"; the old test was
    # `not bot_should_reply(existing)`, which is true for "none" as well, so
    # the branch returned before get_or_create could promote the row - and
    # every later message found the same "none" and did the same thing.
    from app.api.webhook import _is_paused_for_agent
    from app.services import conversation as _conv

    for status, paused, why in (
        (_conv.HUMAN_ACTIVE, True, "an agent is mid-conversation"),
        (_conv.BOT_NONE, False, "the portal made the row and nobody claimed it"),
        (_conv.BOT_ACTIVE, False, "already ours"),
        (None, False, "a row with no status is not a human on the thread"),
        ("", False, "nor is an empty one"),
    ):
        results.append((f"bot_status {status!r}: paused={paused} ({why})",
                        _is_paused_for_agent({"bot_status": status}) is paused))

    # ...and the two questions are deliberately different now. If these ever
    # agree again on "none", the defect is back.
    results.append((
        "'none' may not be replied to yet, but is NOT a human-held thread",
        _conv.bot_should_reply({"bot_status": _conv.BOT_NONE}) is False
        and _is_paused_for_agent({"bot_status": _conv.BOT_NONE}) is False))

    # ...and the WIRING, not just the predicate. Injecting `if False:` at the
    # call site left every check above green - the predicate was perfect and
    # nobody was asking it, which is the exact state the cost guard was in on
    # 2026-09-10. This runs handle_inbound and reads whether the row was
    # CLAIMED (get_or_create with engage=True) or left alone.
    for status, should_claim in ((_conv.BOT_NONE, True),
                                 (_conv.HUMAN_ACTIVE, False),
                                 (_conv.BOT_ACTIVE, True)):
        row = {"id": 4432, "customer_number": "917999600865",
               "bot_status": status, "langgraph_thread_id": "t-1"}
        claimed: list[bool] = []

        async def _get_or_create(phone, name="", *, engage=True, _c=claimed):
            _c.append(engage)
            return {**row, "bot_status": _conv.BOT_ACTIVE}, False

        msg = parse_webhook(payload(**{"from": "917999600865@s.whatsapp.net",
                                       "chat_id": "917999600865@s.whatsapp.net",
                                       "id": f"claim-{status}"}))[0]
        with patch.object(_wh.conversation_service, "get_by_phone",
                          new=AsyncMock(return_value=row)),                patch.object(_wh, "may_engage",
                          new=AsyncMock(return_value=(True, "no recent agent activity"))),                patch.object(_wh, "maybe_return_to_bot", new=AsyncMock(return_value=row)),                patch.object(_wh.conversation_service, "get_or_create", new=_get_or_create),                patch.object(_wh.message_service, "store_incoming", new=AsyncMock()),                patch.object(_wh.conversation_service, "touch_inbound", new=AsyncMock()),                patch.object(_wh, "_schedule_read_receipt", new=lambda *a, **k: None),                patch.object(_wh, "identify_contact", new=AsyncMock(return_value=row)),                patch.object(_wh.debouncer, "add", new=AsyncMock()),                patch.object(_wh, "emergency_override", new=AsyncMock(return_value=False)),                patch.object(_wh, "acknowledge_direct_address", new=AsyncMock()):
            await _wh.handle_inbound(msg)
        results.append((f"bot_status {status!r}: the row is "
                        f"{'claimed' if should_claim else 'left to the human'}",
                        bool(claimed) is should_claim))

    # --- a protocol event is not a client message, 2026-09-22 ------------
    # The client deleted a message and was asked about pets twice, a minute
    # apart, the second time as "Is there anything else living in the home,
    # such as pets?" - the agency: "'Anything else living in the home' is not
    # an appropriate question". `text_for_llm` falls back to "[<type> message]"
    # when there is no body, so the delete notice reached the graph as though
    # they had typed it; the collector found nothing answered and re-asked
    # (pets is max_asks=2, so only the wording changed).
    #
    # `has_message_content` already existed and was wired to the OUTBOUND path
    # alone - the same "one path and never the other" shape as the hiring cost
    # guard on 2026-09-10 - so this runs handle_inbound and reads whether the
    # event ever reached the debouncer, not whether the predicate is right.
    _EVENTS = (
        ("a deleted message", {"type": "action", "text": None,
                               "action": {"type": "delete", "target": "x"}},
         False),
        ("a reaction", {"type": "reaction", "text": None,
                        "action": {"type": "reaction", "emoji": "x",
                                   "target": "x"}}, False),
        ("an edit notice", {"type": "action", "text": None,
                            "action": {"type": "edit", "target": "x"}}, False),
        # The controls. A voice note and an image carry no body either, and
        # both are real messages a client sent - a rule that reads the TYPE
        # rather than the content would drop them.
        ("a text message", {"type": "text", "text": {"body": "3 bedroom condo"}},
         True),
        ("a voice note", {"type": "voice", "text": None,
                          "voice": {"link": "https://e/a.ogg",
                                    "mime_type": "audio/ogg"}}, True),
        ("an image with no caption", {"type": "image", "text": None,
                                      "image": {"link": "https://e/a.jpg",
                                                "mime_type": "image/jpeg"}},
         True),
    )
    for label, over, should_reach in _EVENTS:
        over = {k: v for k, v in over.items() if v is not None}
        msg = parse_webhook(payload(**{
            "from": "917970027379@s.whatsapp.net",
            "chat_id": "917970027379@s.whatsapp.net",
            "id": f"ev-{label}", "text": None, **over}))
        if not msg:
            results.append((f"{label} is parsed away before the bot", not should_reach))
            continue
        msg = msg[0]
        reached: list[bool] = []

        async def _add(_m, _r=reached):
            _r.append(True)

        row = {"id": 7, "customer_number": "917970027379",
               "bot_status": _conv.BOT_ACTIVE, "langgraph_thread_id": "t-1"}
        with patch.object(_wh.conversation_service, "get_by_phone",
                          new=AsyncMock(return_value=row)), \
             patch.object(_wh, "may_engage",
                          new=AsyncMock(return_value=(True, "ok"))), \
             patch.object(_wh, "maybe_return_to_bot",
                          new=AsyncMock(return_value=row)), \
             patch.object(_wh.conversation_service, "get_or_create",
                          new=AsyncMock(return_value=(row, False))), \
             patch.object(_wh.message_service, "store_incoming", new=AsyncMock()), \
             patch.object(_wh.conversation_service, "touch_inbound",
                          new=AsyncMock()), \
             patch.object(_wh, "_schedule_read_receipt", new=lambda *a, **k: None), \
             patch.object(_wh, "_should_identify", new=lambda *a, **k: False), \
             patch.object(_wh.transcription_service, "is_speech",
                          new=lambda *a, **k: False), \
             patch.object(_wh.debouncer, "add", new=_add):
            await _wh.handle_inbound(msg)
        results.append((
            f"{label} {'reaches' if should_reach else 'never reaches'} the bot",
            bool(reached) is should_reach))

    # ...and the OUTBOUND half, which has had this guard since 2026-09-18 and
    # never had a check: injecting `if False:` there on 2026-09-22 came back
    # GREEN in both suites. A reaction sent from the agency's own handset -
    # a thumbs-up on a client's message - must not be read as an agent taking
    # the thread over, because a stand-down with no stored message leaves
    # last_agent_message_at() nothing to measure an idle window against and
    # the bot is silent until the 72h safety net.
    for label, over, should_stand_down in (
        ("an agent's reaction", {"type": "reaction",
                                 "action": {"type": "reaction", "emoji": "x",
                                            "target": "y"}}, False),
        ("an agent's delete notice", {"type": "action",
                                      "action": {"type": "delete",
                                                 "target": "y"}}, False),
        ("a real agent reply", {"type": "text",
                                "text": {"body": "I will call you shortly"}},
         True),
    ):
        msg = parse_webhook(payload(**{
            "from_me": True, "from": "6565342277@s.whatsapp.net",
            "chat_id": "917970027379@s.whatsapp.net",
            "id": f"out-{label}", "text": None, **over}))[0]
        stood_down: list[bool] = []

        async def _took_over(_conversation, _s=stood_down):
            _s.append(True)

        row = {"id": 8, "customer_number": "917970027379",
               "bot_status": _conv.BOT_ACTIVE, "langgraph_thread_id": "t-1"}
        with patch.object(_wh.message_service, "was_sent_by_bot",
                          new=AsyncMock(return_value=False)), \
             patch.object(_wh.conversation_service, "get_by_phone",
                          new=AsyncMock(return_value=row)), \
             patch.object(_wh.message_service, "echoes_our_own_send",
                          new=lambda *a, **k: False), \
             patch.object(_wh.message_service, "is_auto_reply",
                          new=AsyncMock(return_value=False)), \
             patch.object(_wh.message_service, "store_agent_reply",
                          new=AsyncMock()), \
             patch.object(_wh.conversation_service, "touch_outbound",
                          new=AsyncMock()), \
             patch.object(_wh.debouncer, "flush_now", new=AsyncMock()), \
             patch.object(_wh.handover_service, "agent_took_over",
                          new=_took_over):
            await _wh.handle_outbound(msg)
        results.append((
            f"{label} {'stands the bot down' if should_stand_down else 'does NOT stand the bot down'}",
            bool(stood_down) is should_stand_down))

    # --- the chat-list fallback, 2026-09-14 ------------------------------
    # GET /chats/<lid> is not reliable. Measured on the live channel, same
    # minute: /chats/95786411008174@lid returned {"type":"unknown"} with no
    # phone, while /chats/917354708111@s.whatsapp.net returned that same chat
    # WITH its number. We cannot use the second form - the phone is what we are
    # looking for - so a miss falls back to sweeping the chat list. That LID is
    # an allowlisted tester who went unanswered all morning because of it.
    from app.whapi.client import WhapiClient

    def _client(chat_reply, chats_pages):
        c = WhapiClient()
        calls: list[str] = []

        async def _get(path):
            calls.append(path)
            if path.startswith("/chats/"):
                return chat_reply
            if path.startswith("/chats?"):
                off = int(path.split("offset=")[1])
                return {"chats": chats_pages.get(off, [])}
            return None

        c._get = _get
        return c, calls

    LIST = {0: [{"id": "95786411008174@lid", "phone": "917354708111",
                 "type": "contact"}]}

    c, calls = _client({"id": "95786411008174@lid", "type": "unknown"}, LIST)
    got = await c.resolve_lid("95786411008174@lid")
    results.append(("a LID /chats cannot resolve is found in the chat list",
                    got == "+917354708111" and any(p.startswith("/chats?") for p in calls)))

    # The duplicate is the whole reason /chats/<lid> is wrong, so reading the
    # list carelessly reproduces the bug. BOTH orderings, because they are
    # caught by different halves of the code and testing one ordering left an
    # injection green: phone-less first is caught by the `not phone` test,
    # phone-less second by first-wins.
    for order, label in ((("contact", "unknown"), "phone-less second"),
                         (("unknown", "contact"), "phone-less first")):
        entries = []
        for kind in order:
            e = {"id": "95786411008174@lid", "type": kind}
            if kind == "contact":
                e["phone"] = "917354708111"
            entries.append(e)
        c, _ = _client({"type": "unknown"}, {0: entries})
        results.append((f"a phone-less duplicate never wins ({label})",
                        await c.resolve_lid("95786411008174@lid") == "+917354708111"))

    # The live channel is 3,622 chats over 8 pages and the tester's LID was
    # not on the first, so paging is load-bearing rather than incidental -
    # an injection that stopped after one page stayed GREEN until this fixture
    # put a LID on the second.
    c, _ = _client({"type": "unknown"}, {
        0: [{"id": f"filler{i}@lid", "phone": f"65900000{i:02d}", "type": "contact"}
            for i in range(500)],
        500: [{"id": "late@lid", "phone": "917354708111", "type": "contact"}]})
    results.append(("a LID on the second page is still found",
                    await c.resolve_lid("late@lid") == "+917354708111"))

    # ...and the same LID listed twice with two DIFFERENT numbers takes the
    # first, so a client's identity does not depend on page order.
    c, _ = _client({"type": "unknown"}, {0: [
        {"id": "x@lid", "phone": "6591111111", "type": "contact"},
        {"id": "x@lid", "phone": "6599999999", "type": "contact"}]})
    results.append(("two numbers for one LID: the first wins, not the last",
                    await c.resolve_lid("x@lid") == "+6591111111"))

    # One sweep serves every LID on the channel - live it learned 2,544 - so a
    # second unresolvable lookup must be cheap, and a client messaging from a
    # genuinely unknown LID must not sweep once per message.
    c, calls = _client({"type": "unknown"}, {0: [
        {"id": "aaa@lid", "phone": "6591111111", "type": "contact"},
        {"id": "bbb@lid", "phone": "6592222222", "type": "contact"}]})
    first = await c.resolve_lid("aaa@lid")
    sweeps = sum(p.startswith("/chats?") for p in calls)
    second = await c.resolve_lid("bbb@lid")
    results.append(("one sweep resolves every LID on the channel",
                    (first, second) == ("+6591111111", "+6592222222")
                    and sweeps == sum(p.startswith("/chats?") for p in calls)))

    c, calls = _client({"type": "unknown"}, {0: []})
    await c.resolve_lid("nope-1@lid")
    await c.resolve_lid("nope-2@lid")
    results.append(("an unknown LID does not sweep once per message",
                    sum(p.startswith("/chats?") for p in calls) == 1))

    # And a LID nothing knows about still resolves to nothing, which is what
    # makes the webhook drop the message rather than invent a number.
    c, _ = _client({"type": "unknown"}, {0: []})
    results.append(("a LID nobody can resolve still returns None",
                    await c.resolve_lid("ghost@lid") is None))

    # An ordinary message never calls Whapi at all.
    m = parse_webhook(payload(**{"from": "917970027379@s.whatsapp.net",
                                 "chat_id": "917970027379@s.whatsapp.net"}))[0]
    stub = AsyncMock(return_value="+000")
    with patch("app.api.webhook.whapi.resolve_lid", new=stub):
        await _resolve_lid(m)
    results.append(("an ordinary message never asks Whapi anything",
                    m.customer_number == "+917970027379" and not stub.called))

    # --- our own reply, echoed back under an id we never saw, 2026-09-18 ---
    #
    # The worst thing in the direct-hire round, and it does not look like a bot
    # defect from the outside. Live on conversation 3766, "Usually about 4 to 6
    # weeks for a direct hire from overseas." went out twice, 1.9 seconds
    # apart: whapi.client._post retried a send whose request had already been
    # delivered, the retry got a NEW message id, and only that id reached
    # `mark_sent_by_bot`. When Whapi echoed the FIRST copy back as a from_me
    # webhook, `was_sent_by_bot` had never heard of its id - so handle_outbound
    # read our own sentence as a human agent taking the thread over, stood the
    # bot down, and the client's next message ("ok and what is the fees for
    # this") got no reply at all. The row is still on the live database as
    # `is_bot=False, sent_by='agent'`.
    #
    # RUN rather than unit-tested, because both halves of this fix are wiring:
    # the marking happens inside send_bot_reply and the test happens inside
    # handle_outbound, and a predicate that is never asked is the hole this
    # file has now recorded five times. What is read here is whether the bot
    # STOOD DOWN, which is the thing the client actually experienced.
    from app.services import message as _msg

    async def _outbound_stands_down(body: str, mark: str | None) -> bool:
        if mark is not None:
            _msg.mark_body_sent_by_bot("917970027379", mark)
        msg = parse_webhook({"messages": [{
            "id": f"echo-{abs(hash((body, mark)))}", "type": "text", "from_me": True,
            "chat_id": "917970027379@s.whatsapp.net",
            "from": "917970027379@s.whatsapp.net",
            "to": "6565342277@s.whatsapp.net",
            "text": {"body": body}}]})[0]
        row = {"id": 3766, "customer_number": "917970027379",
               "bot_status": "bot_active", "langgraph_thread_id": "t-1"}
        took_over: list[bool] = []
        with patch.object(_wh.conversation_service, "get_by_phone",
                          new=AsyncMock(return_value=row)), \
             patch.object(_wh.message_service, "was_sent_by_bot",
                          new=AsyncMock(return_value=False)), \
             patch.object(_wh.message_service, "is_auto_reply",
                          new=AsyncMock(return_value=False)), \
             patch.object(_wh.message_service, "store_own_echo", new=AsyncMock()), \
             patch.object(_wh.message_service, "store_agent_reply", new=AsyncMock()), \
             patch.object(_wh.conversation_service, "touch_outbound", new=AsyncMock()), \
             patch.object(_wh.debouncer, "flush_now", new=AsyncMock()), \
             patch.object(_wh, "_remember_standdown", new=lambda *a, **k: None), \
             patch.object(_wh.handover_service, "agent_took_over",
                          new=AsyncMock(side_effect=lambda *a, **k: took_over.append(True))):
            await _wh.handle_outbound(msg)
        return bool(took_over)

    REPLY = "Usually about 4 to 6 weeks for a direct hire from overseas."
    results.append((
        "a second copy of our own reply does not stand the bot down",
        await _outbound_stands_down(REPLY, REPLY) is False))
    # ...and the control, which is the half that must not be traded away: a
    # human agent picking the thread up still silences the bot. Without this,
    # a guard wide enough to swallow every outbound message would pass.
    results.append((
        "...and a human agent taking over still does",
        await _outbound_stands_down("Hi John, Grace here from Ming Hwee.", None) is True))
    # Whitespace and case are not what makes it a different message - WhatsApp
    # normalises neither, and a guard that misses on a trailing newline is a
    # guard that misses on the case it exists for.
    results.append((
        "...and a copy differing only in spacing is still ours",
        await _outbound_stands_down("  Usually about 4 to 6 weeks for a direct "
                                    "hire from overseas.  ", REPLY) is False))
    # The phone is part of the key, so the same sentence sent to a DIFFERENT
    # client cannot excuse a real agent on this one.
    _msg.mark_body_sent_by_bot("6591234567", "Thanks, I have passed this on.")
    results.append((
        "...but the same words sent to someone else do not excuse this thread",
        await _outbound_stands_down("Thanks, I have passed this on.", None) is True))

    # ...and the MARKING is wired, and wired BEFORE the request goes out.
    # Both halves matter and neither shows up in the four checks above, which
    # call mark_body_sent_by_bot themselves - a predicate nobody asks is the
    # hole this file has recorded five times, and a mark written AFTER the
    # send loses the race the guard exists for: live, the duplicate was stored
    # 1.9 seconds BEFORE the bot's own copy. So the stub asks, at the moment
    # the request would leave, whether we would already recognise the echo.
    marked_before_send: list[bool] = []

    async def _send_text(phone, body, typing_time=0):
        marked_before_send.append(_msg.echoes_our_own_send(phone, body))
        return {"message": {"id": "sent-1"}}

    with patch.object(_msg.whapi, "send_text", new=_send_text), \
         patch.object(_msg, "_insert_ignoring_duplicates", new=AsyncMock(return_value={})), \
         patch.object(_msg.conversation_service, "touch_outbound", new=AsyncMock()):
        await _msg.send_bot_reply(
            {"id": 3766, "customer_number": "+6598887777"},
            "Our agent will confirm the exact fee for your situation.")
    results.append(("what we are about to say is recorded before we say it",
                    marked_before_send == [True]))

    # --- a send that may already have arrived is not sent again -----------
    # The other end of the same defect. `_post` used to retry three times on
    # any httpx error and any 5xx - and a ReadTimeout or a 502 means the
    # request was written and the RESPONSE was lost, not that nothing was
    # delivered. Whapi has no idempotency key, so the second POST is a second
    # WhatsApp message.
    import httpx as _httpx
    from app.whapi.client import WhapiClient as _WC, WhapiError as _WE

    async def _attempts(failure) -> int:
        c = _WC()
        tries = {"n": 0}

        class _Stub:
            is_closed = False

            async def post(self, path, json=None):
                tries["n"] += 1
                if isinstance(failure, int):
                    return _httpx.Response(failure, text="boom",
                                           request=_httpx.Request("POST", "http://x"))
                raise failure

        c._client = _Stub()
        try:
            await c._post("/messages/text", {})
        except (_WE, Exception):
            pass
        return tries["n"]

    for failure, expected, why in (
        (_httpx.ReadTimeout("t"), 1, "the response was lost, not the request"),
        (_httpx.RemoteProtocolError("p"), 1, "they hung up after receiving it"),
        (502, 1, "their server had it"),
        (_httpx.ConnectError("c"), 3, "nothing was ever sent"),
    ):
        name = failure if isinstance(failure, int) else type(failure).__name__
        results.append((f"a send is attempted {expected}x on {name} ({why})",
                        await _attempts(failure) == expected))
    return results


# (label, carried the reference) for every parked reply that mentions the agent.
_REFERENCE_SWEEP: set = set()


async def _run_case(node, label, overrides):
    """Run one state. Returns (ok, detail, fingerprint).

    The fingerprint is everything a rule change could move - the reply, the
    system prompt the model was handed, and the state the node returned - so
    two passes over the same state under two rule sets can be compared whole.
    """
    global _LAST_SYSTEM_PROMPT
    _LAST_SYSTEM_PROMPT = ""
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
    # ...and what it must NOT contain. Added 2026-09-22 for the
    # greeting fix, where the defect was a WRONG canned string rather
    # than a missing one: "Sure, go ahead" is a perfectly good reply
    # to an announced question and the wrong one to "Hi", so the only
    # assertion that bites is that it is absent from this turn.
    forbid = state.pop("_forbid_reply", None)
    expect_state = state.pop("_expect_state", None)
    # A substring that must appear in the system prompt the model was
    # handed - for a fix that lives in the INSTRUCTION, not the reply.
    expect_prompt = state.pop("_expect_prompt", None)
    forbid_prompt = state.pop("_forbid_prompt", None)
    # What the extractor handed back, and what must survive the filters.
    # `_expect_not_collected` is the important half: a field the client
    # never spoke to must not be filed as though they had, because a
    # filled field is never asked and the ticket then states it as fact.
    global _EXTRACTION, _BRIEFING_RECORDS
    _EXTRACTION = state.pop("_stub_extraction", None) or {}
    _BRIEFING_RECORDS = state.pop("_briefing_records", None) or {}
    not_collected = state.pop("_expect_not_collected", None)
    collected = state.pop("_expect_collected", None)
    with patch("app.graph.llm.complete",
               new=AsyncMock(return_value=stub or STEPPED_REPLY)), \
         patch("app.graph.llm.complete_json", new=AsyncMock(return_value={})):
        try:
            out = await RUNNERS[node](state, stub)
        except Exception as exc:  # noqa: BLE001 - reporting is the whole job
            return False, f"{type(exc).__name__}: {exc}", ("EXC", type(exc).__name__, str(exc))
    ok = isinstance(out, dict)
    detail = sorted(out)[:4] if ok else out
    got_reply = (out.get("reply") or out.get("reply_text") or "") if ok else ""
    fingerprint = (
        got_reply,
        _LAST_SYSTEM_PROMPT,
        repr(sorted((k, repr(v)) for k, v in out.items())) if ok else repr(out),
    )
    # Step 2b (2026-10-13): on a parked topic, EVERY reply that mentions the
    # agent carries that topic's reference - whichever path wrote it. Swept
    # over every blocked_topic_responder state here, not asserted on the one
    # path that was reported: live, the canned chase had it and the model-
    # written holding line did not. A handover (other work) is exempt: that
    # is a new ticket, numbered by handover_executor.
    if ok and node == "blocked_topic_responder":
        from app.graph.reference import mentions_agent
        numbers = [t.get("ticket_number") for t in (state.get("blocked_topics") or {}).values()
                   if isinstance(t, dict) and t.get("ticket_number")]
        if numbers and mentions_agent(got_reply) and not out.get("needs_handover"):
            ok = any(n in got_reply for n in numbers)
            _REFERENCE_SWEEP.add((label, ok))
            if not ok:
                return False, f"mentions the agent with no reference: {got_reply[:90]!r}", fingerprint
    if ok and expect:
        ok = expect.lower() in got_reply.lower()
        detail = f"expected {expect!r} in {got_reply[:80]!r}"
    if ok and forbid:
        ok = forbid.lower() not in got_reply.lower()
        detail = f"reply omits {forbid!r} -> " + (
            "as expected" if ok else f"PRESENT in {got_reply[:80]!r}")
    if ok and expect_state:
        wrong = {k: out.get(k) for k, v in expect_state.items()
                 if out.get(k) != v}
        ok = not wrong
        detail = f"state {expect_state} -> " + ("as expected" if ok
                                                else f"got {wrong}")
    if ok and expect_prompt:
        ok = expect_prompt.lower() in _LAST_SYSTEM_PROMPT.lower()
        detail = f"prompt carries {expect_prompt!r} -> " + (
            "as expected" if ok else "MISSING")
    if ok and forbid_prompt:
        ok = forbid_prompt.lower() not in _LAST_SYSTEM_PROMPT.lower()
        detail = f"prompt omits {forbid_prompt!r} -> " + (
            "as expected" if ok else "PRESENT")
    if ok and not_collected:
        got = out.get("collected_info") or {}
        leaked = [k for k in not_collected if k in got]
        ok = not leaked
        detail = (f"{not_collected} not filed -> "
                  + ("as expected" if ok
                     else f"LEAKED {[(k, got[k]) for k in leaked]}"))
    if ok and collected:
        got = out.get("collected_info") or {}
        wrong = {k: got.get(k) for k, v in collected.items()
                 if got.get(k) != v}
        ok = not wrong
        detail = f"{collected} filed -> " + ("as expected" if ok
                                            else f"got {wrong}")
    return ok, detail, fingerprint


# --- 2026-09-24: the rules moved from code into cb_kb_rules ---------------
# The same states, run under the code defaults (what RULES_FROM_DB=false
# means) and under the rules as they are in the TABLE (what =true means),
# must produce identical replies, prompts and returned state. Every state,
# not only the fee ones: the cost guard, the fee notes, the salary floor and
# the retrieval filter all sit on paths that are not labelled "fee".
#
# And a third pass with ONE rule deliberately wrong must change something.
# Without it, "identical" could just as well mean the comparison is blind -
# the lesson this file has recorded five times, that a green check proves
# nothing until the fault it exists to catch has been shown to turn it red.
async def _rules_for_on_pass():
    from app.services import kb_rules
    if kb_rules.enabled():
        await kb_rules.refresh(force=True)
        if kb_rules.source() != "db":
            raise SystemExit("RULES_FROM_DB is on but cb_kb_rules could not be loaded")
        return kb_rules.rules(), "cb_kb_rules (live table)"
    from scripts.kb_seed import seed_rows
    return kb_rules.parse_rows(seed_rows()), "kb_rules_002_seed.sql (offline)"


async def _pass(rules):
    from app.services import kb_rules
    results = []
    with kb_rules.use(rules):
        for node, label, overrides in CASES:
            results.append(await _run_case(node, label, overrides))
    return results


def _refuse_real_whapi() -> list[str]:
    """Refuse - and record - any real HTTP request to Whapi for the whole run.

    Every Whapi call here is meant to be stubbed. One was not (the typing
    indicator spawned by the real handle_inbound) and it sent a real presence
    request on every run, unseen until it disturbed another check. Guarded at
    httpx itself so a call nobody thought to stub is caught, not just the ones
    a test knows about; other hosts pass through untouched.
    """
    import httpx as _httpx
    from urllib.parse import urlparse

    from app.config import settings as _settings

    whapi_host = urlparse(_settings.whapi_base_url).hostname or "gate.whapi.cloud"
    seen: list[str] = []
    original = _httpx.AsyncClient.send

    async def guarded(self, request, *args, **kwargs):
        if request.url.host == whapi_host:
            seen.append(f"{request.method} {request.url.path}")
            raise _httpx.ConnectError("smoke_nodes: real Whapi request refused",
                                      request=request)
        return await original(self, request, *args, **kwargs)

    _httpx.AsyncClient.send = guarded
    return seen


async def main() -> int:
    from dataclasses import replace

    from app.services import kb_rules

    real_whapi = _refuse_real_whapi()
    # Off for the whole run; the typing checks switch it on for themselves. Left
    # on, every check that drives the real handle_inbound - several, with real
    # allowlisted numbers - started a real indicator (2026-10-12).
    from app.config import settings as _settings
    _settings.typing_indicator = False
    failures = 0
    for label, ok in await _lid_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  webhook  {label:44}")
        failures += not ok
    for label, ok in await _timing_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  webhook  {label:44}")
        failures += not ok
    for label, ok in await _step1b_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  latency  {label:44}")
        failures += not ok
    for label, ok in await _reference_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  handover  {label:44}")
        failures += not ok
    for label, ok in await _tag_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  wording  {label:44}")
        failures += not ok
    for label, ok in await _batch_b_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  wording  {label:44}")
        failures += not ok
    for label, ok in await _stray_script_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  wording  {label:44}")
        failures += not ok
    for label, ok in await _batch_b2_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  batch-b2  {label:44}")
        failures += not ok
    for label, ok in _chase_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  chase  {label:44}")
        failures += not ok
    for label, ok in await _harm_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  safety  {label:44}")
        failures += not ok

    off = await _pass(kb_rules.DEFAULTS)
    for (node, label, _), (ok, detail, _) in zip(CASES, off):
        print(f"  {'PASS' if ok else 'FAIL'}  {node + '  ' + label:44} -> {detail}")
        failures += not ok

    # At least the six paths that mention the agent: model-written holding
    # line, fallback, noted, chase, "are you there", cost deferral.
    swept = sorted(_REFERENCE_SWEEP)
    ok = len(swept) >= 6 and all(carried for _, carried in swept)
    print(f"  {'PASS' if ok else 'FAIL'}  handover  every parked reply that mentions the agent "
          f"carries its reference ({len(swept)} states)")
    failures += not ok

    on_rules, where = await _rules_for_on_pass()
    on = await _pass(on_rules)
    diffs = [f"{node}  {label}" for (node, label, _), a, b in zip(CASES, off, on)
             if a[2] != b[2]]
    ok = not diffs
    print(f"\n  {'PASS' if ok else 'FAIL'}  rules  {len(CASES)} states identical with "
          f"the switch off and on ({where})"
          + ("" if ok else f" - {len(diffs)} differ: {diffs[:5]}"))
    failures += not ok

    fault = replace(
        on_rules,
        fee_stated=on_rules.fee_stated | {"direct_hiring"},
        cost_withheld=on_rules.cost_withheld - {"direct_hiring"},
    )
    changed = [f"{node}  {label}" for (node, label, _), a, b
               in zip(CASES, off, await _pass(fault)) if a[2] != b[2]]
    ok = bool(changed)
    print(f"  {'PASS' if ok else 'FAIL'}  rules  a wrong rule (direct_hiring stated) is "
          f"SEEN by the comparison: {len(changed)} state(s) changed {changed[:3]}")
    failures += not ok

    ok = not real_whapi
    print(f"  {'PASS' if ok else 'FAIL'}  safety  no real Whapi request was made "
          f"by any check {real_whapi[:3] if real_whapi else ''}")
    failures += not ok

    print("\nALL PASS" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
