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
    # --- blocked_topic_responder -------------------------------------------
    # A question asked while a topic sits with an agent. The stepped branch is
    # allowed here too, and the holding branch must stay two sentences.
    ("blocked_topic_responder", "process question, parked",
     {"intent": "process_question", "service_type": "new_hiring",
      "incoming_text": "what is the full process for hiring a helper",
      "history_text": "client: hi\nbot: I've passed this to our team.",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0001"}}}),
    ("blocked_topic_responder", "ordinary message, parked",
     {"intent": "new_hiring", "service_type": "new_hiring",
      "incoming_text": "ok noted thanks",
      "history_text": "client: hi\nbot: I've passed this to our team.",
      "blocked_topics": {"new_hiring": {"ticket_id": 1, "ticket_number": "CB-2026-0001"}}}),
]


async def _run_info_collector(state):
    with patch("app.graph.nodes.info_collector.complete",
               new=AsyncMock(return_value="When does her passport expire?"), create=True), \
         patch("app.graph.nodes.info_collector.complete_json",
               new=AsyncMock(return_value={}), create=True), \
         patch("app.graph.nodes.info_collector._open_lead_early",
               new=AsyncMock(return_value={})):
        from app.graph.nodes.info_collector import info_collector
        return await info_collector(state)


async def _run_response_generator(state):
    # Returns a stepped reply so the widened clamp and the list-marker masking
    # are exercised end to end, not just in a unit assertion.
    with patch("app.graph.nodes.response_generator.complete",
               new=AsyncMock(return_value=STEPPED_REPLY), create=True):
        from app.graph.nodes.response_generator import response_generator
        return await response_generator(state)


async def _run_blocked_topic_responder(state):
    with patch("app.graph.nodes.blocked_topic_responder.complete",
               new=AsyncMock(return_value=STEPPED_REPLY), create=True):
        from app.graph.nodes.blocked_topic_responder import blocked_topic_responder
        return await blocked_topic_responder(state)


RUNNERS = {
    "info_collector": _run_info_collector,
    "response_generator": _run_response_generator,
    "blocked_topic_responder": _run_blocked_topic_responder,
}


async def main() -> int:
    failures = 0
    for node, label, overrides in CASES:
        state = {**BASE, **overrides}
        full = f"{node}  {label}"
        with patch("app.graph.llm.complete", new=AsyncMock(return_value=STEPPED_REPLY)), \
             patch("app.graph.llm.complete_json", new=AsyncMock(return_value={})):
            try:
                out = await RUNNERS[node](state)
                ok = isinstance(out, dict)
                print(f"  {'PASS' if ok else 'FAIL'}  {full:44} -> {sorted(out)[:4]}")
                failures += not ok
            except Exception as exc:  # noqa: BLE001 - reporting is the whole job
                print(f"  FAIL  {full:44} -> {type(exc).__name__}: {exc}")
                failures += 1
    print("\nALL PASS" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
