"""Verify and calibrate retrieval against the live cb_knowledge_base_updated.

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
    print(f"{rag.KB_TABLE} contents")
    print("=" * 78)
    rows = await db.select_many(
        rag.KB_TABLE,
        "namespace, service_type, contact_type, nationality, chunk_type, metadata, "
        "rag_score_floor, is_active",
        limit=10000,
    )
    if not rows:
        print("  EMPTY — nothing to retrieve from.")
        return

    def tally(column: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            key = row.get(column) or "(null)"
            counts[key] = counts.get(key, 0) + 1
        return counts

    inactive = sum(1 for row in rows if not row.get("is_active"))

    print(f"  rows            : {len(rows)}")
    print(f"  inactive rows   : {inactive}  (these are excluded from search)")
    print(f"  namespaces      : {tally('namespace')}")
    print(f"  service_type    : {tally('service_type')}")
    print(f"  contact_type    : {tally('contact_type')}")
    print(f"  nationality     : {tally('nationality')}")
    print(f"  chunk_type      : {tally('chunk_type')}")
    print(
        f"  (style_example rows are tone guidance and are never returned as evidence)"
    )

    # Which metadata keys the classifier actually populates. The schema only
    # promises `metadata jsonb NOT NULL DEFAULT '{}'` — `priority`,
    # `figures_present` and `table_column` are conventions, not guarantees, and
    # priority is what drives reranking.
    keys: dict[str, int] = {}
    priorities: dict[str, int] = {}
    for row in rows:
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            continue
        for key in metadata:
            keys[key] = keys.get(key, 0) + 1
        if "priority" in metadata:
            label = f"{metadata['priority']!r}"
            priorities[label] = priorities.get(label, 0) + 1
    print(f"  metadata keys   : {keys or '(none populated)'}")
    print(f"  priority values : {priorities or '(none — reranking is a no-op)'}")

    floors = [row.get("rag_score_floor") for row in rows if row.get("rag_score_floor") is not None]
    if floors:
        numeric = sorted(float(value) for value in floors)
        print(
            f"  rag_score_floor : {len(numeric)} rows, "
            f"{numeric[0]:.3f}–{numeric[-1]:.3f}"
        )
        below = [value for value in numeric if value < settings.rag_match_threshold]
        if below:
            # The function treats a floor as a bar to RAISE. A floor beneath the
            # global threshold therefore does nothing — if these were meant to
            # let weaker rows through, the function needs the other reading.
            print(
                f"      NOTE: {len(below)} floor(s) sit below RAG_MATCH_THRESHOLD "
                f"({settings.rag_match_threshold}) and have no effect"
            )
    else:
        print("  rag_score_floor : none set")

    # The service vocabulary the bot will actually be able to filter on.
    vocabulary = await rag.service_vocabulary()
    print(f"  service filter  : {sorted(vocabulary) if vocabulary else '(unreadable)'}")


async def check_query(query: str) -> float:
    matches = await rag.search(query, match_threshold=0.0, match_count=5)
    best = rag.best_similarity(matches)
    print(f"\n  Q: {query}")
    if not matches:
        print("     no matches at threshold 0.0 — retrieval is broken, not just cold")
        return 0.0
    for match in matches:
        score = float(match.get("similarity") or 0)
        label = (match.get("question") or match.get("content") or match.get("answer") or "")
        label = label[:88].replace("\n", " ")
        print(
            f"     {score:.3f}  [{match.get('chunk_type')} | {match.get('service_type')}/"
            f"{match.get('contact_type')}/{match.get('nationality')}] {label}"
        )
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
    # RAG_SOFT_FLOOR, not RAG_CONFIDENCE_FLOOR: the latter is defined in config
    # but read by no code path, so suggesting a value for it was advice that
    # could not take effect. This one is the gate that replaces the reply and
    # hands over.
    print(f"  RAG_SOFT_FLOOR={max(0.10, hits[0] - 0.06):.2f}")
    print()
    print("Sanity-check the other side of the gap before trusting these — pass")
    print("off-topic messages as arguments and confirm they score below the floor:")
    print('  python scripts/check_retrieval.py "are you a bot" "can you fix my aircon"')


if __name__ == "__main__":
    asyncio.run(main())
