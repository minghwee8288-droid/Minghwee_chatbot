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
from app.whapi.parser import IncomingMessage  # noqa: E402

import importlib  # noqa: E402

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
              and "From the helper:\n3. A copy of her passport\n4. Her medical report\n5. Her school certificate" in t)
    check("in Singapore: her current Work Permit details are asked for",
          "6. Her current Work Permit details" in text["in Singapore"]
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


async def main() -> int:
    for scenario in (scenario_defaults, scenario_receipt_chat_kind, scenario_ticket_closed,scenario_staff_then_okay, scenario_internal_number, scenario_quoted_reply,
                     scenario_ticket_picked_up, scenario_stale_intake, scenario_helper_rights,
                     scenario_vague_action, scenario_hire_documents, scenario_call_and_wording,
                     scenario_xing_chain, scenario_vd_transfer, scenario_direct_hire_summary):
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
