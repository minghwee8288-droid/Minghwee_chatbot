"""Offline self-check for POST /internal/suggest (app/suggest/).

Runs the real endpoint, context builder, retrieval node, prompt builder and
validator with the database, the model and WhatsApp replaced by in-process
fakes. Nothing touches the network: Supabase is a fake client, the embedding
and the model are stubs, and httpx's real transports refuse to send.

    python scripts/selfcheck_suggest.py

Exit 1 on any failure.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import readonly  # noqa: E402
from app.api import webhook as _wh  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import supabase as _supabase  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.graph import graph as _graph  # noqa: E402
from app.graph import llm as _bot_llm  # noqa: E402
from app.services import assignment as _assignment  # noqa: E402
from app.services import conversation as _conv  # noqa: E402
from app.services import handover as _handover  # noqa: E402
from app.services import kb_rules  # noqa: E402
from app.services import lead as _lead  # noqa: E402
from app.services import message as _msg  # noqa: E402
from app.services import rag  # noqa: E402
from app.services import ticket as _ticket  # noqa: E402
from app.suggest import context as sg_context  # noqa: E402
from app.suggest import llm as sg_llm  # noqa: E402
from app.suggest import meaning as sg_meaning  # noqa: E402
from app.suggest import prompt as sg_prompt  # noqa: E402
from app.suggest import retrieval as sg_retrieval  # noqa: E402
from app.suggest import router as sg_router  # noqa: E402
from app.suggest import settings as sg_settings  # noqa: E402
from app.suggest import validate as sg_validate  # noqa: E402
from app.whapi import debouncer as _debouncer  # noqa: E402
from app.whapi import typing as _typing  # noqa: E402
from app.whapi.client import WhapiClient  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SECRET = "test-suggest-secret-0123456789"
TENANT = "tenant-under-test"
CONVERSATION_ID = 501
ANCHOR_ID = 7

# --- No network at all ------------------------------------------------------

NETWORK_ATTEMPTS: list[str] = []


def _refuse_sync(self, request):  # noqa: ANN001
    NETWORK_ATTEMPTS.append(str(request.url.host))
    raise httpx.ConnectError("selfcheck_suggest: network refused")


async def _refuse_async(self, request):  # noqa: ANN001
    NETWORK_ATTEMPTS.append(str(request.url.host))
    raise httpx.ConnectError("selfcheck_suggest: network refused")


# --- A fake Supabase --------------------------------------------------------

class Store:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.kb: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str]] = []
        self.raise_on_write = True


class FakeQuery:
    def __init__(self, store: Store, table: str) -> None:
        self.store = store
        self.table = table
        self.http_method = "GET"
        self.path = f"/rest/v1/{table}"
        self.max_id: Any = None
        self.orders: list[tuple[str, bool]] = []

    def _write(self, method: str) -> "FakeQuery":
        self.http_method = method
        return self

    def insert(self, *a: Any, **k: Any) -> "FakeQuery":
        return self._write("POST")

    def upsert(self, *a: Any, **k: Any) -> "FakeQuery":
        return self._write("POST")

    def update(self, *a: Any, **k: Any) -> "FakeQuery":
        return self._write("PATCH")

    def delete(self, *a: Any, **k: Any) -> "FakeQuery":
        return self._write("DELETE")

    def __getattr__(self, name: str) -> Any:  # select, eq, order, limit, ...
        return lambda *a, **k: self

    def order(self, column: str, desc: bool = False, **_: Any) -> "FakeQuery":
        self.orders.append((column, desc))
        return self

    def lte(self, column: str, value: Any) -> "FakeQuery":  # the eval's as-of read
        self.max_id = value if column == "id" else None
        return self

    def execute(self) -> Any:
        if self.http_method != "GET":
            self.store.writes.append((self.http_method, self.table))
            if self.store.raise_on_write:
                raise AssertionError(f"WRITE reached the database: {self.http_method} {self.table}")
            return SimpleNamespace(data=[{"id": "written"}], count=None)
        rows = list(self.store.rows.get(self.table, []))
        if self.max_id is not None:
            rows = [r for r in rows if isinstance(r.get("id"), int) and r["id"] <= self.max_id]
        # As PostgREST: the requested keys in turn; a full tie keeps the
        # stored order, which stands in for the database's arbitrary one.
        for column, desc in reversed(self.orders):
            if rows and all(column in r for r in rows):
                rows.sort(key=lambda r: r[column] if isinstance(r[column], int) else str(r[column]),
                          reverse=desc)
        return SimpleNamespace(data=rows, count=None)


class FakeClient:
    def __init__(self, store: Store) -> None:
        self.store = store

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self.store, name)

    def rpc(self, name: str, params: dict) -> Any:
        store = self.store
        if name != rag.KB_MATCH_FUNCTION:
            store.writes.append(("RPC", name))
            raise AssertionError(f"RPC reached the database: {name}")
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=list(store.kb)))


# --- Fixtures ---------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def _at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def message_rows() -> list[dict[str, Any]]:
    """Oldest first."""
    return [
        {"id": 1, "direction": "inbound", "is_bot": False, "sent_by": None,
         "body": "Hi, I need to renew my helper's work permit", "created_at": _at(60)},
        {"id": 2, "direction": "outbound", "is_bot": True, "sent_by": "bot",
         "body": "Hi, I'm Claire, Ming Hwee's AI assistant. May I know your name?", "created_at": _at(59)},
        {"id": 3, "direction": "outbound", "is_bot": True, "sent_by": "whatsapp_auto",
         "body": "Thank you for your message. Our team will reply next working day.", "created_at": _at(59)},
        {"id": 4, "direction": "inbound", "is_bot": False, "sent_by": None,
         "body": "Tan", "created_at": _at(30)},
        {"id": 5, "direction": "outbound", "is_bot": False, "sent_by": "agent",
         "body": "Hello Mr Tan, noted.", "created_at": _at(25)},
        {"id": 6, "direction": "outbound", "is_bot": False, "sent_by": "bot",
         "body": "Thanks, Tan. When does the permit expire?", "created_at": _at(20)},
        {"id": 7, "direction": "inbound", "is_bot": False, "sent_by": None,
         "body": "How much does it cost? My NRIC is S1234567D", "created_at": _at(1)},
    ]


CONVERSATION = {
    "id": CONVERSATION_ID, "customer_number": "6590001234", "customer_name": "Tan",
    "contact_type": "employer", "matched_employer_id": None, "matched_candidate_id": None,
    "matched_supplier_id": None, "matched_case_id": None, "bot_status": "bot_active",
    "langgraph_thread_id": "thread-501",
}

KB_ROWS = [
    {"id": "kb-1", "similarity": 0.81, "chunk_type": "qa_pair",
     "question": "How much does it cost to renew my helper's work permit?",
     "answer": "The work permit renewal fee is $695, and it takes about 1 week.",
     "content": None, "source_document": "Ming Hwee Service Notes", "service_type": "renewal",
     "contact_type": "all", "nationality": "all", "metadata": {}},
    {"id": "kb-2", "similarity": 0.62, "chunk_type": "qa_pair",
     "question": "What documents do I need to renew a work permit?",
     "answer": "We need a copy of your NRIC and her current Work Permit.",
     "content": None, "source_document": "Ming Hwee Service Notes", "service_type": "renewal",
     "contact_type": "all", "nationality": "all", "metadata": {}},
]

GOOD = {"label": "Answer", "text": "The work permit renewal fee is $695, and it usually takes about 1 week.",
        "kb_refs": ["kb-1"], "confidence": "high"}
CLARIFY = {"label": "Clarify", "text": "Could you tell me when the permit expires, so I can check the timing?",
           "kb_refs": [], "confidence": "high"}


def model_output(*drafts: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"suggestions": list(drafts), "kb_gap": False, "sensitive": False,
            "language": "English", **extra}


class FakeGraph:
    async def aget_state(self, config: dict) -> Any:
        return SimpleNamespace(
            values={"service_type": "renewal", "intent": "renewal",
                    "collected_info": {"full_name": "Tan"}},
            created_at=_at(20),
        )

    async def ainvoke(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("the graph was RUN (checkpoint write)")

    async def aupdate_state(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("the checkpoint was WRITTEN")


class FakeLLM:
    """Stands in for the suggestion client's AsyncOpenAI."""

    def __init__(self, output: Any, delay: float = 0.0) -> None:
        self.output = output
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        content = self.output if isinstance(self.output, str) else json.dumps(self.output)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1234, completion_tokens=210),
        )


