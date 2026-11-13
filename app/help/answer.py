"""Generate the staff answer and run it through the guards.

Refused (fixed text, sources still returned) when:
  ungrounded_figure   a figure >= 100 not in the retrieved records (records ONLY:
                      a figure the staff member typed does not ground anything)
  degenerate          repetition or prompt echo (guards.is_degenerate)
  internal_reasoning  process narration (guards.leaks_internal_reasoning)
  empty               nothing left to send

Not covered (status "not_found", the model's text and the sources kept):
  not_covered         the model ended with NOT_COVERED_MARKER: the records found do
                      not answer the question (they scored above the floor, so the
                      router did not catch it)

Rewritten, not refused:
  phone_number            phone-like sequences removed (Help's own guard)
  nationality_fee_guard   guards.guard_nationality_fee cut a fee (or lead time)
                          another nationality's or one the KB does not hold; its
                          client-facing deferral is replaced with a staff line
  bad_citation            [n] pointing at no source removed
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.graph import guards
from app.help import llm as help_llm
from app.help.prompt import STAFF_PROMPT, build_user_prompt

TEMPERATURE = 0.2
MAX_TOKENS = 700

REFUSED_ANSWER = (
    "I found related records but couldn't produce an answer I can fully verify. "
    "Please read the sources below or check with a senior colleague."
)
NOT_FOUND_ANSWER = (
    "I couldn't find this in the knowledge base. Please check with a senior colleague, "
    "and consider asking an editor to add it."
)
# Extra 1: guard_nationality_fee writes a sentence for a client ("Our agent will
# confirm..."). Staff get these instead. guards.py is unchanged.
STAFF_FEE_LINE = (
    "The knowledge base has no confirmed fee for this — please check with a senior colleague."
)
STAFF_TIMELINE_LINE = (
    "The knowledge base has no confirmed timeline for this — please check with a senior colleague."
)

NOT_COVERED_MARKER = "[[NOT_COVERED]]"
_NOT_COVERED = re.compile(r"\[\[\s*NOT[_ ]COVERED\s*\]\]", re.IGNORECASE)

REFUSING_FLAGS = ("ungrounded_figure", "degenerate", "internal_reasoning", "empty")


# --- Phone numbers -----------------------------------------------------------

_PHONE_CANDIDATE = re.compile(r"(?<![\w$#/.,\-])\+?\(?\d[\d ().\-]{5,22}\d(?![\w])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_phone(candidate: str) -> bool:
    groups = re.findall(r"\d+", candidate)
    digits = sum(len(g) for g in groups)
    if not 8 <= digits <= 15:
        return False
    if _ISO_DATE.fullmatch(candidate.strip()):
        return False
    if all(len(g) == 4 and 1900 <= int(g) <= 2099 for g in groups):
        return False  # a year range, 2025-2026
    # "+65 ..." or a group of 4+ digits (0000 1111, 1800-000-1111); "350 250 60" is not.
    return candidate.lstrip().startswith("+") or any(len(g) >= 4 for g in groups)


def strip_phone_numbers(text: str) -> tuple[str, bool]:
    removed = False

    def repl(match: re.Match) -> str:
        nonlocal removed
        if _is_phone(match.group(0)):
            removed = True
            return ""
        return match.group(0)

    out = _PHONE_CANDIDATE.sub(repl, text or "")
    if removed:
        out = re.sub(r"\(\s*\)", "", out)
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out)
        out = re.sub(r"(?m)[ \t]+$", "", out)
    return out, removed


# --- Citations ---------------------------------------------------------------

_CITATION = re.compile(r"\[(\s*\d+(?:\s*,\s*\d+)*\s*)\]")


def fix_citations(text: str, valid: set[int]) -> tuple[str, bool]:
    removed = False

    def repl(match: re.Match) -> str:
        nonlocal removed
        numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
        kept = [n for n in numbers if n in valid]
        if len(kept) != len(numbers):
            removed = True
        return f"[{', '.join(str(n) for n in kept)}]" if kept else ""

    out = _CITATION.sub(repl, text or "")
    if removed:
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out)
    return out, removed


# --- Nationality fee guard (with staff wording) ------------------------------

def nationality_guard(
    text: str, service: str, nationality: str, records_text: str, rows: list
) -> tuple[str, bool]:
    guarded, cut = guards.guard_nationality_fee(
        text, service, nationality, records_text, matches=rows
    )
    if not cut:
        return text, False
    for client_line in {guards.NATIONALITY_FEE_DEFERRAL, guards.nationality_fee_deferral(service, nationality)}:
        if client_line not in text:
            guarded = guarded.replace(client_line, STAFF_FEE_LINE)
    timeline_line = guards.unheld_timeline_deferral(service, nationality)
    if timeline_line not in text:
        guarded = guarded.replace(timeline_line, STAFF_TIMELINE_LINE)
    return guarded, True


# --- The whole step ----------------------------------------------------------

@dataclass
class Answer:
    status: str  # "ok" | "refused" | "not_found"
    text: str
    flags: list[str] = field(default_factory=list)


def check(
    raw: str,
    *,
    records_text: str,
    rows: list,
    valid_sources: set[int],
    service: str | None,
    nationality: str | None,
) -> Answer:
    """Every guard on a generated answer. Pure: no I/O."""
    flags: list[str] = []
    text = (raw or "").strip()

    # The marker is ours, never the staff member's: strip it before any guard reads
    # the text, and remember it for the status.
    not_covered = bool(_NOT_COVERED.search(text))
    if not_covered:
        text = _NOT_COVERED.sub("", text).strip()

    text, phoned = strip_phone_numbers(text)
    if phoned:
        flags.append("phone_number")

    if not text:
        flags.append("empty")
    else:
        if guards.ungrounded_figures(text, records_text):
            flags.append("ungrounded_figure")
        # Without the citations: a list ending every line with "[1][6][7]" reads
        # to is_degenerate as one segment repeated (replay, 2026-11-13).
        if guards.is_degenerate(_CITATION.sub("", text)):
            flags.append("degenerate")
        if guards.leaks_internal_reasoning(text):
            flags.append("internal_reasoning")
    if any(f in REFUSING_FLAGS for f in flags):
        return Answer("refused", REFUSED_ANSWER, flags)

    if service and nationality:
        text, guarded = nationality_guard(text, service, nationality, records_text, rows)
        if guarded:
            flags.append("nationality_fee_guard")

    text, bad = fix_citations(text, valid_sources)
    if bad:
        flags.append("bad_citation")

    text = text.strip()
    if not text:
        flags.append("empty")
        return Answer("refused", REFUSED_ANSWER, flags)
    if not_covered:
        flags.append("not_covered")
        return Answer("not_found", text, flags)
    return Answer("ok", text, flags)


async def generate(messages, records_text: str, *, model: str, timeout: float) -> str:
    return await help_llm.complete(
        STAFF_PROMPT,
        build_user_prompt(messages, records_text),
        model=model,
        timeout=timeout,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
    )
