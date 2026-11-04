"""Go-live guard: replay the two 2026-10-23 production failures, permanently.

1. The bot answered inside a staff conversation. A staff member wrote to a
   client from the agency number; the client answered "Afternoon" / "Okay."
   (quoting the staff message) half an hour later, and the bot asked an old
   intake question.
2. A helper asking her rights (CB-2026-0124) was asked "are you asking about
   your rights as a domestic helper?", said "yes", and was handed to a person.

Every scenario runs the REAL webhook, graph helpers and nodes, with the
database, Whapi and the model stubbed. Nothing touches the network. Phone
numbers are fake (+65 9000 xxxx).

    python scripts/selfcheck_golive.py

Exit 1 on any failure.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import webhook as _wh  # noqa: E402
from app.graph import graph as _graph  # noqa: E402
from app.graph import guards  # noqa: E402
from app.graph.helper_rights import HELPER_RIGHTS_MESSAGE  # noqa: E402
from app.graph.nodes import intent_classifier as _ic_mod  # noqa: E402
from app.graph.state import RESET_KEY  # noqa: E402
from app.services import conversation as _conv  # noqa: E402
from app.services import internal_contacts  # noqa: E402
from app.services import message as _msg  # noqa: E402
from app.services import ticket as ticket_service  # noqa: E402
from app.whapi.parser import IncomingMessage  # noqa: E402

import importlib  # noqa: E402

import re  # noqa: E402
from datetime import date  # noqa: E402

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import settings  # noqa: E402
from app.graph import complaint_templates as _ct_t  # noqa: E402
from app.graph import complaint_triage as _ct  # noqa: E402
from app.services import complaint_case as _ct_cc  # noqa: E402
_ct_tpl_path = _ct_t.__file__
_ct_node = importlib.import_module("app.graph.nodes.complaint_triage")
_ct_he = importlib.import_module("app.graph.nodes.handover_executor")

_ic = importlib.import_module("app.graph.nodes.intent_classifier")
_rag_node = importlib.import_module("app.graph.nodes.rag_retriever")
_rg = importlib.import_module("app.graph.nodes.response_generator")

CLIENT = "+6590000101"
STAFF = "+6590000199"
NOW = datetime.now(timezone.utc)

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, bool(ok)))


def message(body: str, *, mid: str, phone: str = CLIENT, from_me: bool = False,
            quoted: str | None = None) -> IncomingMessage:
    raw = {"id": mid}
    if quoted:
        raw["context"] = {"quoted_id": quoted}
    return IncomingMessage(
        whapi_message_id=mid, chat_id="", customer_number=phone, customer_name="",
        body=body, message_type="text", from_me=from_me, timestamp=NOW, raw=raw,
    )


class World:
    """One conversation row and what the webhook reads about it."""

    def __init__(self, status: str = _conv.BOT_ACTIVE, *, last_agent=None, ticket=False,
                 quoted_origin: str = "agent", closed_at=None) -> None:
        self.row = {"id": 26, "customer_number": CLIENT.lstrip("+"), "bot_status": status,
                    "status": "open", "langgraph_thread_id": "t-26", "last_bot_reply_at": None,
                    "updated_at": NOW.isoformat()}
        self.last_agent = last_agent
        self.ticket = ticket
        self.quoted_origin = quoted_origin
        self.closed_at = closed_at
        self.queued: list[str] = []
        self.takeovers = 0

    async def took_over(self, conversation, reason=None):
        self.takeovers += 1
        self.row["bot_status"] = _conv.HUMAN_ACTIVE

    async def resumed(self, conversation, reason=None):
        self.row["bot_status"] = _conv.BOT_ACTIVE

    async def get_or_create(self, phone, name="", *, engage=True):
        return self.row, False

    async def queue(self, m):
        self.queued.append(m.body)

    async def quoted(self, mid):
        return self.quoted_origin

    def patches(self, *, internal: str = ""):
        h = _wh.handover_service
        return [
            patch.object(_wh.settings, "bot_allowed_numbers", ""),
            patch.object(_wh.settings, "internal_numbers", internal),
            patch.object(internal_contacts, "_table_numbers", new=AsyncMock(return_value=set())),
            patch.object(_wh.conversation_service, "get_by_phone", new=AsyncMock(side_effect=lambda p: self.row)),
            patch.object(_wh.conversation_service, "get_by_id", new=AsyncMock(side_effect=lambda i: self.row)),
            patch.object(_wh.conversation_service, "get_or_create", new=self.get_or_create),
            patch.object(_wh.conversation_service, "touch_inbound", new=AsyncMock()),
            patch.object(_wh.conversation_service, "touch_outbound", new=AsyncMock()),
            patch.object(_wh.message_service, "last_agent_message_at",
                         new=AsyncMock(side_effect=lambda cid: self.last_agent)),
            patch.object(_wh.message_service, "store_incoming", new=AsyncMock()),
            patch.object(_wh.message_service, "store_agent_reply", new=AsyncMock()),
            patch.object(_wh.message_service, "was_sent_by_bot", new=AsyncMock(return_value=False)),
            patch.object(_wh.message_service, "echoes_our_own_send", new=lambda *a: False),
            patch.object(_wh.message_service, "is_auto_reply", new=AsyncMock(return_value=False)),
            patch.object(_wh.message_service, "quoted_message_origin", new=self.quoted),
            patch.object(h, "agent_took_over", new=self.took_over),
            patch.object(h, "resume_from_pause", new=self.resumed),
            patch.object(h, "latest_takeover_at", new=AsyncMock(return_value=None)),
            patch.object(_wh.ticket_service, "latest_closed_at",
                         new=AsyncMock(side_effect=lambda cid: self.closed_at)),
            patch.object(_wh.ticket_service, "picked_up_recently",
                         new=AsyncMock(side_effect=lambda cid, hours: self.ticket)),
            patch.object(_wh, "_schedule_read_receipt", new=lambda *a, **k: None),
            patch.object(_wh, "identify_contact", new=AsyncMock(side_effect=lambda c: c)),
            patch.object(_wh.debouncer, "add", new=self.queue),
            patch.object(_wh.debouncer, "flush_now", new=AsyncMock()),
            patch.object(_wh.typing_indicator, "stop", new=AsyncMock()),
            patch.object(_wh.typing_indicator, "enabled", new=lambda: False),
            patch.object(_wh, "emergency_override", new=AsyncMock(return_value=False)),
            patch.object(_wh, "acknowledge_direct_address", new=AsyncMock()),
        ]


async def inbound(world: World, m: IncomingMessage, **kw) -> None:
    ps = world.patches(**kw)
    for p in ps:
        p.start()
    try:
        await _wh.handle_inbound(m)
    finally:
        for p in reversed(ps):
            p.stop()


async def outbound(world: World, m: IncomingMessage) -> None:
    ps = world.patches()
    for p in ps:
        p.start()
    try:
        await _wh.handle_outbound(m)
        world.last_agent = NOW - timedelta(minutes=5)
    finally:
        for p in reversed(ps):
            p.stop()


async def scenario_staff_then_okay() -> None:
    # The agreed handover rule: a human message pauses the chat; the pause ends
    # after HUMAN_IDLE_MINUTES (30) with no human message, or at once when a
    # ticket is closed.
    world = World()
    await outbound(world, message("Good afternoon, require to purchase 4 insurance",
                                  mid="staff-1", from_me=True))
    check("staff message pauses the bot (human_active)", world.row["bot_status"] == _conv.HUMAN_ACTIVE)
    await inbound(world, message("Afternoon", mid="c-1"))
    world.last_agent = NOW - timedelta(minutes=20)
    await inbound(world, message("Okay.", mid="c-2"))
    check("human reply -> silent (client writes 5 and 20 min after it)", world.queued == [])
    # A second human message restarts the clock: 40 min after the first,
    # 10 after the second, still silent.
    world.last_agent = NOW - timedelta(minutes=10)
    await inbound(world, message("ok noted", mid="c-2b"))
    check("each new human message restarts the 30 min", world.queued == []
          and world.row["bot_status"] == _conv.HUMAN_ACTIVE)
    world.last_agent = NOW - timedelta(minutes=31)
    await inbound(world, message("Hi, I need a helper", mid="c-3"))
    check("30 min with no human message -> the bot answers", world.queued == ["Hi, I need a helper"])


async def scenario_ticket_closed() -> None:
    world = World(status=_conv.HUMAN_ACTIVE, last_agent=NOW - timedelta(minutes=5))
    await inbound(world, message("hello?", mid="tc-0"))
    check("paused 5 min after a human message, ticket open -> silent", world.queued == [])
    world.closed_at = NOW - timedelta(minutes=1)
    await inbound(world, message("Is it done?", mid="tc-1"))
    check("ticket closed in the portal -> the bot answers immediately", world.queued == ["Is it done?"])
    world = World(status=_conv.HUMAN_ACTIVE, last_agent=NOW - timedelta(minutes=2),
                  closed_at=NOW - timedelta(minutes=9))
    await inbound(world, message("hello?", mid="tc-2"))
    check("...a ticket closed BEFORE the last human message does not end the pause", world.queued == [])


async def scenario_internal_number() -> None:
    world = World()
    world.row["customer_number"] = STAFF.lstrip("+")
    await inbound(world, message("Hi I need a helper", mid="i-1", phone=STAFF), internal=STAFF)
    check("an internal (staff) number is never answered", world.queued == [])


async def scenario_quoted_reply() -> None:
    world = World(quoted_origin="agent")
    await inbound(world, message("Okay.", mid="q-1", quoted="staff-9"))
    check("quoted reply to a non-bot message -> silent", world.queued == [])
    check("...and it starts the pause", world.takeovers == 1 and world.row["bot_status"] == _conv.HUMAN_ACTIVE)
    world = World(quoted_origin="bot")
    await inbound(world, message("yes, Filipino", mid="q-2", quoted="bot-1"))
    check("quoting the bot's own message is an ordinary turn", world.queued == ["yes, Filipino"])


async def scenario_ticket_picked_up() -> None:
    world = World(ticket=True)
    await inbound(world, message("any update?", mid="t-1"))
    check("a ticket an agent picked up keeps the bot quiet", world.queued == [])


async def scenario_stale_intake() -> None:
    pending = {"last_question": {"key": "home_type", "text": "What type of home are you in?"},
               "service_type": "new_hiring", "collected_info": {"full_name": "Thomas"}}
    reset = _graph.stale_intake_reset(pending, NOW - timedelta(hours=7))
    check("an intake question 7h old is dropped", bool(reset)
          and reset.get("collected_info", {}).get(RESET_KEY) is True
          and reset.get("last_question") is None and reset.get("service_type") is None)
    check("...one asked 1h ago is kept", _graph.stale_intake_reset(pending, NOW - timedelta(hours=1)) == {})
    check("a question pending from before a human pause is dropped after it",
          bool(_graph.stale_intake_reset(pending, NOW - timedelta(hours=1), human_since=NOW - timedelta(minutes=40))))
    check("...a human message before that question does not drop it",
          _graph.stale_intake_reset(pending, NOW - timedelta(hours=1), human_since=NOW - timedelta(hours=2)) == {})
    check("...and nothing pending is never reset",
          _graph.stale_intake_reset({"service_type": "new_hiring"}, NOW - timedelta(hours=9)) == {})

    # run_turn applies it before the graph runs.
    seen: dict = {}

    class FakeGraph:
        async def aget_state(self, config):
            return SimpleNamespace(values=pending, created_at=(NOW - timedelta(hours=7)).isoformat())

        async def ainvoke(self, state_input, config=None):
            seen.update(state_input)
            return {"reply": ""}

    with patch.object(_graph, "get_graph", new=lambda: FakeGraph()), \
         patch.object(_graph.kb_rules, "refresh", new=AsyncMock()):
        await _graph.run_turn("t-26", {"conversation_id": 26, "incoming_text": "Okay."})
    seen_fresh: dict = {}

    class FreshGraph(FakeGraph):
        async def aget_state(self, config):
            return SimpleNamespace(values=pending, created_at=(NOW - timedelta(minutes=50)).isoformat())

        async def ainvoke(self, state_input, config=None):
            seen_fresh.update(state_input)
            return {"reply": ""}

    with patch.object(_graph, "get_graph", new=lambda: FreshGraph()), \
         patch.object(_graph.kb_rules, "refresh", new=AsyncMock()):
        await _graph.run_turn("t-26", {"conversation_id": 26, "incoming_text": "Hi",
                                       "last_human_at": (NOW - timedelta(minutes=40)).isoformat()})
    check("run_turn: after a human pause the next message is a new conversation",
          (seen_fresh.get("collected_info") or {}).get(RESET_KEY) is True
          and "last_human_at" not in seen_fresh)
    check("run_turn drops a stale collection before the turn",
          (seen.get("collected_info") or {}).get(RESET_KEY) is True and seen.get("last_question") is None)

    # Acknowledgements never answer a pending question.
    base = {"conversation_id": 26, "last_question": pending["last_question"], "service_type": "new_hiring",
            "history_text": "Client: hi\nYou: What type of home are you in?", "contact_type": "employer",
            "blocked_topics": {}, "collected_info": {}}
    for text in ("ok", "Okay.", "Afternoon\nOkay.", "thanks 🙏", "Good afternoon"):
        with patch.object(_ic, "complete_json", new=AsyncMock(side_effect=AssertionError("no LLM"))):
            out = await _ic.intent_classifier({**base, "incoming_text": text})
        check(f"stale/fresh intake + {text!r} -> no intake question", out.get("suppress_reply") is True)
    with patch.object(_ic, "complete_json", new=AsyncMock(return_value={
            "intent": "new_hiring", "service_type": "new_hiring", "confidence": 0.9})):
        out = await _ic.intent_classifier({**base, "incoming_text": "condo"})
    check("...a real answer still is one", not out.get("suppress_reply"))


async def scenario_helper_rights() -> None:
    rights_row = [{"id": "r1", "similarity": 0.53, "question": "What are my rights as a domestic helper in Singapore?",
                   "answer": "1. Salary ...", "content": None, "contact_type": "candidate",
                   "service_type": "general", "chunk_type": "qa_pair", "source_document": "Ming Hwee Service Notes"}]

    async def search(query, **kw):
        return rights_row if "rights" in (query or "").lower() else []

    async def turn(text: str, history: str) -> dict:
        state = {"conversation_id": 4443, "phone": CLIENT, "customer_name": "", "record_name": "",
                 "contact_type": "unknown", "incoming_text": text, "history_text": history,
                 "collected_info": {}, "asked_field_counts": {}, "blocked_topics": {}, "matched_cases": []}
        with patch.object(_ic, "complete_json", new=AsyncMock(return_value={
                "intent": "general_question", "service_type": None, "contact_type": "unclear",
                "confidence": 0.8})), \
             patch.object(_rag_node.rag, "search", new=search):
            for node in (_ic.intent_classifier, _rag_node.rag_retriever, _rg.response_generator):
                out = await node(state)
                state.update(out or {})
                if state.get("suppress_reply"):
                    break
        return state

    opening = await turn("Hello what are the helper rights for me? Please explain", "")
    check("helper rights ('for me') -> the rights list, no ticket",
          HELPER_RIGHTS_MESSAGE[:60] in (opening.get("reply") or "") and not opening.get("needs_handover"))
    confirm = await turn("yes", "Client: Hello what are the helper rights for me? Please explain\n"
                         "You: Hi, I'm Claire, Ming Hwee's AI assistant. Are you asking about your "
                         "rights as a domestic helper in Singapore?")
    check("...'yes' to our clarifying question -> the rights list, no ticket",
          HELPER_RIGHTS_MESSAGE[:60] in (confirm.get("reply") or "") and not confirm.get("needs_handover"))
    check("...and the 'yes' was searched with the question before it", (confirm.get("rag_best_score") or 0) > 0.4)


async def scenario_defaults() -> None:
    from app.config import Settings

    fields = Settings.model_fields
    check("HUMAN_IDLE_MINUTES defaults to 30", fields["human_idle_minutes"].default == 30)
    check("no fixed 24 h pause setting remains", "human_pause_hours" not in fields)
    check("INTAKE_STALE_HOURS defaults to 6", fields["intake_stale_hours"].default == 6)
    check("no internal number is written in code", fields["internal_numbers"].default == "")


async def scenario_receipt_chat_kind() -> None:
    import logging
    from app.whapi import parser

    check("receipt log: a LID chat reads 'lid'", parser.chat_kind("90000000000001@lid") == "lid")
    check("receipt log: a phone chat reads 'phone'", parser.chat_kind("6590000101@s.whatsapp.net") == "phone")
    check("receipt log: a group reads 'group'", parser.chat_kind("120363000000000001@g.us") == "group")
    seen: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    h = _H()
    parser.logger.addHandler(h)
    old_level = parser.logger.level
    parser.logger.setLevel(logging.INFO)
    try:
        parser.parse_webhook({"messages": [{"id": "fake-1", "type": "text", "from_me": False,
                                            "chat_id": "90000000000001@lid", "from": "90000000000001@lid",
                                            "text": {"body": "Okay."}}]})
    finally:
        parser.logger.removeHandler(h)
        parser.logger.setLevel(old_level)
    line = next((m for m in seen if m.startswith("Webhook message received")), "")
    check("receipt log names the chat kind of every hit", "chat=lid" in line)
    check("receipt log carries no digits of the chat id", "90000000000001" not in line)


async def scenario_vague_action() -> None:
    """2026-10-28, conversation 4443: "please send the video here" -> a ticket at once."""
    from app.graph import clarify
    from app.graph.nodes.action_clarifier import action_clarifier
    from app.graph.prompts.templates import BLOCKED_TOPIC_INSTRUCTION
    from app.services import ticket as ticket_service
    _tc = importlib.import_module("app.graph.nodes.ticket_creator")

    open_ticket = {"id": "t-125", "ticket_number": "CB-2026-0125", "service_type": ["new_hiring"],
                   "captured_info": {"topic_key": "candidate_new_hiring"}}
    llm = AsyncMock(return_value={"intent": "other", "service_type": None, "confidence": 0.5})

    async def turn(text: str, history: str, extra: dict) -> dict:
        state = {"conversation_id": 4443, "phone": CLIENT, "contact_type": "employer",
                 "incoming_text": text, "history_text": history, "collected_info": {},
                 "blocked_topics": {}, **extra}
        with patch.object(_ic, "complete_json", new=llm):
            state.update(await _ic.intent_classifier(state))
        state["route"] = _graph.route_after_intent(state)
        if state["route"] == "action_clarifier":
            state.update(await action_clarifier(state))
        return state

    ask = await turn("please send the video here", "", {})
    check("'please send the video here' -> a clarifying question, no ticket yet",
          ask["route"] == "action_clarifier" and "Which video" in (ask.get("reply") or "")
          and not ask.get("needs_handover") and (ask.get("pending_action") or {}).get("kind") == "video")
    history = "Client: please send the video here\nYou: " + (ask.get("reply") or "")
    answer = await turn("the interview video for Siti", history, {"pending_action": ask.get("pending_action")})
    check("...the answer -> a ticket is raised", answer.get("needs_handover") is True
          and _graph.route_after_response(answer) == "ticket_creator")
    created: dict = {}

    async def create(**kw):
        created.update(kw)
        return {"id": "t-new", "ticket_number": "CB-2026-0200"}

    with patch.object(_tc.ticket_service, "create", new=create), \
         patch.object(_tc.ticket_service, "pick_ticket_to_update", new=AsyncMock(return_value=None)), \
         patch.object(_tc.assignment_service, "resolve_agent", new=AsyncMock(return_value=(None, "none"))), \
         patch.object(_tc, "_maybe_create_lead", new=AsyncMock(return_value=None)), \
         patch.object(_tc, "_finish_lead", new=AsyncMock(return_value=None)):
        await _tc.ticket_creator({**answer, "service_type": "new_hiring", "intent": "other"})
    flat = str(created.get("captured_info") or "") + str(created.get("description") or "")
    check("...with the request and the answer in the ticket summary",
          "please send the video here" in flat and "the interview video for Siti" in flat
          and "Request (clarified)" in (created.get("description") or ""))
    check("...filed as a request, not under the collection in flight",
          created.get("service_type") == "case_enquiry")

    follow_ups: list = []

    async def add_follow_up(tid, text):
        follow_ups.append((tid, text))

    asked = clarify.question("video", "candidate")
    with patch.object(ticket_service, "add_follow_up", new=add_follow_up):
        on_open = await turn("my interview video, CB-2026-0125",
                             "Client: please send the video here\nYou: " + asked,
                             {"pending_action": clarify.pending("video", "please send the video here", asked),
                              "blocked_topics": {"candidate_new_hiring": open_ticket},
                              "contact_type": "candidate"})
    check("...an answer quoting an open ticket -> that ticket, no duplicate",
          not on_open.get("needs_handover") and bool(follow_ups) and follow_ups[0][0] == "t-125"
          and "CB-2026-0125" in (on_open.get("reply") or ""))
    parked = await turn("please send the video here", "Client: hi\nYou: Hi there.",
                        {"blocked_topics": {"candidate_new_hiring": open_ticket}, "contact_type": "candidate"})
    check("...a parked topic does not swallow it (asked first, not 'your placement')",
          parked["route"] == "action_clarifier" and "placement" not in (parked.get("reply") or ""))

    human = await turn("I want to talk to a human, send me the video", "", {})
    check("'talk to a human' -> immediate handover, no question",
          human.get("needs_handover") is True and human["route"] != "action_clarifier")
    with patch.object(_ic, "confirm_harm", new=AsyncMock(return_value=True)):
        hurt = await turn("my employer hit me, please send help here", "", {})
    check("a harm report is never clarified", hurt.get("intent") == "dispute_assault"
          and hurt["route"] != "action_clarifier")
    complaint = await turn("I want to lodge a complaint, send me the form", "", {})
    check("a complaint is never clarified", complaint["route"] != "action_clarifier")
    status_one = await turn("any update?", "Client: hi\nYou: Hello.",
                            {"blocked_topics": {"new_hiring": {"id": "t-1", "ticket_number": "CB-2026-0001"}}})
    check("a status question with one open ticket uses that ticket (no question)",
          status_one["route"] != "action_clarifier")
    status_none = await turn("any update?", "Client: hi\nYou: Hello.", {})
    check("a status question with no open ticket asks which request",
          status_none["route"] == "action_clarifier" and "reference number" in (status_none.get("reply") or ""))
    check("the holding line names the ticket's true topic",
          ticket_service.service_types_label(open_ticket) == "finding work as a helper"
          and "exactly these words" in BLOCKED_TOPIC_INSTRUCTION)


async def scenario_hire_documents() -> None:
    """2026-10-28, conversation 3766: a new-hire documents question mid transfer intake."""
    from app.graph.prompts.templates import HIRING_DOCUMENTS_NOTE

    rows = {
        "mwo": {"id": "mwo", "similarity": 0.74, "service_type": "general", "chunk_type": "qa_pair",
                "question": "What documents are needed for a Filipino helper's contract verification at MWO Singapore?",
                "answer": "Original contract, passport copy, Work Permit copy.", "content": None},
        "you": {"id": "you", "similarity": 0.72, "service_type": "new_hiring", "chunk_type": "qa_pair",
                "question": "What documents do I need to provide to hire a helper?", "answer": "Your NRIC.",
                "content": None},
        "her": {"id": "her", "similarity": 0.70, "service_type": "new_hiring", "chunk_type": "qa_pair",
                "question": "What documents are needed from the helper herself?", "answer": "Her passport.",
                "content": None},
        "we": {"id": "we", "similarity": 0.69, "service_type": "new_hiring", "chunk_type": "qa_pair",
               "question": "What documents does Ming Hwee prepare for me to sign?", "answer": "Service Agreement.",
               "content": None},
    }

    async def search(query, **kw):
        q = (query or "").lower()
        if "herself" in q:
            return [rows["her"]]
        if "prepare" in q:
            return [rows["we"]]
        if "provide to hire" in q:
            return [rows["you"]]
        return [rows["mwo"], rows["you"]]

    for nat in ("Filipino", "Indonesian", "Myanmar"):
        text = f"What documents do I need to hire a {nat} helper?"
        state = {"conversation_id": 3766, "phone": CLIENT, "contact_type": "employer",
                 "service_type": "transfer_employer", "collected_service": "transfer_employer",
                 "intent": "transfer", "incoming_text": text, "collected_info": {"full_name": "vaidik"},
                 "asked_field_counts": {"full_name": 1, "transfer_direction": 1}, "blocked_topics": {},
                 "last_question": {"key": "transfer_direction", "text": "Are you taking on a transfer helper?"},
                 "history_text": "Client: What is the transfer fee for a Filipino helper?\n"
                                 "You: Are you taking on a transfer helper already in Singapore, releasing "
                                 "your current helper, or returning her to us?"}
        with patch.object(_ic, "complete_json", new=AsyncMock(return_value={
                "intent": "document_question", "service_type": "transfer", "confidence": 0.9})):
            state.update(await _ic.intent_classifier(state))
        with patch.object(_rag_node.rag, "search", new=search):
            state.update(await _rag_node.rag_retriever(state))
        route = _graph.route_after_rag(state)
        prompts: list = []

        async def complete(system, user, **kw):
            prompts.append(system)
            return ("Here is what we need: From you: 1. Your NRIC. From the helper: 2. Her passport. "
                    "We prepare for you to sign: 3. The Service Agreement. We will guide you.")

        with patch.object(_rg, "complete", new=complete):
            out = await _rg.response_generator(state)
        reply = guards.numbered_items_on_own_lines(out.get("reply") or "")
        ids = [m.get("id") for m in state.get("rag_matches") or []]
        check(f"{nat} new-hire documents: read as new_hiring, answered not collected",
              state.get("service_type") == "new_hiring" and route == "response_generator")
        check(f"{nat} new-hire documents: the three new-hire rows, no MWO row",
              ids[:3] == ["you", "her", "we"] and "mwo" not in ids)
        check(f"{nat} new-hire documents: grouped list asked for, no transfer question",
              bool(prompts) and HIRING_DOCUMENTS_NOTE.strip()[:40] in prompts[0]
              and "taking on a transfer" not in reply)
        check(f"{nat} new-hire documents: numbered items on their own lines",
              "\n2. Her passport" in reply and "\n3. The Service Agreement" in reply)
    check("'hire a transfer helper' is still a transfer",
          not _ic.hires_a_new_helper("I want to hire a transfer helper"))


async def scenario_call_and_wording() -> None:
    """2026-10-29: call requests answered two ways; list wording; 'hey i need a job' ticketed."""
    from app.graph import clarify, guards
    from app.graph.nodes.action_clarifier import action_clarifier
    from app.graph.state import effective_contact_type
    from app.services import ticket as ticket_service
    _tc = importlib.import_module("app.graph.nodes.ticket_creator")
    _hx = importlib.import_module("app.graph.nodes.handover_executor")
    ico = importlib.import_module("app.graph.nodes.info_collector")

    follow_ups: list = []

    async def add_follow_up(tid, text):
        follow_ups.append((tid, text))

    async def turn(text: str, history: str, extra: dict, verdict: dict) -> dict:
        state = {"conversation_id": 1, "phone": CLIENT, "contact_type": "employer",
                 "incoming_text": text, "history_text": history, "collected_info": {},
                 "blocked_topics": {}, **extra}
        with patch.object(_ic, "complete_json", new=AsyncMock(return_value=verdict)), \
             patch.object(ticket_service, "add_follow_up", new=add_follow_up):
            state.update(await _ic.intent_classifier(state))
            state["route"] = _graph.route_after_intent(state)
            if state["route"] == "action_clarifier":
                state.update(await action_clarifier(state))
        return state

    said = "You: Here is everything for your new helper enquiry. Your reference number is `CB-2026-0131`."
    employer_ticket = {"id": "t-131", "ticket_number": "CB-2026-0131", "service_type": ["new_hiring"],
                       "status": "open", "captured_info": {"topic_key": "new_hiring"}}
    helper_ticket = {"id": "t-132", "ticket_number": "CB-2026-0132", "service_type": ["new_hiring"],
                     "status": "open", "captured_info": {"topic_key": "candidate_new_hiring"}}
    # The two live chats, as the classifier read them: the employer kept the
    # parked hiring topic, the job seeker resolved to no service.
    employer = await turn("i want to talk to you on call, when available please call", said,
                          {"blocked_topics": {"new_hiring": employer_ticket}, "service_type": "new_hiring"},
                          {"intent": "case_enquiry", "service_type": "new_hiring", "confidence": 0.8})
    helper = await turn("i want to talk to you on call when available please call",
                        "You: Your registration is complete.",
                        {"blocked_topics": {"candidate_new_hiring": helper_ticket}, "contact_type": "candidate"},
                        {"intent": "case_enquiry", "service_type": None, "confidence": 0.8})
    check("call request with an open ticket -> the call reply with THAT ticket's reference",
          employer.get("reply") == clarify.call_reply("CB-2026-0131") and not employer.get("needs_handover"))
    check("...the job seeker's identical request gets the identical reply (her own reference)",
          helper.get("reply") == clarify.call_reply("CB-2026-0132") and not helper.get("needs_handover"))
    check("...added to the open ticket, no new one",
          [t for t, _ in follow_ups] == ["t-131", "t-132"] and "call" in follow_ups[0][1])
    check("...and the reply names no topic",
          not any(w in (employer.get("reply") or "").lower() for w in ("hiring", "helper", "placement", "regarding")))

    none = await turn("please call me", said.replace(" Your reference number is `CB-2026-0131`.", ""), {},
                      {"intent": "other", "service_type": None, "confidence": 0.5})
    check("call request with no open ticket -> a ticket is raised",
          none.get("needs_handover") is True and _graph.route_after_response(none) == "ticket_creator")
    created: list = []

    async def create(**kw):
        created.append(kw)
        return {"id": "t-new", "ticket_number": "CB-2026-0201"}

    with patch.object(_tc.ticket_service, "create", new=create), \
         patch.object(_tc.ticket_service, "pick_ticket_to_update", new=AsyncMock(return_value=None)), \
         patch.object(_tc.assignment_service, "resolve_agent", new=AsyncMock(return_value=(None, "none"))), \
         patch.object(_tc, "_maybe_create_lead", new=AsyncMock(return_value=None)), \
         patch.object(_tc, "_finish_lead", new=AsyncMock(return_value=None)):
        none.update(await _tc.ticket_creator(none))
    with patch.object(_hx.handover_service, "log_escalation", new=AsyncMock(return_value={})):
        none.update(await _hx.handover_executor(none))
    check("...exactly one ticket, and the same reply with its new reference",
          len(created) == 1 and none.get("reply") == clarify.call_reply("CB-2026-0201"))
    check("'what is a call back?' is not a call request", not clarify.asks_for_call("what is a call back?"))

    cand = ticket_service.missing_fields(ticket_service.CANDIDATE_HIRING, {"full_name": "Siti"})
    full, _ = ico.all_questions_message(cand, {"full_name": "Siti"}, True)
    again, _ = ico.all_questions_message(cand[:1], {"full_name": "Siti"}, False)
    check("the question list ends with the new closing line",
          full.endswith("Take your time, and feel free to skip anything that doesn't apply to you."))
    check("the missing-details message opens with the new line, no old closing",
          again.startswith("Just these few more and we're all set. Skip any that don't apply.")
          and "does not apply" not in again and "still missing" not in again)
    check("...a list sent in the old wording is still read as our question",
          guards.answering_our_question("You: Thank you. A few details are still missing or unclear:", "36"))
    check("helper Q9 says 'your rest day'", "working on your rest day for extra pay" in full
          and "her rest day" not in full)
    import re as _re
    check("no helper-facing question or option says her/she",
          not [f.key for svc in ticket_service.CANDIDATE_SERVICES
               for f in ticket_service.SERVICE_FIELDS.get(svc, [])
               for text in [f.question or "", *(f.options or ())]
               if _re.search(r"\b(?:her|she|hers|herself)\b", text, _re.I)])

    # 6:45 PM: an open employer lead from earlier testing on the number.
    stale = {"lead_kind": "employer", "created_lead_id": "lead-1", "created_lead_kind": "employer",
             "matched_lead_id": "lead-1"}
    seeker = await turn("hey i need a job", "", stale,
                        {"intent": "candidate_registration", "service_type": None,
                         "contact_type": "candidate", "confidence": 0.9})
    resolved = ticket_service.resolve_service(seeker.get("service_type"), effective_contact_type(seeker))
    check("first 'hey i need a job' (old employer lead on the number) -> the intake, no ticket",
          resolved == ticket_service.CANDIDATE_HIRING and bool(ticket_service.missing_fields(resolved, {}))
          and not seeker.get("needs_handover"))
    relay = await turn("my friend's maid is looking for a new employer", "", {},
                       {"intent": "candidate_registration", "service_type": None, "confidence": 0.9})
    check("...somebody else's helper is still a third party",
          relay.get("service_type") != ticket_service.CANDIDATE_HIRING)


async def scenario_xing_chain() -> None:
    """2026-10-29 (4443, Xing Chain): style misread, no closing summary,
    household re-asked in full, the workload pushback."""
    ico = importlib.import_module("app.graph.nodes.info_collector")
    from app.services import ticket as ticket_service
    prompts: list[str] = []

    async def run(state: dict, extraction: dict | None = None, reply: str = "Got it.") -> dict:
        async def capture(system_prompt, *a, **kw):
            prompts.append(system_prompt or "")
            return reply
        with patch.object(ico, "complete", new=capture, create=True), \
             patch.object(ico, "complete_json", new=AsyncMock(return_value=dict(extraction or {})),
                          create=True), \
             patch.object(_rag_node, "briefing_records",
                          new=AsyncMock(return_value={"rag_matches": [{"id": "r", "similarity": 0.6}],
                                                      "rag_context": "New hire: 4 to 6 weeks."})), \
             patch.object(ico, "_open_lead_early", new=AsyncMock(return_value={})):
            return await ico.info_collector(state)

    # 1. Question style.
    check("'in one time, i don't have much time' answers the offer as all at once",
          ico.chosen_style("in one time, i don't have much time") == "all")
    for phrase in ("at one time", "all in one time", "one shot", "quickly please", "send all"):
        check(f"offer answer '{phrase}' = all at once", ico.chosen_style(phrase) == "all")
    check("'i mean all question in one time' switches from one by one",
          ico.switches_to_all("one", "i mean all question in one time"))
    check("...but 'all of them' answering a question does not switch",
          not ico.switches_to_all("one", "all of them"))
    base = {"conversation_id": 1, "phone": CLIENT, "contact_type": "employer", "intent": "new_hiring",
            "service_type": "new_hiring", "collected_service": "new_hiring",
            "collected_info": {"full_name": "Xing Chain"}, "asked_field_counts": {"full_name": 1, "household": 1},
            "question_style": "one", "rag_matches": [], "history_text":
            "Client: Xing Chain\nYou: How many people live in your household, and who are they?",
            "incoming_text": "i mean all question in one time"}
    out = await run(base)
    check("the correction sends the full list at once",
          out.get("question_style") == "all" and (out.get("reply") or "").startswith(guards.QUESTION_LIST_HEADS[0]))

    # 2. The closing summary, when the briefing's key ended "not provided".
    # CB-2026-0133's own details, as the live intake closed.
    collected = {"pets": "yes", "budget": "$600-700", "rest_day": "1 day off on weekends",
                 "full_name": "Xing Chain", "home_size": "5 bedrooms, 3 bathrooms", "household": "6",
                 "home_type": "landed", "pet_detail": "1 dog, 2 parrots", "helper_room": "own room",
                 "hire_source": "worked in Singapore before (to be hired from overseas)",
                 "requirement": "eldercare, cooking", "elderly_detail": "70 and 80; diabetes; cannot walk",
                 "helper_profile": "at least 4 years", "preferred_nationality": "Filipino",
                 "first_time_hire": "first time with us - no placement on record",
                 **{k: ticket_service.UNANSWERED for k in
                    ("special_duties", "start_timeline", "update_channel", "referral_source")}}
    left = ticket_service.missing_fields("new_hiring", collected)
    prompts.clear()
    done = await run({**base, "collected_info": collected, "question_style": "all",
                      "asked_field_counts": {k: 1 for k in collected},
                      "rag_matches": [{"id": "r", "similarity": 0.6}], "rag_context": "x",
                      "incoming_text": "as I said, later"},
                     reply="Here is everything for your new helper enquiry: 4 to 6 weeks.")
    check(f"a completed intake with start_timeline 'not provided' gets the closing summary (left={[f.key for f in left]})",
          not left and done.get("info_complete") and "new_hiring" in (done.get("briefed_services") or [])
          and any("THIS message is the one" in p for p in prompts))

    # 3. A partly answered household question asks only for the rest.
    hist = ("You: Sure, here are the questions.\nClient: In my household I want someone to handle my "
            "parents because they are very old.")
    follow = ico.household_follow_up({"history_text": hist, "incoming_text": "my father cannot walk"},
                                     [ticket_service.Field(key="household", label="household", question="q")])
    check("household named in the answer -> 'Besides your parents...' only", follow ==
          "Besides your parents, who else lives with you, and how many people are there in total?")
    out = await run({**base, "question_style": "all", "history_text": hist,
                     "collected_info": {"full_name": "Xing Chain", "elderly_detail": "70 and 80"},
                     "asked_field_counts": {"household": 1, "full_name": 1},
                     "incoming_text": "my father cannot walk"})
    reply = out.get("reply") or ""
    check("the missing-details list asks only the missing part of the household",
          "Besides your parents" in reply and "How many people live in your household" not in reply)

    # 4. The workload concern: one gentle line, the list goes on, no second helper.
    heavy = {"full_name": "Xing Chain", "requirement": "eldercare, cooking", "household": "6",
             "home_size": "5 bedrooms, 3 bathrooms", "home_type": "landed"}
    out = await run({**base, "question_style": "all", "collected_info": heavy,
                     "asked_field_counts": {k: 1 for k in heavy}, "incoming_text": "landed, 5 bedrooms"})
    reply = out.get("reply") or ""
    check("all at once: the concern is one gentle line and the list follows in the same message",
          reply.startswith(ico.WORKLOAD_LINE) and "\n1. " in reply and "workload" in (out.get("flagged_once") or []))
    prompts.clear()
    await run({**base, "collected_info": heavy, "asked_field_counts": {k: 1 for k in heavy},
               "incoming_text": "landed, 5 bedrooms"}, reply="Noted. Will she have her own room?")
    prompt = prompts[-1] if prompts else ""
    check("one by one: never suggest more than one helper, never question the choice",
          "NEVER suggest more than one helper" in prompt and "look at more than one helper" not in prompt
          and "or more than one" not in (ico.WORKLOAD_LINE + ico.GENTLE_WORKLOAD_INSTRUCTION).replace("NEVER suggest more than one helper", ""))


async def scenario_vd_transfer() -> None:
    """2026-10-30 (3766, VD, transfer all at once): bare follow-up, wrong
    clarification, lost answers on a switch, "yes" to either/or, experience."""
    ico = importlib.import_module("app.graph.nodes.info_collector")
    from app.services import ticket as ticket_service
    prompts: list[str] = []

    async def collect(state: dict, extraction: dict | None = None) -> dict:
        async def capture(system_prompt, *a, **kw):
            prompts.append(system_prompt or "")
            return "Got it."
        with patch.object(ico, "complete", new=capture, create=True), \
             patch.object(ico, "complete_json", new=AsyncMock(return_value=dict(extraction or {})),
                          create=True), \
             patch.object(_rag_node, "briefing_records", new=AsyncMock(return_value={})), \
             patch.object(ico, "_open_lead_early", new=AsyncMock(return_value={})):
            return await ico.info_collector(state)

    async def classify(text: str, history: str, extra: dict, verdict: dict) -> dict:
        state = {"conversation_id": 1, "phone": CLIENT, "contact_type": "employer",
                 "incoming_text": text, "history_text": history, "blocked_topics": {}, **extra}
        with patch.object(_ic, "complete_json", new=AsyncMock(return_value=verdict)):
            return await _ic.intent_classifier(state)

    answers = {"full_name": "VD growwstacks", "transfer_direction": "taking on a transfer helper",
               "requirement": "general housework", "household": "8", "home_type": "HDB",
               "home_size": "5 bedrooms, 5 bathrooms", "helper_room": "own room", "pets": "yes",
               "preferred_nationality": "Indonesian",
               "helper_profile": "above 30, at least 4 years experience",
               "special_duties": "everything", "budget": "500-600", "rest_day": "weekly day off",
               "referral_source": "google", "update_channel": "both", "email": "vd@example.com"}
    live = {"conversation_id": 1, "phone": CLIENT, "contact_type": "employer",
            "intent": "transfer", "service_type": "transfer_employer",
            "collected_service": "transfer_employer", "collected_info": dict(answers),
            "asked_field_counts": {k: 1 for k in answers}, "question_style": "all",
            "rag_matches": []}

    # 1. A follow-up names its subject, in the list and as the field's question.
    q = next(f.question for f in ticket_service.fields_for("new_hiring") if f.key == "pet_detail")
    check("the pet follow-up names its subject", q.startswith("You mentioned you have pets"))
    out = await collect({**live, "history_text": "You: Sure, here are the questions.",
                         "incoming_text": "4. yes\n5. yes"})
    check("the missing-details list asks 'You mentioned you have pets - what kind...'",
          "You mentioned you have pets" in (out.get("reply") or ""))

    # 2. "what is this for?" stays on the collection and re-asks the SAME question.
    hist = "You: Just these few more and we're all set.\n1. You mentioned you have pets – what kind, and how many?"
    c = await classify("i did not understand what is this for ?", hist,
                       {"service_type": "transfer_employer", "intent": "transfer",
                        "collected_service": "transfer_employer", "collected_info": dict(answers)},
                       {"intent": "new_hiring", "service_type": "new_hiring", "confidence": 0.8})
    check("a question about our question does not switch the live transfer to new hiring",
          c.get("service_type") == "transfer_employer")
    prompts.clear()
    await collect({**live, "question_style": "one", "history_text": hist,
                   "incoming_text": "i did not understand what is this for ?",
                   "last_question": {"key": "pet_detail", "text": "pets"}})
    p = prompts[-1] if prompts else ""
    check("the clarification re-asks the pending pets question, not another one",
          "DID NOT UNDERSTAND YOUR LAST QUESTION" in p and "You mentioned you have pets" in p)

    # 3. A sibling switch never drops answers, in the classifier and in the collector.
    c = await classify("yes she should have there own room and yes i have 1 pet", hist,
                       {"service_type": "transfer_employer", "intent": "transfer",
                        "collected_service": "transfer_employer", "collected_info": dict(answers)},
                       {"intent": "new_hiring", "service_type": "new_hiring", "confidence": 0.8})
    check("the model's new_hiring does not replace a live transfer on a plain answer",
          c.get("service_type") == "transfer_employer")
    out = await collect({**live, "service_type": "new_hiring", "intent": "new_hiring",
                         "question_style": "one", "history_text": hist,
                         "incoming_text": "yes i have 1 pet"}, {"pet_detail": "1 pet"})
    kept = out.get("collected_info") or {}
    check("a forced switch to new hiring keeps every answer the client gave",
          all(kept.get(k) == v for k, v in answers.items()
              if k in {f.key for f in ticket_service.fields_for("new_hiring")}))

    # 4. "yes" to "own room, or sharing?" is own room.
    out = await collect({**live, "collected_info": {k: v for k, v in answers.items() if k != "helper_room"},
                         "history_text": "You: Will she have her own room, or would she be sharing?",
                         "incoming_text": "yes"}, {"helper_room": "yes"})
    check("a bare yes to 'own room, or sharing?' is filed as own room",
          (out.get("collected_info") or {}).get("helper_room") == "own room")

    # 5. Experience stated answers the first-timer question.
    hire = {k: v for k, v in answers.items() if k not in ("transfer_direction",)}
    out = await collect({**live, "service_type": "new_hiring", "intent": "new_hiring",
                         "collected_service": "new_hiring", "collected_info": hire,
                         "history_text": "You: Any preference on her age, or how much experience?",
                         "incoming_text": "above 30, at least 4 years experience"})
    check("'at least 4 years experience' answers 'first-timer or experienced'",
          str((out.get("collected_info") or {}).get("hire_source") or "").startswith("experienced"))


async def scenario_direct_hire_summary() -> None:
    """2026-10-30 (direct hire, Filipino helper overseas, "available in 2 weeks"):
    broken timeline sentence, ungrouped documents, generic steps, full-name heading."""
    ico = importlib.import_module("app.graph.nodes.info_collector")
    base = {"full_name": "VD", "helper_name": "Maria Santos Cruz",
            "helper_transfer_case": "no - she is in the Philippines, not in Singapore",
            "helper_availability": "available in 2 weeks", "helper_contact": "not provided",
            "update_channel": "WhatsApp"}
    routes = {
        "PH overseas": {"helper_nationality": "Filipino"},
        "ID overseas": {"helper_nationality": "Indonesian"},
        "MM overseas": {"helper_nationality": "Myanmar"},
        "in Singapore": {"helper_nationality": "Filipino",
                         "helper_transfer_case": "yes, she works for another employer here",
                         "notice_clearance": "her employer agrees"},
    }
    text = {k: ico.direct_hire_summary({**base, **v}) for k, v in routes.items()}

    # 1. The client's date against the real timeline; nothing when it is realistic.
    check("PH overseas, 2 weeks: 'She won't be able to start in 2 weeks - ... 5 to 6 weeks after MOM approval'",
          "She won't be able to start in 2 weeks – for a helper coming from the Philippines it "
          "takes about 5 to 6 weeks after MOM approval." in text["PH overseas"])
    check("ID overseas, 2 weeks: too soon against about 4 weeks",
          "start in 2 weeks" in text["ID overseas"] and "4 weeks after MOM approval" in text["ID overseas"])
    check("MM overseas, 2 weeks: realistic (2 to 4 weeks) - no 'won't be able' sentence",
          "won't be able" not in text["MM overseas"]
          and "it takes about 2 to 4 weeks after MOM approval" in text["MM overseas"])
    check("in Singapore, 2 weeks: realistic (1 to 2 weeks) - no 'won't be able' sentence",
          "won't be able" not in text["in Singapore"] and "1 to 2 weeks" in text["in Singapore"])
    check("the after-MOM guard keeps the client's date in a sentence that has the span",
          "start in 2 weeks" in guards.with_after_mom_approval(
              "She cannot start in 2 weeks because a Filipino helper takes about 5 to 6 weeks "
              "after MOM approval.", "PH"))

    # 2. Documents grouped, from the fees document.
    for k, t in text.items():
        check(f"{k}: documents grouped 'From you' (NRIC/ID, income) / 'From the helper'",
              "From you:\n1. A copy of your NRIC or ID\n2. Proof of income" in t
              and "From the helper:\n1. A copy of her passport\n2. Her medical report\n3. Her school certificate" in t)
    check("in Singapore: her current Work Permit details are asked for",
          "4. Her current Work Permit details" in text["in Singapore"]
          and "Work Permit details" not in text["PH overseas"])

    # 3. Process for her nationality and location.
    check("PH overseas: MOM application, IPA, Philippine embassy contract and OEC, medical, travel",
          all(w in text["PH overseas"] for w in ("work permit application with MOM", "(IPA)",
              "Philippine embassy", "(OEC)", "fit-to-fly medical", "travels to Singapore")))
    check("ID overseas: Indonesian embassy portal job order and runner",
          "Indonesian embassy portal" in text["ID overseas"] and "OEC" not in text["ID overseas"])
    check("MM overseas: partner in Myanmar and the security bond form",
          "partner in Myanmar" in text["MM overseas"] and "OEC" not in text["MM overseas"])
    check("in Singapore: no embassy step and no travel",
          "no embassy step and no travel" in text["in Singapore"]
          and "travels to Singapore" not in text["in Singapore"])

    # 4. Heading with her first name.
    check("heading 'Here is everything for hiring Maria directly:'",
          all(t.startswith("Here is everything for hiring Maria directly:") for t in text.values())
          and "Santos" not in text["PH overseas"])

    # Wired: the completing turn sends it, without asking the model.
    model = AsyncMock(return_value="Here is everything for Maria Santos Cruz: 4 generic steps.")
    with patch.object(ico, "complete", new=model, create=True), patch.object(ico, "complete_json", new=AsyncMock(return_value={"update_channel": "WhatsApp"}),
                      create=True), patch.object(_rag_node, "briefing_records", new=AsyncMock(return_value={})), patch.object(ico, "_open_lead_early", new=AsyncMock(return_value={})):
        collected = {**base, "helper_nationality": "Filipino"}
        collected.pop("update_channel")
        out = await ico.info_collector({
            "conversation_id": 1, "phone": CLIENT, "contact_type": "employer",
            "intent": "direct_hiring", "service_type": "direct_hiring",
            "collected_service": "direct_hiring", "collected_info": collected,
            "asked_field_counts": {k: 1 for k in collected} | {"update_channel": 1},
            "history_text": "You: Would you prefer updates by email, here on WhatsApp, or both?",
            "incoming_text": "whatsapp", "rag_matches": []})
    check("the completing direct-hire turn sends the built summary",
          str(out.get("reply") or "").startswith("Here is everything for hiring Maria directly:")
          and "won't be able to start in 2 weeks" in str(out.get("reply") or ""))


async def scenario_direct_hire_intake() -> None:
    """2026-10-30 (983b006 replay): "skip" re-asked, nationality asked with the
    name and again, closing summary numbered straight through."""
    ico = importlib.import_module("app.graph.nodes.info_collector")

    async def collect(state: dict, extraction: dict, model_reply: str = "Got it.") -> tuple[dict, list]:
        prompts: list[str] = []

        async def capture(system_prompt, *a, **kw):
            prompts.append(system_prompt or "")
            return model_reply
        with patch.object(ico, "complete", new=capture, create=True), \
             patch.object(ico, "complete_json", new=AsyncMock(return_value=dict(extraction)),
                          create=True), \
             patch.object(_rag_node, "briefing_records", new=AsyncMock(return_value={})), \
             patch.object(ico, "_open_lead_early", new=AsyncMock(return_value={})):
            return await ico.info_collector(state), prompts

    dh = {"conversation_id": 1, "phone": CLIENT, "contact_type": "employer",
          "intent": "direct_hiring", "service_type": "direct_hiring",
          "collected_service": "direct_hiring", "question_style": "one", "rag_matches": []}
    asked = {"full_name": "VD", "helper_name": "Maria Santos Cruz", "helper_nationality": "Filipino",
             "helper_transfer_case": "no", "helper_availability": "in 2 weeks"}

    # 1. "skip" to the helper's number is its answer.
    out, _ = await collect({**dh, "collected_info": dict(asked),
                            "asked_field_counts": {k: 1 for k in asked} | {"helper_contact": 1},
                            "last_question": {"key": "helper_contact",
                                              "text": "What is the best number to reach Maria on?"},
                            "history_text": "You: What is the best number to reach Maria on?",
                            "incoming_text": "skip"}, {})
    check("'skip' to the helper's number is filed as answered and not asked again",
          (out.get("collected_info") or {}).get("helper_contact") == "not provided"
          and "number" not in str(out.get("reply") or "").lower())

    # 2. The nationality in the first message is not asked; the name question asks the name only.
    out, prompts = await collect({**dh, "collected_info": {"full_name": "VD"},
                                  "asked_field_counts": {"full_name": 1},
                                  "collected_since": "I found a Filipino helper and want a direct hire",
                                  "history_text": "Client: I found a Filipino helper and want a direct hire\n"
                                                  "You: May I know your name?",
                                  "incoming_text": "VD"}, {},
                                 "Thanks, VD. May I know the helper's full name and nationality?")
    check("a nationality in the first message fills helper_nationality, and the name "
          "question does not ask it",
          (out.get("collected_info") or {}).get("helper_nationality") == "Filipino"
          and "nationalit" not in str(out.get("reply") or "").lower())

    # 3. Every flow's summary numbers each group from 1.
    pr = {"full_name": "VD", "helper_name": "Siti", "nationality": "Indonesian",
          "passport_expiry": "next year"}
    out, _ = await collect({"conversation_id": 1, "phone": CLIENT, "contact_type": "employer",
                            "intent": "passport_renewal", "service_type": "passport_renewal",
                            "collected_service": "passport_renewal", "collected_info": dict(pr),
                            "asked_field_counts": {k: 1 for k in pr}, "question_style": "one",
                            "rag_matches": [{"question": "x", "answer": "y", "similarity": 0.6}],
                            "history_text": "You: When does her passport expire?",
                            "incoming_text": "next year"}, {},
                           "Here is everything for Siti's passport renewal:\n\n"
                           "Here is what we will need:\nFrom you:\n1. A copy of your NRIC\n"
                           "From the helper:\n2. A copy of her passport\n3. A copy of her Work Permit\n\n"
                           "Here is the process from here:\n1. Send the documents.\n\n"
                           "I've passed everything to our team, and a live agent will connect with you shortly.")
    reply = str(out.get("reply") or "")
    check("a closing summary numbers each group from 1 ('From the helper' starts at 1)",
          "From the helper:\n1. A copy of her passport\n2. A copy of her Work Permit" in reply)


async def scenario_replacement_summary() -> None:
    """2026-10-30 replacement test: 2 years with the client, sent home, ASAP."""
    ico = importlib.import_module("app.graph.nodes.info_collector")
    base = {"full_name": "VD", "helper_name": "pogo growwstacks", "helper_tenure": "2 years",
            "helper_from_us": "hired elsewhere - no placement on record",
            "reason": "rude", "current_helper_exit": "send her back to her home country",
            "timeline": "as soon as possible",
            "replacement_preferences": "Filipino, 3 years of experience"}
    t = ico.replacement_summary(base)
    inside = ico.replacement_summary({**base, "helper_tenure": "4 months",
                                      "helper_from_us": "from Ming Hwee - placed by us"})

    # 1. The package rule: passed after 6 months (no figure), $288 inside it.
    check("replacement fee: 2 years -> the 6-month period has passed, agent confirms, no "
          "figure; inside the package -> $288",
          "2 replacements within 6 months" in t and "that replacement period has passed" in t
          and "$" not in t and "$288" in inside)
    # 2. ASAP -> the transfer-helper line, once.
    check("replacement ASAP: one line offering a transfer helper in 1-2 weeks",
          t.count("If you need someone sooner, a transfer helper already in Singapore can "
                  "usually start in 1–2 weeks.") == 1
          and "sooner" not in ico.replacement_summary({**base, "timeline": "next month"}))
    # 3. Documents grouped and numbered from 1; her exit steps before the new helper's.
    check("replacement documents 'From you' / 'About your current helper' from 1, and her "
          "Work Permit cancelled, flight home, bond discharged before the new helper's steps",
          "From you:\n1. A copy of your NRIC\n2. Proof of income" in t
          and "About your current helper:\n1. A copy of her passport\n2. Her Work Permit details" in t
          and t.index("cancel Pogo's Work Permit") < t.index("book her flight home")
          < t.index("security bond is discharged") < t.index("collected through our partner"))
    # 4. Heading with her first name, and the node sends the built summary.
    async def capture(*a, **kw):
        return "MODEL-WRITTEN"
    with patch.object(ico, "complete", new=capture, create=True), \
         patch.object(ico, "complete_json", new=AsyncMock(return_value={}), create=True), \
         patch.object(_rag_node, "briefing_records", new=AsyncMock(return_value={})), \
         patch.object(ico, "_open_lead_early", new=AsyncMock(return_value={})):
        out = await ico.info_collector({
            "conversation_id": 1, "phone": CLIENT, "contact_type": "employer",
            "intent": "replacement", "service_type": "replacement",
            "collected_service": "replacement", "collected_info": dict(base),
            "asked_field_counts": {k: 1 for k in base}, "question_style": "all",
            "rag_matches": [], "history_text": "You: What are you looking for?",
            "incoming_text": "Filipino, 3 years of experience"})
    check("replacement heading 'Here is everything for replacing Pogo:' and the node sends it",
          t.startswith("Here is everything for replacing Pogo:\n")
          and str(out.get("reply") or "").startswith("Here is everything for replacing Pogo:"))


async def scenario_crisis_reply() -> None:
    """2026-10-30 7:45 PM IST: "hey i was beaten up by my employer please help me"."""
    he = importlib.import_module("app.graph.nodes.handover_executor")
    created = []

    async def fake_create(**kw):
        created.append(ticket_service.priority_for(kw.get("service_type")))
        return {"id": "t1", "ticket_number": "CB-2026-0143"}
    model = ("Hi, I'm Claire, Ming Hwee's AI assistant. I'm so sorry this happened to you. "
             "Please make sure you are safe now, and call the police at 999 if you are in "
             "immediate danger. I've alerted our team, and a live agent will connect with "
             "you shortly.")
    with patch.object(he, "complete", new=AsyncMock(return_value=model)), \
         patch.object(ticket_service, "create", new=fake_create), \
         patch.object(he.assignment_service, "resolve_agent",
                      new=AsyncMock(return_value=(None, "admin"))), \
         patch.object(he.handover_service, "log_escalation", new=AsyncMock(return_value={})):
        out = await he.handover_executor({
            "conversation_id": 1, "phone": CLIENT, "intent": "dispute_assault",
            "incoming_text": "hey i was beaten up by my employer please help me",
            "history_text": ""})
    reply = str(out.get("reply") or "")
    check("crisis reply: high-priority ticket, no intro, 999 then the 995 and MOM lines, "
          "and the reference",
          created == ["high"] and "Claire" not in reply
          and reply.index("999") < reply.index("If you are injured, please call an ambulance on 995.")
          and "1800-339-5505" in reply and "`CB-2026-0143`" in reply
          and "anything else" not in reply)


async def scenario_crisis_keywords() -> None:
    """2026-10-30: crisis phrases force the safety reply without the model;
    intake phrasing with the same words does not."""
    ic = importlib.import_module("app.graph.nodes.intent_classifier")

    async def routed(text: str) -> str:
        with patch.object(ic, "complete_json", new=AsyncMock(side_effect=AssertionError("model called"))):
            try:
                out = await ic.intent_classifier({"conversation_id": 1, "incoming_text": text,
                                                  "history_text": ""})
            except AssertionError:
                return "model"
        return out.get("intent") or ""

    groups = {
        "locked in / not allowed out": (["i am locked inside the house", "i cannot go out",
                                         "they are not letting me out", "saya dikurung"],
                                        ["she cannot go out alone at night"]),
        "no food": (["they do not give me food", "i am starving", "hungry for days",
                     "tidak diberi makan"], ["no food allergies"]),
        "passport taken": (["my employer took my passport", "employer kept my passport",
                            "paspor saya ditahan"], ["should i keep her passport"]),
        "not paid for months": (["i have not been paid for 3 months", "no salary for months",
                                 "gaji saya belum dibayar"], ["i have not paid the levy for 2 months"]),
        "touched / sexual / harassed": (["sir touched me", "he says sexual things to me",
                                         "his son harassed me"], ["home leave for my helper"]),
        "threatened": (["my employer threatened me", "ma'am threatens to send me home"],
                       ["how much is a transfer helper"]),
        "hit / slapped / beaten / hurt me": (["she slapped me", "i was beaten up", "he hurt me",
                                             "binugbog ako"], ["she will hit the market on sunday"]),
        "suicide / want to die": (["i want to die", "i will kill myself", "bunuh diri"],
                                  ["renew my helper passport"]),
    }
    for name, (pos, neg) in groups.items():
        hits = [await routed(t) for t in pos]
        misses = [bool(ic.CRISIS_PATTERNS.search(t)) for t in neg]
        check(f"crisis keywords ({name}): forced to the safety reply, intake phrasing not",
              all(h == "dispute_assault" for h in hits) and not any(misses))


async def scenario_home_leave_2nov() -> None:
    """2 Nov home-leave tests: helper's own list in "you", ticket after our agent
    confirms, a reason in the opening message kept, travel sooner than usual."""
    ico = importlib.import_module("app.graph.nodes.info_collector")
    from app.services import ticket as _t

    # 1. A helper asking for herself is asked as "you", on home leave and passport renewal.
    hl = [ico.for_herself(f) for f in _t.SERVICE_FIELDS["home_leave"]]
    pr = [ico.for_herself(f) for f in _t.SERVICE_FIELDS["passport_renewal"]]
    text = " ".join(f.question for f in hl + pr).lower()
    msg, _ = ico.all_questions_message([f for f in hl if f.key in ("nationality", "leave_dates")],
                                       {}, True)
    check("helper-for-herself: home leave and passport questions say 'you', never 'she/her'",
          "which country are you from?" in msg.lower()
          and "when are you planning to travel, and when would you be back?" in msg.lower()
          and "your current passport" in text
          and not __import__("re").search(r"\b(?:she|her)\b", text)
          and ico.HELPER_QUESTIONS_SECOND_PERSON)

    # 1b. Phone test 2 Nov 3:47 PM: "i am sick i wanna go home" is her asking for herself.
    check("helper-for-herself: 'i am sick i wanna go home' is the helper; 'my helper wanna "
          "go home' is not",
          ico._home_leave_for_herself({"incoming_text": "hey i am sick i wanna go home"})
          and ico._home_leave_for_herself({"incoming_text": "i need to go back to my country"})
          and not ico._home_leave_for_herself({"incoming_text": "my helper wanna go home"})
          and not ico._home_leave_for_herself({"incoming_text": "i want her to go home"}))

    # 2. The ticket step: only once our agent confirms, never "now".
    briefing = ("Here is everything for Sneaky's home leave:\n\nIt takes about 2 weeks.\n\n"
                "Here is the process from here:\n"
                "1. Please book Sneaky's air ticket now and send us a copy once you have it.\n"
                "2. Sneaky signs the Home Leave form.")
    out = ico.ticket_step_after_confirm(briefing, False)
    own = ico.ticket_step_after_confirm(briefing, True)
    check("home leave ticket step: 'Once our agent confirms, you can book her air ticket...', "
          "no 'now'; the helper's own says 'you or your employer'",
          "1. " + ico.TICKET_STEP_EMPLOYER in out and " now" not in out.lower()
          and ico.TICKET_STEP_HERSELF in own)

    # 3. A reason stated in the opening message is not asked again.
    check("home leave: 'she is not well' in the opening message is a reason (not re-asked)",
          ico.names_a_leave_reason("my helper want to go home she is not well")
          and not ico.names_a_leave_reason("my helper want to go home"))

    # 4. Travel sooner than the usual time is said plainly, no promise.
    line = ico.leave_timeline_line({"nationality": "Indonesian",
                                    "leave_dates": "this week, back after 3 weeks"})
    later = ico.leave_timeline_line({"nationality": "Indonesian", "leave_dates": "next month"})
    fixed = ico.with_leave_timeline_line(briefing, line)
    check("home leave 'this week' (ID, ~2 weeks): 'It usually takes about 2 weeks. We'll treat "
          "it as urgent, but we can't promise it will be ready by then.'; next month: nothing",
          line == ("It usually takes about 2 weeks. We'll treat it as urgent, but we can't "
                   "promise it will be ready by then.")
          and line in fixed and "It takes about 2 weeks." not in fixed and later == "")

    # 5. Phone test 2 Nov 8:08 PM (Pogo, PH): the urgency clean-up took the whole
    #    closing line, handover included, so the reference landed in step 1.
    closing = ("Here is everything for your home leave:\n\nIt takes approximately 4 weeks.\n\n"
               "1. Our agent will contact your employer.\n\nWe will flag your request as urgent "
               "with our team. I have passed everything to our team, and a live agent will "
               "connect with you shortly.")
    out5 = ico.with_leave_timeline_line(closing, "It usually takes about 4 weeks. We'll treat it "
                                        "as urgent, but we can't promise it will be ready by then.")
    check("home leave urgent clean-up drops only the urgent sentence, the handover line stays",
          "I have passed everything to our team" in out5 and "flag your request" not in out5)

    # 6. The same test: nationality answered in the completing message, $400 deferred.
    st = {"service_type": "home_leave", "collected_info": {"requested_by": "helper"},
          "incoming_text": "1. i am from philipines"}
    check("home leave fee: nationality answered this turn counts (PH fee held), none -> not held",
          ico.briefing_fee_known(st, {"nationality": "Filipino"}, "home_leave")
          and not ico.briefing_fee_known(st, {}, "home_leave"))


# --- Helper complaint severity and triage (Thomas's guidelines, 2 Nov 2026) --

class ComplaintWorld:
    """The real graph, nodes and ticket service, with a list for cb_tickets."""

    def __init__(self, contact="unknown", employer_id=None):
        self.rows: list[dict] = []
        self.contact, self.employer_id = contact, employer_id
        self.history: list[str] = []
        self.classified = 0
        self.assessed = 0
        self.model = _graph.build_graph(MemorySaver())
        self.thread = f"t-{id(self)}"

    async def insert_numbered(self, table, row, *, number_field, next_number):
        saved = {**row, "id": f"id-{len(self.rows) + 1}",
                 number_field: f"CB-2026-{len(self.rows) + 900:04d}"}
        self.rows.append(saved)
        return saved

    async def select_one(self, table, columns="*", **filters):
        return next((r for r in self.rows if r["id"] == filters.get("id")), None)

    async def update(self, table, payload, **filters):
        for r in self.rows:
            if r["id"] == filters.get("id"):
                r.update(payload)
        return self.rows

    def open_topics(self):
        topics = {}
        for row in self.rows:
            if row.get("status") in ticket_service.OPEN_STATUSES:
                info = row.get("captured_info") or {}
                topics[info.get("topic_key") or ticket_service.primary_service_type(row)] = row
        return topics

    async def classify(self, state):
        self.classified += 1
        return {"suppress_reply": True, "reply": "", "intent": state.get("intent")}

    async def turn(self, text, llm=None):
        async def ask(message, history, pending):
            self.assessed += 1
            if isinstance(llm, Exception):
                raise llm
            return dict(llm or {"speaker": "unknown", "level": "none"})

        db = SimpleNamespace(insert_numbered=self.insert_numbered, select_one=self.select_one,
                             update=self.update, execute=AsyncMock(), rpc=AsyncMock(return_value=None))
        payload = {
            "conversation_id": 77, "phone": CLIENT, "customer_name": "", "contact_type": self.contact,
            "matched_employer_id": self.employer_id, "blocked_topics": self.open_topics(),
            "incoming_text": text, "history_text": "\n".join(self.history), "media_items": [],
        }
        with patch.object(_graph, "_graph", self.model), \
                patch.object(_graph, "_classify", new=self.classify), \
                patch.object(_ct, "_ask_model", new=ask), \
                patch.object(_ct_cc, "db", db), patch.object(ticket_service, "db", db), \
                patch.object(_ct_node.handover_service, "_log", new=AsyncMock()), \
                patch.object(_ct_he.handover_service, "log_escalation", new=AsyncMock(return_value={})), \
                patch.object(_ct_he.assignment_service, "resolve_agent",
                             new=AsyncMock(return_value=(None, "round_robin"))), \
                patch.object(_ct_he, "_assault_reply", new=AsyncMock(return_value="Call the police on 999.")), \
                patch.object(_ct_cc, "RETRY_PAUSE_SECONDS", 0):
            result = await _graph.run_turn(self.thread, payload)
        reply = result.get("reply") or ""
        self.history += [f"Client: {text}", f"You: {reply}"]
        return result, reply

    def ticket(self):
        return self.rows[-1] if self.rows else None


def _complaints_tab(row) -> bool:
    """tickets_repo._CB_IS_COMPLAINT, the portal's own rule (checked against the
    real SQL by a read-only replay)."""
    return bool(set(row.get("service_type") or []) & {"dispute_salary", "dispute_assault"})


def _h(level, kind, **more):
    return {"speaker": "helper", "level": level, "complaint_type": kind, **more}


REF_RE = "CB-2026-09"


async def scenario_complaint_triage():
    Q = _ct_t

    # 1. P1: no question first, the ticket raised first.
    w = ComplaintWorld()
    r, reply = await w.turn("my ma'am beat me just now, I'm bleeding", _h("P1", "physical"))
    t = w.ticket()
    check("complaint 1: 'beat me just now, bleeding' -> the P1 template word for word, no question "
          "before it; P1 ticket high priority, reference in the message",
          reply.startswith("I'm very sorry this is happening. Your safety comes first.")
          and "call the police on 999 now" in reply and "Call 995 or go to your nearest hospital A&E" in reply
          and 'Can you reply "safe" or "not safe"?' in reply
          and t and t["complaint_level"] == "P1" and t["priority"] == "high"
          and f"Your reference is `{t['ticket_number']}`." in reply and r.get("reply_must_send"))
    w0 = ComplaintWorld()
    _r, down = await w0.turn("my ma'am beat me just now, I'm bleeding", RuntimeError("model down"))
    check("complaint 1: the keyword net holds with the model down -> the same P1, ticket P1",
          down.startswith("I'm very sorry this is happening.") and w0.ticket()["complaint_level"] == "P1")

    # 2. Suicide template, then the reference as its own message.
    w = ComplaintWorld()
    r, reply = await w.turn("I don't want to live anymore", _h("P1", "self_harm"))
    extra = r.get("extra_replies") or []
    check("complaint 2: 'don't want to live' -> the P1 suicide template, then a separate reference message",
          reply.startswith("I'm really glad you told me. Are you thinking about ending your life right now?")
          and "call 995 now" in reply and "reference" not in reply
          and extra == [f"Your reference is `{w.ticket()['ticket_number']}`."]
          and w.ticket()["complaint_level"] == "P1")
    w0 = ComplaintWorld()
    _r, down = await w0.turn("I don't want to live anymore", RuntimeError("model down"))
    check("complaint 2: the keyword net catches it with the model down",
          down.startswith("I'm really glad you told me."))

    # 2, the answer to its question (4 Nov 2026).
    for answer, label in (("yes", "yes"), ("oo", "oo"), ("yes I am thinking about it", "yes, thinking"),
                          ("I want to die", "self-harm words"),
                          ("no I want to die", "a no, then self-harm words")):
        w = ComplaintWorld()
        await w.turn("I don't want to live anymore", _h("P1", "self_harm"))
        w.ticket()["workflow_status"] = None  # so only this answer can set it
        r2b, a2b = await w.turn(answer, {"speaker": "helper", "level": "none"})
        check(f"complaint 2: suicide + '{label}' -> the 995 line with the reference, ticket urgent",
              a2b == ("Please call 995 now or go to your nearest hospital A&E. We are not a medical "
                      f"service, so please contact them first. Your reference is `{w.ticket()['ticket_number']}`.")
              and w.ticket()["workflow_status"] == "escalated" and w.ticket()["priority"] == "high"
              and w.ticket()["status"] == "open" and r2b.get("reply_must_send"))
    for answer in ("no", "I'm ok", "no thanks"):
        w = ComplaintWorld()
        await w.turn("I don't want to live anymore", _h("P1", "self_harm"))
        _r, a2n = await w.turn(answer, {"speaker": "helper", "level": "none"})
        check(f"complaint 2: suicide + '{answer}' -> FOLLOW_SAFE",
              a2n.startswith("Thank you for letting us know.")
              and w.ticket()["captured_info"]["complaint"]["safe_status"] == "safe")
    w = ComplaintWorld()
    await w.turn("I don't want to live anymore", _h("P1", "self_harm"))
    _r, a2u = await w.turn("no I'm not ok", {"speaker": "helper", "level": "none"})
    check("complaint 2: 'no I'm not ok' is not a no: never FOLLOW_SAFE, and the 995 line stays in the reply",
          "Thank you for letting us know" not in a2u and "995" in a2u)
    w = ComplaintWorld()
    await w.turn("my ma'am beat me just now, I'm bleeding", _h("P1", "physical"))
    _r, a2p = await w.turn("yes", {"speaker": "helper", "level": "none"})
    check("complaint 2: the 995 line belongs to the suicide template: 'yes' after the emergency one does not get it",
          "We are not a medical service" not in a2p)

    # Every P1 ticket is escalated, however it got there; a P2 or P3 is not.
    p1_tickets = {}
    w = ComplaintWorld()
    await w.turn("my ma'am beat me just now, I'm bleeding", _h("P1", "physical"))
    p1_tickets["emergency"] = w.ticket()
    w = ComplaintWorld()
    await w.turn("I don't want to live anymore", _h("P1", "self_harm"))
    p1_tickets["suicide"] = w.ticket()
    w = ComplaintWorld()
    await w.turn("my ma'am beat me just now", RuntimeError("model down"))
    p1_tickets["keywords only"] = w.ticket()
    w = ComplaintWorld()
    await w.turn("she hit me last week", _h("P2", "physical"))
    p2_row = dict(w.ticket())
    await w.turn("yes", {"speaker": "helper", "level": "none"})
    await w.turn("yes I'm hurt", {"speaker": "helper", "level": "none"})
    p1_tickets["raised from P2"] = w.ticket()
    check("complaint: every P1 ticket has workflow_status=escalated, status still open "
          f"({', '.join(p1_tickets)})",
          all(t["complaint_level"] == "P1" and t["workflow_status"] == "escalated" and t["status"] == "open"
              for t in p1_tickets.values()))
    check("complaint: a P2 ticket is not escalated", "workflow_status" not in p2_row)
    w = ComplaintWorld()
    await w.turn("salary not paid for 2 months", _h("P3", "salary"))
    check("complaint: a P3 ticket is not escalated", "workflow_status" not in w.ticket())
    w = ComplaintWorld()
    await w.turn("she hit me last week", _h("P2", "physical"))
    await w.turn("yes", {"speaker": "helper", "level": "none"})
    await w.turn("no", {"speaker": "helper", "level": "none"})
    await w.turn("yes", {"speaker": "helper", "level": "none"})
    await w.turn("no", {"speaker": "helper", "level": "none"})
    before = w.ticket().get("workflow_status")
    await w.turn("not safe", {"speaker": "helper", "level": "none"})
    check("complaint: 'not safe' on an open P2 still escalates it",
          before is None and w.ticket()["workflow_status"] == "escalated")
    w = ComplaintWorld()
    await w.turn("my ma'am beat me just now, I'm bleeding", _h("P1", "physical"))
    w.ticket()["workflow_status"] = "stuck"  # an agent moved it on
    await w.turn("she took my things", {"speaker": "helper", "level": "none"})
    check("complaint: a later message does not undo what an agent set on a P1",
          w.ticket()["workflow_status"] == "stuck")

    # Assignment: as at 7b20d23, whatever the level.
    rows = []
    for text, llm_ in (("my ma'am beat me just now, I'm bleeding", _h("P1", "physical")),
                       ("I don't want to live anymore", _h("P1", "self_harm")),
                       ("she hit me last week", _h("P2", "physical")),
                       ("salary not paid for 2 months", _h("P3", "salary"))):
        w = ComplaintWorld()
        await w.turn(text, llm_)
        await w.turn("not safe", {"speaker": "helper", "level": "none"})
        rows.append(w.ticket())
    check("complaint: assignment fields unchanged from 7b20d23: no owner, rule round_robin, on every level, "
          "after an update too",
          all(r["assigned_agent_id"] is None and r["assignment_rule"] == "round_robin" for r in rows))
    source = Path(_ct_cc.__file__).read_text(encoding="utf-8")
    check("complaint: the complaint flow neither picks an agent nor writes the owner",
          "resolve_agent" not in source and source.count("assigned_agent_id") == 1
          and "resolve_agent" not in Path(_ct_node.__file__).read_text(encoding="utf-8"))

    # 3. P2 physical: Q1 -> Q2 -> Q3 -> Q4 -> the template, with no question repeated.
    w = ComplaintWorld()
    llm = _h("P2", "physical", facts_complete=True)
    _r, a1 = await w.turn("she hit me last week", llm)
    ticket_first = w.ticket() is not None and w.ticket()["complaint_level"] == "P2"
    _r, a2 = await w.turn("yes", {"speaker": "helper", "level": "P1", "complaint_type": "physical"})
    _r, a3 = await w.turn("no", {"speaker": "helper", "level": "none"})
    _r, a4 = await w.turn("yes", {"speaker": "helper", "level": "none"})
    _r, a5 = await w.turn("no", {"speaker": "helper", "level": "none"})
    check("complaint 3: 'she hit me last week' -> ticket first at P2, then Q1, Q2, Q3, Q4, one per message",
          ticket_first and (a1, a2, a3, a4) == (Q.Q1, Q.Q2, Q.Q3, Q.Q4))
    check("complaint 3: at most four questions, then the P2 general template with the reference, "
          "the safety line dropped because triage asked it",
          a5.startswith("Thank you for telling us. This sounds serious")
          and "Are you safe right now?" not in a5 and "Is it safe to chat?" not in a5
          and "We will not contact your employer unless you agree" in a5
          and f"`{w.ticket()['ticket_number']}`" in a5 and w.ticket()["complaint_level"] == "P2")

    # 3b. A P1 sign in an answer stops triage.
    w = ComplaintWorld()
    await w.turn("she hit me last week", llm)
    await w.turn("yes", {"speaker": "helper", "level": "none"})
    _r, stop = await w.turn("yes I'm hurt", {"speaker": "helper", "level": "none"})
    check("complaint 3b: Q2 'yes I'm hurt' stops triage -> P1 template, level raised on the ticket, "
          "with the history",
          stop.startswith("I'm very sorry this is happening.") and w.ticket()["complaint_level"] == "P1"
          and [h["level"] for h in w.ticket()["captured_info"]["complaint"]["level_history"]] == ["P2", "P1"])

    # 4. No sleep: Q1, then the sleep template with the duration.
    w = ComplaintWorld()
    _r, a1 = await w.turn("I haven't slept for 2 days", _h("P2", "overwork", duration="2 days"))
    _r, a2 = await w.turn("yes", {"speaker": "helper", "level": "none"})
    check("complaint 4: 'haven't slept for 2 days' -> Q1, then the sleep template with 'Going 2 days'",
          a1 == Q.Q1 and "Going 2 days without sleep is serious and can affect your health." in a2
          and "Are you feeling very unwell, or do you feel unsafe right now?" in a2)
    w0 = ComplaintWorld()
    await w0.turn("no sleep at all", _h("P2", "overwork"))
    _r, nod = await w0.turn("yes", {"speaker": "helper", "level": "none"})
    check("complaint 4: duration unknown -> 'Going without sleep is serious'",
          "Going without sleep is serious and can affect your health." in nod)

    # 5. P3 salary.
    w = ComplaintWorld()
    _r, a1 = await w.turn("salary not paid for 2 months", _h("P3", "salary"))
    _r, a2 = await w.turn("yes", {"speaker": "helper", "level": "none"})
    check("complaint 5: unpaid salary -> ticket P3 medium priority, Q1, then the P3 template asking "
          "for the salary months",
          a1 == Q.Q1 and "Please tell us which salary months are affected." in a2
          and "offer mediation" in a2 and w.ticket()["complaint_level"] == "P3"
          and w.ticket()["priority"] == "medium" and f"`{w.ticket()['ticket_number']}`" in a2)

    # 6. P4: answered, no ticket.
    w = ComplaintWorld()
    with patch.object(_ct_node, "_kb_answer", new=AsyncMock(return_value="You get one rest day a week.")):
        r6, a6 = await w.turn("how many rest days do I get?", _h("P4", "general"))
    check("complaint 6: 'how many rest days' -> the P4 template with the answer, no ticket",
          a6 == ("Thanks for your message. You get one rest day a week.\n\n"
                 "Is there anything else about this that is worrying you?") and not w.rows)

    # 7. Not safe to chat.
    w = ComplaintWorld()
    await w.turn("she hit me last week", llm)
    await w.turn("yes", {"speaker": "helper", "level": "none"})
    await w.turn("no", {"speaker": "helper", "level": "none"})
    _r, a7 = await w.turn("no, ma'am can see my phone", {"speaker": "helper", "level": "none"})
    tri = w.ticket()["captured_info"]
    check("complaint 7: Q3 'ma'am can see my phone' -> the NOT-SAFE template, triage stops, ticket kept",
          a7 == Q.NOT_SAFE_TO_CHAT and tri["complaint"]["safe_to_chat"] == "no"
          and tri["triage"]["stage"] == "open" and tri["triage"]["pending"] is None
          and w.ticket()["status"] == "open")
    r7, a7b = await w.turn("after 9pm", {"speaker": "helper", "level": "P1", "complaint_type": "physical",
                                         "best_time": "after 9pm"})
    check("complaint 7: the time she gives is not re-graded from the history: no P1 template to a "
          "phone someone can see (real model, 2026-11-04)",
          "Your safety comes first" not in a7b and a7b.startswith("Thank you, we have added this")
          and w.ticket()["complaint_level"] == "P2")
    check("complaint 7: the best time she gives is saved on the case",
          w.ticket()["captured_info"]["complaint"]["best_time_to_contact"] == "after 9pm")

    # 8. Several issues: the highest, of the higher type.
    w = ComplaintWorld()
    _r, a8 = await w.turn("no salary and she slaps me", _h("P3", "salary"))
    check("complaint 8: 'no salary and she slaps me' -> P2 (the highest), physical type, triage begins",
          a8 == Q.Q1 and w.ticket()["complaint_level"] == "P2"
          and w.ticket()["captured_info"]["complaint"]["complaint_type"] == "physical")

    # 9. Tagalog: translated, numbers and reference intact; a bad translation falls back.
    tagalog = ("Pasensya na. Tumawag sa pulis sa 999. Tumawag sa 995 o pumunta sa A&E. "
               "Ang iyong reference ay {ref}.")

    async def translate(system, text, **kwargs):
        ref = next(iter(guards.TICKET_REFERENCE.findall(text)), "")
        return tagalog.format(ref=ref)

    w = ComplaintWorld()
    with patch.object(_ct_node, "complete", new=translate):
        _r, a9 = await w.turn("sinaktan ako ng amo ko ngayon", _h("P1", "physical", language="Tagalog"))
    check("complaint 9: Tagalog -> P1 in Tagalog with 999, 995, A&E and the reference intact",
          "Tumawag sa pulis sa 999" in a9 and "995" in a9 and "A&E" in a9
          and f"`{w.ticket()['ticket_number']}`" in a9 and w.ticket()["complaint_level"] == "P1")
    w0 = ComplaintWorld()
    _r, kw = await w0.turn("sinaktan ako ng amo ko ngayon", RuntimeError("model down"))
    check("complaint 9: the Tagalog keywords reach P1 with the model down", w0.ticket()["complaint_level"] == "P1")
    bad = AsyncMock(return_value="Pasensya na, tumawag sa pulis.")
    w1 = ComplaintWorld()
    with patch.object(_ct_node, "complete", new=bad):
        _r, eng = await w1.turn("sinaktan ako ng amo ko ngayon", _h("P1", "physical", language="Tagalog"))
    check("complaint 9: a translation that loses 999 or the reference -> the English goes instead",
          eng.startswith("I'm very sorry this is happening."))

    # 10. "not safe" after P1.
    w = ComplaintWorld()
    await w.turn("my ma'am beat me just now, I'm bleeding", _h("P1", "physical"))
    r10, a10 = await w.turn("not safe", _h("P1", "physical"))
    t = w.ticket()
    check("complaint 10: 'not safe' -> the 999 line with the reference, ticket urgent (high + "
          "Escalated, status still open so the chat is not paused), safe_status=not_safe",
          a10.startswith("Please call the police on 999 now.") and t["priority"] == "high"
          and t["workflow_status"] == "escalated" and t["status"] == "open"
          and t["captured_info"]["complaint"]["safe_status"] == "not_safe" and r10.get("reply_must_send"))
    _r, a10b = await w.turn("safe", {"speaker": "helper", "level": "none"})
    _r, a10c = await w.turn("she took my things", {"speaker": "helper", "level": "none"})
    check("complaint 10: 'safe' and any other message get their fixed lines, and every message is "
          "on the ticket",
          a10b.startswith("Thank you for letting us know.")
          and a10c.startswith("Thank you, we have added this to your case") and "995" in a10c
          and "she took my things" in str(w.ticket()["captured_info"]["follow_ups"]))
    _r, again = await w.turn("she hit me again right now", _h("P1", "physical"))
    check("complaint 10: a new P1 sign on an open case -> the P1 template again",
          again.startswith("I'm very sorry this is happening."))

    # 11. No FCC number: no placeholder anywhere.
    with patch.object(settings, "fcc_contact_number", ""):
        texts = [Q.p1_emergency("X"), Q.p2_sleep("X", "2 days"), Q.p2_general("X")]
    check("complaint 11: FCC_CONTACT_NUMBER unset -> no recruiter clause, no placeholder, no gap",
          not any("recruiter" in x or "[" in x or "{" in x or "  " in x for x in texts)
          and "message us here. We have issued" in texts[0])
    with patch.object(settings, "fcc_contact_number", "NUMBER"):
        with_number = Q.p1_emergency("X"), Q.p2_general("X")
    check("complaint 11: set -> read from the setting, the clause and the sentence appear",
          "message us here or call your recruiter on NUMBER. We" in with_number[0]
          and "You can also call your recruiter on NUMBER." in with_number[1])
    source = Path(_ct_tpl_path).read_text(encoding="utf-8")
    check("complaint 11: no phone number written in the templates module",
          not re.search(r"\d{4}[ -]?\d{4}", source))

    # 12. An employer's fee question: flow unchanged, no assessment at all.
    w = ComplaintWorld(contact="employer", employer_id="e1")
    r12, a12 = await w.turn("how much is the agency fee?", _h("P4", "general"))
    check("complaint 12: employer fee question -> normal flow, no model call for severity, no "
          "'worrying you?' line, no ticket",
          w.classified == 1 and w.assessed == 0 and "worrying" not in a12 and not w.rows)
    w = ComplaintWorld()
    await w.turn("my helper wants a salary increase, what should I do", {"speaker": "employer", "level": "P3"})
    check("complaint 12: an unknown sender the model reads as an employer -> normal flow",
          w.classified == 1 and not w.rows)
    w = ComplaintWorld(contact="employer", employer_id="e1")
    r12b, _a = await w.turn("my helper is unconscious, please help")
    check("complaint 12: an employer's emergency -> the crisis path (999/995), not the helper flow",
          r12b.get("intent") == "dispute_assault" and w.assessed == 0)

    # 13. Silent after Q1.
    w = ComplaintWorld()
    await w.turn("she hit me last week", llm)
    t = w.ticket()
    check("complaint 13: silent after Q1 -> the ticket already exists at the provisional level, on "
          "the Complaints tab",
          t and t["complaint_level"] == "P2" and _complaints_tab(t)
          and t["captured_info"]["triage"]["pending"] == "Q1")

    # 14. Switch off: the old crisis reply, exactly.
    w = ComplaintWorld()
    async def old_reply(state):
        return ("I'm so sorry. Please call the police at 999 immediately. I've alerted our team and a "
                "live agent will connect with you shortly.")
    with patch.object(settings, "complaint_triage_enabled", False), \
            patch.object(_graph.complaint_triage, "decide_turn", new=AsyncMock(side_effect=AssertionError)), \
            patch.object(_ct_he, "_assault_reply", new=old_reply), \
            patch.object(_ct_he.handover_service, "log_escalation", new=AsyncMock(return_value={})), \
            patch.object(_ct_he.assignment_service, "resolve_agent", new=AsyncMock(return_value=(None, "round_robin"))), \
            patch.object(_ct, "_ask_model", new=AsyncMock(side_effect=AssertionError)), \
            patch.object(_graph, "_graph", w.model), patch.object(ticket_service, "db", SimpleNamespace(
                insert_numbered=w.insert_numbered, rpc=AsyncMock(return_value=None), execute=AsyncMock())):
        r14 = await _graph.run_turn("off-1", {
            "conversation_id": 77, "phone": CLIENT, "contact_type": "unknown", "blocked_topics": {},
            "incoming_text": "my ma'am beat me up", "history_text": "", "media_items": []})
    reply14 = r14.get("reply") or ""
    check("complaint 14: COMPLAINT_TRIAGE_ENABLED=false -> the old crisis reply (MOM helpline, 995 "
          "line), a dispute_assault ticket with no level, no complaint step",
          _ct_he.MOM_HELPLINE_LINE in reply14 and _ct_he.AMBULANCE_LINE in reply14
          and w.rows and "complaint_level" not in w.rows[-1] and not r14.get("reply_is_fixed"))

    # 15 + 16. Which tab.
    w = ComplaintWorld()
    await w.turn("my ma'am beat me just now, I'm bleeding", _h("P1", "physical"))
    check("complaint 15: the P1 ticket is in the Complaints tab's query and not the General tab's",
          _complaints_tab(w.ticket()) and w.ticket()["service_type"] == ["dispute_assault"])
    plain = ComplaintWorld()
    with patch.object(ticket_service, "db", SimpleNamespace(
            insert_numbered=plain.insert_numbered, rpc=AsyncMock(return_value=None))):
        await ticket_service.create(conversation={"id": 1}, service_type="fee_enquiry", captured_info={},
                                    assigned_agent_id=None, assignment_rule="round_robin")
    check("complaint 16: an employer's fee ticket is in General, not Complaints, and carries no level",
          not _complaints_tab(plain.ticket()) and "complaint_level" not in plain.ticket())
    t = w.ticket()
    t["captured_info"]["topic_key"] = ticket_service.HELPER_COMPLAINT
    merged = await ticket_service.pick_ticket_to_update({"helper_complaint": t}, message="fee?", service_type="fee_enquiry")
    check("complaint 16: a later fee ticket on the same chat is not folded into the complaint", merged is None)

    # 17. P4 resolved / unresolved / no KB answer.
    w = ComplaintWorld()
    with patch.object(_ct_node, "_kb_answer", new=AsyncMock(return_value="You get one rest day a week.")):
        await w.turn("how many rest days do I get?", _h("P4", "general"))
    _r, a17 = await w.turn("no thanks", {"speaker": "helper", "level": "none"})
    check("complaint 17: a P4 question answered, 'no thanks' -> no ticket, nothing more sent",
          not w.rows and not a17)
    w = ComplaintWorld()
    with patch.object(_ct_node, "_kb_answer", new=AsyncMock(return_value="You get one rest day a week.")):
        await w.turn("how many rest days do I get?", _h("P4", "general"))
    _r, a17b = await w.turn("yes I still don't get any, it is not solved", {"speaker": "helper", "level": "none"})
    check("complaint 17: the same question left unresolved -> a P4 ticket on the Complaints tab, "
          "reference sent",
          w.ticket() and w.ticket()["complaint_level"] == "P4" and _complaints_tab(w.ticket())
          and f"`{w.ticket()['ticket_number']}`" in a17b)
    w = ComplaintWorld()
    with patch.object(_ct_node, "_kb_answer", new=AsyncMock(return_value=None)):
        _r, a17c = await w.turn("can I use my phone at night?", _h("P4", "general"))
    check("complaint 17: the records hold no answer -> a P4 ticket, not an invented answer",
          w.ticket()["complaint_level"] == "P4" and "We have issued a ticket." in a17c)

    # The P1 message does not wait on the database.
    w = ComplaintWorld()
    calls = []

    async def slow(*a, **k):
        await asyncio.sleep(0.2)
        return {"id": "late-1", "ticket_number": "CB-2026-0990"}

    sent: list[str] = []

    async def capture(conv, body):
        sent.append(body)

    with patch.object(_ct_cc, "raise_ticket", new=slow), patch.object(_ct_node, "P1_TICKET_SECONDS", 0.01), \
            patch.object(_ct_node.message_service, "send_bot_reply", new=capture):
        _r, fast = await w.turn("my ma'am beat me just now, I'm bleeding", _h("P1", "physical"))
        no_ref = "reference" not in fast and fast.endswith('Can you reply "safe" or "not safe"?')
        await asyncio.sleep(0.3)
    check("complaint: P1 ticket slower than the limit -> the template goes at once with no reference "
          "line, then 'Your reference is ...' follows",
          no_ref and sent == ["Your reference is `CB-2026-0990`."])

    # Guardrails, over every template.
    every = [Q.p1_emergency("R"), Q.P1_SUICIDE, Q.p2_sleep("R", "2 days"), Q.p2_general("R"), Q.p3("R", "salary"),
             Q.p4("x"), Q.NOT_SAFE_TO_CHAT, Q.FOLLOW_SAFE, Q.FOLLOW_NOT_SAFE, Q.FOLLOW_OTHER, Q.P3_ADDED,
             Q.P4_TICKET, *Q.QUESTIONS.values(), Q.Q6_SEXUAL]
    check("complaint guardrails: no template says confront your employer, promises an outcome, gives "
          "legal or medical advice, accuses, says we will contact the employer, or asks for details",
          not any(Q.FORBIDDEN.search(x) for x in every))
    check("complaint guardrails: the sexual type asks only 'when did it last happen', never for details",
          _ct_node._text_for("QUESTION", {"pending": "Q6", "complaint_type": "sexual"}, None) == "When did it last happen?")
    check("complaint guardrails: the check itself catches each thing",
          all(Q.FORBIDDEN.search(x) for x in (
              "Please confront your employer.", "We will contact your employer today.",
              "We guarantee this.", "It will be resolved.", "You can sue her.", "Take some medicine.",
              "Your employer is guilty.", "Tell us exactly what he did.")))
    # Outside working hours: nothing reads the clock. Asserted over the two modules.
    for mod in (_ct, _ct_node):
        src = Path(mod.__file__).read_text(encoding="utf-8")
    check("complaint: outside working hours the flow is identical (the only date read is the follow-up day)",
          "business_hours" not in Path(_ct.__file__).read_text(encoding="utf-8"))
    with patch.object(settings, "working_days", "Mon,Tue,Wed,Thu,Fri"), \
            patch.object(settings, "public_holidays", "2026-11-09"):
        day = _ct.next_working_day(date(2026, 11, 6))  # a Friday, then a holiday Monday
    check("complaint: next_followup_date skips the weekend and a listed public holiday",
          day == date(2026, 11, 10))
    check("complaint: the safe-word reply is read before the model, 'not safe' on a helper's own words",
          _ct._NOT_SAFE_WORD.search("I am not safe") and _ct._SAFE_WORD.search("safe")
          and not _ct._SAFE_WORD.search("not safe"))


async def scenario_complaint_webhook():
    """The two webhook paths: a P1 is never dropped or held, and a chat a human holds
    still gets the complaint's safety reply. The real _process_locked and send_bot_reply."""
    conversation = {"id": 77, "customer_number": CLIENT.lstrip("+"), "bot_status": _conv.BOT_ACTIVE,
                    "status": "open", "langgraph_thread_id": "t-77", "contact_type": "unknown",
                    "last_bot_reply_at": NOW.isoformat()}
    sent: list[str] = []

    async def send_text(to, body, **kwargs):
        sent.append(body)
        return {"message": {"id": f"out-{len(sent)}"}}

    none, empty = AsyncMock(return_value=None), AsyncMock(return_value=[])

    async def one_turn(result, history=()):
        sent.clear()
        ms = _wh.message_service
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in (
                patch.object(_wh.conversation_service, "get_by_phone", new=AsyncMock(return_value=dict(conversation))),
                patch.object(ms, "get_history", new=AsyncMock(return_value=list(history))),
                patch.object(_wh.ticket_service, "recent_for_conversation", new=empty),
                patch.object(_wh.lead_service, "find_by_phone", new=none),
                patch.object(_wh.contact_service, "count_prior_hires", new=AsyncMock(return_value=0)),
                patch.object(_wh.contact_service, "get_placed_helper", new=none),
                patch.object(_wh.contact_service, "get_placement_helpers", new=empty),
                patch.object(_wh.ticket_service, "helpers_on_conversation", new=empty),
                patch.object(_wh.contact_service, "get_record_name", new=none),
                patch.object(_wh.contact_service, "get_cases", new=empty),
                patch.object(_wh.contact_service, "get_candidate_record", new=none),
                patch.object(_wh.ticket_service, "open_topics_for_conversation", new=empty),
                patch.object(_wh.ticket_service, "completed_topics_for_conversation", new=empty),
                patch.object(_wh, "last_human_activity_at", new=none),
                patch.object(_wh, "run_turn", new=AsyncMock(return_value=result)),
                patch.object(_wh.debouncer, "has_pending", new=AsyncMock(return_value=True)),
                patch.object(ms.whapi, "send_text", new=send_text),
                patch.object(ms, "_insert_ignoring_duplicates", new=none),
                patch.object(ms.conversation_service, "touch_outbound", new=none),
                patch.object(_wh.conversation_service, "update", new=none),
                patch.object(_wh.typing_indicator, "stop", new=AsyncMock()),
            ):
                stack.enter_context(p)
            await _wh._process_locked(CLIENT, [message("help", mid="c-1")])
        return list(sent)

    out = await one_turn({"reply": "P1 TEMPLATE", "reply_must_send": True, "extra_replies": ["REF LINE"]})
    check("complaint webhook: a P1 goes out while she is still typing, and the reference follows as "
          "its own message", out == ["P1 TEMPLATE", "REF LINE"])
    out = await one_turn({"reply": "ordinary reply"})
    check("complaint webhook: an ordinary reply is still held while she is typing", out == [])
    earlier = [{"created_at": NOW.isoformat(), "direction": "outbound", "is_bot": True, "sent_by": "bot",
                "body": "Please call the police on 999 now."}]
    fixed = "Please call the police on 999 now."
    with patch.object(_wh, "recent_bot_lines", new=lambda *a: [fixed]):
        out = await one_turn({"reply": fixed, "reply_must_send": True}, earlier)
    check("complaint webhook: the 999 line is sent again however recently it went out", out == [fixed])

    # A chat a human holds.
    raised = AsyncMock(return_value={"id": "e1", "ticket_number": "CB-2026-0991"})
    delivered: list[str] = []

    async def deliver(conv, body):
        delivered.append(body)

    held = {**conversation, "bot_status": _conv.HUMAN_ACTIVE}
    with patch.object(_wh, "confirm_harm", new=AsyncMock(return_value=True)), \
            patch.object(_ct_cc, "raise_ticket", new=raised), \
            patch.object(_ct, "_ask_model", new=AsyncMock(return_value=_h("P1", "physical"))), \
            patch.object(_wh.message_service, "send_bot_reply", new=deliver):
        done = await _wh.emergency_override(held, "my ma'am beat me just now, I'm bleeding")
    check("complaint webhook: a report on a chat a human holds gets the same P1 template, reference "
          "included, with a complaint ticket",
          done and delivered and delivered[0].startswith("I'm very sorry this is happening.")
          and "`CB-2026-0991`" in delivered[0] and raised.await_count == 1)
    delivered.clear()
    old = AsyncMock(return_value={"id": "e2", "ticket_number": "CB-2026-0992"})
    with patch.object(settings, "complaint_triage_enabled", False), \
            patch.object(_wh, "confirm_harm", new=AsyncMock(return_value=True)), \
            patch.object(_wh.assignment_service, "resolve_agent", new=AsyncMock(return_value=(None, "round_robin"))), \
            patch.object(_wh.ticket_service, "create", new=old), \
            patch.object(_wh.handover_service, "_log", new=AsyncMock()), \
            patch.object(_wh.message_service, "send_bot_reply", new=deliver):
        await _wh.emergency_override(held, "my ma'am beat me just now, I'm bleeding")
    check("complaint webhook: switch off -> the old emergency reply", delivered == [_wh.ASSAULT_FALLBACK_REPLY])

    # The reply guards leave approved wording alone.
    w = ComplaintWorld()
    w.history = []
    leave = {"service_type": "home_leave", "collected_info": {"leave_reason": "family matter"}}
    with patch.object(_graph, "_graph", SimpleNamespace(ainvoke=AsyncMock(return_value={
            **leave, "reply": _ct_t.p1_emergency("CB-2026-0900"), "reply_is_fixed": True}))), \
            patch.object(_graph, "STALE_INTAKE_RESET", False):
        r = await _graph.run_turn("t", {"incoming_text": "family emergency, she hit me", "history_text": ""})
    check("complaint: approved wording passes run_turn untouched apart from the reference as code",
          r["reply"] == _ct_t.p1_emergency("`CB-2026-0900`"))


async def main() -> int:
    for scenario in (scenario_defaults, scenario_receipt_chat_kind, scenario_ticket_closed,scenario_staff_then_okay, scenario_internal_number, scenario_quoted_reply,
                     scenario_ticket_picked_up, scenario_stale_intake, scenario_helper_rights,
                     scenario_vague_action, scenario_hire_documents, scenario_call_and_wording,
                     scenario_xing_chain, scenario_vd_transfer, scenario_direct_hire_summary,
                     scenario_direct_hire_intake,
                     scenario_replacement_summary,
                     scenario_crisis_reply,
                     scenario_crisis_keywords,
                     scenario_home_leave_2nov,
                     scenario_complaint_triage,
                     scenario_complaint_webhook):
        try:
            await scenario()
        except Exception as exc:  # noqa: BLE001 - a crash is a failure, by name
            results.append((f"{scenario.__name__} CRASHED: {type(exc).__name__}: {exc}", False))
    failed = 0
    for name, ok in results:
        print(("PASS  " if ok else "FAIL  ") + name)
        failed += not ok
    print(f"\n{len(results) - failed} of {len(results)} pass")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