class World:
    """Everything faked for one check."""

    def __init__(self, output: Any = None, delay: float = 0.0) -> None:
        self.store = Store()
        self.store.rows["wp_chat_conversations"] = [dict(CONVERSATION)]
        # The app reads newest first, as PostgREST would return them.
        self.store.rows["wp_chat_messages"] = list(reversed(message_rows()))
        self.store.kb = [dict(r) for r in KB_ROWS]
        self.llm = FakeLLM(output if output is not None else model_output(GOOD, CLARIFY), delay)
        self.vectors: dict[str, list[float]] = {}
        self.embedded: list[list[str]] = []
        self.stack = ExitStack()

    def __enter__(self) -> "World":
        s = self.stack
        s.enter_context(patch.object(_supabase, "get_supabase", lambda: FakeClient(self.store)))
        s.enter_context(patch.object(_graph, "get_graph", lambda: FakeGraph()))
        s.enter_context(patch.object(sg_llm, "_client", lambda: self.llm))

        async def _embed(text: str) -> list[float]:
            return [0.0] * 8

        s.enter_context(patch.object(rag, "embed_query", _embed))

        async def _meaning(texts: list[str]) -> list[list[float]]:
            self.embedded.append(list(texts))
            return [self.vectors.get(t) or [1.0 if i == n else 0.0 for i in range(len(texts))]
                    for n, t in enumerate(texts)]

        s.enter_context(patch.object(sg_meaning, "_embed", _meaning))
        s.enter_context(patch.object(rag, "_vocabularies", {}))
        s.enter_context(patch.object(settings, "tenant_id", TENANT))
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stack.close()


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- Tripwire: every writer, sender and cache the live bot uses -------------

TRIPWIRE: list[tuple[Any, str]] = [
    (_msg, "send_bot_reply"), (_msg, "mark_sent_by_bot"), (_msg, "mark_body_sent_by_bot"),
    (_msg, "remember_bot_reply"), (_msg, "store_incoming"), (_msg, "store_agent_reply"),
    (_msg, "store_auto_reply"), (_msg, "store_own_echo"), (_msg, "_insert_ignoring_duplicates"),
    (_conv, "update"), (_conv, "get_or_create"), (_conv, "touch_inbound"),
    (_conv, "touch_outbound"), (_conv, "set_bot_status"),
    (_handover, "_log"), (_handover, "to_human"), (_handover, "log_escalation"),
    (_handover, "agent_took_over"), (_handover, "undo_agent_takeover"),
    (_handover, "back_to_bot"), (_handover, "resume_from_pause"),
    (_ticket, "create"), (_ticket, "add_follow_up"), (_ticket, "set_also_asked"),
    (_ticket, "add_service_type"), (_ticket, "add_topic_key"), (_ticket, "merge_into"),
    (_ticket, "next_ticket_number"),
    (_lead, "create_if_absent"), (_lead, "update_from_collected"), (_lead, "write_summary"),
    (_lead, "next_lead_number"),
    (_assignment, "resolve_agent"), (_assignment, "claim_portal_owner"),
    (_assignment, "_next_round_robin_agent"),
    (_wh, "identify_contact"), (_wh, "_already_processed"), (_wh, "_schedule_read_receipt"),
    (_wh, "_remember_standdown"), (_wh, "_fail_over_to_human"), (_wh, "_conversation_lock"),
    (_wh, "process_batch"), (_wh, "maybe_return_to_bot"),
    (_typing, "start"), (_typing, "stop"), (_typing, "_keep_alive"),
    (_debouncer.MessageDebouncer, "add"), (_debouncer.MessageDebouncer, "flush_now"),
    (_debouncer.MessageDebouncer, "drain"), (_debouncer.MessageDebouncer, "requeue"),
    (WhapiClient, "send_text"), (WhapiClient, "send_media"), (WhapiClient, "send_presence"),
    (WhapiClient, "mark_read"), (WhapiClient, "_post"), (WhapiClient, "_get"),
    (WhapiClient, "_get_client"), (WhapiClient, "resolve_lid"),
    (_bot_llm, "complete"), (_bot_llm, "complete_json"),
    (kb_rules, "refresh"), (_graph, "run_turn"), (_graph, "init_graph"),
]

CACHES: list[tuple[Any, str]] = [
    (_msg, "_SENT_BY_BOT"), (_msg, "_SENT_BODIES"), (_msg, "_LAST_BOT_REPLY"),
    (_msg, "_AUTO_REPLY_VERDICTS"), (_msg, "_OUTBOUND_BODY_SEEN"),
    (_wh, "_PROCESSED"), (_wh, "_SILENCED_BY_BODY"), (_wh, "_IDENTIFIED_AT"), (_wh, "_LOCKS"),
]


def _snapshot() -> dict[str, Any]:
    return {f"{getattr(o, '__name__', o)}.{n}": repr(getattr(o, n)) for o, n in CACHES}


def tripwired(called: list[str]) -> ExitStack:
    stack = ExitStack()
    for owner, name in TRIPWIRE:
        original = getattr(owner, name)
        label = f"{getattr(owner, '__name__', owner)}.{name}"
        if inspect.iscoroutinefunction(original):
            async def boom(*a: Any, _label: str = label, **k: Any) -> Any:
                called.append(_label)
                raise AssertionError(f"tripwire: {_label} was called")
        else:
            def boom(*a: Any, _label: str = label, **k: Any) -> Any:  # type: ignore[misc]
                called.append(_label)
                raise AssertionError(f"tripwire: {_label} was called")
        stack.enter_context(patch.object(owner, name, boom))
    return stack


# --- Checks -----------------------------------------------------------------

def checks_labelling() -> None:
    rows = message_rows()
    items = sg_context.labelled(rows)
    roles = [(i["id"], i["role"]) for i in items]
    check("history: inbound -> CUSTOMER, is_bot -> CLAIRE, agent -> STAFF, "
          "sent_by='bot' with is_bot false -> CLAIRE, whatsapp_auto excluded",
          roles == [(1, "CUSTOMER"), (2, "CLAIRE"), (4, "CUSTOMER"), (5, "STAFF"),
                    (6, "CLAIRE"), (7, "CUSTOMER")], str(roles))
    staff = [i for i in items if i["role"] == "STAFF"][0]
    check("history: the staff member's sent_by is kept as the name", staff["name"] == "agent")
    text = sg_context.render(items)
    check("history: rendered in the bot's own labels Client:/You:/Agent:",
          text.splitlines()[:3] == [
              "Client: Hi, I need to renew my helper's work permit",
              "You: Hi, I'm Claire, Ming Hwee's AI assistant. May I know your name?",
              "Client: Tan"] and "Agent: Hello Mr Tan, noted." in text, text)
    check("history: the staff name is never in the prefix (guards read 'Agent:')",
          all(not line.startswith("Agent (") for line in text.splitlines()))

    reason, earlier, batch = sg_context.split_at_anchor(rows, 7)
    check("anchor: the newest inbound message is not stale", reason == "" and [r["id"] for r in batch] == [7])
    reason, _, _ = sg_context.split_at_anchor(rows, 4)
    check("anchor: an older inbound message is stale", reason == sg_context.NOT_NEWEST_INBOUND)
    answered = rows + [{"id": 8, "direction": "outbound", "is_bot": False, "sent_by": "agent",
                        "body": "It is $695.", "created_at": _at(0)}]
    reason, _, _ = sg_context.split_at_anchor(answered, 7)
    check("anchor: a message already answered by staff is stale", reason == sg_context.ALREADY_ANSWERED)
    burst = rows + [{"id": 9, "direction": "inbound", "is_bot": False, "sent_by": None,
                     "body": "and how long?", "created_at": _at(0)}]
    reason, _, batch = sg_context.split_at_anchor(burst, 9)
    check("anchor: consecutive client messages are one batch, as the debouncer batches them",
          reason == "" and [r["id"] for r in batch] == [7, 9])


