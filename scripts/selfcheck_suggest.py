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
from app.suggest import prompt as sg_prompt  # noqa: E402
from app.suggest import retrieval as sg_retrieval  # noqa: E402
from app.suggest import router as sg_router  # noqa: E402
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
        self.stack = ExitStack()

    def __enter__(self) -> "World":
        s = self.stack
        s.enter_context(patch.object(_supabase, "get_supabase", lambda: FakeClient(self.store)))
        s.enter_context(patch.object(_graph, "get_graph", lambda: FakeGraph()))
        s.enter_context(patch.object(sg_llm, "_client", lambda: self.llm))

        async def _embed(text: str) -> list[float]:
            return [0.0] * 8

        s.enter_context(patch.object(rag, "embed_query", _embed))
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
    check("anchor: an older inbound message is stale", reason.startswith("anchor is not"))
    answered = rows + [{"id": 8, "direction": "outbound", "is_bot": False, "sent_by": "agent",
                        "body": "It is $695.", "created_at": _at(0)}]
    reason, _, _ = sg_context.split_at_anchor(answered, 7)
    check("anchor: a message already answered by staff is stale", reason.endswith("already been answered"))
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

    with World(model_output(invented_money, invented_time, uncited_time, bad_ref, nric, claire, ai)) as world:
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
              reasons.count("names Claire or claims to be an AI") == 2, str(reasons))
        check("validate: an NRIC in a draft is redacted and the draft kept",
              texts == ["Thanks, I have noted [REDACTED-ID]. When does her permit expire?"], str(texts))

    # The model cites records in the shape it is shown them ("[ref <id>]").
    # Live, 2026-10-27: every cited draft was dropped for writing "ref <id>".
    wrapped = dict(GOOD, kb_refs=["ref kb-1"])
    bracketed = dict(GOOD, text="Renewal is $695 and takes about 1 week.", kb_refs=["[ref kb-1]"])
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
        check("validate: duplicates and over-700-character drafts are dropped, at most 3 kept",
              len(out["suggestions"]) == 3 and "duplicate" in reasons
              and "over 700 characters" in reasons, str(reasons))

    with World("this is not json") as world:
        try:
            _suggest(world)
            check("llm: output that is not JSON is an error, not drafts", False)
        except sg_llm.SuggestLLMError:
            check("llm: output that is not JSON is an error, not drafts", True)


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
                keys = {"status", "anchor_message_id", "bot_status", "suggestions", "kb_gap",
                        "sensitive", "language", "model", "usage", "latency_ms", "prompt_version"}
                check("http: 200 with exactly the response contract",
                      r.status_code == 200 and set(data) == keys and data["status"] == "ready"
                      and data["bot_status"] == "bot_active", json.dumps(data)[:300])

                r = client.post("/internal/suggest", json=dict(body, anchor_message_id=4),
                                headers={"X-Suggest-Secret": SECRET})
                check("http: a stale anchor -> 200 status stale", r.status_code == 200
                      and r.json()["status"] == "stale")

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


def main() -> int:
    with patch.object(httpx.HTTPTransport, "handle_request", _refuse_sync), \
            patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _refuse_async):
        for group in (checks_labelling, checks_identity, checks_grounding_units, checks_drafting,
                      checks_stale_and_state, checks_tripwire, checks_readonly_scope, checks_http):
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
