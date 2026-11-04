"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.config import settings
from app.db.supabase import db

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
_CRITICAL_CHECKS = frozenset({"database", "whapi_token", "checkpointer_durable"})


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": __version__,
        "environment": settings.environment,
    }


@router.get("/health/ready")
async def readiness() -> dict[str, object]:
    database_ok = await db.healthcheck()
    checks = {
        "database": database_ok,
        "whapi_token": bool(settings.whapi_api_token),
        "openrouter_key": bool(settings.openrouter_api_key),
        "embedding_key": bool(settings.resolved_embedding_key),
        "transcription_key": not settings.transcription_enabled
        or bool(settings.resolved_transcription_key),
        "tenant_id": bool(settings.tenant_id),
    }
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
