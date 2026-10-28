"""POST /internal/suggest - up to 4 reply drafts for a consultant. Writes nothing.

Called server to server by the Smart Inbox. Protected the way POST
/admin/preview is (app/api/admin.py):

1. No SUGGEST_SECRET: app/main.py never registers this router - 404.
2. The secret is checked in a route DEPENDENCY, before the body is read, and the
   body is not a declared parameter (FastAPI would decode a declared body
   first and answer 422 to anyone). No or wrong secret is always 401.
3. tenant_id must be the bot's TENANT_ID (403), so a key cannot be pointed at
   another tenant's data.
4. At most SUGGEST_MAX_CONCURRENCY at once; over that is 429 at once, never
   queued, so a burst cannot hold connections the webhook needs.
5. The whole request is cut at SUGGEST_TIMEOUT_SECONDS (504).

Everything runs inside readonly.read_only() as a second lock: a database write
or a non-search RPC is refused, a Whapi request raises, and either makes the
request fail with 500 instead of returning drafts. The first lock is that
nothing here calls a writer. read_only() is a ContextVar, so a live turn
running at the same moment is not affected (selfcheck_suggest.py proves it).

Logs ids, counts and timings only - never message or draft text.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app import readonly
from app.config import settings
from app.graph import language
from app.suggest import context, meaning, retrieval, validate
from app.suggest import llm as suggest_llm
from app.suggest import settings as suggest_settings
from app.suggest.prompt import SUGGEST_PROMPT_VERSION, build_prompts

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_BODY_BYTES = 4_000


class SuggestRequest(BaseModel):
    tenant_id: str
    conversation_id: int
    anchor_message_id: int


def require_suggest_secret(x_suggest_secret: str | None = Header(default=None)) -> None:
    secret = suggest_settings.secret()
    # Defence in depth: main.py does not register the route without a secret.
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")
    if not hmac.compare_digest((x_suggest_secret or "").encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="bad or missing X-Suggest-Secret")


# --- Concurrency ------------------------------------------------------------

_semaphore: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(suggest_settings.max_concurrency())
    return _semaphore


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


def _parse(raw: bytes) -> SuggestRequest:
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"body over {MAX_BODY_BYTES} bytes")
    try:
        return SuggestRequest.model_validate_json(raw or b"")
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False, include_input=False),
        ) from exc


def _empty(status: str, anchor: int, bot_status: str, model: str, reason: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "anchor_message_id": anchor,
        "bot_status": bot_status,
        "suggestions": [],
        "kb_gap": False,
        "sensitive": False,
        "language": "",
        "model": model,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "latency_ms": 0,
        "prompt_version": SUGGEST_PROMPT_VERSION,
    }


async def run_suggest(
    conversation_id: int, anchor_message_id: int, *, model: str | None = None,
    as_of: bool = False,
) -> dict[str, Any]:
    """The drafting itself, without the HTTP layer. Returns the response body
    plus an internal "_dropped" list (reasons only) for scripts; the route
    removes it. Must be called inside readonly.read_only(). `as_of` is for
    scripts/eval_suggest.py (see context.build); the route never sets it."""
    started = time.monotonic()
    model = model or suggest_settings.model()
    timings: dict[str, int] = {}
    mark = started

    def lap(name: str) -> None:
        nonlocal mark
        now = time.monotonic()
        timings[name] = int((now - mark) * 1000)
        mark = now

    ctx = await context.build(conversation_id, anchor_message_id, as_of=as_of)
    lap("db_reads")
    bot_status = str(ctx.conversation.get("bot_status") or "")
    if ctx.status in ("stale", "skipped"):
        # No retrieval and no model call: there is nothing to draft (stale) or
        # nobody to draft for (skipped: a group chat, or one of our own people).
        body = _empty(ctx.status, anchor_message_id, bot_status, model, ctx.reason)
        body["_dropped"] = []
        body["latency_ms"] = int((time.monotonic() - started) * 1000)
        return body

    # The pricing rules are whatever snapshot the live bot has in force
    # (kb_rules.rules(), read by the guards). Never refreshed from here: the
    # bot's own turn keeps it current (graph.py L536), and refresh() swaps in a
    # new snapshot only after a whole, valid read, so not calling it can never
    # leave the live rules half replaced.

    found = await retrieval.retrieve(ctx.state)
    lap("kb_search")
    state = {
        **ctx.state,
        "rag_matches": found["matches"],
        "rag_context": found["rag_context"],
        "rag_best_score": found["best"],
        "case_summary": found["case_summary"],
    }
    system_prompt, user_prompt = build_prompts(state, found["records"])
    raw, usage = await suggest_llm.draft(
        system_prompt, user_prompt, model=model, timeout=suggest_settings.timeout_seconds()
    )
    lap("model")
    checked = validate.validate(
        raw, state=state, payload=ctx.payload,
        matches=found["matches"], rag_context=found["rag_context"], finish_now=False,
    )
    # Same meaning in other words (an embedding), then the best 4.
    lap("checks")
    checked.kept = await meaning.drop_same_meaning(checked.kept, checked.dropped)
    checked = validate.finish(checked)
    lap("meaning")
    kb_gap = (
        bool(raw.get("kb_gap"))
        or not found["matches"]
        or found["best"] < settings.rag_soft_floor
    )
    lang = raw.get("language") if isinstance(raw.get("language"), str) else ""
    body = {
        "status": "ready" if checked.kept else "insufficient",
        "reason": "" if checked.kept else "no_valid_draft",
        "anchor_message_id": anchor_message_id,
        "bot_status": bot_status,
        "suggestions": checked.kept,
        "kb_gap": kb_gap,
        "sensitive": bool(raw.get("sensitive")),
        "language": lang.strip() or language.reply_language(state.get("incoming_text") or ""),
        "model": model,
        "usage": usage,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "timings_ms": timings,
        "prompt_version": SUGGEST_PROMPT_VERSION,
        "_dropped": checked.dropped,
    }
    return body


@router.post("/internal/suggest", dependencies=[Depends(require_suggest_secret)])
async def suggest(request: Request) -> Any:
    # Reached only with a valid secret (the dependency runs first, and there is
    # no declared body for FastAPI to validate ahead of it).
    body = _parse(await request.body())
    if not settings.tenant_id or not hmac.compare_digest(
        body.tenant_id.encode(), settings.tenant_id.encode()
    ):
        return _error(403, "wrong_tenant")

    gate = _gate()
    if gate.locked():
        return _error(429, "busy")
    async with gate:
        started = time.monotonic()
        try:
            with readonly.read_only() as refused:
                result = await asyncio.wait_for(
                    run_suggest(body.conversation_id, body.anchor_message_id),
                    timeout=suggest_settings.timeout_seconds(),
                )
                refused_writes = list(refused)
        except asyncio.TimeoutError:
            logger.warning("suggest: conversation %s timed out", body.conversation_id)
            return _error(504, "timeout")
        except context.ConversationNotFound:
            return _error(404, "conversation_not_found")
        except readonly.ReadOnlyViolation:
            logger.error("suggest: conversation %s tried to reach WhatsApp", body.conversation_id)
            return _error(500, "read_only_violation")
        except suggest_llm.SuggestLLMError as exc:
            logger.warning("suggest: conversation %s model error (%s)", body.conversation_id, exc)
            return _error(500, "llm_error")
        except Exception:  # noqa: BLE001 - never an unhandled error to the caller
            logger.exception("suggest: conversation %s failed", body.conversation_id)
            return _error(500, "internal")

        if refused_writes:
            # Something on this path tried to write. Nothing was written, but
            # that is a defect: no drafts until it is found.
            logger.error(
                "suggest: conversation %s attempted %d write(s): %s",
                body.conversation_id, len(refused_writes),
                [w.get("target") for w in refused_writes],
            )
            return _error(500, "read_only_violation")

        dropped = result.pop("_dropped", [])
        timings = result.pop("timings_ms", {})  # logged, not part of the response
        logger.info(
            "suggest: conversation %s status=%s kept=%d dropped=%d tokens=%s/%s %dms %s",
            body.conversation_id, result["status"], len(result["suggestions"]), len(dropped),
            result["usage"]["prompt_tokens"], result["usage"]["completion_tokens"],
            int((time.monotonic() - started) * 1000), timings,
        )
        return result
