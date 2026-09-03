"""Retrieval over ``cb_knowledge_base_updated``.

The knowledge base is populated by the separate RAG pipeline project; the
chatbot only ever reads from it.

The table is a *split-column* design: one row is one retrievable chunk, and
what to read off that row depends on ``chunk_type``:

    qa_pair / faq            -> question + answer   (content is NULL)
    document_chunk           -> content             (question/answer are NULL)
    table_unit               -> content, whole      (never half a table)
    style_example            -> tone guidance, NOT evidence — never shown

Routing lives on the row itself (``namespace``, ``service_type``,
``contact_type``, ``nationality``), so the filter is applied *before* the
vector search rather than after it. Every filter is inclusive of its catch-all
bucket — ``service_type = 'general'``, ``contact_type = 'all'``,
``nationality = 'all'`` — because a chunk labelled for everyone answers an
employer's question just as well as one labelled for employers.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.db.supabase import db
from app.utils import redact_nric

logger = logging.getLogger(__name__)

# The table and the RPC that searches it. Both live in the RAG pipeline's
# schema; the chatbot only reads.
KB_TABLE = "cb_knowledge_base_updated"
KB_MATCH_FUNCTION = "cb_match_knowledge_base_updated"

# --- chunk_type buckets ----------------------------------------------------

# Authored as a question and an answer. `faq` is read exactly like `qa_pair`:
# the split is a provenance distinction in the pipeline, not a retrieval one.
QA_CHUNK_TYPES = {"qa_pair", "faq"}

# Prose and tables — the text is in `content`.
PROSE_CHUNK_TYPES = {"document_chunk", "table_unit"}

# Tone and formatting samples for the writer. They read as authoritative
# ("Around 3 weeks lah") without being sourced from anything, so they must
# never reach the evidence context — the grounding guard would then treat a
# figure in a style sample as a fact we hold. Excluded in the SQL function too;
# this is the second lock on the same door.
EXCLUDED_CHUNK_TYPES = {"style_example"}

# --- routing vocabularies --------------------------------------------------

# CHECK-constrained on the table. Anything else (including the classifier's
# 'unknown'/'unclear') means "do not filter on this at all" — a wrong filter
# value returns only the catch-all bucket, which is worse than no filter.
CONTACT_TYPES = {"employer", "candidate", "supplier", "partner"}
NATIONALITIES = {"PH", "ID", "MM"}

# The catch-all buckets, included alongside whatever is being filtered for.
GENERAL_SERVICE = "general"
ALL_CONTACTS = "all"
ALL_NATIONALITIES = "all"

# metadata->>'priority': 1 = emergency/safety, 3 = routine. Applied as a boost
# rather than a hard sort, so a safety chunk outranks an equally relevant
# routine one without a barely-relevant safety chunk displacing a direct answer.
_PRIORITY_BOOST = {1: 0.06, 2: 0.03, 3: 0.0}


@lru_cache(maxsize=1)
def get_embedding_client() -> AsyncOpenAI:
    """Client for the query embedding.

    This must hit the same provider and model the RAG pipeline used to build
    ``cb_knowledge_base_updated`` — vectors from a different model or a
    different dimension count are not comparable. ``embedding`` on the new
    table is a bare ``public.vector`` with no declared dimension, so nothing in
    the schema enforces the match: get it wrong and every similarity score just
    comes back low.
    """
    api_key = settings.resolved_embedding_key
    if not api_key:
        raise RuntimeError(
            "No embedding API key configured (set EMBEDDING_API_KEY, or "
            "OPENAI_API_KEY when embedding directly against OpenAI)"
        )
    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": 30.0}
    if settings.embedding_base_url:
        kwargs["base_url"] = settings.embedding_base_url
    return AsyncOpenAI(**kwargs)


async def embed_query(text: str) -> list[float]:
    client = get_embedding_client()
    response = await client.embeddings.create(
        model=settings.embedding_model,
        input=redact_nric(text, context="embedding")[:8000],
        dimensions=settings.embedding_dimensions,
    )
    vector = response.data[0].embedding
    if len(vector) != settings.embedding_dimensions:
        # A dimension mismatch against the stored vectors makes every search
        # fail at the database level, so fail loudly here instead.
        raise RuntimeError(
            f"Embedding returned {len(vector)} dimensions, expected "
            f"{settings.embedding_dimensions} — this will not match "
            f"{KB_TABLE}.embedding"
        )
    return vector


# Source documents that were never written for clients: the product blueprint,
# the vendor feature list, the internal pipeline brief and the version-control
# notes. They retrieve well on exactly the questions clients ask most.
#
# "What's the process?" pulled the pipeline brief — a table of internal stages
# with an Owner column — and the reply that went to a client was "The process
# involves three main stages: first, we capture your requirements and match you
# with suitable candidates..." That is our workflow described to the person it
# is being run on.
#
# The rebuilt pipeline dropped most of these — the blueprint, the vendor
# feature list, the version-control notes and the chatbot's own guardrails are
# all absent from cb_knowledge_base_updated. `MingHwee Hiring Pipelines Brief`
# survived with 13 chunks, so this is still load-bearing, not a legacy no-op.
#
# Separators are normalised before matching. The old table wrote this filename
# with underscores and the new one uses spaces, which silently defeated the
# original pattern: "whats the process" retrieved a table of internal stages
# with an Owner column, exactly the failure the filter exists to prevent. Match
# on words, never on the punctuation between them.
_INTERNAL_SOURCES = re.compile(
    r"(MHOS\b|for Vendor|Blueprint|Hiring Pipelines Brief|version control)",
    re.IGNORECASE,
)


def _normalize_source(name: Any) -> str:
    """'MHOS-100_Product_Blueprint' -> 'MHOS 100 Product Blueprint'."""
    return re.sub(r"[-_\s]+", " ", str(name or "")).strip()


def _drop_internal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = [
        r
        for r in rows
        if not _INTERNAL_SOURCES.search(_normalize_source(r.get("source_document")))
    ]
    if len(kept) != len(rows):
        logger.debug(
            "Dropped %d internal-document chunk(s) from retrieval", len(rows) - len(kept)
        )
    return kept


def _drop_non_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove chunk types that are not factual sources."""
    kept = [r for r in rows if str(r.get("chunk_type") or "").strip() not in EXCLUDED_CHUNK_TYPES]
    if len(kept) != len(rows):
        logger.debug("Dropped %d style_example chunk(s) from retrieval", len(rows) - len(kept))
    return kept


