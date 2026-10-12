"""How long each step of one turn took, written as one JSON log line.

Added 2026-10-12 because the agency's complaint is the wait, and the wait had
only ever been estimated from message timestamps. A turn is opened in
webhook._process_locked; while it is open, the database doors
(Database.execute / Database.rpc), the LLM wrapper (llm.complete), the
knowledge-base search (rag.search) and the WhatsApp send each add their
elapsed time here. When the turn ends one line is logged:

    TURN_TIMING {"conversation_id": 26, "total_ms": ..., "steps": {...}}

scripts/latency_report.py reads those lines and prints p50/p90 per step.

What it must never do, and how each is kept:

* break a turn - every entry point swallows its own exceptions;
* leak anything - the line carries the conversation id and durations only,
  never message text, a phone number or a name. Database steps are keyed by
  TABLE name, LLM steps by node and purpose;
* cost anything when off - TURN_TIMING_LOG=false makes active() false and every
  entry point returns at once.

A ContextVar rather than a global, for the reason app/readonly.py is one: it is
scoped to the one turn, and tasks the graph creates inside it inherit it. The
value is a mutable dict, so a write from inside a copied context is still seen
by the turn that owns it.

Durations of nested steps overlap by design - a RAG search contains its own
database call, and the setup reads run concurrently - so the steps are not
meant to add up to the total. The phases (setup, graph, send) do not overlap.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Iterator

logger = logging.getLogger(__name__)

LOG_PREFIX = "TURN_TIMING "

_turn: ContextVar[dict[str, Any] | None] = ContextVar("turn_timing", default=None)
_purpose: ContextVar[str | None] = ContextVar("turn_timing_purpose", default=None)

# What an LLM call is FOR, by the node module and function that made it. The
# rewrite retry in info_collector._write shares its function with the first
# attempt, so it names itself with purpose("rewrite") at the call site.
_LLM_PURPOSES = {
    ("intent_classifier", "intent_classifier"): "classify",
    ("intent_classifier", "confirm_harm"): "harm_check",
    ("info_collector", "_extract"): "extract",
    ("info_collector", "_write"): "reply",
    ("response_generator", "response_generator"): "reply",
    ("blocked_topic_responder", "blocked_topic_responder"): "reply",
    ("handover_executor", "_assault_reply"): "reply",
    ("lead", "write_summary"): "lead_summary",
    ("ticket", "find_merge_candidate"): "ticket_match",
}


def _enabled() -> bool:
    try:
        from app.config import settings

        return bool(settings.turn_timing_log)
    except Exception:  # noqa: BLE001
        return False


def active() -> bool:
    return _turn.get() is not None


@contextmanager
def turn(conversation_id: Any) -> Iterator[None]:
    """Open a turn. Logs the timing line when the block exits, however it exits."""
    if not _enabled():
        yield
        return
    state = {
        "conversation_id": conversation_id,
        "started": time.monotonic(),
        "phase": "setup",
        "phase_started": time.monotonic(),
        "phases": {},
        "steps": {},
        "extra": {},
        "emit": False,
    }
    token = _turn.set(state)
    try:
        yield
    finally:
        _turn.reset(token)
        try:
            _close_phase(state)
            if state["emit"]:
                _emit(state)
        except Exception:  # noqa: BLE001
            logger.debug("turn timing could not be written", exc_info=True)


def phase(name: str) -> None:
    """Move the turn into its next phase (setup -> graph -> send)."""
    state = _turn.get()
    if state is None:
        return
    try:
        _close_phase(state)
        state["phase"] = name
        state["phase_started"] = time.monotonic()
        # A turn that reached the graph is worth a line; one that returned
        # early (taken over, empty batch) is not.
        state["emit"] = True
    except Exception:  # noqa: BLE001
        pass


def note(key: str, value: Any) -> None:
    """A number about the turn that is not a step (e.g. the debounce wait)."""
    state = _turn.get()
    if state is None:
        return
    try:
        state["extra"][key] = value
    except Exception:  # noqa: BLE001
        pass


def add(step: str, elapsed_ms: float) -> None:
    state = _turn.get()
    if state is None:
        return
    try:
        entry = state["steps"].setdefault(step, {"n": 0, "ms": 0.0})
        entry["n"] += 1
        entry["ms"] += elapsed_ms
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def step(name: str) -> Iterator[None]:
    if _turn.get() is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        add(name, (time.monotonic() - started) * 1000)


@asynccontextmanager
async def astep(name: str) -> AsyncIterator[None]:
    with step(name):
        yield


@contextmanager
def purpose(name: str) -> Iterator[None]:
    """Name what the LLM calls inside this block are for."""
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def llm_step_name() -> str:
    """'llm.<node>.<purpose>', worked out from the caller outside app.graph.llm."""
    try:
        frame = sys._getframe(1)
        while frame is not None and frame.f_globals.get("__name__") in (
            __name__,
            "app.graph.llm",
        ):
            frame = frame.f_back
        module = (frame.f_globals.get("__name__") or "?").rsplit(".", 1)[-1] if frame else "?"
        function = frame.f_code.co_name if frame else "?"
        what = _purpose.get() or _LLM_PURPOSES.get((module, function), function)
        return f"llm.{module}.{what}"
    except Exception:  # noqa: BLE001
        return "llm.unknown"


def timed(name: str):
    """Decorator: time every call of an async function as one step."""

    def wrap(fn):
        import functools

        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            with step(name):
                return await fn(*args, **kwargs)

        return inner

    return wrap


# When each inbound message reached us, by Whapi message id, so the turn can
# say how long the client waited from their last message (the debounce) and
# from their first (the whole wait). Bounded: oldest out first.
_received: dict[str, float] = {}
_RECEIVED_LIMIT = 2000


def mark_received(message_id: Any) -> None:
    if not _enabled() or not message_id:
        return
    try:
        _received[str(message_id)] = time.time()
        while len(_received) > _RECEIVED_LIMIT:
            _received.pop(next(iter(_received)))
    except Exception:  # noqa: BLE001
        pass


def received_at(message_ids: list[Any]) -> list[float]:
    return [_received[str(m)] for m in message_ids if str(m) in _received]


def table_of(path: Any) -> str:
    """'cb_tickets' from a postgrest path such as '/rest/v1/cb_tickets'."""
    text = str(path or "")
    return text.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0] or "?"


def _close_phase(state: dict[str, Any]) -> None:
    name = state["phase"]
    elapsed = (time.monotonic() - state["phase_started"]) * 1000
    state["phases"][name] = state["phases"].get(name, 0.0) + elapsed
    state["phase_started"] = time.monotonic()


def _emit(state: dict[str, Any]) -> None:
    total = (time.monotonic() - state["started"]) * 1000
    line = {
        "conversation_id": state["conversation_id"],
        "total_ms": round(total),
        "phases": {k: round(v) for k, v in state["phases"].items()},
        "steps": {
            k: {"n": v["n"], "ms": round(v["ms"])}
            for k, v in sorted(state["steps"].items())
        },
        **{k: (round(v) if isinstance(v, float) else v) for k, v in state["extra"].items()},
    }
    logger.info("%s%s", LOG_PREFIX, json.dumps(line, separators=(",", ":")))
