"""Retrieval for a draft: the bot's own rag_retriever node, run on the built state.

Calling the node rather than rag.search directly means the query, the service,
audience and nationality filters, the widening retry and the briefing/overview
extras are exactly the bot's (rag_retriever.py L781 onward). The node only
reads: the knowledge-base search (an RPC on readonly.READ_RPCS) and, on a case
question, contact.get_case_summary.

The records are then rendered with each row's id, so the model can cite them
and validate.py can check the citations.
"""

from __future__ import annotations

from typing import Any

from app.services import rag


def ref_of(match: dict[str, Any]) -> str:
    return str(match.get("id") or "")


def records_with_refs(matches: list[dict[str, Any]]) -> str:
    """Our records, one block per row, each headed by the ref the model cites."""
    blocks = []
    for match in matches:
        ref = ref_of(match)
        body = rag.format_context([match]).strip()
        if ref and body:
            blocks.append(f"[ref {ref}]\n{body}")
    return "\n\n".join(blocks) if blocks else rag.format_context([])


def text_of(match: dict[str, Any]) -> str:
    """Everything a row says, for the grounding check."""
    parts = [match.get(key) for key in ("question", "answer", "content", "section_heading")]
    return "\n".join(str(part) for part in parts if part)


async def retrieve(state: dict[str, Any]) -> dict[str, Any]:
    """{matches, records (with refs), rag_context (the bot's own rendering), best}."""
    from app.graph.nodes.rag_retriever import rag_retriever

    out = await rag_retriever(state)
    matches = [m for m in (out.get("rag_matches") or []) if isinstance(m, dict)]
    return {
        "matches": matches,
        "records": records_with_refs(matches),
        "rag_context": out.get("rag_context") or "",
        "best": float(out.get("rag_best_score") or 0.0),
        "case_summary": out.get("case_summary"),
    }
