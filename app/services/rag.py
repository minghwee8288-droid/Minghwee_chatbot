"""Retrieval over ``cb_knowledge_base``.

The knowledge base is populated by the separate RAG pipeline project; the
chatbot only ever reads from it.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.db.supabase import db
from app.utils import redact_nric

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_embedding_client() -> AsyncOpenAI:
    """Client for the query embedding.

    This must hit the same provider and model the RAG pipeline used to build
    ``cb_knowledge_base`` — vectors from a different model or a different
    dimension count are not comparable, and the failure is silent: every
    similarity score simply comes back low.
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
            f"cb_knowledge_base.embedding"
        )
    return vector


async def search(
    query: str,
    *,
    match_count: int | None = None,
    match_threshold: float | None = None,
    namespace: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Vector search the knowledge base. Returns [] on any failure."""
    query = (query or "").strip()
    if not query:
        return []

    try:
        embedding = await embed_query(query)
    except Exception:  # noqa: BLE001 - a failed embedding must not break the reply
        logger.exception("Query embedding failed")
        return []

    params = {
        "query_embedding": embedding,
        "match_threshold": (
            settings.rag_match_threshold if match_threshold is None else match_threshold
        ),
        "match_count": match_count or settings.rag_match_count,
        "filter_namespace": namespace or (settings.rag_namespace or None),
        "filter_category": category,
    }
    try:
        rows = await db.rpc("cb_match_knowledge_base", params)
    except Exception:  # noqa: BLE001
        logger.exception("cb_match_knowledge_base failed")
        return []

    matches = rows or []
    logger.info(
        "KB search returned %s matches (best=%.3f)",
        len(matches),
        float(matches[0].get("similarity") or 0) if matches else 0.0,
    )
    return matches


def best_similarity(matches: list[dict[str, Any]]) -> float:
    if not matches:
        return 0.0
    try:
        return float(matches[0].get("similarity") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _truncate(content: str | None) -> str:
    """Keep long document chunks from swamping the prompt."""
    text = (content or "").strip()
    limit = settings.rag_max_chunk_chars
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def format_context(matches: list[dict[str, Any]]) -> str:
    """Render retrieved chunks as Part D of the system prompt."""
    if not matches:
        return (
            "Based on our records:\n(no relevant records found)\n\n"
            "You do not have information to answer this. Tell the client you will "
            "check with the team and get back to them."
        )

    lines = ["Based on our records:"]
    for index, match in enumerate(matches, start=1):
        similarity = match.get("similarity")
        score = f"{float(similarity):.2f}" if similarity is not None else "n/a"
        question = (match.get("question") or "").strip()
        answer = (match.get("answer") or "").strip()
        if question and answer:
            body = f"Q: {question}\n   A: {answer}"
        else:
            body = _truncate(match.get("content"))
        source = match.get("source_id") or match.get("category") or "knowledge base"
        lines.append(f"{index}. [{score} | {source}]\n   {body}")

    lines.append(
        "\nUse this information to answer the client's question. Do not invent "
        "figures, dates or policies that are not written above. If none of these "
        "are relevant, say you will check with the team."
    )
    return "\n".join(lines)
