"""Verify and calibrate retrieval against the live cb_knowledge_base.

Run this once the .env is filled in. It proves the query embedding matches the
vectors the RAG pipeline stored, and prints the similarity distribution so
RAG_MATCH_THRESHOLD and RAG_CONFIDENCE_FLOOR can be set from real numbers
instead of guesses.

    python scripts/check_retrieval.py
    python scripts/check_retrieval.py "can i hire a helper from myanmar"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.services import rag  # noqa: E402

# Questions clients actually ask, spread across the knowledge base.
DEFAULT_QUERIES = [
    "how much do you charge for a filipino helper",
    "how long does the whole hiring process take",
    "what documents do i need to hire a maid",
    "can i transfer a helper from another employer",
    "what is the salary for an indonesian helper",
    "my helper's work permit is expiring, how do i renew",
    "she wants to go home for leave, what do i need to do",
    "what happens if the helper doesn't suit us",
    "do i need to buy insurance for the helper",
    "how much is the levy for a domestic helper",
]


async def inspect_knowledge_base() -> None:
    print("=" * 78)
    print("cb_knowledge_base contents")
    print("=" * 78)
    rows = await db.select_many("cb_knowledge_base", "namespace, category, is_active", limit=5000)
    if not rows:
        print("  EMPTY — nothing to retrieve from.")
        return

    namespaces: dict[str, int] = {}
    categories: dict[str, int] = {}
    inactive = 0
    for row in rows:
        namespaces[row.get("namespace") or "(null)"] = namespaces.get(row.get("namespace") or "(null)", 0) + 1
        categories[row.get("category") or "(null)"] = categories.get(row.get("category") or "(null)", 0) + 1
        if not row.get("is_active"):
            inactive += 1

    print(f"  rows            : {len(rows)}")
    print(f"  inactive rows   : {inactive}  (these are excluded from search)")
    print(f"  namespaces      : {namespaces}")
    print(f"  categories      : {categories}")


async def check_query(query: str) -> float:
    matches = await rag.search(query, match_threshold=0.0, match_count=5)
    best = rag.best_similarity(matches)
    print(f"\n  Q: {query}")
    if not matches:
        print("     no matches at threshold 0.0 — retrieval is broken, not just cold")
        return 0.0
    for match in matches:
        score = float(match.get("similarity") or 0)
        label = (match.get("question") or match.get("content") or "")[:88].replace("\n", " ")
        print(f"     {score:.3f}  [{match.get('namespace')}] {label}")
    return best


async def main() -> None:
    queries = sys.argv[1:] or DEFAULT_QUERIES

    print(f"embedding model : {settings.embedding_model}")
    print(f"dimensions      : {settings.embedding_dimensions}")
    print(f"embedding host  : {settings.embedding_base_url or 'https://api.openai.com/v1 (direct)'}")
    print(f"match threshold : {settings.rag_match_threshold}")
    print(f"confidence floor: {settings.rag_confidence_floor}")
    print()

    try:
        vector = await rag.embed_query("test")
        print(f"embedding call OK — returned {len(vector)} dimensions")
    except Exception as exc:  # noqa: BLE001
        print(f"EMBEDDING CALL FAILED: {exc}")
        print("Fix this before anything else — no retrieval can work without it.")
        return

    await inspect_knowledge_base()

    print()
    print("=" * 78)
    print("Similarity for representative client questions")
    print("=" * 78)
    scores = [await check_query(query) for query in queries]

    hits = [score for score in scores if score > 0]
    print()
    print("=" * 78)
    if not hits:
        print("No query matched anything. Check that the stored vectors were built with")
        print("the same model and dimension count configured above.")
        return
    hits.sort()
    print(f"best-match similarity: min {hits[0]:.3f} | median {hits[len(hits) // 2]:.3f} | max {hits[-1]:.3f}")
    print()
    print("Suggested settings (answer confidently on good matches, hand over on weak ones):")
    print(f"  RAG_MATCH_THRESHOLD={max(0.05, hits[0] - 0.10):.2f}")
    print(f"  RAG_CONFIDENCE_FLOOR={max(0.10, hits[len(hits) // 2] - 0.10):.2f}")


if __name__ == "__main__":
    asyncio.run(main())
