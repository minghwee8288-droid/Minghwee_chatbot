"""Supabase client singleton plus small async helpers.

supabase-py is synchronous, so every call is pushed onto a worker thread with
``asyncio.to_thread`` to keep the FastAPI event loop free.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any, Callable, TypeVar

import httpx
from supabase import Client, create_client

from app.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _is_duplicate_key(exc: Exception) -> bool:
    """Whether this failure is Postgres rejecting a value that already exists."""
    if str(getattr(exc, "code", "")) == "23505":
        return True
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text


# A connection that died between requests, rather than a request that failed.
#
# supabase-py holds one httpx client with HTTP/2 keep-alive. Supabase closes
# idle connections from its side; the next call picks the dead socket out of the
# pool and raises before the request is answered. One client messaging slowly
# rarely trips it — two clients on separate worker threads trip it regularly,
# which is why it appeared the day a second number was added to the allowlist.
_DISCONNECTED = (
    httpx.RemoteProtocolError,  # "Server disconnected"
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
)

# Only reads are retried, and only these. The failure happens while the response
# is being received, so the request may already have reached Postgres — harmless
# for a SELECT, a duplicate row for an INSERT. Writes are left to fail loudly.
_READ_RETRIES = 2


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set before the "
            "Supabase client can be created."
        )
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


async def run_sync(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking supabase-py call on a worker thread."""
    return await asyncio.to_thread(fn, *args, **kwargs)


class Database:
    """Thin async facade over the Supabase REST client."""

    @property
    def client(self) -> Client:
        return get_supabase()

    def table(self, name: str):
        return self.client.table(name)

    async def execute(self, query: Any) -> Any:
        """Execute a prepared postgrest query off the event loop.

        A read whose connection died in flight is retried rather than raised.
        Without this, one stale pooled socket takes out whatever was running on
        it — and the read on the hot path is get_by_phone(), the first thing
        every inbound message does. When that raised, handle_payload logged the
        traceback and the client's message was simply gone: no reply, no ticket,
        no handover, nothing to say it had happened.

        Reads are identified by the method postgrest is about to use, so this
        covers callers that build their own query and pass it here — which is
        most of them — without any of them having to opt in. Writes fall
        straight through to the raise.
        """
        is_read = str(getattr(query, "http_method", "")).upper() in {"GET", "HEAD"}
        attempts = _READ_RETRIES + 1 if is_read else 1
        for attempt in range(1, attempts + 1):
            try:
                return await run_sync(query.execute)
            except _DISCONNECTED as exc:
                if attempt == attempts:
                    raise
                logger.warning(
                    "Supabase read lost its connection (%s: %s) — retrying (%d/%d)",
                    type(exc).__name__,
                    exc,
                    attempt,
                    attempts - 1,
                )
                # A fresh connection is what is needed, not a pause; the sleep
                # only keeps a genuinely down server from being hammered.
                await asyncio.sleep(0.25 * attempt)
        return None  # unreachable — the loop either returns or raises

    async def select_one(self, table: str, columns: str = "*", **filters: Any) -> dict | None:
        query = self.table(table).select(columns)
        for column, value in filters.items():
            query = query.eq(column, value)
        result = await self.execute(query.limit(1))
        rows = result.data or []
        return rows[0] if rows else None

    async def select_many(self, table: str, columns: str = "*", limit: int = 100, **filters: Any) -> list[dict]:
        query = self.table(table).select(columns)
        for column, value in filters.items():
            query = query.eq(column, value)
        result = await self.execute(query.limit(limit))
        return result.data or []

    async def insert(self, table: str, payload: dict) -> dict | None:
        result = await self.execute(self.table(table).insert(payload))
        rows = result.data or []
        return rows[0] if rows else None

    async def insert_numbered(
        self,
        table: str,
        payload: dict,
        *,
        number_field: str,
        next_number: Callable[[], Any],
        attempts: int = 5,
    ) -> dict | None:
        """Insert a row whose reference number is derived from the last one.

        CB-2026-0007 and L-2026-0003 are produced by reading the highest number
        on the table and adding one. That is safe with one client talking to the
        bot and unsafe the moment there are two: both turns read the same last
        number, both build the same next one, and the unique index
        (cb_tkt_tenant_number_uidx / leads_tenant_lead_number_uidx) rejects the
        second insert. The caller logs it and returns None — so the client is
        told a colleague will follow up while no ticket exists for anyone to
        follow up from. Nothing surfaces the loss.

        Re-reading and retrying closes it: the loser of the race sees the
        winner's row on the next read and takes the number after it. The same
        applies to the portal creating a lead at the same moment, which no
        amount of in-process locking would cover.
        """
        for attempt in range(1, attempts + 1):
            row = {**payload, number_field: await next_number()}
            try:
                return await self.insert(table, row)
            except Exception as exc:  # noqa: BLE001 - re-raised unless it is the race
                if attempt == attempts or not _is_duplicate_key(exc):
                    raise
                logger.warning(
                    "%s %s was taken (attempt %d/%d) — re-reading and retrying",
                    table,
                    row.get(number_field),
                    attempt,
                    attempts,
                )
        return None

    async def update(self, table: str, payload: dict, **filters: Any) -> list[dict]:
        query = self.table(table).update(payload)
        for column, value in filters.items():
            query = query.eq(column, value)
        result = await self.execute(query)
        return result.data or []

    async def rpc(self, function_name: str, params: dict | None = None) -> Any:
        result = await run_sync(lambda: self.client.rpc(function_name, params or {}).execute())
        return result.data

    async def healthcheck(self) -> bool:
        try:
            await self.execute(self.table("cb_tickets").select("id").limit(1))
            return True
        except Exception:  # noqa: BLE001 - health endpoint must never raise
            logger.exception("Supabase healthcheck failed")
            return False


db = Database()
