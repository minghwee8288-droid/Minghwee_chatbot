"""The LangGraph conversation engine: nodes, routing and checkpointing."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.config import settings
from app.graph import guards
from app.graph import language
from app.services import lead as lead_service
from app.graph.guards import answering_our_question, without_tag_before_possessive
from app.graph.nodes.blocked_topic_responder import (
    PROBE_REPLY,
    asks_general_info,
    blocked_topic_responder,
)
from app.graph.nodes.action_clarifier import action_clarifier
from app.graph.nodes.handover_executor import handover_executor
from app.graph.nodes.info_collector import (
    asks_for_service_again,
    enquiry_overview_due,
    finished_collection,
    info_collector,
)
from app.graph.nodes.intent_classifier import intent_classifier
from app.graph.nodes.rag_retriever import _search_query, rag_retriever
from app.graph.nodes.response_generator import completed_request, response_generator
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
from app.services import kb_rules
from app.services import rag
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

    # A vague action request, or the answer to our question about one
    # (app/graph/clarify.py). Before the parked-topic check: "please send the
    # video here" on a thread with an open ticket is not about that ticket
    # until the client says so (conversation 4443, 2026-10-28).
    if state.get("clarify_action") or state.get("action_answer"):
        return "action_clarifier"

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


def _other_service_established(state: ConversationState) -> bool:
    """Whether a real service is already in hand when a money question arrives.

    "Real" deliberately excludes the money enquiries themselves: a client whose
    whole conversation IS a fee question still gets fee_enquiry's two questions,
    which is what makes an opening "how much do you charge?" collectible at all.
    """
    collected = state.get("collected_service")
    if collected and collected not in ENQUIRY_INTENTS:
        return True
    service = state.get("service_type")
    if service and service not in ENQUIRY_INTENTS and ticket_service.fields_for(service):
        return True
    return any(key not in ENQUIRY_INTENTS for key in (state.get("blocked_topics") or {}))


def route_after_rag(state: ConversationState) -> str:
    """Collect the service's details, or answer the question outright."""
    intent = state.get("intent") or "other"

    # A parked topic that came here for retrieval goes back to its own
    # responder, which is the only node that logs the follow-up onto the ticket
    # and the only one that will not re-escalate a topic a human already owns.
    if _blocked_topic(state):
        return "blocked_topic_responder"

    # A question about hiring a helper asked in the middle of something else
    # (HIRE_IS_NEW_HIRE in intent_classifier): answered, not collected.
    if state.get("answer_only"):
        return "response_generator"

    # "Any update on Yoyo's home leave?" about a request our team has already
    # completed is answered as completed - never re-collected into a duplicate
    # ticket (conversation 3766, 2026-10-08). See completed_request.
    if completed_request(state):
        return "response_generator"

    # The answers still in memory became a ticket our team has since completed.
    # Only a new request for that service reopens the collection (which then
    # starts clean); anything else - "tell me about your agency", "what is
    # Choral's nationality", "hello" - is answered. Live, conversation 3766,
    # 2026-10-08, both of the first two re-filed the finished request as a
    # new ticket (CB-2026-0028, 0030). See finished_collection.
    if finished_collection(state) and not (
        intent in SERVICE_INTENTS and asks_for_service_again(state)
    ):
        return "response_generator"

    # "Tell me about direct hire - the process and the costs" opens a service.
    # The collector answers it in full and THEN asks its first question, even
    # when the classifier called the turn a fee question - the money branch
    # below would answer it in two sentences and stop, which on conversation
    # 26 (2026-10-07) left the client with no timeline, no costs worth the
    # name and nothing to reply to. See enquiry_overview_due.
    if enquiry_overview_due(state):
        return "info_collector"

    # A money question asked ON TOP of a service we are already handling is a
    # QUESTION, not a second job to qualify. fee_enquiry and salary_enquiry are
    # real services with their own two fields (nationality, care_type), so
    # sending them to the collector starts a competing intake inside somebody
    # else's flow. Live, 2026-09-07, one afternoon of testing produced both
    # halves of the damage:
    #   - mid passport renewal, "Ok and what is the cost" came back "I'll
    #     confirm the exact cost and come back to you. What kind of care would
    #     this be for?" - care_type, put to a man renewing a passport. He asked
    #     "Care??", the field was still empty so it asked a SECOND time, and he
    #     wrote "But I come here for passport renewal not for care".
    #   - after a finished hiring intake, "any approximate salary range" came
    #     back "For an Indonesian helper, the approximate salary is $550 to
    #     $600 ... Which nationality are you looking at?" - salary_enquiry's own
    #     `nationality`, asked in the same sentence as the answer that used it,
    #     because the hiring flow had stored it as `preferred_nationality`.
    #
    # An agent asked the price mid-job answers the price. Only a money question
    # that opens a conversation qualifies anything.
    # ... but only when it really is a QUESTION. The classifier misreads a bare
    # answer often enough to matter - live, "3-4" (the client's answer to our
    # own household-size question) came back as a money intent, this branch
    # diverted it to response_generator, and response_generator's "nothing
    # asked yet" path replied "Could you share what your question is about?".
    # The stickiness rule in intent_classifier had already corrected
    # service_type back to the live service; it does not touch `intent`, which
    # is what this branch reads, so the correction never reached the routing.
    #
    # An answer to a question we just asked belongs to the collection that
    # asked it, whatever label the turn was given.
    if (
        intent in ENQUIRY_INTENTS
        and _other_service_established(state)
        and not answering_our_question(
            state.get("history_text") or "", state.get("incoming_text") or ""
        )
    ):
        return "response_generator"

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