def checks_identity() -> None:
    bad = [
        "Hi, I'm Claire from Ming Hwee.",
        "As an AI assistant, I can help with that.",
        "I am a bot, but our team will help.",
        "This chatbot can answer that.",
        "I'm Ming Hwee's virtual assistant.",
        "Our A.I system says $695.",
    ]
    good = [
        "The fee is $695 and we will handle everything for you.",
        "Kami akan bantu urus permit pekerja anda.",  # Malay/Indonesian, no false hit
        "I will check with our team and confirm the timing.",
        "Our admin assistant will email you the form.",
    ]
    check("identity: drafts naming Claire or claiming to be an AI/bot/assistant are caught",
          all(sg_validate.identifies_as_claire_or_ai(t) for t in bad),
          str([t for t in bad if not sg_validate.identifies_as_claire_or_ai(t)]))
    check("identity: ordinary consultant drafts are not caught",
          not any(sg_validate.identifies_as_claire_or_ai(t) for t in good),
          str([t for t in good if sg_validate.identifies_as_claire_or_ai(t)]))


def checks_grounding_units() -> None:
    check("grounding: S$ amounts and day/week/month counts are found",
          sg_validate.figures_in("It costs S$1,200 and takes 2 to 3 weeks, or 5 working days.")
          == {"1200", "2", "3", "5"})
    check("grounding: a figure the sources state is grounded",
          not sg_validate.ungrounded("about 1 week for $695", "fee is $695, takes about 1 week"))
    check("grounding: a duration the sources do not state is caught",
          sg_validate.ungrounded("it takes 3 weeks", "fee is $695, takes about 1 week") == {"3"})


def _suggest(world: World, conversation_id: int = CONVERSATION_ID, anchor: int = ANCHOR_ID,
             as_of: bool = False) -> dict:
    async def go() -> dict:
        with readonly.read_only() as refused:
            body = await sg_router.run_suggest(conversation_id, anchor, as_of=as_of)
        body["_refused"] = list(refused)
        return body
    return run(go())


def checks_drafting() -> None:
    with World() as world:
        out = _suggest(world)
        check("ready: two valid drafts are returned", out["status"] == "ready"
              and [s["label"] for s in out["suggestions"]] == ["Answer", "Clarify"], json.dumps(out)[:400])
        check("ready: kb_refs are the retrieved ids", out["suggestions"][0]["kb_refs"] == ["kb-1"])
        check("ready: usage and model are reported",
              out["usage"] == {"prompt_tokens": 1234, "completion_tokens": 210} and out["model"])
        check("ready: no write was even attempted (read-only refusals empty)",
              out["_refused"] == [] and world.store.writes == [], str(world.store.writes))
        call = world.llm.calls[0]
        system = call["messages"][0]["content"]
        user = call["messages"][1]["content"]
        check("llm: JSON mode, temperature 0.3, the suggestion model",
              call["response_format"] == {"type": "json_object"} and call["temperature"] == 0.3
              and call["model"] == out["model"])
        check("prompt: Claire's system prompt with the consultant wrapper last",
              "--- This message ---" in system and "YOU ARE NOT REPLYING TO THE CLIENT" in system
              and system.rstrip().endswith(sg_prompt.SUGGEST_WRAPPER.rstrip()[-80:]))
        check("prompt: the wrapper overrides the identity rules",
              "Never introduce yourself, never use the name Claire" in system
              and '"You:" lines were written by Claire' in system
              and '"Agent:" lines by our human staff' in system)
        check("prompt: the records carry their refs", "[ref kb-1]" in system and "$695" in system)
        check("prompt: the NRIC never reaches the model",
              "S1234567D" not in system and "S1234567D" not in user and "[REDACTED-ID]" in user)
        check("prompt: the auto-reply is not in the transcript", "next working day" not in user)
        check("prompt: history uses the bot's labels",
              "Agent: Hello Mr Tan, noted." in user and "You: Thanks, Tan." in user)

    invented_money = {"label": "Answer", "text": "The renewal fee is S$850.", "kb_refs": ["kb-1"],
                      "confidence": "high"}
    invented_time = {"label": "Answer", "text": "It usually takes about 3 weeks.",
                     "kb_refs": ["kb-1"], "confidence": "high"}
    uncited_time = {"label": "Answer", "text": "It usually takes about 1 week.",
                    "kb_refs": [], "confidence": "high"}
    bad_ref = {"label": "Answer", "text": "We will confirm the timing shortly.",
               "kb_refs": ["kb-999"], "confidence": "high"}
    nric = {"label": "Clarify", "text": "Thanks, I have noted S1234567D. When does her permit expire?",
            "kb_refs": [], "confidence": "high"}
    claire = {"label": "Next step", "text": "Hi, I'm Claire. Our team will confirm the fee.",
              "kb_refs": [], "confidence": "low"}
    ai = {"label": "Next step", "text": "As an AI assistant I will pass this on.",
          "kb_refs": [], "confidence": "low"}

    with World(model_output(claire, ai)) as world:
        identity_reasons = [d["reason"] for d in _suggest(world)["_dropped"]]
    with World(model_output(invented_money, invented_time, uncited_time, bad_ref, nric)) as world:
        out = _suggest(world)
        reasons = [d["reason"] for d in out["_dropped"]]
        texts = [s["text"] for s in out["suggestions"]]
        check("validate: a draft with an invented S$ amount is dropped",
              any(r == "figure not in the records" for r in reasons), str(reasons))
        check("validate: a draft with an invented duration is dropped",
              any(r.startswith("figure not in the cited records") and r.endswith("3") for r in reasons),
              str(reasons))
        check("validate: a duration only in an UNCITED record is dropped",
              sum(r.startswith("figure not in the cited records") for r in reasons) == 2, str(reasons))
        check("validate: a draft citing a record that was not retrieved is dropped",
              "cites a record that was not retrieved" in reasons, str(reasons))
        check("validate: drafts naming Claire or claiming to be an AI are dropped",
              identity_reasons.count("names Claire or claims to be an AI") == 2, str(identity_reasons))
        check("validate: an NRIC in a draft is redacted and the draft kept",
              texts == ["Thanks, I have noted [REDACTED-ID]. When does her permit expire?"], str(texts))

    # The model cites records in the shape it is shown them ("[ref <id>]").
    # Live, 2026-10-27: every cited draft was dropped for writing "ref <id>".
    wrapped = dict(GOOD, kb_refs=["ref kb-1"])
    bracketed = dict(GOOD, label="Next step", text="For a renewal we charge $695; allow about 1 week.",
                     kb_refs=["[ref kb-1]"])
    wrapped_bad = dict(GOOD, text="We will confirm the timing.", kb_refs=["ref kb-999"])
    with World(model_output(wrapped, bracketed, wrapped_bad)) as world:
        out = _suggest(world)
        reasons = [d["reason"] for d in out["_dropped"]]
        check("validate: a ref cited as 'ref <id>' or '[ref <id>]' is the retrieved id",
              [s["kb_refs"] for s in out["suggestions"]] == [["kb-1"], ["kb-1"]],
              json.dumps(out["suggestions"])[:300])
        check("validate: 'ref <unknown id>' is still dropped",
              reasons == ["cites a record that was not retrieved"], str(reasons))

    # The consultant is the live agent. Live, 2026-10-27 (eval): a draft naming
    # a colleague and a time was swapped by strip_handover_talk for Claire's
    # "a live agent will connect with you shortly" line, and kept.
    named_colleague = {"label": "Answer", "kb_refs": [], "confidence": "high",
                       "text": "Sure, I'll check with Catherine whether she is available at 11:00am and confirm."}
    handover = {"label": "Next step", "kb_refs": [], "confidence": "low",
                "text": "I've passed this to our team and a live agent will connect with you shortly."}
    with World(model_output(named_colleague, handover, CLARIFY)) as world:
        out = _suggest(world)
        reasons = [d["reason"] for d in out["_dropped"]]
        texts = [s["text"] for s in out["suggestions"]]
        check("validate: a draft the handover guard rewrites to Claire's line is dropped",
              not any("live agent" in t for t in texts) and len(texts) == 1, str(texts))
        check("validate: a draft announcing a handover to a live agent is dropped",
              reasons.count("hands over to a live agent") == 2, str(reasons))

    with World(model_output(invented_money, bad_ref, claire)) as world:
        out = _suggest(world)
        check("validate: zero valid drafts -> status insufficient",
              out["status"] == "insufficient" and out["suggestions"] == [], out["status"])

    many = [dict(CLARIFY, text=f"Could you tell me more about point {n}?") for n in "ABCD"]
    long = dict(CLARIFY, text=(
        "Thank you for checking with us about the renewal. Before we go ahead, could you "
        "let me know when her current permit expires, whether she will continue with you "
        "after the renewal, and if anything about her employment has changed since the last "
        "application? It also helps to know whether her passport is still valid for at least "
        "seven more months, because the ministry looks at that when the application is "
        "reviewed. If her medical examination is due soon, please tell me the clinic you "
        "usually use so we can plan around the appointment. Once I have these details I can "
        "prepare the forms, send them to you for signing, and arrange the submission so that "
        "nothing lapses while we wait for approval. Please also confirm the best time to "
        "reach you during the week, in case the officer needs anything else from you."))
    dup = dict(CLARIFY)
    with World(model_output(CLARIFY, dup, long, *many)) as world:
        out = _suggest(world)
        reasons = [d["reason"] for d in out["_dropped"]]
        check("validate: duplicates and over-700-character drafts are dropped, at most 4 kept",
              len(out["suggestions"]) <= sg_validate.MAX_DRAFTS and "duplicate" in reasons
              and "over 700 characters" in reasons, str(reasons))

    with World("this is not json") as world:
        try:
            _suggest(world)
            check("llm: output that is not JSON is an error, not drafts", False)
        except sg_llm.SuggestLLMError:
            check("llm: output that is not JSON is an error, not drafts", True)


