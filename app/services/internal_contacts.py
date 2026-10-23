"""Owner and staff numbers the bot never replies to (2026-10-23).

Two sources, merged: INTERNAL_NUMBERS in the environment, and the
cb_internal_contacts table (scripts/sql/internal_contacts_001_create.sql,
applied only on a go-word). Numbers are never written in code.

The table is read at most once every few minutes. A failed read - the table
not created yet, the database briefly away - keeps the last good set, else the
environment list alone, and is logged. Never "no internal numbers" because of a
read error: a missed staff number is the bot talking over a colleague.
"""

from __future__ import annotations

import logging
import time

from app.config import settings
from app.db.supabase import db
from app.utils import normalize_phone

logger = logging.getLogger(__name__)

TABLE = "cb_internal_contacts"
_REFRESH_SECONDS = 300

_cached: set[str] = set()
_read_at: float = 0.0


async def _table_numbers() -> set[str]:
    global _cached, _read_at
    if _read_at and time.monotonic() - _read_at < _REFRESH_SECONDS:
        return _cached
    _read_at = time.monotonic()
    try:
        result = await db.execute(db.table(TABLE).select("phone").eq("active", True))
        _cached = {normalize_phone(str(r["phone"])) for r in (result.data or []) if r.get("phone")}
    except Exception as exc:  # noqa: BLE001 - keep the last good set
        logger.info("Internal contacts table not read (%s) - using the last good set", type(exc).__name__)
    return _cached


async def is_internal(phone: str) -> bool:
    """Whether this number belongs to the agency's own owner or staff."""
    number = normalize_phone(phone or "")
    if not number:
        return False
    if number in settings.internal_number_set:
        return True
    return number in await _table_numbers()