# Step 1d: the search embedding starts at the same moment as the classifier.
# Its query is built from the turn as it stands before classification - the
# intent and service carry over from the previous turn - and the retriever only
# reuses it if the query it builds afterwards is the same text. If the
# classifier changed the service (or anything else the query reads), the
# prefetched vector is simply not used and the real query is embedded as
# before. False = the classifier runs alone, as before.
PREFETCH_SEARCH_EMBEDDING = True


async def _classify_with_prefetch(state: ConversationState) -> dict[str, Any]:
    if PREFETCH_SEARCH_EMBEDDING and state.get("intent"):
        try:
            rag.prefetch_query(_search_query(state))
        except Exception:  # noqa: BLE001 - a prefetch must never cost a turn
            logger.exception("Could not predict the search query")
    # Step 6b: an answer to our own question is extracted beside the classifier.
    from app.graph.nodes.info_collector import start_speculative_extraction
    start_speculative_extraction(state)
    return await intent_classifier(state)


# --- Construction ----------------------------------------------------------

def build_graph(checkpointer: Any = None):
    builder = StateGraph(ConversationState)

    builder.add_node("intent_classifier", _classify_with_prefetch)
    builder.add_node("rag_retriever", rag_retriever)
    builder.add_node("response_generator", response_generator)
    builder.add_node("info_collector", info_collector)
    builder.add_node("ticket_creator", ticket_creator)
    builder.add_node("handover_executor", handover_executor)
    builder.add_node("blocked_topic_responder", blocked_topic_responder)
    builder.add_node("action_clarifier", action_clarifier)

    builder.add_edge(START, "intent_classifier")
    builder.add_conditional_edges(
        "intent_classifier",
        route_after_intent,
        {
            "rag_retriever": "rag_retriever",
            "handover_executor": "handover_executor",
            "blocked_topic_responder": "blocked_topic_responder",
            "action_clarifier": "action_clarifier",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "action_clarifier",
        route_after_response,
        {
            "handover_executor": "handover_executor",
            "ticket_creator": "ticket_creator",
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

    # Every connection is tested before it is handed out, and a dead one is
    # replaced instead of being used. Live, conversation 3766, 2026-10-08: the
    # thread sat idle for 28 minutes, the server terminated the pooled
    # connection ("terminating connection due to administrator command"), the
    # next turn's checkpoint read got that dead connection, the graph raised,
    # and the failover silenced the bot on a client who had only said "hey
    # there". Reproduced by pg_terminate_backend on a pooled connection: the
    # same error without the check, a normal query with it. One "SELECT 1" per
    # checkout is the cost.
    check = getattr(AsyncConnectionPool, "check_connection", None)
    if check is None:
        logger.error("psycopg_pool has no check_connection (needs >= 3.2) - dead pooled connections will not be replaced")
    _pool = AsyncConnectionPool(
        conninfo=settings.supabase_db_url,
        min_size=1,
        max_size=10,
        open=False,
        check=check,
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
    "clarify_action": None,
    "action_answer": False,
    "action_summary": None,
    "answer_only": False,
}


# Step 2c: "Devi (MDW)'s home leave" is sent as "Devi's home leave".
# False = the reply goes out exactly as the node wrote it.
NO_TAG_BEFORE_POSSESSIVE = True

# 2026-10-23: a collector question older than settings.intake_stale_hours is
# not answered by the next message. The collection is dropped before the turn,
# so the message is read as a fresh one (the welcome-back rule still applies).
# Live: a client's "Okay." to a staff message was taken as the answer to an
# old intake question and the bot asked the next one. False = the collection
# resumes however old it is.
STALE_INTAKE_RESET = True


def _as_utc(value: Any):
    from datetime import datetime, timezone

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def stale_intake_reset(
    values: dict[str, Any], last_turn_at: Any, now: Any = None, human_since: Any = None
) -> dict[str, Any]:
    """The state to apply before a turn when the pending question has gone stale.

    `values` is the checkpoint's state and `last_turn_at` when it was written
    (the bot's last turn on this thread). Stale = older than intake_stale_hours,
    or a human was on the chat after it (`human_since`, the agreed handover
    rule: after a human pause the next message is a new conversation). Empty
    when nothing is pending, when the time is unknown, or when it is fresh.
    """
    from datetime import datetime, timezone

    from app.graph.state import RESET_KEY

    if not STALE_INTAKE_RESET or settings.intake_stale_hours <= 0:
        return {}
    if not (values or {}).get("last_question"):
        return {}
    if isinstance(last_turn_at, str):
        try:
            last_turn_at = datetime.fromisoformat(last_turn_at.replace("Z", "+00:00"))
        except ValueError:
            return {}
    if not isinstance(last_turn_at, datetime):
        return {}
    if last_turn_at.tzinfo is None:
        last_turn_at = last_turn_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    human_since = _as_utc(human_since)
    after_human = human_since is not None and human_since > last_turn_at
    if not after_human and (now - last_turn_at).total_seconds() < settings.intake_stale_hours * 3600:
        return {}
    return {
        "collected_info": {RESET_KEY: True},
        "asked_field_counts": {RESET_KEY: True},
        "service_type": None,
        "intent": "",
        "collected_service": None,
        "collected_since": None,
        "last_question": None,
        "missing_field_keys": [],
    }


async def run_turn(thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one conversation turn and return the resulting state."""
    # The pricing rules are read synchronously by the nodes, so they are
    # refreshed here, before the turn, and never inside it. A no-op with
    # RULES_FROM_DB off, and at most one read a minute with it on.
    await kb_rules.refresh()
    graph = get_graph()
    payload = dict(payload)
    human_since = payload.pop("last_human_at", None)
    state_input = {**_TURN_RESET, **payload}
    config = {"configurable": {"thread_id": thread_id}}
    if STALE_INTAKE_RESET and hasattr(graph, "aget_state"):
        try:
            snapshot = await graph.aget_state(config)
            reset = stale_intake_reset(
                dict(snapshot.values or {}), snapshot.created_at, human_since=human_since
            )
        except Exception:  # noqa: BLE001 - a failed read must not cost the turn
            logger.exception("Could not read the checkpoint to check for a stale intake")
            reset = {}
        if reset:
            logger.info(
                "Conversation %s: the pending question is over %gh old or from before a "
                "human pause - dropping the collection and reading this message as a fresh one",
                payload.get("conversation_id"),
                settings.intake_stale_hours,
            )
            state_input = {**state_input, **reset}
    result = dict(await graph.ainvoke(state_input, config=config))
    # Step 2c: every node's reply passes here, so the tag is fixed once.
    if NO_TAG_BEFORE_POSSESSIVE and result.get("reply"):
        result["reply"] = without_tag_before_possessive(result["reply"])
    # 2026-10-28: three or more numbered items, one per line (WhatsApp layout).
    if guards.NUMBERED_ITEMS_ON_OWN_LINES and result.get("reply"):
        result["reply"] = guards.numbered_items_on_own_lines(result["reply"])
    # Step 5 item 15: stray-script glitches are cut once, here, for the same
    # reason - every node's reply (and every fixed sentence added to it) has
    # been written by now.
    if guards.STRAY_SCRIPT_GUARD and result.get("reply"):
        result["reply"] = guards.without_stray_script(
            result["reply"], *stray_script_context(payload)
        )
    # Step 5 item 9: "as this is your first helper" only where they said so.
    if guards.FIRST_HELPER_CONDITIONAL and result.get("reply"):
        result["reply"] = guards.without_assumed_first_helper(
            result["reply"], *stray_script_context(payload)
        )
    # Step 5 item 17, every service: no "approximate" cost without a figure.
    if guards.NO_APPROXIMATE_COST_ANYWHERE and result.get("reply"):
        result["reply"] = guards.without_figureless_approximate(result["reply"])
    # Step 5 Batch B2 item 2: a fee the records state exactly is stated plainly.
    if guards.FIXED_FEE_STATED_PLAINLY and result.get("reply"):
        result["reply"] = guards.state_fixed_fee_plainly(result["reply"], records_text(result))
    # Fees document 2026-10-15 decision E: an overseas direct hire is counted
    # after MOM approval, and the reply says so.
    if result.get("reply") and result.get("service_type") == "direct_hiring":
        result["reply"] = guards.with_after_mom_approval(
            result["reply"], lead_service.nationality_code(str(
                (result.get("collected_info") or {}).get("helper_nationality") or "")))
    # Step 5 Batch E item 2: "why this much?" never gets the price back, on any path.
    if (guards.WHY_THE_PRICE_EVERY_PATH and result.get("reply")
            and guards.asks_why_the_price(payload.get("incoming_text") or "")):
        result["reply"] = guards.price_probe_reply(
            result["reply"], payload.get("history_text") or "", PROBE_REPLY
        )
    # Step 5 Batch B2 item 4: "DEVI's" -> "Devi's" for the helpers we know.
    if guards.TIDY_HELPER_NAME_CASE and result.get("reply"):
        result["reply"] = guards.tidy_helper_names_in_reply(
            result["reply"], helper_names(result, payload)
        )
    # Step 5 Batch B2 item 6: no "Hi, I'm Claire" once she has spoken.
    if guards.NO_REINTRODUCTION and result.get("reply"):
        result["reply"] = guards.without_reintroduction(
            result["reply"], payload.get("history_text") or "", payload.get("incoming_text") or ""
        )
    # Step 5 Batch C item 12: the urgent-request fee only when they asked for urgent.
    if (guards.CONDITIONAL_FEE_ONLY_IF_ASKED and result.get("reply")
            and result.get("service_type") == "home_leave"):
        result["reply"] = guards.without_unasked_conditional_fee(
            result["reply"], *stray_script_context(payload)[:-2]
        )
    # Step 5 Batch B2 item 5: a family matter or an emergency gets a caring line.
    if (guards.CARING_LINE_FOR_FAMILY_MATTERS and result.get("reply")
            and result.get("service_type") == "home_leave"):
        result["reply"] = guards.with_caring_line(
            result["reply"], payload.get("incoming_text") or "",
            (result.get("collected_info") or {}).get("leave_reason"),
        )
    # Step 5 Batch F item 5: no Malay-only word in a reply to Bahasa Indonesia.
    if (language.INDONESIAN_NOT_MALAY and result.get("reply")
            and language.client_writes_indonesian(
                payload.get("incoming_text") or "", payload.get("history_text") or "")):
        result["reply"] = language.to_indonesian(result["reply"])
    # Batch G G4: never an internal case number (the CB reference stays).
    if guards.NO_CASE_NUMBER_READ_OUT and result.get("reply"):
        result["reply"] = guards.without_case_numbers(
            result["reply"], guards.case_numbers_held(payload, result)
        )
    # Step 5 Batch F item 6: a reference in backticks is not a phone link.
    from app.graph import reference as _reference
    if _reference.REFERENCE_AS_CODE and result.get("reply"):
        result["reply"] = guards.references_as_code(result["reply"])
    return result


def helper_names(result: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """Every helper name this turn knows: the one being collected, the ones
    named earlier in the conversation, and the ones on file."""
    names = [(result.get("collected_info") or {}).get("helper_name")]
    names += list(result.get("helpers_named") or payload.get("helpers_named") or [])
    names += [h.get("helper_name") for h in (payload.get("known_helpers") or [])
              if isinstance(h, dict)]
    return [str(n) for n in names if n]


def records_text(result: dict[str, Any]) -> str:
    """The retrieved records the turn's reply was written from, as plain text."""
    rows = result.get("rag_matches") or []
    return "\n".join(
        [result.get("rag_context") or ""]
        + [str(row.get("answer") or row.get("content") or "") for row in rows if isinstance(row, dict)]
    )


def stray_script_context(payload: dict[str, Any]) -> list[str]:
    """What the client wrote and the names we hold for them: scripts in these
    are never cut from a reply."""
    return [
        payload.get("incoming_text") or "",
        *guards.client_lines(payload.get("history_text") or ""),
        payload.get("customer_name") or "",
        payload.get("record_name") or "",
    ]
