"""Node 2 — pull supporting context out of cb_knowledge_base."""

from __future__ import annotations

import logging
from typing import Any

from app.services import contact as contact_service
from app.services import rag
from app.graph.state import ConversationState

logger = logging.getLogger(__name__)


def _search_query(state: ConversationState) -> str:
    """Bias the query with the last thing the client said plus the intent."""
    message = (state.get("incoming_text") or "").strip()
    intent = state.get("intent") or ""
    if intent in {"greeting", "smalltalk", "other"}:
        return message
    readable_intent = intent.replace("_", " ")
    return f"{message}\n({readable_intent})" if message else readable_intent


async def rag_retriever(state: ConversationState) -> dict[str, Any]:
    case_summary = None
    if state.get("intent") == "case_enquiry" and state.get("matched_case_id"):
        case_summary = await contact_service.get_case_summary(state["matched_case_id"])

    matches = await rag.search(_search_query(state))
    best = rag.best_similarity(matches)

    return {
        "rag_matches": matches,
        "rag_context": rag.format_context(matches),
        "rag_best_score": best,
        "case_summary": case_summary,
    }
