"""The numbered records block for the staff prompt, and the sources list.

rag.format_context is not used: its wording is written for a client-facing
reply. The body of each row is read the way rag._body reads it - COPIED here,
because _body and _truncate are private to rag.py and Help must not depend on
private names:

    qa_pair / faq      -> "Q: question / A: answer"
    table_unit         -> whole, or dropped if over RAG_MAX_TABLE_CHARS
    anything else      -> content, cut at RAG_MAX_CHUNK_CHARS
    style_example      -> never (not evidence)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.services import rag


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def body(match: dict[str, Any]) -> str:
    """Copy of rag._body (see the module docstring)."""
    chunk_type = str(match.get("chunk_type") or "").strip()
    if chunk_type in rag.EXCLUDED_CHUNK_TYPES:
        return ""
    question = (match.get("question") or "").strip()
    answer = (match.get("answer") or "").strip()
    content = (match.get("content") or "").strip()
    if chunk_type in rag.QA_CHUNK_TYPES or (question and answer):
        if question and answer:
            return f"Q: {question}\nA: {answer}"
        return answer or question
    if chunk_type == "table_unit":
        return "" if len(content) > settings.rag_max_table_chars else content
    return _truncate(content, settings.rag_max_chunk_chars)


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def label(match: dict[str, Any]) -> str:
    source = _text(match.get("source_document")) or "knowledge base"
    heading = _text(match.get("section_heading"))
    head = f"{source} — {heading}" if heading else source
    return (
        f"{head} | service: {_text(match.get('service_type')) or '-'}"
        f" | audience: {_text(match.get('contact_type')) or '-'}"
        f" | nationality: {_text(match.get('nationality')) or '-'}"
    )


@dataclass
class Records:
    text: str
    sources: list[dict[str, Any]]
    rows: list[dict[str, Any]]  # the matches that made it into the block, in order


def build(matches: list[dict[str, Any]]) -> Records:
    blocks: list[str] = []
    sources: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for match in matches:
        text = body(match)
        if not text:
            continue
        n = len(sources) + 1
        blocks.append(f"[{n}] {label(match)}\n{text}")
        try:
            similarity = round(float(match.get("similarity") or 0.0), 4)
        except (TypeError, ValueError):
            similarity = 0.0
        sources.append({
            "n": n,
            "kb_id": str(match.get("id") or ""),
            "source_document": str(match.get("source_document") or ""),
            "section_heading": _text(match.get("section_heading")),
            "service_type": _text(match.get("service_type")),
            "contact_type": _text(match.get("contact_type")),
            "nationality": _text(match.get("nationality")),
            "similarity": similarity,
        })
        rows.append(match)
    return Records("\n\n".join(blocks), sources, rows)
