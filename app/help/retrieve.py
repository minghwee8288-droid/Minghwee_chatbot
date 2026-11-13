"""Knowledge-base search for Help, through the bot's own rag.search.

The rules are the bot's (app/graph/nodes/rag_retriever.py), minus the client-only
ones:

* A price question with a service keeps the service filter and is NEVER
  widened - widening once quoted another service's fee ($695 for a $450
  passport renewal).
* Otherwise, a service-filtered search below RAG_SOFT_FLOOR is retried once
  with no service filter (nationality kept), and the wider result is used only
  if it scores better.
* Both audiences: contact_type is never filtered (staff see employer and helper
  rows alike).
* cost_withheld_services is NOT applied: staff may see every fee in the KB (the
  client's decision).

The embedding is made first, on its own, so a provider failure is reported as
search_unavailable instead of looking like "not found" (rag.search swallows
it). rag's embedding cache means the search then reuses that vector. An RPC
failure inside rag.search still returns [] and so still reads as not found.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.services import kb_rules, rag

logger = logging.getLogger(__name__)

# The KB labels a transfer `transfer` whichever side asks (rag_retriever's
# _RETRIEVAL_ALIASES, copied: one line, and a private name).
_ALIASES = {"transfer_employer": "transfer"}


class SearchUnavailable(RuntimeError):
    """The query could not be embedded, so no search could run."""


def allowed_services() -> frozenset[str]:
    """Service keys the planner may return, from the code (kb_rules), in KB labels.

    fee_enquiry is a conversation state of the bot's, not a subject the KB is
    filed under, so it is left out.
    """
    return frozenset(
        _ALIASES.get(s, s) for s in kb_rules.PRICED_SERVICES if s != "fee_enquiry"
    )


@dataclass
class Retrieval:
    search_query: str
    service_type: str | None
    nationality: str | None
    price_question: bool
    matches: list[dict[str, Any]] = field(default_factory=list)
    widened: bool = False

    @property
    def top_score(self) -> float | None:
        return rag.best_similarity(self.matches) if self.matches else None


async def _service_in_kb(service: str | None) -> str | None:
    """None if the KB has no row labelled with it (rag would narrow to 'general')."""
    if not service:
        return None
    vocabulary = await rag.column_vocabulary("service_type")
    if vocabulary is not None and service.lower() not in vocabulary:
        logger.info("help: service '%s' is not a KB label - searching without it", service)
        return None
    return service


async def retrieve(
    query: str,
    *,
    service_type: str | None,
    nationality: str | None,
    price_question: bool,
    match_count: int,
) -> Retrieval:
    try:
        await rag.embed_query(query)
    except Exception as exc:  # noqa: BLE001
        raise SearchUnavailable(type(exc).__name__) from exc

    service = await _service_in_kb(service_type)
    result = Retrieval(query, service, nationality, price_question)
    result.matches = await rag.search(
        query,
        match_count=match_count,
        service_type=service,
        contact_type=None,
        nationality=nationality,
    )
    best = rag.best_similarity(result.matches)

    if price_question and service:
        # Never widened: another service's fee is a wrong answer, not a vague one.
        return result
    if service and best < settings.rag_soft_floor:
        wider = await rag.search(
            query,
            match_count=match_count,
            service_type=None,
            contact_type=None,
            nationality=nationality,
        )
        if rag.best_similarity(wider) > best:
            result.matches = wider
            result.widened = True
    return result
