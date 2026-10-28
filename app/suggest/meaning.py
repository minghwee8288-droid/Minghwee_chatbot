"""Same-meaning drafts: the second near-duplicate pass, by embedding.

validate.near_duplicates catches very similar WORDING. Two drafts can also
say the same thing in different words - live, 2026-10-28 (eval): 4641's
"I understand the agency fee feels too expensive. I'll check with our team
whether there are any options" and "I understand. I'll check with our team
about the agency fee and confirm what options are available" (cosine 0.894),
and 739's Hold and Answer, both "I'll check the status of ... and confirm"
(0.953). Different approaches measured 0.30-0.83 on the same conversations, so
SAME_MEANING_COSINE is 0.85.

One batched request to the bot's embedding model, through rag's client but NOT
rag.embed_query: that fills the query cache the live bot's searches use, and
nothing here touches a cache the bot relies on. Any failure keeps the drafts
as they are (the wording pass has already run); it never fails the request.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.config import settings
from app.services import rag
from app.suggest.validate import _rank

logger = logging.getLogger(__name__)

SAME_MEANING_COSINE = 0.85


async def _embed(texts: list[str]) -> list[list[float]]:
    response = await rag.get_embedding_client().embeddings.create(
        model=settings.embedding_model, input=texts, dimensions=settings.embedding_dimensions,
    )
    return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]


def cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


async def drop_same_meaning(
    drafts: list[dict[str, Any]], dropped: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Keep the better of each same-meaning pair (high confidence, then the
    model's order); the model's order is kept."""
    if len(drafts) < 2:
        return drafts
    try:
        vectors = await _embed([d["text"] for d in drafts])
    except Exception as exc:  # noqa: BLE001 - the wording pass already ran
        logger.warning("suggest: same-meaning check skipped (%s)", type(exc).__name__)
        return drafts
    if len(vectors) != len(drafts):
        return drafts
    vector_of = {id(d): v for d, v in zip(drafts, vectors)}
    kept: list[dict[str, Any]] = []
    for draft in _rank(drafts):
        if any(cosine(vector_of[id(draft)], vector_of[id(k)]) >= SAME_MEANING_COSINE for k in kept):
            dropped.append({"label": draft["label"], "reason": "says the same as another draft"})
            continue
        kept.append(draft)
    return [d for d in drafts if any(d is k for k in kept)]
