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
                         "permit_expiry": "2 months"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1, "nationality": 1,
                             "passport_expiry": 1, "permit_expiry": 1},
      "briefed_services": [],
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "A consultant will confirm the cost for her embassy.",
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
      "_forbid_prompt": "release your current helper",
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
      "_expect_collected": {"transfer_direction": "releasing my current helper"},
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
      "_expect_prompt": "release your current helper"}),

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
      "_forbid_prompt": "release your current helper"}),

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
    ("response_generator", "Claire introduces herself on a first process question",
     {"intent": "process_question", "service_type": "new_hiring",
      "incoming_text": "hi i would like to hire a helper. what is the process to go about it?",
      "history_text": "",
      "_expect_prompt": "introduce yourself in one short sentence"}),
    # ...and on no other turn, or she says it every time.
    ("response_generator", "...and only on the first message",
     {"intent": "process_question", "service_type": "new_hiring",
      "incoming_text": "what is the process to go about it?",
      "history_text": "client: hi\nbot: Hi, I'm Claire, Ming Hwee's AI assistant.",
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
                         "leave_dates": "12 December to 8 January"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "leave_dates": 1},
      "briefed_services": [],
      "_expect_prompt": "book her air ticket"}),
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
                         "leave_dates": "12 December to 8 January"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1,
                             "nationality": 1, "leave_dates": 1},
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
    ("info_collector", "their own passport is answered, not collected",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "There isn't any helper here. I want to renew my passport",
      "collected_info": {"full_name": "Rats"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "history_text": "bot: may I know your helper's name?",
      "_expect_prompt": "asking about THEIR OWN passport",
      # `flagged_once` is the half that makes the NEXT turn work. Without it
      # "why not?" is met with "May I know your helper's name?" again, and the
      # assertion on the predicate cannot see that, because it supplies the
      # flag itself.
      "_expect_state": {"info_complete": False, "needs_handover": False,
                        "flagged_once": ["own_passport"]}}),
    # The records are stripped on that turn, which is the guard half rather
    # than the prompt half: the retrieved set holds "$450" and would ground it.
    ("info_collector", "...and the helper's fee is not offered to the model",
     {"service_type": "passport_renewal", "intent": "passport_renewal",
      "incoming_text": "There isn't any helper here. I want to renew my passport",
      "collected_info": {"full_name": "Rats"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "rag_context": "A passport renewal costs approximately $450 and takes "
                     "approximately 3 working days.",
      "history_text": "bot: may I know your helper's name?",
      "_forbid_prompt": "450"}),
    # The same request on the service the words actually misroute into.
    ("info_collector", "...on the work-permit flow it lands in too",
     {"service_type": "renewal", "intent": "renewal",
      "incoming_text": "Also I want to renew my passport also",
      "collected_info": {"full_name": "Vaidik", "helper_name": "Polo"},
      "asked_field_counts": {"full_name": 1, "helper_name": 1},
      "history_text": "bot: Here is everything for Polo's passport renewal.",
      "_expect_prompt": "asking about THEIR OWN passport"}),
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
    ("info_collector", "new hiring explains itself before the questions",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Thomas",
      "collected_info": {"full_name": "Thomas"},
      "asked_field_counts": {"full_name": 1},
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Hiring from overseas usually takes 4 to 6 weeks.",
      "history_text": "bot: May I know your name?",
      "_expect_prompt": "roughly how long it takes"}),
    # ...and it is not told it is a short job we handle end to end, which is
    # true of a permit renewal and false of a 25-question hire.
    ("info_collector", "...and is not called a short, well-defined job",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Thomas",
      "collected_info": {"full_name": "Thomas"},
      "asked_field_counts": {"full_name": 1},
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "Hiring from overseas usually takes 4 to 6 weeks.",
      "history_text": "bot: May I know your name?",
      "_forbid_prompt": "short, well-defined job"}),
    # ...and the overview does not spend its one sentence on a price that
    # quotes_hiring_package_cost is about to swap for the deferral line.
    ("info_collector", "...and is told not to put a package price in it",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "Thomas",
      "collected_info": {"full_name": "Thomas"},
      "asked_field_counts": {"full_name": 1},
      "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
      "rag_context": "The total package is approximately $4,225.",
      "history_text": "bot: May I know your name?",
      "_expect_prompt": "Do NOT put a total, a package price"}),
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
]


# What the EXTRACTOR returned on this turn. Set per state by the loop below.
#
# Added 2026-09-16. The guard that drops an invented care type was tested
# only by calling the predicate, so restoring the broken call site left every
# check green - the same "imported and never called" hole that let the
# hiring-cost guard sit disabled for two days (2026-09-10). A value the
# extractor made up is a thing only the NODE can drop, so the node has to run.
_EXTRACTION: dict = {}

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

    with patch("app.graph.nodes.info_collector.complete",
               new=_capture, create=True), \
         patch("app.graph.nodes.info_collector.complete_json",
               new=AsyncMock(return_value=dict(_EXTRACTION)), create=True), \
         patch("app.graph.nodes.info_collector._open_lead_early",
               new=AsyncMock(return_value={})):
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


RUNNERS = {
    "info_collector": _run_info_collector,
    "response_generator": _run_response_generator,
    "blocked_topic_responder": _run_blocked_topic_responder,
    "intent_classifier": _run_intent_classifier,
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
                          new=AsyncMock(return_value=(True, "no recent agent activity"))),                patch.object(_wh, "maybe_return_to_bot", new=AsyncMock(return_value=row)),                patch.object(_wh.conversation_service, "get_or_create", new=_get_or_create),                patch.object(_wh.message_service, "store_incoming", new=AsyncMock()),                patch.object(_wh, "_schedule_read_receipt", new=lambda *a, **k: None),                patch.object(_wh, "identify_contact", new=AsyncMock(return_value=row)),                patch.object(_wh.debouncer, "add", new=AsyncMock()),                patch.object(_wh, "emergency_override", new=AsyncMock(return_value=False)),                patch.object(_wh, "acknowledge_direct_address", new=AsyncMock()):
            await _wh.handle_inbound(msg)
        results.append((f"bot_status {status!r}: the row is "
                        f"{'claimed' if should_claim else 'left to the human'}",
                        bool(claimed) is should_claim))

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
        # A substring that must appear in the system prompt the model was
        # handed - for a fix that lives in the INSTRUCTION, not the reply.
        expect_prompt = state.pop("_expect_prompt", None)
        forbid_prompt = state.pop("_forbid_prompt", None)
        # What the extractor handed back, and what must survive the filters.
        # `_expect_not_collected` is the important half: a field the client
        # never spoke to must not be filed as though they had, because a
        # filled field is never asked and the ticket then states it as fact.
        global _EXTRACTION
        _EXTRACTION = state.pop("_stub_extraction", None) or {}
        not_collected = state.pop("_expect_not_collected", None)
        collected = state.pop("_expect_collected", None)
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
                print(f"  {'PASS' if ok else 'FAIL'}  {full:44} -> {detail}")
                failures += not ok
            except Exception as exc:  # noqa: BLE001 - reporting is the whole job
                print(f"  FAIL  {full:44} -> {type(exc).__name__}: {exc}")
                failures += 1
    print("\nALL PASS" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
