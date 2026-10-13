"""The ticket reference, put into the handover sentence a reply already has.

Added 2026-10-12 (Phase 3, Step 2). A complaint has carried its CB-YYYY-NNNN
since 2026-10-07 (app/graph/complaint.py); every other handover told the client
"a live agent will connect with you shortly" and gave them nothing to quote
when they chased it, or when the agent rang.

The reply is written BEFORE the ticket exists - the collector or
response_generator writes it, then ticket_creator inserts the row - so the
number cannot be in the prompt. handover_executor runs straight after
ticket_creator and calls add_reference() on the finished reply.

Inside the existing sentence, never a new one, so the sentence-count rules
(clamp_reply, prompt rule 6) still hold:

  "I've passed this to our team (reference CB-2026-0142), and a live agent
   will connect with you shortly."
  "... a live agent will connect with you shortly (reference CB-2026-0142)."

A reply with no handover sentence is left alone rather than given one.
HANDOVER_REFERENCE=false switches all of it off.
"""

from __future__ import annotations

import logging
import re

from app.config import settings
from app.graph.guards import TICKET_REFERENCE

logger = logging.getLogger(__name__)

# The reference itself, defined once in guards: ungrounded_figures strips it
# before reading figures, so "2026" and "0142" are never taken for a price.
REFERENCE = TICKET_REFERENCE

# "I've passed this to our team", "handed that to our team", "passed everything
# on to the team". The reference goes straight after "team".
_PASSED_TO_TEAM = re.compile(
    r"\b(?:passed|handed|sent|forwarded|given)\b[^.?!\n]{0,40}?\bto\s+(?:our|the)\s+team\b",
    re.IGNORECASE,
)

# A sentence that announces a live agent: the reference goes before its end.
_LIVE_AGENT_SENTENCE = re.compile(
    r"[^.?!\n]*\blive\s+agent\b[^.?!\n]*", re.IGNORECASE
)

# Any other sentence that mentions our agent: "Noted, I've added that for our
# agent", "one of our agents will take you through it". Step 2b (2026-10-13):
# on a parked topic every reply that mentions the agent carries the reference,
# and these are the parked replies the two patterns above miss. Last, so a
# handover sentence always wins.
_AGENT_SENTENCE = re.compile(r"[^.?!\n]*\bagents?\b[^.?!\n]*", re.IGNORECASE)


def enabled() -> bool:
    return bool(settings.handover_reference)


def mention(ticket_number: str) -> str:
    return f"(reference {ticket_number})"


def mentions_agent(reply: str) -> bool:
    """Whether the reply hands over or mentions our agent - the replies that
    must carry the parked topic's reference (Step 2b)."""
    return any(p.search(reply or "") for p in (_PASSED_TO_TEAM, _LIVE_AGENT_SENTENCE, _AGENT_SENTENCE))


def add_reference(reply: str, ticket_number: str | None) -> str:
    """The reply with the reference in its handover sentence, or unchanged."""
    if not enabled() or not reply or not ticket_number:
        return reply
    if ticket_number in reply:
        return reply  # a complaint's closing already carries it
    tag = mention(ticket_number)
    match = _PASSED_TO_TEAM.search(reply)
    if match:
        return f"{reply[:match.end()]} {tag}{reply[match.end():]}"
    match = _LIVE_AGENT_SENTENCE.search(reply) or _AGENT_SENTENCE.search(reply)
    if match:
        end = match.end()
        # Before trailing spaces and the sentence's own punctuation.
        while end > match.start() and reply[end - 1] in " ,;:":
            end -= 1
        return f"{reply[:end]} {tag}{reply[end:]}"
    logger.info("No handover sentence to put %s into - reply left as written", ticket_number)
    return reply
