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


async def main() -> int:
    for scenario in (scenario_defaults, scenario_receipt_chat_kind, scenario_ticket_closed,scenario_staff_then_okay, scenario_internal_number, scenario_quoted_reply,
                     scenario_ticket_picked_up, scenario_stale_intake, scenario_helper_rights):
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
