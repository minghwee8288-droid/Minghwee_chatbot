"""Supabase client singleton plus small async helpers.

supabase-py is synchronous, so every call is pushed onto a worker thread with
``asyncio.to_thread`` to keep the FastAPI event loop free.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any, Callable, TypeVar

from supabase import Client, create_client

from app.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


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
        """Execute a prepared postgrest query off the event loop."""
        return await run_sync(query.execute)

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
