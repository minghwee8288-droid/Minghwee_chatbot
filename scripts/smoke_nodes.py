"""Actually RUN each graph node once, with the LLM and the database stubbed.

The point is to catch errors that only appear when the code executes —
UnboundLocalError, a bad f-string, a renamed helper. selfcheck_flows.py reads
data structures and passed cleanly on 2026-09-04 while info_collector raised
UnboundLocalError on every single turn: the intro note read `first_contact`
eighty lines above the line that assigned it. The graph caught it as
"bot_confused" and handed each message to a human, so from the client's side
the bot had simply stopped replying.

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

CASES = [
    ("info_collector  first contact", {}),
    ("info_collector  mid-conversation", {"history_text": "client: hi\nbot: Hello",
                                          "collected_info": {"helper_name": "Koko"},
                                          "asked_field_counts": {"helper_name": 1}}),
    ("info_collector  new_hiring opening", {"service_type": "new_hiring", "intent": "new_hiring"}),
    ("info_collector  recognised client",
     {"prior_hires": 1, "placed_helper": {"helper_name": "Liza Fernandez",
                                          "nationality": "PH",
                                          "passport_expiry": "27 September 2033"}}),
    ("info_collector  volunteered requirement",
     {"service_type": "new_hiring", "intent": "new_hiring",
      "incoming_text": "She shouldn't smoke and no drinking please",
      "history_text": "bot: How many people live at home?"}),
    ("info_collector  transfer take-on",
     {"service_type": "transfer_employer", "intent": "transfer",
      "incoming_text": "I'm looking for a transfer helper"}),
    ("info_collector  insurance", {"service_type": "insurance", "intent": "insurance"}),
]


async def main() -> int:
    failures = 0
    for label, overrides in CASES:
        state = {**BASE, **overrides}
        with patch("app.graph.llm.complete", new=AsyncMock(return_value="When does her passport expire?")), \
             patch("app.graph.llm.complete_json", new=AsyncMock(return_value={})), \
             patch("app.graph.nodes.info_collector.complete", new=AsyncMock(return_value="When does her passport expire?"), create=True), \
             patch("app.graph.nodes.info_collector.complete_json", new=AsyncMock(return_value={}), create=True), \
             patch("app.graph.nodes.info_collector._open_lead_early", new=AsyncMock(return_value={})):
            from app.graph.nodes.info_collector import info_collector
            try:
                out = await info_collector(state)
                ok = isinstance(out, dict)
                print(f"  {'PASS' if ok else 'FAIL'}  {label:38} -> {sorted(out)[:4]}")
                failures += not ok
            except Exception as exc:  # noqa: BLE001 - reporting is the whole job
                print(f"  FAIL  {label:38} -> {type(exc).__name__}: {exc}")
                failures += 1
    print("\nALL PASS" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
