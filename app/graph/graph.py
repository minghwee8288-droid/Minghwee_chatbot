"""The LangGraph conversation engine: nodes, routing and checkpointing."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.config import settings
from app.graph.nodes.blocked_topic_responder import asks_general_info, blocked_topic_responder
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
    KB_QUESTION_INTENTS,
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

    # An acknowledgement or a sign-off. Nothing downstream should run: no
    # retrieval, no collection question, no ticket, no reply. Checked before the
    # blocked-topic branch so "ok" on a parked topic is silence rather than yet
    # another "still checking on that for you".
    if state.get("suppress_reply"):
        return END

    # This turn's topic already has a human working it — acknowledge, do not
    # re-answer, re-collect or re-escalate. Checked first: whatever else the
    # classifier made of this message, a parked topic is parked.
    #
    # Parked, but not deaf. A general question we can answer from our own
    # records still gets answered: it goes through retrieval and comes back to
    # the same responder, which then has something to say beyond "still checking
    # on that". Everything else — a chase, a correction, a new detail — takes
    # the direct route as before.
    # asks_general_info covers the questions the classifier labels with a SERVICE
    # name rather than a question intent — "how long does passport renewal take?"
    # comes back as intent 'passport_renewal', which is not in
    # KB_QUESTION_INTENTS, so it used to skip retrieval entirely and be answered
    # "a live agent is handling it" while the row that answers it sat in the
    # knowledge base at 0.814 similarity (live, twice, 2026-09-03). Retrieval
    # runs; route_after_rag sends it back to the same responder, which can now
    # actually answer it. A chase ("any update?") is excluded and still parks.
    if _blocked_topic(state):
        answerable_question = intent in KB_QUESTION_INTENTS or asks_general_info(
            state.get("incoming_text") or ""
        )
        return "rag_retriever" if answerable_question else "blocked_topic_responder"

    # Assault escalates immediately — no retrieval, no questions. Topic-scoped
    # like everything else once a ticket exists (the check above catches a
    # repeat), but the first report always gets the full safety response.
    if intent == "dispute_assault":
        return "handover_executor"

    # Everything else goes through the knowledge base first, including the
    # flows that then go on to collect. Retrieval used to be skipped entirely
    # for anything with a service_type, which meant a fee, salary or document
    # question never touched cb_knowledge_base_updated — the collector answered "let me
    # pull the details together" from a prompt that had no records in it, on
    # exactly the questions the knowledge base exists to answer. route_after_rag
    # makes the collect-or-answer decision afterwards, on the same rules.
    return "rag_retriever"


def route_after_rag(state: ConversationState) -> str:
    """Collect the service's details, or answer the question outright."""
    intent = state.get("intent") or "other"

    # A parked topic that came here for retrieval goes back to its own
    # responder, which is the only node that logs the follow-up onto the ticket
    # and the only one that will not re-escalate a topic a human already owns.
    if _blocked_topic(state):
        return "blocked_topic_responder"

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

    return "response_generator"


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
        # Nothing collectable — answer it instead. Retrieval has already run
        # for this turn, so this goes straight to the responder.
        return "response_generator"
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
            "handover_executor": "handover_executor",
            "blocked_topic_responder": "blocked_topic_responder",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "rag_retriever",
        route_after_rag,
        {
            "info_collector": "info_collector",
            "response_generator": "response_generator",
            "blocked_topic_responder": "blocked_topic_responder",
        },
    )
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
        {
            "ticket_creator": "ticket_creator",
            "response_generator": "response_generator",
            END: END,
        },
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

    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from app.graph.checkpointer import CbAsyncPostgresSaver

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
    checkpointer = CbAsyncPostgresSaver(_pool)
    await checkpointer.setup()
    logger.info("LangGraph Postgres checkpointer ready (cb_checkpoint* tables)")
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
#
# ticket_id/ticket_number belong here now that a single conversation can raise
# more than one ticket (topic-scoping). Left out, LangGraph's checkpoint keeps
# whichever value was last written forever — so once ANY ticket existed on the
# thread, every later escalation for a genuinely different topic saw a "truthy"
# ticket_id from turns ago and treated it as already handled:
# ticket_creator's own guard (state.get("ticket_id")) returned {} without ever
# creating anything, route_after_response skipped ticket_creator entirely, and
# handover_executor's assault-ticket guard would have skipped raising a NEW
# assault ticket for a client who had any earlier ticket on the conversation —
# reported live: a passport_renewal enquiry that completed collection normally
# but got silently logged against an unrelated new_hiring ticket from earlier
# in the same conversation, with no ticket ever created for it at all.
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
    "ticket_id": None,
    "ticket_number": None,
    "suppress_reply": False,
}


async def run_turn(thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one conversation turn and return the resulting state."""
    graph = get_graph()
    state_input = {**_TURN_RESET, **payload}
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(state_input, config=config)
    return dict(result)