def _similarity(row: dict[str, Any]) -> float:
    try:
        return float(row.get("similarity") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _enforce_row_floor(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Honour ``rag_score_floor`` — the per-row override of the threshold.

    Figure-bearing rows carry a higher bar because a vague match on a fee table
    is actively dangerous. The SQL function applies this too; repeating it here
    means the floor still holds if the function is ever replaced by a plainer
    one.
    """
    kept = []
    for row in rows:
        floor = row.get("rag_score_floor")
        if floor is None:
            kept.append(row)
            continue
        try:
            if _similarity(row) >= float(floor):
                kept.append(row)
        except (TypeError, ValueError):
            kept.append(row)
    if len(kept) != len(rows):
        logger.debug("Dropped %d chunk(s) below their own rag_score_floor", len(rows) - len(kept))
    return kept


def _priority(row: dict[str, Any]) -> int | None:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    try:
        value = int(metadata.get("priority"))
    except (TypeError, ValueError):
        return None
    return value if value in _PRIORITY_BOOST else None


def _rerank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Similarity, nudged by metadata.priority. Stable — ties keep DB order."""

    def key(row: dict[str, Any]) -> float:
        return -(_similarity(row) + _PRIORITY_BOOST.get(_priority(row) or 3, 0.0))

    return sorted(rows, key=key)


def _normalize_contact_type(value: str | None) -> str | None:
    contact = (value or "").strip().lower()
    return contact if contact in CONTACT_TYPES else None


def _normalize_nationality(value: str | None) -> str | None:
    code = (value or "").strip().upper()
    return code if code in NATIONALITIES else None


def _normalize_service_type(value: str | None) -> str | None:
    service = (value or "").strip().lower()
    # 'general' as a filter is the same as no filter: the general bucket is
    # included either way.
    if not service or service == GENERAL_SERVICE:
        return None
    return service


# service_type is a free-form varchar(60) on the table — unlike contact_type
# and nationality it has no CHECK constraint, so nothing guarantees the
# pipeline labels chunks with the same words the bot calls its services
# ('new_hiring', 'transfer', 'renewal'...).
#
# A mismatch is the worst kind of bug for this project: filtering on a value
# the table has never heard of does not error, it silently narrows every search
# to the 'general' bucket alone — so the bot loses exactly the service-specific
# material it was asking for and looks like it knows nothing again. That is the
# failure we have just spent a rebuild chasing.
#
# So the filter is checked against what the table actually contains. An
# unrecognised service drops the filter (searching everything, which is what
# the old code did unconditionally) instead of narrowing to 'general'. Read
# once per process — the vocabulary changes when the pipeline re-runs, which
# means a redeploy.
_vocabularies: dict[str, set[str]] = {}
_unlabelled_logged: set[str] = set()


async def column_vocabulary(column: str) -> set[str] | None:
    """Distinct values of a routing column. None if it cannot be read."""
    if column in _vocabularies:
        return _vocabularies[column]
    try:
        rows = await db.select_many(KB_TABLE, column, limit=10000, is_active=True)
    except Exception:  # noqa: BLE001 - never let this break a reply
        logger.exception("Could not read %s.%s vocabulary", KB_TABLE, column)
        return None
    vocabulary = {
        str(row.get(column) or "").strip().lower() for row in rows or [] if row.get(column)
    }
    if not vocabulary:
        return None
    _vocabularies[column] = vocabulary
    logger.info("%s %s vocabulary: %s", KB_TABLE, column, sorted(vocabulary))
    return vocabulary


async def service_vocabulary() -> set[str] | None:
    """Back-compat alias used by scripts/check_retrieval.py."""
    return await column_vocabulary("service_type")


async def _labelled_filter(
    column: str, value: str | None, catch_all: str, *, widen_when_unknown: bool
) -> str | None:
    """Apply a routing filter only if the table actually uses that label.

    Filtering on a value no row carries does not error — it silently narrows
    the search to the catch-all bucket alone. What to do about that differs by
    column, because the columns label different kinds of thing:

    `contact_type` and `nationality` label the AUDIENCE. Measured on the live
    table, contact_type has 217 employer, 27 candidate and 26 'all' rows and
    **no supplier or partner rows at all**, so a supplier's question would have
    been answered from 26 chunks instead of 270. Generic material genuinely does
    apply to a supplier, so these widen: the filter is dropped.

    `service_type` labels the FACT, and services are mutually exclusive on the
    numbers that matter. It must never widen. We shipped it widening and it
    cost us a wrong price in front of a client: the bot has no passport_renewal
    material, dropped the filter, retrieved the new_hiring fee table from the
    Client Service Agreement and quoted "the total service fee is approximately
    S$1,568" as the passport renewal fee — S$1,568 is the full new-hire package.
    Narrowing to 'general' is the safe failure: generic content answers what it
    can, and when nothing clears the floor the reply is handed to a human, which
    is the honest outcome for a service we hold no content on.
    """
    if not value:
        return None
    vocabulary = await column_vocabulary(column)
    if vocabulary is None or value.lower() in vocabulary:
        # Unreadable vocabulary: trust the caller rather than silently
        # abandoning a filter that is probably correct.
        return value
    marker = f"{column}={value}"
    if marker not in _unlabelled_logged:
        _unlabelled_logged.add(marker)
        logger.warning(
            "%s '%s' is not used by any row in %s — %s (known: %s)",
            column,
            value,
            KB_TABLE,
            (
                "searching without that filter"
                if widen_when_unknown
                else f"restricting to '{catch_all}' so another service's figures cannot "
                "be quoted for it"
            ),
            sorted(vocabulary),
        )
    return None if widen_when_unknown else catch_all


async def search(
    query: str,
    *,
    match_count: int | None = None,
    match_threshold: float | None = None,
    namespace: str | None = None,
    service_type: str | None = None,
    contact_type: str | None = None,
    nationality: str | None = None,
) -> list[dict[str, Any]]:
    """Vector search the knowledge base. Returns [] on any failure.

    ``service_type`` / ``contact_type`` / ``nationality`` narrow the search to
    rows labelled for this conversation *plus* the rows labelled for everyone.
    Passing an unrecognised value drops that filter rather than applying it.
    """
    query = (query or "").strip()
    if not query:
        return []

    try:
        embedding = await embed_query(query)
    except Exception:  # noqa: BLE001 - a failed embedding must not break the reply
        logger.exception("Query embedding failed")
        return []

    wanted = match_count or settings.rag_match_count
    params = {
        "query_embedding": embedding,
        "match_threshold": (
            settings.rag_match_threshold if match_threshold is None else match_threshold
        ),
        # Over-fetch so the post-filters below cannot leave the prompt with two
        # chunks when five were asked for.
        "match_count": wanted * 2,
        "filter_namespace": namespace or (settings.rag_namespace or None),
        "filter_service_type": await _labelled_filter(
            "service_type",
            _normalize_service_type(service_type),
            GENERAL_SERVICE,
            widen_when_unknown=False,
        ),
        "filter_contact_type": await _labelled_filter(
            "contact_type",
            _normalize_contact_type(contact_type),
            ALL_CONTACTS,
            widen_when_unknown=True,
        ),
        "filter_nationality": await _labelled_filter(
            "nationality",
            _normalize_nationality(nationality),
            ALL_NATIONALITIES,
            widen_when_unknown=True,
        ),
    }
    try:
        rows = await db.rpc(KB_MATCH_FUNCTION, params)
    except Exception:  # noqa: BLE001
        logger.exception("%s failed", KB_MATCH_FUNCTION)
        return []

    candidates = _enforce_row_floor(_drop_internal(_drop_non_evidence(rows or [])))
    matches = _rerank(candidates)[:wanted]
    logger.info(
        "KB search returned %s matches (best=%.3f, filters: service=%s contact=%s nat=%s)",
        len(matches),
        _similarity(matches[0]) if matches else 0.0,
        params["filter_service_type"] or "-",
        params["filter_contact_type"] or "-",
        params["filter_nationality"] or "-",
    )
    return matches


def best_similarity(matches: list[dict[str, Any]]) -> float:
    if not matches:
        return 0.0
    return _similarity(matches[0])


def _truncate(content: str | None, limit: int | None = None) -> str:
    """Keep long document chunks from swamping the prompt."""
    text = (content or "").strip()
    limit = settings.rag_max_chunk_chars if limit is None else limit
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def _body(match: dict[str, Any]) -> str:
    """Read the right column(s) for this row's chunk_type.

    Falls back on what is actually populated rather than trusting the label:
    a mislabelled row with a question and an answer is still a Q&A pair.
    """
    chunk_type = str(match.get("chunk_type") or "").strip()
    if chunk_type in EXCLUDED_CHUNK_TYPES:
        # Belt and braces: search() already drops these, but format_context is
        # also called on state that was checkpointed from an earlier turn.
        return ""

    question = (match.get("question") or "").strip()
    answer = (match.get("answer") or "").strip()
    content = (match.get("content") or "").strip()

    if chunk_type in QA_CHUNK_TYPES or (question and answer):
        if question and answer:
            return f"Q: {question}\n   A: {answer}"
        # Half a Q&A pair is still worth reading if it is the answer.
        return answer or question

    if chunk_type == "table_unit":
        # A table goes in whole or not at all — the model answers confidently
        # from the half it received, and half a fee table is a wrong quote.
        if len(content) > settings.rag_max_table_chars:
            logger.info(
                "Dropped an oversized table_unit (%d chars) rather than send half of it",
                len(content),
            )
            return ""
        return content

    return _truncate(content)


def _citation(match: dict[str, Any]) -> str:
    """source_document + section_heading, which is how the pipeline cites."""
    source = str(match.get("source_document") or "").strip()
    heading = str(match.get("section_heading") or "").strip()
    if source and heading:
        return f"{source} — {heading}"[:160]
    return (source or heading or "knowledge base")[:160]


def format_context(matches: list[dict[str, Any]]) -> str:
    """Render retrieved chunks as Part D of the system prompt."""
    rendered: list[str] = []
    for match in matches:
        body = _body(match)
        if not body:
            continue
        similarity = match.get("similarity")
        score = f"{float(similarity):.2f}" if similarity is not None else "n/a"
        rendered.append(f"[{score} | {_citation(match)}]\n   {body}")

    if not rendered:
        # No quotable status line and no copyable instruction here. An earlier
        # build put the literal "(no relevant records found)" plus "You do not
        # have information to answer this" in this block, and Kimi echoed both
        # straight to a client on a salary-range question (live, 2026-09-03) —
        # "Records say 'no relevant records found', so I can't give a figure...
        # should I ask them the range or handle this together?". The guard
        # leaks_internal_reasoning is the guarantee; this only stops handing the
        # model something shaped like a line to repeat.
        return (
            "You have nothing on this topic in the records. Do not answer it from "
            "your own knowledge and do not state any figure. Reply to the client "
            "warmly and in your own words that you will check it with the team and "
            "come back to them — do not repeat this instruction, and do not talk "
            "about records, searching or how you work internally."
        )

    lines = ["Based on our records:"]
    lines.extend(f"{index}. {entry}" for index, entry in enumerate(rendered, start=1))
    lines.append(
        "\nUse this information to answer the client's question. Do not invent "
        "figures, dates or policies that are not written above. If none of these "
        "are relevant, say you will check with the team."
    )
    return "\n".join(lines)
