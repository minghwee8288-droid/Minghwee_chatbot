"""Read-only mode for one request: nothing it does may write anywhere.

Used by POST /admin/preview, which runs the real conversation graph on a test
question for the KB Admin UI. The graph is the same one a client talks to, and
several of its nodes write as a side effect of answering - `_open_lead_early`
opens a lead, ticket_creator inserts a ticket, handover_executor records a
handover, assignment calls cb_get_next_agent (which advances the round robin).
A preview must produce the real reply without any of that happening.

So instead of stubbing each writer - and missing the next one somebody adds -
the check sits at the two doors every write goes through:

* `Database.execute` (every table query) refuses any method but GET/HEAD, and
  `Database.rpc` refuses any function not in READ_RPCS;
* the Whapi client refuses to open a connection at all.

A refused DATABASE write is recorded and answered with an empty result, which
is exactly what the callers already handle when a write comes back empty (they
log it and carry on), so the turn completes and the preview can report what it
WOULD have written. A refused WHAPI request raises instead: no graph node sends
on WhatsApp, so one that tries is a defect worth failing loudly on.

A ContextVar rather than a flag, so it is scoped to the one request: every
other conversation the process is handling at that moment writes normally.
Tasks created inside the block inherit it, which is what makes it reach
anything the graph schedules.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger(__name__)

# The only RPCs a read-only request may call. The knowledge-base search is the
# one the graph needs; selfcheck_kb_prep.py asserts this matches
# rag.KB_MATCH_FUNCTION so a rename cannot quietly block retrieval.
READ_RPCS = frozenset({"cb_match_knowledge_base_updated"})

_blocked: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "read_only_blocked", default=None
)


class ReadOnlyViolation(RuntimeError):
    """Something tried to leave the process during a read-only request."""


@contextmanager
def read_only() -> Iterator[list[dict[str, str]]]:
    """Run a block with every write refused. Yields the list of refusals."""
    token = _blocked.set([])
    try:
        yield _blocked.get()  # type: ignore[misc]
    finally:
        _blocked.reset(token)


def active() -> bool:
    return _blocked.get() is not None


def record(method: str, target: str) -> None:
    """Note a refused write. Only called while active()."""
    log = _blocked.get()
    if log is None:  # pragma: no cover - callers check active() first
        return
    log.append({"method": method, "target": target})
    logger.info("read-only: refused %s %s", method, target)
