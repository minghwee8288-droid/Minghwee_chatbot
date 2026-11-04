"""Health and readiness endpoints."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter

from app import __version__
from app.config import settings
from app.db.supabase import db
from app.whapi.client import whapi

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# 2026-11-04: /health/ready says where conversation state is kept, and memory
# is degraded. It is what the container's healthcheck reads (app/healthcheck.py).
# False = the checkpointer is not reported.
CHECKPOINTER_IN_READY = True

# 2026-11-04: only these decide the status, and so the container's label and the
# uptime monitor. Anything else failing is named under "warnings", because a
# missing transcription key costs voice notes and not the bot. The whole body is
# public (no auth, through nginx), so it holds booleans and fixed names only:
# never an exception's text, a count, a version or a value from the environment.
# False = any failing check is degraded, and no warnings.
CRITICAL_CHECKS_ONLY = True
_CRITICAL_CHECKS = frozenset({"database", "whapi_token", "whapi_channel", "checkpointer_durable"})

# 2026-11-04: whapi_token only proves the token is set, and production will not
# start without one, so a disconnected channel still read as healthy. This asks
# Whapi. One call per _CHANNEL_CACHE_SECONDS whoever is asking, because the
# endpoint is public, and the timeout stays under healthcheck.TIMEOUT_SECONDS.
# Measured on the live channel: about 350 ms, and one stall of over 10 s in 15
# calls. So "could not tell" keeps the last good answer for one refresh, and only
# a second one in a row, or Whapi itself saying the channel is not connected,
# makes this false. False = the check is not made and whapi_channel not reported.
WHAPI_CHANNEL_IN_READY = True
_CHANNEL_CACHE_SECONDS = 60.0
_CHANNEL_TIMEOUT_SECONDS = 2.5
_channel_lock = asyncio.Lock()
_channel_checked_at: float | None = None
_channel_ok = False
_channel_unknown_in_a_row = 0


async def _whapi_channel_ok() -> bool:
    global _channel_checked_at, _channel_ok, _channel_unknown_in_a_row

    def fresh() -> bool:
        return (
            _channel_checked_at is not None
            and time.monotonic() - _channel_checked_at < _CHANNEL_CACHE_SECONDS
        )

    if not settings.whapi_api_token:
        return False
    if fresh():
        return _channel_ok
    async with _channel_lock:
        if fresh():
            return _channel_ok
        try:
            seen = await whapi.channel_connected(_CHANNEL_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - a health check must answer
            logger.warning("Whapi channel check failed: %s", type(exc).__name__)
            seen = None
        was_ok = _channel_ok
        if seen is None:
            _channel_unknown_in_a_row += 1
            _channel_ok = _channel_ok and _channel_unknown_in_a_row < 2
        else:
            _channel_unknown_in_a_row = 0
            _channel_ok = seen
        if was_ok and not _channel_ok:
            logger.error("Whapi channel is not connected (or Whapi cannot be reached)")
        _channel_checked_at = time.monotonic()
        return _channel_ok


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": __version__,
        "environment": settings.environment,
    }


@router.get("/health/ready")
async def readiness() -> dict[str, object]:
    checks: dict[str, bool] = {}
    if WHAPI_CHANNEL_IN_READY:
        checks["database"], checks["whapi_channel"] = await asyncio.gather(
            db.healthcheck(), _whapi_channel_ok()
        )
    else:
        checks["database"] = await db.healthcheck()
    checks.update({
        "whapi_token": bool(settings.whapi_api_token),
        "openrouter_key": bool(settings.openrouter_api_key),
        "embedding_key": bool(settings.resolved_embedding_key),
        "transcription_key": not settings.transcription_enabled
        or bool(settings.resolved_transcription_key),
        "tenant_id": bool(settings.tenant_id),
    })
    body: dict[str, object] = {"checks": checks}
    if CHECKPOINTER_IN_READY:
        from app.graph.graph import checkpointer_backend

        backend = checkpointer_backend()
        checks["checkpointer_durable"] = backend == "postgres"
        body["checkpointer"] = backend
    if CRITICAL_CHECKS_ONLY:
        body["warnings"] = sorted(k for k, ok in checks.items() if k not in _CRITICAL_CHECKS and not ok)
        checks = {k: ok for k, ok in checks.items() if k in _CRITICAL_CHECKS}
        body["checks"] = checks
    body["status"] = "ok" if all(checks.values()) else "degraded"
    return body