def checks_skipped() -> None:
    """A group chat or one of our own numbers: status skipped, no model call."""
    with World() as world:
        world.store.rows["wp_chat_conversations"] = [dict(CONVERSATION, is_group=True)]
        out = _suggest(world)
        check("skipped: a group chat -> status skipped, reason group, no model call",
              out["status"] == "skipped" and out["reason"] == "group"
              and out["suggestions"] == [] and not world.llm.calls, json.dumps(out)[:200])

    with World() as world, patch.object(settings, "internal_numbers", "+65 9000 1234"):
        out = _suggest(world)
        check("skipped: an INTERNAL_NUMBERS number -> status skipped, reason internal, no model call",
              out["status"] == "skipped" and out["reason"] == "internal" and not world.llm.calls,
              json.dumps(out)[:200])

    with World() as world:
        world.store.rows["cb_internal_contacts"] = [{"phone": "6590001234", "active": True}]
        out = _suggest(world)
        check("skipped: a cb_internal_contacts number -> skipped internal",
              out["status"] == "skipped" and out["reason"] == "internal" and not world.llm.calls)

    with World() as world, patch.object(settings, "internal_numbers", "+65 9999 8888"):
        out = _suggest(world)
        check("skipped: an ordinary client number is drafted", out["status"] == "ready", out["status"])


def checks_flags() -> None:
    """Kept drafts carry flags, and any flag makes confidence low."""
    with World() as world:
        out = _suggest(world)
        good = out["suggestions"][0]
        check("flags: a draft whose figures are in the cited record has no flag and keeps its confidence",
              good["flags"] == [] and good["confidence"] == "high", json.dumps(good))
        check("flags: every kept draft carries a flags list",
              all(isinstance(s.get("flags"), list) for s in out["suggestions"]))
        system = world.llm.calls[0]["messages"][0]["content"]
        check("prompt: a yes/no is given only when the records state it",
              "gets a yes or a no ONLY if \"Our records\" state that answer explicitly" in system)

    # Live, 2026-10-27 (eval, conversation 1381): the client asked why it is
    # "52 or 78" and a draft explained both figures with invented rest-day counts.
    client_numbers = {"label": "Answer", "kb_refs": ["kb-1"], "confidence": "high",
                      "text": "The rest day compensation is $52 or $78, for 2 or 3 rest days."}
    invented_count = {"label": "Next step", "kb_refs": [], "confidence": "high",
                      "text": "We will send you 2 copies of the agreement to sign."}
    with World(model_output(client_numbers, invented_count, CLARIFY)) as world:
        world.store.rows["wp_chat_messages"][0] = dict(
            world.store.rows["wp_chat_messages"][0],
            body="Why does the rest day compensation say 52 or 78?")
        out = _suggest(world)
        by_label = {s["label"]: s for s in out["suggestions"]}
        first = by_label.get("Answer", {})
        check("flags: a number only the client wrote is kept, flagged unverified_figures, confidence low",
              "unverified_figures" in first.get("flags", []) and first.get("confidence") == "low",
              json.dumps(out["suggestions"])[:400])
        second = by_label.get("Next step", {})
        check("flags: a count nobody wrote is flagged unverified_figures",
              second.get("flags") == ["unverified_figures"] and second.get("confidence") == "low",
              json.dumps(second))
        check("flags: a Clarify with no numbers and no claims has no flag",
              by_label.get("Clarify", {}).get("flags") == [], json.dumps(out["suggestions"])[-200:])

    # Live, 2026-10-27 (conversation 2409): "Yes, we can start the renewal while
    # she goes for the medical" from a record that lists the steps, not that.
    overreach = {"label": "Answer", "kb_refs": ["kb-1"], "confidence": "high",
                 "text": "Yes, we can kickstart the application while she attends the medical examination."}
    clarify_overreach = dict(overreach, label="Clarify",
                             text="Could you tell me when her medical examination is booked?")
    with World(model_output(overreach, clarify_overreach)) as world:
        out = _suggest(world)
        by_label = {s["label"]: s for s in out["suggestions"]}
        answer = by_label.get("Answer", {})
        check("flags: an Answer whose claim words are not in its cited record -> weak_support, low",
              answer.get("flags") == ["weak_support"] and answer.get("confidence") == "low",
              json.dumps(answer))
        check("flags: the support check applies to Answer drafts only",
              "weak_support" not in by_label.get("Clarify", {}).get("flags", ["?"]),
              json.dumps(by_label.get("Clarify")))
    check("flags: an Answer citing nothing that makes claims is weak_support",
          sg_validate.weakly_supported("We can kickstart the application tomorrow.", "", "hi"))
    check("flags: claim words the client used themselves do not count against support",
          not sg_validate.weakly_supported("Yes, the renewal fee is noted.",
                                           "The work permit renewal fee is $695.",
                                           "what is the renewal fee?"))


def _set_body(world: "World", message_id: int, body: str) -> None:
    for row in world.store.rows["wp_chat_messages"]:
        if row["id"] == message_id:
            row["body"] = body


