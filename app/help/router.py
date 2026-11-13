"""POST /internal/help/answer - a grounded answer for office staff. Writes nothing.

Called server to server by the OS Portal backend. In order:

1. HELP_SECRET empty: 404, the same body as any unknown path.
2. X-Help-Secret checked with hmac.compare_digest BEFORE the body is read
   (the body is not a declared parameter, so FastAPI cannot answer 422 first):
   no or wrong secret is 401.
3. At most HELP_MAX_CONCURRENCY at once; over that is 429 at once, never queued.
4. Body over 32 KB: 413. Invalid body: 422.
5. The whole run inside readonly.read_only() and HELP_TIMEOUT_SECONDS (504).
   A refused write, or any attempt to reach WhatsApp, is 500
   read_only_violation and no answer is returned.

Logs one line per request: request_id, status, flags, top_score, rows,
latency_ms, model. Never message text, names, numbers or secrets.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import readonly
from app.config import settings
from app.graph import guards
from app.help import answer as help_answer
from app.help import context as help_context
from app.help import llm as help_llm
from app.help import planner as help_planner
from app.help import retrieve as help_retrieve
from app.help import schemas
from app.help import settings as help_settings
from app.services import kb_rules
from app.utils import redact_nric

logger = logging.getLogger(__name__)

router = APIRouter()

ANSWER_PATH = "/internal/help/answer"

_gate: tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None = None


def _semaphore() -> asyncio.Semaphore:
    """One semaphore per event loop and cap (tests run several loops)."""
    global _gate
    loop = asyncio.get_running_loop()
    cap = help_settings.max_concurrency()
    if _gate is None or _gate[0] is not loop or _gate[1] != cap:
        _gate = (loop, cap, asyncio.Semaphore(cap))
    return _gate[2]


def error(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def not_found() -> JSONResponse:
    return error(404, "not_found", "Not Found")


async def _read_body(request: Request) -> bytes:
    """The body, refusing past the cap without reading it all."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > help_settings.MAX_BODY_BYTES:
        raise _TooLarge()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > help_settings.MAX_BODY_BYTES:
            raise _TooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


class _TooLarge(Exception):
    pass


async def run_help(req: schemas.HelpRequest) -> dict[str, Any]:
    """The pipeline, without the HTTP layer. Must be called inside read_only()."""
    started = time.monotonic()
    model = help_settings.model()
    floor = settings.rag_soft_floor

    # 1. NRICs out of every message, before anything else sees them.
    messages = tuple(
        schemas.Message(m.role, redact_nric(m.content, context="help")) for m in req.messages
    )
    last_user = messages[-1].content

    # The pricing snapshot this process uses. Never raises; at most once a minute.
    await kb_rules.refresh()

    # 2. Plan.
    plan = await help_planner.plan(
        messages, help_retrieve.allowed_services(),
        model=model, timeout=help_settings.planner_timeout_seconds(),
    )
    price_question = guards.asks_about_price(last_user) or guards.asks_about_price(
        plan.standalone_query
    )

    # 3. Retrieve.
    found = await help_retrieve.retrieve(
        plan.standalone_query,
        service_type=plan.service_type,
        nationality=plan.nationality,
        price_question=price_question,
        match_count=help_settings.match_count(),
    )
    records = help_context.build(found.matches)
    top = found.top_score

    body: dict[str, Any] = {
        "status": "ok",
        "answer": "",
        "sources": records.sources,
        "retrieval": {
            "search_query": found.search_query,
            "service_type": found.service_type,
            "nationality": found.nationality,
            "price_question": price_question,
            "widened": found.widened,
            "top_score": round(top, 4) if top is not None else None,
            "floor": floor,
            "rows": len(records.sources),
        },
        "guard_flags": [],
        "model": model,
        "latency_ms": 0,
    }

    # 4. Nothing good enough: a fixed answer and no model call.
    if not records.sources or top is None or top < floor:
        body["status"] = "not_found"
        body["answer"] = help_answer.NOT_FOUND_ANSWER
        body["sources"] = []
    else:
        # 5-7. Generate, then guard.
        raw = await help_answer.generate(
            messages, records.text, model=model, timeout=help_settings.timeout_seconds()
        )
        checked = help_answer.check(
            raw,
            records_text=records.text,
            rows=records.rows,
            valid_sources={s["n"] for s in records.sources},
            service=found.service_type,
            nationality=found.nationality,
        )
        body["status"] = checked.status
        body["answer"] = checked.text
        body["guard_flags"] = checked.flags

    body["latency_ms"] = int((time.monotonic() - started) * 1000)
    return body


def _log(request_id: str, status: str, started: float, model: str, body: dict | None = None) -> None:
    retrieval = (body or {}).get("retrieval") or {}
    logger.info(
        "help: request_id=%s status=%s flags=%s top_score=%s rows=%s latency_ms=%d model=%s",
        request_id, status, (body or {}).get("guard_flags", []),
        retrieval.get("top_score"), retrieval.get("rows"),
        int((time.monotonic() - started) * 1000), model,
    )


@router.post(ANSWER_PATH)
async def help_answer_route(request: Request) -> Any:
    secret = help_settings.secret()
    if not secret:
        return not_found()
    given = request.headers.get("x-help-secret") or ""
    if not hmac.compare_digest(given.encode(), secret.encode()):
        return error(401, "unauthorized", "bad or missing X-Help-Secret")

    gate = _semaphore()
    if gate.locked():
        return error(429, "busy", "too many requests in flight")
    async with gate:
        started = time.monotonic()
        model = help_settings.model()
        try:
            raw = await _read_body(request)
        except _TooLarge:
            return error(413, "body_too_large", f"body over {help_settings.MAX_BODY_BYTES} bytes")
        try:
            req = schemas.parse(raw)
        except schemas.InvalidRequest as exc:
            return error(422, "invalid_request", str(exc))

        try:
            with readonly.read_only() as refused:
                result = await asyncio.wait_for(run_help(req), timeout=help_settings.timeout_seconds())
                refused_writes = list(refused)
        except asyncio.TimeoutError:
            _log(req.request_id, "timeout", started, model)
            return error(504, "timeout", "the answer took too long")
        except readonly.ReadOnlyViolation:
            _log(req.request_id, "read_only_violation", started, model)
            return error(500, "read_only_violation", "the request tried to reach WhatsApp")
        except help_retrieve.SearchUnavailable:
            _log(req.request_id, "search_unavailable", started, model)
            return error(500, "search_unavailable", "the knowledge-base search is unavailable")
        except help_llm.HelpLLMError:
            _log(req.request_id, "llm_error", started, model)
            return error(500, "llm_error", "the model call failed")
        except Exception:  # noqa: BLE001 - never an unhandled error to the caller
            logger.exception("help: request_id=%s failed", req.request_id)
            return error(500, "internal", "internal error")

        if refused_writes:
            logger.error(
                "help: request_id=%s attempted %d write(s): %s",
                req.request_id, len(refused_writes), [w.get("target") for w in refused_writes],
            )
            return error(500, "read_only_violation", "the request tried to write")

        _log(req.request_id, result["status"], started, model, result)
        return result
