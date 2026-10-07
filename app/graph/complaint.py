"""What a client lodging a complaint is told, and when (2026-10-07).

Live, conversation 26: "Hi / I want to lodge a complaint" was answered "I'm
sorry to hear that, Thomas. Please share your complaint in writing, and our live
agent will pick it up shortly; in the meantime, is there anything else I can help
you with?" - no ticket was raised, nobody was told, and the client's description
of the complaint on the next turn was answered with the next direct-hire
question. The agency's verdict and its own example reply:

    "Bot again doesn't introduce herself, knowing client is upset but moves to
    other topics like it isn't important. Says will revert shortly but a
    complaint needs to have a clear expectation like within next 24 hours ...
    it should ask for details and provide complaint / ticketing number."

    "I'm sorry to hear that [name], and I want to make sure this is handled
    properly. Please tell us here: your name, your case or helper details and
    what happened. Your complaint will be logged and a senior team member will
    contact you within [one working day]. For urgent matters, you can reach us
    at 65342277."

The opening and closing messages are written HERE rather than by the model,
for two reasons. They carry a promised time and a phone number, which
strip_handover_talk and ungrounded_figures exist to remove from model output -
correctly everywhere else, because a time nobody has agreed to is worse than
none. This one the agency has agreed to, in writing, so it is said exactly as
they said it. And the closing message carries the ticket number, which only
exists after ticket_creator has run.

Neither message ends with "is there anything else I can help you with?". Rule 2
asks for that offer after an ordinary handover; to somebody who has just
complained it reads as moving on as though it did not matter, which is the
agency's objection word for word. Rule 2b makes the same exception for a harm
report.
"""

from __future__ import annotations

import re

from app.services.ticket import COMPLAINT_RESPONSE_TIME, COMPLAINT_URGENT_PHONE

INTRODUCTION = "I'm Claire, Ming Hwee's AI assistant."

# What each outstanding field is called when the opening asks for it, in the
# agency's own words.
_ASK_FOR = {
    "full_name": "your name",
    "complaint_subject": "your case or helper details",
    "complaint_detail": "what happened",
}

_EXPECTATION = (
    "Your complaint will be logged and a senior team member will contact you "
    f"{COMPLAINT_RESPONSE_TIME}. For urgent matters, you can call us on "
    f"{COMPLAINT_URGENT_PHONE}."
)

# Instruction for the turns in between, when one detail is still missing and
# the model asks for it.
COMPLAINT_COLLECT_NOTE = (
    "\n\nTHIS CLIENT IS MAKING A COMPLAINT ABOUT US. Stay with it. Acknowledge "
    "what they have just told you in a short, sincere clause - you are sorry, "
    "not cheerful - and then ask only for the one detail below. Do NOT offer "
    "help with anything else, do NOT ask whether there is anything else, do "
    "NOT defend us or explain what went wrong, and do NOT promise anything "
    "about when or how it will be resolved: that has already been said."
)

# "I want to lodge a complaint" filed as the complaint itself would close the
# collection on the opening turn with nothing in it - the request restated, the
# shape _PREFERENCE_FILLER guards for a replacement. Subtractive, so anything a
# client actually describes survives.
_FILLER = re.compile(
    r"\b(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|i|i'?m|i\s+am|we|"
    r"want|wants|would|like|need|to|a|an|the|make|makes|lodge|lodges|file|raise|"
    r"submit|register|formal|official|complaint|complaints|complain|complaining|"
    r"grievance|about|regarding|please|wanting|wished|wish|client|customer|"
    r"has|have|is|on|for|lodging|making|filing|my|this|that|one|there)\b",
    re.IGNORECASE,
)


def states_a_complaint(text: str) -> bool:
    """Whether the value says what the complaint IS, not just that there is one."""
    remainder = _FILLER.sub(" ", text or "")
    return len(re.sub(r"[^a-z0-9]+", "", remainder.lower())) >= 3


def _first_name(name: str) -> str:
    """The first word of the name they gave, spelled as they wrote it (§9.24)."""
    parts = (name or "").split()
    return parts[0] if parts else ""


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def complaint_opening(name: str, missing_keys: list[str], introduce: bool) -> str:
    """The first reply to a complaint: sorry, what we need, what happens next."""
    first = _first_name(name)
    asks = [_ASK_FOR[key] for key in missing_keys if key in _ASK_FOR]
    if introduce:
        opener = (
            f"Hi{' ' + first if first else ''}, {INTRODUCTION} I'm sorry to hear "
            "that, and I want to make sure this is handled properly."
        )
    else:
        opener = (
            f"I'm sorry to hear that{', ' + first if first else ''}, and I want "
            "to make sure this is handled properly."
        )
    if len(asks) > 1:
        ask = f" Please tell me here: {_join(asks)}."
    elif asks:
        ask = f" Please tell me here {asks[0]}."
    else:
        ask = ""
    return f"{opener}{ask} {_EXPECTATION}"


def complaint_closing(name: str, ticket_number: str | None) -> str:
    """The reply once it is logged: the reference, the wait, the number to call."""
    first = _first_name(name)
    reference = f" under reference {ticket_number}" if ticket_number else ""
    return (
        f"Thank you{', ' + first if first else ''}. Your complaint has been "
        f"logged{reference}, and a senior team member will contact you "
        f"{COMPLAINT_RESPONSE_TIME}. For urgent matters, you can call us on "
        f"{COMPLAINT_URGENT_PHONE}."
    )


# Vetted at import, like blocked_topic_responder's canned strings: these go out
# verbatim, past every guard.
for _sample in (
    complaint_opening("Thomas", list(_ASK_FOR), introduce=True),
    complaint_opening("", ["complaint_detail"], introduce=False),
    complaint_closing("Thomas", "CB-2026-0001"),
    complaint_closing("", None),
):
    assert "anything else" not in _sample.lower(), _sample
    assert COMPLAINT_RESPONSE_TIME in _sample and COMPLAINT_URGENT_PHONE in _sample, _sample