def checks_eval_round() -> None:
    """The four fixes from the 2026-10-27 eval (conversations 4633, 4641, 4645, 194)."""
    # 1. 4633: the handset's greeting, stored by the portal as a staff message
    # 4 s after the client wrote, made the request stale.
    with World() as world:
        anchor_at = NOW - timedelta(minutes=1)
        world.store.rows["wp_chat_messages"].insert(0, {
            "id": 8, "direction": "outbound", "is_bot": False, "sent_by": "Ming Hwee Agency",
            "body": "Thank you for contacting Ming Hwee Agency! Please let us know how we can help you.",
            "created_at": (anchor_at + timedelta(seconds=4)).isoformat()})
        out = _suggest(world)
        system = world.llm.calls[0]["messages"][0]["content"] if world.llm.calls else ""
        check("auto-reply: a WhatsApp Business greeting after the client's message is ignored "
              "(not stale, not in the history)",
              out["status"] == "ready" and "Please let us know how we can help" not in system
              and "Please let us know how we can help" not in world.llm.calls[0]["messages"][1]["content"],
              json.dumps(out)[:300])

    # 2. 4641: staff had quoted "Agency Fee $1,388"; a draft repeating it was
    # dropped as "figure not in the records".
    repeat = {"label": "Next step", "kb_refs": [], "confidence": "high",
              "text": "The agency fee we quoted is $1,388, and I can check with management about a discount."}
    with World(model_output(repeat, CLARIFY)) as world:
        _set_body(world, 5, "Hello Mr Tan, the Agency Fee is $1,388.")
        out = _suggest(world)
        check("grounding: a fee our staff already quoted in this conversation may be repeated",
              any("$1,388" in s["text"] for s in out["suggestions"]),
              json.dumps(out["_dropped"])[:300])

    # 3. 194: the eval showed old backfilled messages (new ids) after newer ones.
    import importlib.util
    spec = importlib.util.spec_from_file_location("eval_suggest", Path(__file__).with_name("eval_suggest.py"))
    eval_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eval_module)
    shuffled = [{"id": 30, "created_at": "2026-01-05T10:00:00+00:00"},
                {"id": 10, "created_at": "2026-10-01T10:00:00+00:00"},
                {"id": 20, "created_at": "2026-10-01T09:00:00.500+00:00"}]
    check("eval: the transcript is in created_at order, not id order",
          [r["id"] for r in eval_module._time_order(shuffled)] == [30, 20, 10])

    # 5. 4633: a guard cut a draft down to its greeting, and the cut version
    # was offered as a "Next step".
    full = ("Hi Geraldine, thank you for completing our form. We help with new hiring, transfers "
            "and renewals, and can arrange a free consultation.")
    check("cut drafts: shortened by more than 40% or ending mid-thought is dropped; untouched is kept",
          sg_validate.cut_by_guards(full, "Hi Geraldine, thank you for completing our form.")
          and sg_validate.cut_by_guards("The fee is $695, and it takes about 1 week.",
                                        "The fee is $695, and it takes about 1 week and")
          and not sg_validate.cut_by_guards(full, full)
          and not sg_validate.cut_by_guards(full, full.replace("Hi Geraldine, ", "")))

    # 4. 4645: staff said the helpers are in the Philippines / Myanmar; a draft
    # quoted the Indonesian charges from an ID-labelled record.
    indonesian = {"id": "kb-3", "similarity": 0.8, "chunk_type": "qa_pair",
                  "question": "What third-party charges apply to an Indonesian new hire?",
                  "answer": "For an Indonesian new hire: insurance $590, medical $70.",
                  "content": None, "source_document": "Ming Hwee Service Notes",
                  "service_type": "renewal", "contact_type": "all", "nationality": "ID",
                  "metadata": {}}
    quote = {"label": "Answer", "kb_refs": ["kb-3"], "confidence": "high",
             "text": "Insurance is $590 and the medical is $70."}
    hold = {"label": "Next step", "kb_refs": [], "confidence": "high",
            "text": "I will check the charges for her and confirm."}
    with World(model_output(quote, hold)) as world:
        world.store.kb.append(indonesian)
        _set_body(world, 5, "All is in the Philippines. Only Myanmar is available in SG.")
        _set_body(world, 7, "How about all the admin fee, checkup, insurance fees charges??")
        out = _suggest(world)
        texts = " ".join(s["text"] for s in out["suggestions"])
        clarify = [s for s in out["suggestions"] if s["label"] == "Clarify"]
        check("nationality: another or unknown nationality's fee is not quoted, and a Clarify "
              "flagged nationality_unconfirmed is offered",
              "$590" not in texts and clarify
              and "nationality_unconfirmed" in clarify[0]["flags"]
              and any("nationality" in d["reason"] for d in out["_dropped"]),
              json.dumps(out["suggestions"])[:400])


def checks_model_choice() -> None:
    """2026-10-28: gpt-6-luna chosen; the 4641 and 4615 fixes."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SUGGEST_MODEL", None)
        os.environ.pop("SUGGEST_TIMEOUT_SECONDS", None)
        default_model, default_timeout = sg_settings.model(), sg_settings.timeout_seconds()
        os.environ["SUGGEST_MODEL"] = "some/other-model"
        overridden = sg_settings.model()
    check("settings: the model defaults to openai/gpt-6-luna (SUGGEST_MODEL overrides) and the "
          "timeout to 25 s",
          default_model == "openai/gpt-6-luna" and overridden == "some/other-model"
          and default_timeout == 25.0, f"{default_model} {overridden} {default_timeout}")

    # 4615: two client messages stored in the same second came back in either
    # order, so the anchor read as stale on one run and ready on the next.
    with World() as world:
        rows = world.store.rows["wp_chat_messages"]   # newest first
        seven = next(r for r in rows if r["id"] == 7)
        rows.insert(1, {"id": 8, "direction": "inbound", "is_bot": False, "sent_by": None,
                        "body": "and the medical?", "created_at": seven["created_at"]})
        out = _suggest(world, anchor=8)
        check("stale: two client messages in the same second are ordered by id, so the later one "
              "is the newest inbound (4615)", out["status"] == "ready", json.dumps(out)[:200])

    # 4641: staff had already named the helper; the drafts still got the fixed
    # "which helper is this for?" Clarify.
    labelled = {"id": "kb-4", "similarity": 0.8, "chunk_type": "qa_pair",
                "question": "Is GST charged on agency fees for a Myanmar helper?",
                "answer": "No GST is charged.", "content": None,
                "source_document": "Ming Hwee Service Notes", "service_type": "renewal",
                "contact_type": "all", "nationality": "MM", "metadata": {}}
    cite = {"label": "Next step", "kb_refs": ["kb-4"], "confidence": "high",
            "text": "I understand. I will check with management whether a discount is possible."}
    hold = {"label": "Answer", "kb_refs": [], "confidence": "high",
            "text": "I understand the fee feels high, and I will see what we can do."}
    for staff, expect_clarify, name in (
            ("Cost Breakdown for Eaint Thawtar Khin (MDW): Agency Fee $1,388.", False, "a helper staff named"),
            ("Here are the fees for the helpers we discussed.", True, "no helper named (control)")):
        with World(model_output(cite, hold)) as world:
            world.store.kb.append(labelled)
            _set_body(world, 5, staff)
            _set_body(world, 7, "It's okay, agency fee too expensive")
            out = _suggest(world)
            fixed = [s for s in out["suggestions"] if s["text"] == sg_validate.CLARIFY_NATIONALITY]
            check(f"nationality: {name} -> fixed Clarify {'added' if expect_clarify else 'not added'} (4641)",
                  bool(fixed) == expect_clarify, json.dumps(out["suggestions"])[:300])

    with World() as world:
        _suggest(world)
        system = world.llm.calls[0]["messages"][0]["content"] if world.llm.calls else ""
        check("prompt: every draft answers the client's latest message, no unrelated topic (4641)",
              "Never bring up a topic they did not raise" in system)


def checks_review_round() -> None:
    """Patterns from Thomas's review of thomas_review.csv, 2026-10-28."""
    def run(*drafts: dict[str, Any], staff: str | None = None, client: str | None = None,
            earlier: str | None = None, last_ours: str | None = None) -> dict:
        with World(model_output(*drafts)) as world:
            if staff is not None:
                _set_body(world, 5, staff)
            if last_ours is not None:
                _set_body(world, 6, last_ours)
            if earlier is not None:
                _set_body(world, 4, earlier)
            if client is not None:
                _set_body(world, 7, client)
            return _suggest(world)

    def draft(label: str, text: str) -> dict[str, Any]:
        return {"label": label, "text": text, "kb_refs": [], "confidence": "high"}

    def texts(out: dict) -> str:
        return " | ".join(s["text"] for s in out["suggestions"])

    hold = draft("Hold", "I will check this for you and confirm.")

    # 739: a status the client asked about, stated as done.
    out = run(draft("Answer", "We received the documents and they are in order."), hold,
              client="Please confirm the documents are in order")
    ok = run(draft("Answer", "We have received the documents for Jolyn."), hold,
             staff="Received with thanks", client="How is the application going?")
    check("status: 'received ... in order' only the client wrote is dropped; 'received' our staff "
          "said is kept (739)",
          "in order" not in texts(out) and any("status" in d["reason"] for d in out["_dropped"])
          and "We have received the documents" in texts(ok), texts(out) + " || " + texts(ok))

    # 194: staff said "tomorrow" the day before; the draft repeated it.
    out = run(draft("Next step", "Shirley will call your wife tomorrow."), hold,
              staff="I will ask Shirley to contact you tomorrow.", client="Can Shirley call my wife")
    same_day = run(draft("Next step", "Yes, Shirley can call tomorrow."), hold,
                   client="Can Shirley call tomorrow?")
    flagged = [s for s in out["suggestions"] if "unverified_commitment" in s["flags"]]
    check("commitment: a relative day not in the client's latest message is flagged "
          "unverified_commitment and low; one they asked for is not (194)",
          flagged and flagged[0]["confidence"] == "low"
          and not any("unverified_commitment" in s["flags"] for s in same_day["suggestions"]),
          json.dumps(out["suggestions"])[:300])
    # A numeric span nobody gave is already dropped by the figure guard; a
    # weekday is not a figure, so it is this check that catches it.
    out = run(draft("Next step", "We will confirm by Friday."), hold)
    check("commitment: a weekday deadline nobody gave is flagged unverified_commitment",
          any("unverified_commitment" in s["flags"] for s in out["suggestions"]),
          json.dumps(out)[:400])

    # 4615: a language nobody mentioned.
    out = run(draft("Next step", "We will let you know if we have a helper who speaks Tamil."), hold)
    asked = run(draft("Next step", "Sorry, we do not have a Tamil-speaking helper at the moment."), hold,
                client="Do you have anyone who can speak Tamil")
    check("attributes: a language, religion or nationality nobody mentioned is dropped; one the "
          "client asked about is kept (4615)",
          "Tamil" not in texts(out) and "Tamil" in texts(asked)
          and any("Tamil".lower() in d["reason"] for d in out["_dropped"]),
          json.dumps(out["_dropped"])[:200])

    # 4615 as it happened: staff said no, the draft promised to look anyway.
    out = run(draft("Next step", "I'll check if we have any helpers who speak Tamil and confirm."), hold,
              last_ours="Sorry , we do nit have .", earlier="Do you have anyone who can speak Tamil")
    fine = run(draft("Next step", "I'll check if we have any helpers who speak Tamil and confirm."), hold,
               last_ours="Noted, thank you.", earlier="Do you have anyone who can speak Tamil")
    check("promise: 'I'll check if we have' after our last message said we do not have is dropped; "
          "the same draft after a neutral reply is kept (4615)",
          "Tamil" not in texts(out) and "Tamil" in texts(fine), texts(out) + " || " + texts(fine))

    # 4645: a Clarify guessing the nationality.
    out = run(draft("Clarify", "Is this for the Indonesian new hire or the transfer helper?"), hold,
              client="How about all the admin fee, checkup, insurance fees charges??")
    check("clarify: a Clarify assuming a nationality is replaced by 'Which helper are you "
          "referring to?' (4645)",
          "Indonesian" not in texts(out)
          and any(s["text"] == sg_validate.CLARIFY_WHICH_HELPER for s in out["suggestions"]),
          texts(out))

    # 4553: a phone number, and "contact Ming Hwee".
    # The number is in our own earlier message, so the figure guard passes it
    # and only this check stands in the way.
    out = run(draft("Answer", "Please WhatsApp us on +65 6534 2277 and tell us why."),
              draft("Next step", "Please contact Ming Hwee and tell us what happened."), hold,
              staff="Our new WhatsApp number is +65 6534 2277.")
    check("contact: a draft with a phone number or telling the client to contact Ming Hwee is "
          "dropped (4553)",
          "6534" not in texts(out) and "contact Ming Hwee" not in texts(out)
          and sum("phone number" in d["reason"] for d in out["_dropped"]) == 2,
          json.dumps(out["_dropped"]))

    # 194: the client gave his wife's number; dropping every draft that
    # repeated it left nothing. It becomes "on that number".
    out = run(draft("Next step", "I'll ask Shirley to call your wife at 91234567."), hold,
              client="Can Shirley call my wife number 91234567")
    check("phone: a number the client just gave becomes 'on that number', not a dropped draft (194)",
          any(s["text"] == "I'll ask Shirley to call your wife on that number." for s in out["suggestions"])
          and "4567" not in texts(out), texts(out) + json.dumps(out["_dropped"]))

    # 4614: "Cannot play" - unclear; one Clarify, not a guess.
    with World() as world:
        _suggest(world)
        system = world.llm.calls[0]["messages"][0]["content"] if world.llm.calls else ""
    check("prompt: an unclear message gets ONE Clarify asking what they mean (4614)",
          "unclear and could mean more than one thing" in system and "ONE \"Clarify\"" in system)


