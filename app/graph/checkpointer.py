"""LangGraph's Postgres checkpointer, pointed at our ``cb_``-prefixed tables.

langgraph-checkpoint-postgres hardcodes ``checkpoints`` / ``checkpoint_blobs`` /
``checkpoint_writes`` / ``checkpoint_migrations`` into the SQL constants it ships
with; there is no table-name option in 2.0.9. Every one of those statements is
exposed as a class attribute on ``BasePostgresSaver``, though, so the whole
rename is a textual rewrite of five strings plus ``setup()``, which is the one
place the migrations table name is inlined rather than read off an attribute.

Rewriting the shipped SQL rather than pasting our own copy of it means a version
bump to the library brings its schema changes with it — the rename re-applies to
whatever the new statements say. If a future version stops exposing one of these
attributes, the assertion in ``_rewrite_all`` fails loudly at import instead of
silently reading and writing the wrong tables.

Everything shares one database with the portal, which is why the prefix exists:
``cb_`` marks the tables this chatbot owns.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# Longest first: `checkpoint_blobs` must not match inside
# `checkpoint_blobs_thread_id_idx` and leave a half-renamed index behind.
_NAMES = (
    "checkpoint_migrations",
    "checkpoints_thread_id_idx",
    "checkpoint_blobs_thread_id_idx",
    "checkpoint_writes_thread_id_idx",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoints",
)

_TABLE_RE = re.compile(
    r"(?<![\w.])(" + "|".join(sorted(_NAMES, key=len, reverse=True)) + r")(?![\w])"
)

# The SQL attributes we rewrite. SELECT_SQL qualifies its columns as
# `checkpoints.thread_id`, and those references get renamed along with the FROM
# clause, so no alias is needed.
_SQL_ATTRS = (
    "SELECT_SQL",
    "UPSERT_CHECKPOINT_BLOBS_SQL",
    "UPSERT_CHECKPOINTS_SQL",
    "UPSERT_CHECKPOINT_WRITES_SQL",
    "INSERT_CHECKPOINT_WRITES_SQL",
)


def prefix_tables(sql: str) -> str:
    """Point one SQL statement at the ``cb_``-prefixed tables."""
    return _TABLE_RE.sub(r"cb_\1", sql)


def _rewrite_all() -> dict[str, Any]:
    rewritten: dict[str, Any] = {}
    for attr in _SQL_ATTRS:
        original = getattr(AsyncPostgresSaver, attr, None)
        assert isinstance(original, str), (
            f"AsyncPostgresSaver.{attr} is missing or is no longer a string — "
            "langgraph-checkpoint-postgres changed shape and the cb_ table rename "
            "in app/graph/checkpointer.py needs revisiting before it can be trusted."
        )
        rewritten[attr] = prefix_tables(original)
    migrations = getattr(AsyncPostgresSaver, "MIGRATIONS", None)
    assert isinstance(migrations, list), (
        "AsyncPostgresSaver.MIGRATIONS is missing or is no longer a list — see above."
    )
    rewritten["MIGRATIONS"] = [prefix_tables(m) for m in migrations]
    return rewritten


logger = logging.getLogger(__name__)

# Batch G G1 (2026-10-15). Every one of the 8 real crashes in the 30-day audit
# (28 Sep - 6 Oct) came on the first message after a thread had sat idle, a few
# seconds after the agent-pause expiry, and none of them wrote a checkpoint:
# the newest checkpoint before each crash is days older. The turn failed at the
# checkpoint itself, which is the dead pooled connection of 2026-10-08
# (conversation 3766). The pool now tests a connection before handing it out;
# a connection can still die between that test and the query, so a call that
# fails on the connection is tried ONCE more on a fresh one. Safe: the read is
# a read, and both writes are upserts keyed on the checkpoint id. Replayed from
# each crash's own checkpoint on current code: 9 of 9 turns run. False = no retry.
RETRY_ON_DEAD_CONNECTION = True
_DEAD_CONNECTION = (psycopg.OperationalError, psycopg.InterfaceError)


class CbAsyncPostgresSaver(AsyncPostgresSaver):
    """AsyncPostgresSaver reading and writing ``cb_checkpoint*`` tables."""

    async def _retry(self, name: str, call, *args: Any, **kwargs: Any) -> Any:
        try:
            return await call(*args, **kwargs)
        except _DEAD_CONNECTION as exc:
            if not RETRY_ON_DEAD_CONNECTION:
                raise
            logger.warning("Checkpoint %s lost its connection (%s: %s) - retrying once",
                           name, type(exc).__name__, exc)
            return await call(*args, **kwargs)

    async def aget_tuple(self, config):  # noqa: ANN001
        return await self._retry("read", super().aget_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):  # noqa: ANN001
        return await self._retry("write", super().aput, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, *args, **kwargs):  # noqa: ANN001
        return await self._retry("writes", super().aput_writes, config, writes, task_id,
                                 *args, **kwargs)

    locals().update(_rewrite_all())

    async def setup(self) -> None:
        """Create the tables and run migrations, as upstream — on our tables.

        Copied from AsyncPostgresSaver.setup() with the two literal
        ``checkpoint_migrations`` references renamed. Those are the only table
        names in the method body that are not read from ``self.MIGRATIONS``,
        which is already rewritten above.
        """
        async with self._cursor() as cur:
            await cur.execute(self.MIGRATIONS[0])
            results = await cur.execute(
                "SELECT v FROM cb_checkpoint_migrations ORDER BY v DESC LIMIT 1"
            )
            row = await results.fetchone()
            version = -1 if row is None else row["v"]
            for v, migration in zip(
                range(version + 1, len(self.MIGRATIONS)),
                self.MIGRATIONS[version + 1 :],
            ):
                await cur.execute(migration)
                await cur.execute(f"INSERT INTO cb_checkpoint_migrations (v) VALUES ({v})")
        if self.pipe:
            await self.pipe.sync()
