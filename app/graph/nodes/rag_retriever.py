"""Node 2 — pull supporting context out of cb_knowledge_base_updated."""

from __future__ import annotations

import logging
from typing import Any

from app.graph.guards import last_bot_line
from app.graph.state import ConversationState, effective_contact_type
from app.services import contact as contact_service
from app.services import rag
from app.services.lead import nationality_code

logger = logging.getLogger(__name__)


# Below this a message is too short to embed meaningfully on its own: "how
# much?", "and the levy?", "how long ah" carry almost no signal, so every
# similarity comes back near zero and a knowledge base full of the answer looks
# empty. What the client is asking about is in the question we just asked them.
_SHORT_QUERY_WORDS = 6


def _search_query(state: ConversationState) -> str:
    """Bias the query with the last thing the client said plus the intent."""
    message = (state.get("incoming_text") or "").strip()
    intent = state.get("intent") or ""
    if intent in {"greeting", "smalltalk", "other"}:
        return message

    readable_intent = intent.replace("_", " ")
    if not message:
        return readable_intent

    parts = [message]
    if len(message.split()) < _SHORT_QUERY_WORDS:
        previous = last_bot_line(state.get("history_text") or "")
        if previous:
            parts.append(previous)
    parts.append(f"({readable_intent})")
    return "\n".join(parts)


def _nationality(state: ConversationState) -> str | None:
    """The PH/ID/MM code this conversation is about, if we know it.

    Both the client's own nationality (a helper) and their preference (an
    employer) narrow the same way. 'none' is what nationality_code() returns
    for "no preference", which is not a filter — it is the absence of one.
    """
    collected = state.get("collected_info") or {}
    code = nationality_code(
        str(collected.get("nationality") or "")
    ) or nationality_code(str(collected.get("preferred_nationality") or ""))
    return code if code and code != "none" else None


async def rag_retriever(state: ConversationState) -> dict[str, Any]:
    case_summary = None
    if state.get("intent") == "case_enquiry" and state.get("matched_case_id"):
        case_summary = await contact_service.get_case_summary(state["matched_case_id"])

    # Routing labels are per chunk, so the filter runs before the vector search.
    # Each one is inclusive of its catch-all bucket inside the match function;
    # anything we cannot determine is passed as None and simply not filtered on.
    matches = await rag.search(
        _search_query(state),
        service_type=state.get("service_type"),
        contact_type=effective_contact_type(state),
        nationality=_nationality(state),
    )
    best = rag.best_similarity(matches)

    return {
        "rag_matches": matches,
        "rag_context": rag.format_context(matches),
        "rag_best_score": best,
        "case_summary": case_summary,
    }