def checks_stale_and_state() -> None:
    with World() as world:
        out = _suggest(world, anchor=4)
        check("stale: an anchor that is not the newest inbound returns status stale",
              out["status"] == "stale" and out["suggestions"] == [] and not world.llm.calls)

    # The eval's as-of mode: the same old anchor drafts from the conversation
    # as it stood then, and never reads the saved state (it holds the future).
    with World() as world:
        saved_read: list[str] = []
        real_saved = sg_context._saved_state

        async def _spy_saved(thread_id: Any) -> Any:
            saved_read.append(str(thread_id))
            return await real_saved(thread_id)

        with patch.object(sg_context, "_saved_state", _spy_saved):
            out = _suggest(world, anchor=4, as_of=True)
        user = world.llm.calls[0]["messages"][1]["content"] if world.llm.calls else ""
        check("as_of: an earlier anchor drafts from the conversation as it stood",
              out["status"] == "ready" and "Tan" in user, out["status"])
        check("as_of: messages after the anchor are not in the prompt",
              "Hello Mr Tan, noted." not in user and "S1234567D" not in user
              and "[REDACTED-ID]" not in user, user[-300:])
        check("as_of: the saved state is not read", saved_read == [], str(saved_read))

    with World() as world:
        world.store.rows["wp_chat_conversations"] = []
        try:
            _suggest(world)
            check("not found: an unknown conversation raises ConversationNotFound", False)
        except sg_context.ConversationNotFound:
            check("not found: an unknown conversation raises ConversationNotFound", True)

    with World() as world:
        conversation = dict(CONVERSATION, langgraph_thread_id=None)
        world.store.rows["wp_chat_conversations"] = [conversation]
        out = _suggest(world)
        check("state: no thread id -> empty saved state, nothing minted, still drafts",
              out["status"] == "ready" and world.store.writes == [])


def checks_tripwire() -> None:
    called: list[str] = []
    before = _snapshot()
    with World() as world, tripwired(called):
        world.store.raise_on_write = True
        try:
            out = run(sg_router.run_suggest(CONVERSATION_ID, ANCHOR_ID))
            status = out["status"]
        except Exception as exc:  # noqa: BLE001
            status = f"raised {type(exc).__name__}: {exc}"
    check("tripwire: a full suggestion (outside read-only mode) calls no writer, sender, "
          "debouncer, typing or bot-LLM function", status == "ready" and called == [],
          f"{status} {called}")
    check("tripwire: no database write and no non-search RPC", world.store.writes == [],
          str(world.store.writes))
    check("tripwire: the sent-by-bot, body-echo, replay and webhook caches are unchanged",
          _snapshot() == before)

    # The tripwire itself works: a path that DOES mark something is caught.
    caught: list[str] = []
    with tripwired(caught):
        try:
            _msg.mark_sent_by_bot("wamid.test")
        except AssertionError:
            pass
    check("tripwire: the harness catches a call (fault injection on the check)",
          caught == ["app.services.message.mark_sent_by_bot"], str(caught))


def checks_readonly_scope() -> None:
    """read_only() is a ContextVar: a live task writes while a suggestion runs."""
    store = Store()
    store.raise_on_write = False
    order: list[str] = []

    async def suggestion() -> list[dict[str, str]]:
        with readonly.read_only() as refused:
            order.append("suggestion in read-only")
            await asyncio.sleep(0.05)
            await db.insert("cb_tickets", {"x": 1})
            await asyncio.sleep(0.05)
            order.append("suggestion done")
            return list(refused)

    async def live_turn() -> Any:
        await asyncio.sleep(0.02)
        order.append("live write")
        return await db.insert("wp_chat_messages", {"body": "live"})

    async def both() -> tuple[Any, Any]:
        return await asyncio.gather(suggestion(), live_turn())

    with patch.object(_supabase, "get_supabase", lambda: FakeClient(store)):
        refused, live = run(both())
    check("read-only scope: the live task's write went through while a suggestion was in read-only",
          ("POST", "wp_chat_messages") in store.writes and live == {"id": "written"}
          and order.index("live write") < order.index("suggestion done"), f"{store.writes} {order}")
    check("read-only scope: the suggestion's own write was refused and never reached the database",
          ("POST", "cb_tickets") not in store.writes
          and refused == [{"method": "POST", "target": "/rest/v1/cb_tickets"}], str(refused))
    check("read-only scope: off again after the block", readonly.active() is False)


