"""The LangGraph conversation engine: nodes, routing and checkpointing."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.config import settings
from app.graph.nodes.blocked_topic_responder import blocked_topic_responder
from app.graph.nodes.handover_executor import handover_executor
from app.graph.nodes.info_collector import info_collector
from app.graph.nodes.intent_classifier import intent_classifier
from app.graph.nodes.rag_retriever import rag_retriever
from app.graph.nodes.response_generator import response_generator
from app.graph.nodes.ticket_creator import ticket_creator
from app.graph.state import (
    CANDIDATE_INTENT,
    DISPUTE_INTENTS,
    ENQUIRY_INTENTS,
    SERVICE_INTENTS,
    ConversationState,
    effective_contact_type,
)
from app.services import ticket as ticket_service

logger = logging.getLogger(__name__)

_checkpointer: Any = None
_pool: Any = None
_graph: Any = None


# --- Routing ---------------------------------------------------------------

def _blocked_topic(state: ConversationState) -> str | None:
    """The key of this turn's topic, if a ticket already parked it.

    Computed the same way ticket_creator's topic_key ends up stored (see
    topic_key_for) — resolve_service applied first, so a candidate's
    new_hiring flow and an employer's do not collide on the same key, and a
    ticket raised before the service resolved (weak retrieval, no
    service_type at all) still matches on intent.
    """
    key = ticket_service.topic_key_for(
        state.get("service_type"), effective_contact_type(state), state.get("intent")
    )
    return key if key and key in (state.get("blocked_topics") or {}) else None


def route_after_intent(state: ConversationState) -> str:
    intent = state.get("intent") or "other"

    # This turn's topic already has a human working it — acknowledge, do not
    # re-answer, re-collect or re-escalate. Checked first: whatever else the
    # classifier made of this message, a parked topic is parked.
    if _blocked_topic(state):
        return "blocked_topic_responder"

    # Assault escalates immediately — no retrieval, no questions. Topic-scoped
    # like everything else once a ticket exists (the check above catches a
    # repeat), but the first report always gets the full safety response.
    if intent == "dispute_assault":
        return "handover_executor"

    if (
        intent in SERVICE_INTENTS
        or intent in ENQUIRY_INTENTS
        or intent in DISPUTE_INTENTS
        or intent == CANDIDATE_INTENT
    ):
        return "info_collector"

    # A collection already in progress keeps going, whatever this message
    # classified as. The client answering "Philippines" is not a new topic, but
    # the classifier has nothing to call it and returns 'other' — and where the
    # in-flight service is a derived one (candidate_new_hiring is a flow, not an
    # intent name) the classifier cannot promote it back to an intent either, so
    # the answer would go to the knowledge base and the questions start again.
    if ticket_service.fields_for(state.get("service_type")):
        return "info_collector"

    # Informational, case enquiries and anything unclassified go through the KB.
    return "rag_retriever"


def route_after_response(state: ConversationState) -> str:
    if not state.get("needs_handover"):
        return END
    # Every stand-down now needs a ticket — it is the thing a follow-up on
    # this topic gets matched and blocked against, and the only record of
    # what a human still needs to act on now that the bot itself never falls
    # silent for a whole conversation.
    if not state.get("ticket_id"):
        return "ticket_creator"
    return "handover_executor"


def route_after_collector(state: ConversationState) -> str:
    if not state.get("service_type"):
        # Nothing collectable — fall back to answering from the knowledge base.
        return "rag_retriever"
    return "ticket_creator" if state.get("info_complete") else END


# --- Construction ----------------------------------------------------------

def build_graph(checkpointer: Any = None):
    builder = StateGraph(ConversationState)

    builder.add_node("intent_classifier", intent_classifier)
    builder.add_node("rag_retriever", rag_retriever)
    builder.add_node("response_generator", response_generator)
    builder.add_node("info_collector", info_collector)
    builder.add_node("ticket_creator", ticket_creator)
    builder.add_node("handover_executor", handover_executor)
    builder.add_node("blocked_topic_responder", blocked_topic_responder)

    builder.add_edge(START, "intent_classifier")
    builder.add_conditional_edges(
        "intent_classifier",
        route_after_intent,
        {
            "rag_retriever": "rag_retriever",
            "info_collector": "info_collector",
            "handover_executor": "handover_executor",
            "blocked_topic_responder": "blocked_topic_responder",
        },
    )
    builder.add_edge("rag_retriever", "response_generator")
    builder.add_conditional_edges(
        "response_generator",
        route_after_response,
        {
            "handover_executor": "handover_executor",
            "ticket_creator": "ticket_creator",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "info_collector",
        route_after_collector,
        {"ticket_creator": "ticket_creator", "rag_retriever": "rag_retriever", END: END},
    )
    builder.add_edge("ticket_creator", "handover_executor")
    builder.add_edge("handover_executor", END)
    builder.add_edge("blocked_topic_responder", END)

    return builder.compile(checkpointer=checkpointer)


async def _build_checkpointer() -> Any:
    """Postgres checkpointer when a connection string is configured."""
    global _pool

    if not settings.supabase_db_url:
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning(
            "SUPABASE_DB_URL is not set — using an in-memory checkpointer. "
            "Conversation state will be lost on restart."
        )
        return MemorySaver()

    if sys.platform == "win32" and isinstance(
        asyncio.get_running_loop(), asyncio.ProactorEventLoop
    ):
        # psycopg refuses to run async on the Proactor loop, and would otherwise
        # spend 15s retrying before falling back. Fail fast with the actual fix.
        from langgraph.checkpoint.memory import MemorySaver

        logger.error(
            "Running on Windows with the ProactorEventLoop — psycopg cannot connect "
            "asynchronously, so conversation state will not persist. Start the app "
            "with `python run.py` instead of calling uvicorn directly."
        )
        return MemorySaver()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    _pool = AsyncConnectionPool(
        conninfo=settings.supabase_db_url,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={
            "autocommit": True,
            # Supabase's transaction pooler cannot handle prepared statements.
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
    await _pool.open(wait=True, timeout=15)
    checkpointer = AsyncPostgresSaver(_pool)
    await checkpointer.setup()
    logger.info("LangGraph Postgres checkpointer ready")
    return checkpointer


async def init_graph() -> Any:
    """Compile the graph once at application startup."""
    global _graph, _checkpointer

    if _graph is not None:
        return _graph
    try:
        _checkpointer = await _build_checkpointer()
    except Exception:  # noqa: BLE001 - the bot still works without persistence
        from langgraph.checkpoint.memory import MemorySaver

        logger.exception("Postgres checkpointer unavailable — falling back to memory")
        _checkpointer = MemorySaver()

    _graph = build_graph(_checkpointer)
    logger.info("Conversation graph compiled")
    return _graph


def get_graph() -> Any:
    if _graph is None:
        raise RuntimeError("Conversation graph is not initialised — call init_graph() first")
    return _graph


async def close_graph() -> None:
    global _graph, _checkpointer, _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
    _graph = None
    _checkpointer = None


# --- Execution -------------------------------------------------------------

# Per-turn fields are reset on every invocation so nothing leaks between turns.
_TURN_RESET: dict[str, Any] = {
    "reply": "",
    "needs_handover": False,
    "handover_reason": None,
    "handover_done": False,
    "info_complete": False,
    "rag_matches": [],
    "rag_context": "",
    "rag_best_score": 0.0,
    "case_summary": None,
    "media_items": [],
}


async def run_turn(thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one conversation turn and return the resulting state."""
    graph = get_graph()
    state_input = {**_TURN_RESET, **payload}
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(state_input, config=config)
    return dict(result)
