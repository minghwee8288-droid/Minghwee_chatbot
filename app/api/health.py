"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.config import settings
from app.db.supabase import db

router = APIRouter(tags=["health"])


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
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}