def _client(env: dict[str, str]) -> TestClient:
    from app import main as main_module

    with patch.dict(os.environ, env, clear=False):
        if "SUGGEST_SECRET" not in env:
            os.environ.pop("SUGGEST_SECRET", None)
        app = main_module.create_app()
    return TestClient(app)


def checks_http() -> None:
    body = {"tenant_id": TENANT, "conversation_id": CONVERSATION_ID, "anchor_message_id": ANCHOR_ID}
    env = {"SUGGEST_SECRET": SECRET}
    saved = os.environ.get("SUGGEST_SECRET")
    try:
        client = _client({})
        r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": SECRET})
        check("http: with SUGGEST_SECRET unset the route does not exist (404)", r.status_code == 404)

        with patch.dict(os.environ, env):
            client = _client(env)
            with World():
                sg_router._semaphore = None
                r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": "wrong"})
                check("http: bad secret -> 401", r.status_code == 401)
                r = client.post("/internal/suggest", content=b"{not json",
                                headers={"X-Suggest-Secret": "wrong"})
                check("http: bad secret with a malformed body is still 401, never 422", r.status_code == 401)
                r = client.post("/internal/suggest", json=body)
                check("http: no secret header -> 401", r.status_code == 401)
                r = client.post("/internal/suggest", json=dict(body, tenant_id="other"),
                                headers={"X-Suggest-Secret": SECRET})
                check("http: wrong tenant -> 403", r.status_code == 403 and r.json() == {"error": "wrong_tenant"})

                r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": SECRET})
                data = r.json()
                keys = {"status", "reason", "anchor_message_id", "bot_status", "suggestions", "kb_gap",
                        "sensitive", "language", "model", "usage", "latency_ms", "prompt_version"}
                draft_keys = {"label", "text", "kb_refs", "confidence", "flags"}
                check("http: 200 with exactly the response contract",
                      r.status_code == 200 and set(data) == keys and data["status"] == "ready"
                      and data["reason"] == "" and data["bot_status"] == "bot_active"
                      and all(set(d) == draft_keys and isinstance(d["flags"], list)
                              for d in data["suggestions"]), json.dumps(data)[:300])

                r = client.post("/internal/suggest", json=dict(body, anchor_message_id=4),
                                headers={"X-Suggest-Secret": SECRET})
                check("http: a stale anchor -> 200 status stale", r.status_code == 200
                      and r.json()["status"] == "stale"
                      and r.json()["reason"] == sg_context.NOT_NEWEST_INBOUND)

            with World() as world:
                sg_router._semaphore = None
                world.store.rows["wp_chat_conversations"] = [dict(CONVERSATION, group_jid="1203@g.us")]
                r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": SECRET})
                check("http: a group chat -> 200 status skipped, reason group, no model call",
                      r.status_code == 200 and r.json()["status"] == "skipped"
                      and r.json()["reason"] == "group" and r.json()["suggestions"] == []
                      and not world.llm.calls, r.text[:200])

            with World() as world:
                world.store.rows["wp_chat_conversations"] = []
                r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": SECRET})
                check("http: unknown conversation -> 404", r.status_code == 404
                      and r.json() == {"error": "conversation_not_found"})

            with World():
                sem = asyncio.Semaphore(1)
                run(sem.acquire())
                sg_router._semaphore = sem
                r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": SECRET})
                check("http: semaphore full -> 429, not queued", r.status_code == 429
                      and r.json() == {"error": "busy"})
                sg_router._semaphore = None

            with patch.dict(os.environ, {"SUGGEST_TIMEOUT_SECONDS": "0.3"}), World(delay=2.0):
                r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": SECRET})
                check("http: past SUGGEST_TIMEOUT_SECONDS -> 504", r.status_code == 504
                      and r.json() == {"error": "timeout"})
                sg_router._semaphore = None

            async def writes_a_ticket(state: dict) -> dict:
                await db.insert("cb_tickets", {"x": 1})
                return {"matches": [], "records": "", "rag_context": "", "best": 0.0, "case_summary": None}

            with World() as world, patch.object(sg_retrieval, "retrieve", writes_a_ticket):
                world.store.raise_on_write = True
                r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": SECRET})
                check("http: a path that tries to write is refused by read-only mode and answered 500",
                      r.status_code == 500 and r.json() == {"error": "read_only_violation"}
                      and world.store.writes == [], f"{r.status_code} {world.store.writes}")
                sg_router._semaphore = None

            async def explodes(state: dict) -> dict:
                raise RuntimeError("boom")

            with World(), patch.object(sg_retrieval, "retrieve", explodes):
                r = client.post("/internal/suggest", json=body, headers={"X-Suggest-Secret": SECRET})
                check("http: an unexpected error -> 500 with a short code, never a traceback",
                      r.status_code == 500 and r.json() == {"error": "internal"})
                sg_router._semaphore = None
    finally:
        if saved is None:
            os.environ.pop("SUGGEST_SECRET", None)
        else:
            os.environ["SUGGEST_SECRET"] = saved

# --- More drafts and multi-message turns (2026-10-28) -------------------------

NEXT_STEP = {"label": "Next step", "text": "Once you confirm, I will prepare the renewal forms for your signature.",
             "kb_refs": [], "confidence": "high"}
HOLD = {"label": "Hold", "text": "Let me check the details of her renewal and get back to you on this.",
        "kb_refs": [], "confidence": "high"}
EMPATHISE = {"label": "Empathise", "text": "I understand you want the renewal settled without any gap for her.",
             "kb_refs": [], "confidence": "high"}

MULTI = ["Hi", "What is the transfer fee?", "For a Filipino helper"]


def _multi_rows() -> list[dict[str, Any]]:
    """The client sends three messages in a row after our last reply (id 6);
    a reaction (an outbound row with no text) sits between two of them."""
    rows = message_rows()[:6]
    rows += [
        {"id": 7, "direction": "inbound", "is_bot": False, "sent_by": None, "body": MULTI[0],
         "created_at": _at(5)},
        {"id": 8, "direction": "outbound", "is_bot": False, "sent_by": "agent", "body": "",
         "created_at": _at(4)},
        {"id": 9, "direction": "inbound", "is_bot": False, "sent_by": None, "body": MULTI[1],
         "created_at": _at(3)},
        {"id": 10, "direction": "inbound", "is_bot": False, "sent_by": None, "body": MULTI[2],
         "created_at": _at(2)},
    ]
    return rows


