"""A vague request for something only a person can do is clarified before it is ticketed.

Added 2026-10-28. Live, conversation 4443, 12:51 PM SGT: a helper wrote
"please send the video here" and was told "A live agent is handling your
placement and will connect with you shortly. Your reference number is
CB-2026-0125." Nobody knew which video she meant, the agent opening the
ticket had nothing to act on, and "placement" was a word nobody had used:
her open ticket was her REGISTRATION, filed under new_hiring, and the
holding-line prompt was told "hiring a new helper" and paraphrased it.

So a vague action request - send a video, a photo, a profile or a document,
call me, update me, check the status - gets ONE short message with the one or
two questions an agent would ask first. The next message is taken as the
answer, and the ticket is raised with both on it (the request, and what they
said). If the answer names an open ticket's reference, or a status question
has exactly one open ticket to be about, the existing ticket is used instead
of a new one.

Still immediate, never clarified: a harm report, a missing helper, a
complaint, and an explicit "let me talk to a person" - the classifier checks
those before this. The questions are fixed text: they are sent verbatim, so
they are vetted at import like the canned strings in blocked_topic_responder.

VAGUE_ACTION_CLARIFY = False restores the old behaviour exactly.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.graph.guards import is_degenerate, mentions_handover

VAGUE_ACTION_CLARIFY = True

# A request this long has usually said what it is about already.
_MAX_WORDS = 12

_SEND = r"(?:send|share|forward|give|show|pass|email|whatsapp|need|want|get)"
_ACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("video", re.compile(rf"\b{_SEND}\b[^.?!]{{0,30}}\bvideos?\b|\bvideos?\b[^.?!]{{0,15}}"
                         r"\b(?:here|please|pls|plz|now)\b|^\s*(?:the\s+)?videos?\s*(?:pls|please)?\s*[.!?]*\s*$",
                         re.IGNORECASE)),
    ("photo", re.compile(rf"\b{_SEND}\b[^.?!]{{0,30}}\b(?:photos?|pictures?|pics?|images?)\b",
                         re.IGNORECASE)),
    ("profile", re.compile(rf"\b{_SEND}\b[^.?!]{{0,30}}\b(?:profiles?|biodata|bio\s*data|resumes?|cvs?)\b",
                           re.IGNORECASE)),
    ("document", re.compile(rf"\b{_SEND}\b[^.?!]{{0,30}}\b(?:documents?|docs?|forms?|contracts?|"
                            r"receipts?|invoices?|papers?|copy|copies)\b", re.IGNORECASE)),
    ("call", re.compile(r"\b(?:call|ring|phone)\s+me\b|\bgive\s+me\s+a\s+(?:call|ring)\b"
                        r"|\bcan\s+(?:someone|anyone|you|your\s+agent)\s+call\b", re.IGNORECASE)),
    ("update", re.compile(r"\bupdate\s+me\b|\bkeep\s+me\s+(?:updated|posted)\b"
                          r"|\bany\s+(?:updates?|news|progress)\b|\bcheck\s+(?:the\s+|my\s+|on\s+)?status\b"
                          r"|\bwhat(?:'s|\s+is)\s+the\s+status\b|\bstatus\s+(?:of|on|update)\b",
                          re.IGNORECASE)),
)

# Asking what a thing IS, or which documents are needed, is a question we
# answer from our records - not an action for a person.
_A_QUESTION_ABOUT_IT = re.compile(
    r"^\s*(?:what|which|how|why|do\s+i|does|is\s+there|are\s+there)\b", re.IGNORECASE
)
# A request that already names what it is about needs no question first.
_SPECIFIC = re.compile(
    r"\b(?:of|for|about|from)\s+(?!me\b|us\b|it\b|this\b|that\b|here\b|the\b|my\b|our\b)[A-Za-z]{3,}"
    r"|\binterview\s+video\b", re.IGNORECASE,
)
# A reference as a client types it - any case, spaces allowed.
_REFERENCE = re.compile(r"\bcb\s*-?\s*(\d{4})\s*-?\s*(\d{4})\b", re.IGNORECASE)
_DROPS = re.compile(r"\b(?:never\s*mind|forget\s+it|no\s+need|cancel|not\s+needed)\b", re.IGNORECASE)

# One short message each. Employer phrasing first, a helper's (candidate) second
# where "which helper" would be asked of the helper herself.
_QUESTIONS: dict[str, tuple[str, str]] = {
    "video": (
        "Which video do you need: a helper's profile or interview video, or something else? "
        "And which helper is it for (her name or reference number)?",
        "Which video do you mean: your interview video, or something else? "
        "And what do you need it for?",
    ),
    "photo": (
        "Which photo do you need: a helper's profile photo, or something else? "
        "And which helper is it for (her name or reference number)?",
        "Which photo do you mean, and what do you need it for?",
    ),
    "profile": (
        "Which helper's profile would you like (her name or reference number)? "
        "And do you need her biodata, her video, or both?",
        "Which profile do you mean: your own biodata, or an employer's details? "
        "And what do you need it for?",
    ),
    "document": (
        "Which document do you need, and what is it for (for example a contract, "
        "a receipt or a form for MOM)?",
        "Which document do you need, and what is it for (for example your contract "
        "or a copy of your Work Permit)?",
    ),
    "call": (
        "Sure. What would you like our agent to call you about, and when is a good time to call?",
        "Sure. What would you like our agent to call you about, and when is a good time to call?",
    ),
    "update": (
        "Which request would you like an update on? If you have a reference number "
        "(it starts with CB-), please send it.",
        "Which request would you like an update on? If you have a reference number "
        "(it starts with CB-), please send it.",
    ),
}

PASSED_ON_REPLY = (
    "Thank you. I've passed this to our team, and a live agent will connect with you shortly. "
    "In the meantime, is there anything else I can help you with?"
)
ADDED_TO_TICKET_REPLY = (
    "Thank you. I've added this to your request for our team, and a live agent will connect "
    "with you shortly."
)

# Sent verbatim, so vetted here: a question each, no figure, no promised time,
# no handover wording on the questions themselves.
for _kind, _pair in _QUESTIONS.items():
    for _text in _pair:
        assert _text.count("?") >= 1 and not re.search(r"\d", _text), _kind
        assert not mentions_handover(_text) and not is_degenerate(_text), _kind
assert not mentions_handover(PASSED_ON_REPLY) and not mentions_handover(ADDED_TO_TICKET_REPLY)


def vague_action(message: str) -> str | None:
    """The kind of action asked for when the request does not say what it is about."""
    text = (message or "").strip()
    if not text or len(text.split()) > _MAX_WORDS:
        return None
    if _REFERENCE.search(text) or _SPECIFIC.search(text):
        return None
    for kind, pattern in _ACTIONS:
        # "what is the status" is a status request; "what video is that?" is a question.
        if pattern.search(text) and (kind == "update" or not _A_QUESTION_ABOUT_IT.search(text)):
            return kind
    return None


def question(kind: str, contact_type: str | None) -> str:
    employer, helper = _QUESTIONS.get(kind, _QUESTIONS["document"])
    return helper if (contact_type or "") == "candidate" else employer


def pending(kind: str, message: str, text: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "request": (message or "").strip()[:300],
        "question": text,
        "asked_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def awaiting_answer(pending_action: dict[str, Any] | None, last_bot_line: str,
                    max_hours: float) -> bool:
    """Whether this turn is the answer to our clarifying question."""
    if not VAGUE_ACTION_CLARIFY or not pending_action:
        return False
    try:
        asked = datetime.fromisoformat(str(pending_action.get("asked_at")))
    except (TypeError, ValueError):
        return False
    if datetime.now(tz=timezone.utc) - asked > timedelta(hours=max_hours):
        return False
    # Only while our clarifying question is still the last thing we said.
    asked_text = str(pending_action.get("question") or "")
    return bool(asked_text) and asked_text[:60] in (last_bot_line or "")


def dropped(message: str) -> bool:
    return bool(_DROPS.search(message or ""))


def summary(pending_action: dict[str, Any], answer: str) -> str:
    """What the agent reads first on the ticket."""
    return (
        f"Client asked: {pending_action.get('request') or ''}\n"
        f"We asked: {pending_action.get('question') or ''}\n"
        f"Their answer: {(answer or '').strip()[:500]}"
    )


def ticket_named(answer: str, open_tickets: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    """The open ticket whose reference the client quoted, if any."""
    found = _REFERENCE.search(answer or "")
    if not found:
        return None
    wanted = f"CB-{found.group(1)}-{found.group(2)}"
    for ticket in (open_tickets or {}).values():
        if str(ticket.get("ticket_number") or "").upper() == wanted:
            return ticket
    return None


def single_open_ticket(open_tickets: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    """The one open ticket, when there is exactly one (a status question is about it)."""
    tickets = {t.get("id"): t for t in (open_tickets or {}).values() if t.get("id")}
    return next(iter(tickets.values())) if len(tickets) == 1 else None
