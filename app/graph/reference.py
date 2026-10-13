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

Step 2e (2026-10-13), the agency's wording: a plain sentence after the
handover sentence instead of a bracket inside it, in the reply's language.
The first time the client is given the number:
  "Your reference number is CB-2026-0142. Please quote it whenever you ask
   about this request."
and once our earlier messages already carry it, only:
  "Your reference number is CB-2026-0142."
It is added after the reply's own sentence clamp, which nothing re-runs.
REFERENCE_AS_SENTENCE = False keeps the bracket.
"""

from __future__ import annotations

import logging
import re

from app.config import settings
from app.graph.guards import TICKET_REFERENCE, mentions_handover
from app.graph.language import reply_language

logger = logging.getLogger(__name__)

REFERENCE_AS_SENTENCE = True

# (first time, later) per reply language. The translations are ours and have
# not been checked by a native speaker; English is the fallback.
_SENTENCES = {
    "en": ("Your reference number is {ref}. Please quote it whenever you ask about this request.",
           "Your reference number is {ref}."),
    "id": ("Nomor referensi Anda adalah {ref}. Mohon sebutkan nomor ini setiap kali Anda "
           "menanyakan permintaan ini.",
           "Nomor referensi Anda adalah {ref}."),
    "ms": ("Nombor rujukan anda ialah {ref}. Sila nyatakan nombor ini setiap kali anda "
           "bertanya tentang permintaan ini.",
           "Nombor rujukan anda ialah {ref}."),
    "tl": ("Ang iyong reference number ay {ref}. Pakibanggit ito tuwing magtatanong ka "
           "tungkol sa kahilingang ito.",
           "Ang iyong reference number ay {ref}."),
    "zh": ("您的参考编号是 {ref}。查询此请求时，请提供此编号。",
           "您的参考编号是 {ref}。"),
    "my": ("သင့်ရည်ညွှန်းနံပါတ်မှာ {ref} ဖြစ်ပါသည်။ ဤတောင်းဆိုမှုအကြောင်း မေးမြန်းတိုင်း "
           "ဤနံပါတ်ကို ဖော်ပြပေးပါ။",
           "သင့်ရည်ညွှန်းနံပါတ်မှာ {ref} ဖြစ်ပါသည်။"),
    "ta": ("உங்கள் குறிப்பு எண் {ref}. இந்தக் கோரிக்கை பற்றி கேட்கும்போதெல்லாம் இதைக் "
           "குறிப்பிடவும்.",
           "உங்கள் குறிப்பு எண் {ref}."),
    "hi": ("आपका संदर्भ नंबर {ref} है। इस अनुरोध के बारे में पूछते समय कृपया इसे बताएं।",
           "आपका संदर्भ नंबर {ref} है।"),
}

# Sent as written, so vetted at import like the other canned strings.
for _code, _pair in _SENTENCES.items():
    for _s in _pair:
        assert "{ref}" in _s and not mentions_handover(_s.format(ref="CB-2026-0001")), (
            f"reference sentence {_code!r} lost its number or promises a colleague or a time"
        )

_SENTENCE_END = re.compile(r"[.?!。။！？]+")

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
# Burmese and Chinese end a sentence with their own marks (Step 2e).
_LIVE_AGENT_SENTENCE = re.compile(
    r"[^.?!\n။。！？]*\blive\s+agent\b[^.?!\n။。！？]*", re.IGNORECASE
)

# Step 2e: our team or our agent in the other reply languages, so a reply in
# the client's language carries the reference too (until now only an English
# phrase could anchor it). Indonesian/Malay, Chinese, Burmese, Tamil, Hindi;
# Tagalog replies use "team" / "agent" and are caught by the English patterns.
_AGENT_SENTENCE_OTHER = re.compile(
    r"[^.?!\n။。！？]*(?:\btim\s+kami\b|\bpasukan\s+kami\b|\bagen\b|\bejen\b|团队|专员|顾问|客服|"
    r"အဖွဲ့|ஏஜென்ட்|குழு|टीम|एजेंट)[^.?!\n။。！？]*",
    re.IGNORECASE,
)

# Any other sentence that mentions our agent: "Noted, I've added that for our
# agent", "one of our agents will take you through it". Step 2b (2026-10-13):
# on a parked topic every reply that mentions the agent carries the reference,
# and these are the parked replies the two patterns above miss. Last, so a
# handover sentence always wins.
_AGENT_SENTENCE = re.compile(r"[^.?!\n။。！？]*\bagents?\b[^.?!\n။。！？]*", re.IGNORECASE)


def enabled() -> bool:
    return bool(settings.handover_reference)


def mention(ticket_number: str) -> str:
    return f"(reference {ticket_number})"


def mentions_agent(reply: str) -> bool:
    """Whether the reply hands over or mentions our agent - the replies that
    must carry the parked topic's reference (Step 2b)."""
    return any(p.search(reply or "") for p in (_PASSED_TO_TEAM, _LIVE_AGENT_SENTENCE, _AGENT_SENTENCE,
                                         _AGENT_SENTENCE_OTHER))


def reference_sentence(reply: str, ticket_number: str, history_text: str = "") -> str:
    """The reference as a sentence in the reply's language - the long form the
    first time, the short one once our earlier messages carry the number."""
    first, later = _SENTENCES.get(reply_language(reply), _SENTENCES["en"])
    told = ticket_number in (history_text or "")
    return (later if told else first).format(ref=ticket_number)


def _after_sentence(reply: str, start: int, sentence: str) -> str:
    """`sentence` inserted after the sentence that contains position `start`."""
    end = _SENTENCE_END.search(reply, start)
    newline = reply.find("\n", start)
    if end and (newline < 0 or end.end() <= newline):
        cut = end.end()
        gap = "" if reply[cut - 1] in "。！？" else " "  # Chinese takes no space
        return f"{reply[:cut]}{gap}{sentence}{reply[cut:]}"
    cut = newline if newline >= 0 else len(reply)
    body = reply[:cut].rstrip(" ,;:")
    return f"{body}. {sentence}{reply[cut:]}"


def add_reference(reply: str, ticket_number: str | None, history_text: str = "") -> str:
    """The reply with the reference added at its handover sentence, or unchanged."""
    if not enabled() or not reply or not ticket_number:
        return reply
    if ticket_number in reply:
        return reply  # a complaint's closing already carries it
    if REFERENCE_AS_SENTENCE:
        match = (_PASSED_TO_TEAM.search(reply) or _LIVE_AGENT_SENTENCE.search(reply)
                 or _AGENT_SENTENCE.search(reply) or _AGENT_SENTENCE_OTHER.search(reply))
        if match:
            return _after_sentence(
                reply, match.start(), reference_sentence(reply, ticket_number, history_text)
            )
        logger.info("No handover sentence to put %s after - reply left as written", ticket_number)
        return reply
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