def checks_more_drafts() -> None:
    # 5 candidates in, at most 4 out.
    five = (GOOD, CLARIFY, NEXT_STEP, HOLD, EMPATHISE)
    with World(model_output(*five)) as world:
        out = _suggest(world)
        labels = [d["label"] for d in out["suggestions"]]
        check("more: 5 valid candidates in -> 4 out, all different labels",
              len(labels) == 4 and len(set(labels)) == 4, f"{labels} {out['_dropped']}")
        check("more: the 5th candidate is not returned (selection, not a drop for a fault)",
              "Empathise" not in labels, str(labels))
    # Five candidates that all fail, then a valid sixth: the sixth is never read.
    broken = [dict(CLARIFY, text=f"Draft {n} relies on a record we never retrieved.", kb_refs=["kb-999"])
              for n in "ABCDE"]
    with World(model_output(*broken, GOOD)) as world:
        out = _suggest(world)
        check("more: only the first 5 candidates are read", out["suggestions"] == []
              and len(out["_dropped"]) == 5, json.dumps(out["suggestions"])[:200])

    # Selection: high first, diverse labels, never padding.
    low = lambda d, **k: dict(d, confidence="low", **k)  # noqa: E731
    picked = sg_validate.select([low(CLARIFY), GOOD, low(HOLD), dict(NEXT_STEP), low(
        CLARIFY, text="Which month does her permit end?")])
    check("select: high-confidence drafts come first",
          [d["confidence"] for d in picked] == sorted([d["confidence"] for d in picked],
                                                      key=lambda c: c != "high"), str(picked))
    check("select: a low draft with a new label is kept; a second low draft of a covered label is not",
          [d["label"] for d in picked].count("Clarify") == 1 and "Hold" in [d["label"] for d in picked]
          and len(picked) == 4, str([d["label"] for d in picked]))
    check("select: a low draft is not added as a second draft of a label already covered",
          len(sg_validate.select([GOOD, CLARIFY, low(CLARIFY, text="Which month does it end?")])) == 2)
    check("select: never pads - two candidates give two drafts",
          len(sg_validate.select([GOOD, CLARIFY])) == 2)
    check("select: two high drafts of one label may both be kept when there is room",
          len(sg_validate.select([GOOD, dict(GOOD, text="Another answer, worded on its own.")])) == 2)
    with World(model_output(GOOD, CLARIFY)) as world:
        out = _suggest(world)
        check("more: only 2 survive -> 2 returned, nothing padded", len(out["suggestions"]) == 2,
              str(out["suggestions"]))

    # Near-duplicates dropped, the better one kept.
    paraphrase = dict(GOOD, text="The work permit renewal fee is $695 and it usually takes around 1 week.",
                      confidence="high")
    same_meaning = dict(HOLD, text="Let me check her renewal details and get back to you on this one.")
    check("near-dup: very similar wording is a near-duplicate",
          sg_validate.near_duplicates(GOOD["text"], paraphrase["text"]))
    check("near-dup: same content words reordered is a near-duplicate",
          sg_validate.near_duplicates(HOLD["text"], same_meaning["text"]))
    check("near-dup: different approaches are not near-duplicates",
          not any(sg_validate.near_duplicates(a["text"], b["text"])
                  for i, a in enumerate(five) for b in five[i + 1:]))
    with World(model_output(low(GOOD, flags=[]), paraphrase, CLARIFY, HOLD, same_meaning)) as world:
        out = _suggest(world)
        texts = [d["text"] for d in out["suggestions"]]
        reasons = [d["reason"] for d in out["_dropped"]]
        check("near-dup: dropped with its reason, the high-confidence one kept",
              reasons.count("near-duplicate of another draft") == 2 and paraphrase["text"] in texts
              and GOOD["text"] not in texts and same_meaning["text"] not in texts,
              f"{texts} {reasons}")
        check("near-dup: what is left is distinct drafts only", len(texts) == 3, str(texts))

    check("prompt: asks for 5 candidates (at least 3) with the five approaches",
          "write 5 candidate drafts" in sg_prompt.SUGGEST_WRAPPER and "at least 3" in sg_prompt.SUGGEST_WRAPPER
          and all(f'"{x}"' in sg_prompt.SUGGEST_WRAPPER
                  for x in ("Answer", "Clarify", "Next step", "Hold", "Empathise")))
    check("prompt: the JSON schema lists the five labels",
          '"Answer" | "Clarify" | "Next step" | "Hold" | "Empathise"' in sg_prompt.SUGGEST_WRAPPER)
    check("validate: the new labels are kept as labels", {"Hold", "Empathise"} <= sg_validate.LABELS
          and sg_validate.MAX_DRAFTS == 4 and sg_validate.MAX_CANDIDATES == 5)

    # Multi-message: "Hi" / "What is the transfer fee?" / "For a Filipino helper".
    rows = _multi_rows()
    reason, earlier, batch = sg_context.split_at_anchor(rows, 10)
    check("multi: every client message since our last reply is one batch (a reaction does not end it)",
          reason == "" and [r["id"] for r in batch] == [7, 9, 10] and earlier[-1]["id"] == 6,
          f"{reason} {[r['id'] for r in batch]}")
    searched: list[str] = []

    async def _embed(text: str) -> list[float]:
        searched.append(text)
        return [0.0] * 8

    with World(model_output(GOOD, CLARIFY)) as world, patch.object(rag, "embed_query", _embed):
        world.store.rows["wp_chat_messages"] = list(reversed(rows))
        out = _suggest(world, anchor=10)
        user = world.llm.calls[0]["messages"][1]["content"] if world.llm.calls else ""
        check("multi: drafts are made for the 3-message turn", out["status"] == "ready", out["status"])
        check("multi: the knowledge-base search uses all 3 messages",
              bool(searched) and all(m in searched[0] for m in MULTI), str(searched))
        check("multi: the search does not use an earlier, answered message",
              bool(searched) and "Tan" not in searched[0].split(), str(searched))
        check("multi: the prompt marks them as the client's unanswered messages, one per line",
              "The client's unanswered messages" in user
              and "1. Hi\n2. What is the transfer fee?\n3. For a Filipino helper" in user, user[-400:])
        check("multi: the wrapper says to answer the unanswered messages together",
              "taken TOGETHER" in sg_prompt.SUGGEST_WRAPPER)
    check("multi: one message is shown as it is, unnumbered",
          sg_prompt.unanswered_block({"suggest_unanswered": ["How much?"]}) == "How much?")
    check("llm: room for five candidates", sg_llm.MAX_TOKENS >= 1400)

    # Same meaning in other words (embedding pass), 4641's shape.
    empathise = dict(EMPATHISE, text="I understand the fee feels high. I'll check with our team for options.")
    hold_same = dict(HOLD, text="I understand. I'll ask our team about the fee and confirm what is possible.")
    with World(model_output(GOOD, empathise, hold_same, CLARIFY)) as world:
        world.vectors = {empathise["text"]: [1.0, 0.0, 0.0], hold_same["text"]: [0.9, 0.3, 0.0],
                         GOOD["text"]: [0.0, 1.0, 0.0], CLARIFY["text"]: [0.0, 0.0, 1.0]}
        out = _suggest(world)
        texts = [d["text"] for d in out["suggestions"]]
        reasons = [d["reason"] for d in out["_dropped"]]
        check("meaning: a draft saying the same in other words (cosine >= 0.85) is dropped, the first kept",
              "says the same as another draft" in reasons and empathise["text"] in texts
              and hold_same["text"] not in texts and len(texts) == 3, f"{texts} {reasons}")
        check("meaning: one batched embedding request for the surviving drafts",
              len(world.embedded) == 1 and len(world.embedded[0]) == 4, str(world.embedded))
    check("meaning: drafts below the threshold are both kept",
          sg_meaning.cosine([1.0, 0.0], [0.8, 0.6]) < sg_meaning.SAME_MEANING_COSINE)

    async def _fails(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding down")

    with World(model_output(GOOD, empathise, hold_same, CLARIFY)) as world, \
            patch.object(sg_meaning, "_embed", _fails):
        out = _suggest(world)
        check("meaning: an embedding failure keeps the drafts (never fails the request)",
              out["status"] == "ready" and len(out["suggestions"]) == 4, str(out["suggestions"]))
    check("meaning: never the live bot's query cache (rag.embed_query)",
          "embed_query(" not in inspect.getsource(sg_meaning))


def main() -> int:
    with patch.object(httpx.HTTPTransport, "handle_request", _refuse_sync), \
            patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _refuse_async):
        for group in (checks_labelling, checks_identity, checks_grounding_units, checks_drafting,
                      checks_stale_and_state, checks_skipped, checks_flags, checks_eval_round,
                      checks_model_choice, checks_review_round, checks_more_drafts,
                      checks_tripwire,
                      checks_readonly_scope, checks_http):
            try:
                group()
            except Exception as exc:  # noqa: BLE001 - a crash is a FAIL, never silence
                import traceback

                traceback.print_exc()
                check(f"{group.__name__} crashed", False, f"{type(exc).__name__}: {exc}")
    check("safety: no real network request was made by any check", NETWORK_ATTEMPTS == [],
          str(NETWORK_ATTEMPTS))

    failed = 0
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n        {detail}"))
        failed += not ok
    print()
    print("ALL PASS" if not failed else f"{failed} FAILED of {len(RESULTS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
