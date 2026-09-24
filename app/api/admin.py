"""POST /admin/preview - what would the bot say to this? Writes nothing.

For the KB Admin UI's "test a question" panel: runs the REAL conversation graph
(same nodes, same prompts, same guards, same retrieval) on one or more test
messages and returns the replies, the intent and service each turn resolved
to, and the knowledge-base rows retrieval used. That is the only honest answer
to "did my edit work?" - copying the routing and guards into the UI would be
§9.8's duplication problem, and a copy drifts.

It must leave no trace, and three things make sure of that:

1. The whole request runs inside `readonly.read_only()`: every database write
   and every RPC except the knowledge-base search is refused and listed in the
   response under `refused_writes`, and any WhatsApp request raises.
2. The graph is compiled per request with an in-memory checkpointer on a
   throwaway thread id, so no cb_checkpoint* row is written or read.
3. No conversation row is looked up or created. The payload is built here from
   the request, never from a real number.

Known and accepted: a turn that COMPLETES a collection would create a ticket
and a lead. Both inserts are refused and listed, so the reply on that turn is
what the bot says when those inserts come back empty - usually identical, but
not guaranteed identical. The response says so via `refused_writes`.

The reply is the graph's reply. The webhook's own post-processing after the
graph (the 90s repeat guard, splitting, sending) is not run, because it is
about delivery rather than content.

Protected by its own secret (ADMIN_PREVIEW_SECRET, header X-Admin-Preview-Key),
compared in constant time. With no secret configured the route answers 404,
the same as a path that does not exist.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import readonly
from app.config import settings
from app.services import kb_rules

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_TURNS = 10
MAX_MESSAGE_CHARS = 2000
MAX_SOURCES = 5


class PreviewRequest(BaseModel):
    # One message, or a short conversation played turn by turn on one thread.
    messages: list[str] = Field(..., min_length=1, max_length=MAX_TURNS)
    contact_type: Literal["employer", "candidate", "unknown"] = "unknown"
    customer_name: str = ""
    # Record-derived context a real turn would carry, e.g. {"prior_hires": 2}
    # or {"record_name": "Ratna"}. Only these keys are accepted, so a preview
    # cannot claim a real conversation or employer id.
    context: dict[str, Any] = Field(default_factory=dict)


_CONTEXT_KEYS = frozenset({
    "prior_hires", "placed_helper", "record_name", "blocked_topics",
    "recent_tickets", "matched_cases",
})


def _check_key(given: str | None) -> None:
    secret = settings.admin_preview_secret
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")
    if not given or not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="bad or missing X-Admin-Preview-Key")


def _sources(matches: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for row in (matches or [])[:MAX_SOURCES]:
        out.append({
            "id": row.get("id"),
            "service_type": row.get("service_type"),
            "nationality": row.get("nationality"),
            "question": row.get("question") or (row.get("section_heading") or ""),
            "similarity": round(float(row.get("similarity") or 0.0), 4),
        })
    return out


@router.post("/admin/preview")
async def preview(
    body: PreviewRequest,
    x_admin_preview_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_key(x_admin_preview_key)
    for text in body.messages:
        if not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"each message must be 1-{MAX_MESSAGE_CHARS} characters",
            )
    unknown = set(body.context) - _CONTEXT_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown context keys: {sorted(unknown)}")

    # Imported here: the graph pulls in every node, and the app must still
    # start (and /admin/preview still 404) with the graph unbuilt.
    from langgraph.checkpoint.memory import MemorySaver

    from app.graph.graph import _TURN_RESET, build_graph

    await kb_rules.refresh()
    graph = build_graph(MemorySaver())
    thread_id = f"preview-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}
    history: list[str] = []
    turns: list[dict[str, Any]] = []

    with readonly.read_only() as refused:
        for text in body.messages:
            payload = {
                "conversation_id": 0,
                "phone": "",
                "customer_name": body.customer_name,
                "thread_id": thread_id,
                "contact_type": body.contact_type,
                "matched_employer_id": None,
                "matched_candidate_id": None,
                "matched_supplier_id": None,
                "matched_case_id": None,
                "matched_cases": [],
                "prior_hires": 0,
                "placed_helper": None,
                "record_name": "",
                "recent_tickets": [],
                "matched_lead_id": None,
                "matched_lead_number": None,
                "lead_kind": None,
                "matched_lead": None,
                "salesperson_profile_id": None,
                "blocked_topics": [],
                "incoming_text": text,
                # The same "Client:" / "You:" rendering message.format_history
                # produces - written any other way, last_bot_line finds nothing
                # and several guards switch off silently (2026-09-18 L).
                "history_text": "\n".join(history),
                "media_items": [],
                **body.context,
            }
            try:
                result = dict(await graph.ainvoke({**_TURN_RESET, **payload}, config=config))
            except readonly.ReadOnlyViolation as exc:
                logger.error("preview: %s", exc)
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            reply = "" if result.get("suppress_reply") else (result.get("reply") or "")
            turns.append({
                "message": text,
                "reply": reply,
                "intent": result.get("intent"),
                "service_type": result.get("service_type"),
                "needs_handover": bool(result.get("needs_handover")),
                "rag_best_score": round(float(result.get("rag_best_score") or 0.0), 4),
                "sources": _sources(result.get("rag_matches")),
                "collected_info": result.get("collected_info") or {},
            })
            history.append(f"Client: {text}")
            if reply:
                history.append(f"You: {reply}")
        refused_writes = list(refused)

    return {
        "turns": turns,
        "refused_writes": refused_writes,
        "rules_source": kb_rules.source(),
    }
