"""Which helper nationality a draft may quote a fee for.

Live, 2026-10-27 (eval, conversation 4645): the client asked about "admin fee,
checkup, insurance fees", staff had just said the helpers were in the
Philippines and Myanmar, and a high-confidence Answer quoted the INDONESIAN
new-hire charges from an ID-labelled record. Every figure was in the record, so
grounding passed. A fee that belongs to one nationality is quoted only when the
conversation is about that nationality.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.lead import _NATIONALITY_NAMED, nationality_code, nationality_named_in

CODES = ("PH", "ID", "MM")
RECENT_LINES = 6


def named(text: str | None) -> set[str]:
    """Every PH/ID/MM nationality a text names."""
    return {code for pattern, code in _NATIONALITY_NAMED if pattern.search(text or "")}


def _from_state(state: dict[str, Any]) -> str | None:
    collected = state.get("collected_info") or {}
    for key in ("nationality", "helper_nationality", "preferred_nationality"):
        code = nationality_code(str(collected.get(key) or ""))
        if code in CODES:
            return code
    return None


def _from_records(state: dict[str, Any]) -> str | None:
    helpers = list(state.get("known_helpers") or [])
    if isinstance(state.get("placed_helper"), dict):
        helpers.append(state["placed_helper"])
    codes = {nationality_code(str(h.get("nationality") or "")) for h in helpers if isinstance(h, dict)}
    codes &= set(CODES)
    return codes.pop() if len(codes) == 1 else None


def in_play(state: dict[str, Any], ours: str = "") -> str | None:
    """The PH/ID/MM code the conversation is about, or None when unknown or
    ambiguous. In order: the client's message naming exactly one; the recent
    messages (any speaker) naming exactly one - several named means ambiguous,
    and nothing older overrides that; the saved state; the helper records, when
    they all agree; and last, our own earlier messages (staff or Claire)
    naming exactly one."""
    incoming = state.get("incoming_text") or ""
    code = nationality_named_in(incoming)
    if code:
        return code
    recent = (state.get("history_text") or "").splitlines()[-RECENT_LINES:]
    mentioned = named("\n".join(recent))
    if len(mentioned) == 1:
        return mentioned.pop()
    if len(mentioned) > 1:
        return None
    return _from_state(state) or _from_records(state) or _single(named(ours))


def _single(codes: set[str]) -> str | None:
    return next(iter(codes)) if len(codes) == 1 else None


# Staff write a helper as "Eaint Thawtar Khin (MDW)".
_TAGGED_NAME = re.compile(r"[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,4}\s*\((?:MDW|FDW)\)")


def _record_names(state: dict[str, Any]) -> list[str]:
    helpers = list(state.get("known_helpers") or [])
    if isinstance(state.get("placed_helper"), dict):
        helpers.append(state["placed_helper"])
    names = [str(h.get("name") or h.get("full_name") or "") for h in helpers if isinstance(h, dict)]
    names.append(str((state.get("collected_info") or {}).get("helper_name") or ""))
    return [n.strip() for n in names if len(n.strip()) >= 3]


def identified_by_us(state: dict[str, Any], ours: str) -> bool:
    """True when our own earlier messages (staff or Claire) already name the
    helper - tagged "(MDW)", or a helper we hold on record - or exactly one
    nationality. Then the fixed "which helper?" Clarify is not added: live,
    2026-10-28 (eval, 4641), staff had sent "Cost Breakdown for Eaint Thawtar
    Khin (MDW)" and the drafts still asked which helper it was for."""
    if not ours:
        return False
    if _single(named(ours)) or _TAGGED_NAME.search(ours):
        return True
    lowered = ours.lower()
    return any(name.lower() in lowered for name in _record_names(state))


def specific_codes(match: dict[str, Any]) -> set[str]:
    """The nationality a KB row is labelled for, if it is one of the three."""
    code = str((match or {}).get("nationality") or "").strip().upper()
    return {code} if code in CODES else set()
