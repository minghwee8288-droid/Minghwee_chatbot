"""Deterministic checks on generated text, applied before anything is sent.

The system prompt states these rules too, but prompts are not guarantees — a
DeepSeek run quoted an insurance figure that was not in the retrieved records,
and another announced a handover the client was never supposed to hear about.
These guards make the two rules that carry real business risk unbreakable.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# --- Grounded figures ------------------------------------------------------

_FIGURE = re.compile(r"\d[\d,\.]*")
# Small numbers are ordinary prose ("2 types", "3-4 weeks") and are ignored.
_MIN_GROUNDED_FIGURE = 100


def _digit_forms(text: str) -> set[str]:
    """Every figure in the text, normalised so $60,000 == S$60000 == 60,000."""
    forms: set[str] = set()
    for match in _FIGURE.findall(text or ""):
        normalised = match.replace(",", "").rstrip(".")
        if not normalised:
            continue
        forms.add(normalised)
        if "." in normalised:  # 60000.00 -> 60000
            forms.add(normalised.split(".", 1)[0])
    return forms


def ungrounded_figures(reply: str, *contexts: str) -> list[str]:
    """Figures in the reply that appear in none of the given contexts.

    Money amounts, levies, insurance limits and permit durations are what a
    client acts on, and a wrong one is worse than no answer at all. Used two
    ways: against the retrieved records when answering a question, and against
    what the client themselves said when collecting requirements — where the bot
    was caught inventing "salaries range from $600 to $800", a straight breach
    of the rule that it never quotes a price.
    """
    grounded = _digit_forms(" ".join(c for c in contexts if c))
    suspect: list[str] = []
    for figure in _digit_forms(reply):
        head = figure.split(".", 1)[0]
        if not head.isdigit() or int(head) < _MIN_GROUNDED_FIGURE:
            continue
        if figure not in grounded:
            suspect.append(figure)
    return suspect


# --- Silent handovers ------------------------------------------------------

# Phrases that tell the client somebody else is taking over. Handovers must be
# invisible: the client believes they are talking to one person throughout.
_HANDOVER_TALK = re.compile(
    r"\b("
    r"our|my|the)\s+"
    r"(consultant|colleague|team|manager|supervisor|specialist|agent|staff|"
    r"department|representative)\b"
    r"|\b(transfer|connect|refer|forward|pass)\s+(you|this|your\s+\w+)\b"
    r"|\bsomeone\s+(else\s+)?(will|from)\b"
    r"|\banother\s+(agent|consultant|colleague)\b"
    r"|\bwill\s+be\s+in\s+touch\s+with\s+you\s+shortly\s+regarding\b",
    re.IGNORECASE,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# "Let me check with the team and get back to you" is the sanctioned way to say
# 'I don't know' — the consultant is going to check, not handing the client on.
_ALLOWED = re.compile(
    r"\b(check|confirm|verify|ask|clarify|double[-\s]?check)\s+"
    r"(it\s+|this\s+|that\s+)?(with\s+)?(the|my|our)\s+team\b",
    re.IGNORECASE,
)

NEUTRAL_FOLLOW_UP = "Let me get that sorted and come back to you shortly."


def mentions_handover(text: str) -> bool:
    # Blank out sanctioned phrasing first so it cannot trip the pattern below.
    return bool(_HANDOVER_TALK.search(_ALLOWED.sub(" ", text or "")))


# --- Degenerate output -----------------------------------------------------

# The model sometimes stops answering and starts continuing its own prompt:
# "Noted, $500 budget. (No handover) (No lists) (No "kindly") (No "please")..."
# or repeats one phrase until the token budget runs out. Both have been observed
# from DeepSeek on OpenRouter, and both would go straight to a client.

# Prompt scaffolding the model echoes back.
_PROMPT_ECHO = re.compile(
    r"(your reply:|client'?s? latest message|<<<|conversation so far:|"
    r"\(transfers? happen|\(do not |\(no handover|\(one sentence|\(keep it simple|"
    r"\(just asking|\(conversation continues|this is a handover|"
    # The model narrating its own output: "Here's a concise closing message:"
    r"here'?s? (a|the) (concise |short |brief )?(closing |reply|response|message)|"
    # Self-review notes it emits in asterisks: *Maintains natural flow*
    r"\*(keeps?|maintains?|avoids?|shows?|sets? up|stays?|uses?|doesn'?t) )",
    re.IGNORECASE,
)

# Trailing self-instructions in brackets or asterisks:
# "(No lists)  (Short & natural)"  /  "*Avoids jargon* *Shows active listening*"
_TRAILING_NOTES = re.compile(
    r"(\s*[(*](?:no|not|just|only|keep|short|one|avoid|maintain|show|use|set|stay)\b[^)*]*[)*]\s*)+$",
    re.IGNORECASE,
)


# The model explaining its own reply to the client, in brackets:
#   "(Note: This is a helper/candidate registration request which requires human
#    handling... The response follows protocol by not attempting to explain...)"
# That went to a real client. It is not degenerate output — the message before
# it was fine — so the bracketed part is cut and the rest kept.
_META_MARKERS = re.compile(
    r"\b(note|reasoning|explanation|internal|system|assistant|context)\s*:"
    r"|\b(this|the)\s+(response|reply|message|request)\b"
    r"|\bfollows?\s+(the\s+)?protocol\b"
    r"|\brequires?\s+human\b"
    r"|\bper\s+(the\s+)?(rules?|instructions?|protocol)\b"
    r"|\bhandover\b|\bknowledge base\b|\bsystem prompt\b"
    r"|\bwithout\s+confirmed\s+details\b",
    re.IGNORECASE,
)
_BRACKETED = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
# The same thing left unclosed because the token budget ran out.
_UNCLOSED_TAIL = re.compile(r"[(\[][^()\[\]]*$")


def strip_meta_commentary(reply: str) -> str:
    """Remove bracketed asides in which the model explains itself.

    Only brackets that read as commentary are cut — "(MDW)" and a genuine
    aside the client would understand are left alone.
    """
    text = (reply or "").strip()
    if not text:
        return text

    def _cut(match: re.Match[str]) -> str:
        inner = match.group(1) or match.group(2) or ""
        return "" if _META_MARKERS.search(inner) else match.group(0)

    cleaned = _BRACKETED.sub(_cut, text)

    tail = _UNCLOSED_TAIL.search(cleaned)
    if tail and _META_MARKERS.search(tail.group(0)):
        cleaned = cleaned[: tail.start()]

    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if cleaned != text:
        logger.warning("Stripped meta commentary from reply: %r -> %r", text[:200], cleaned[:200])
    return cleaned


def same_opening(reply: str, previous: str, words: int = 3) -> bool:
    """Whether two messages start with the same few words.

    "May I know ..." seven messages running is what the client sees, and the
    filler-opener guard below never fires on it because "may" is not filler.
    """
    def head(text: str) -> str:
        tokens = normalize_text_local(text).split()
        return " ".join(tokens[:words])

    a, b = head(reply), head(previous)
    return bool(a) and a == b


def is_degenerate(text: str) -> bool:
    """Whether a generated reply has collapsed into repetition or prompt echo."""
    body = (text or "").strip()
    if not body:
        return False
    if _PROMPT_ECHO.search(body):
        return True

    words = body.split()
    if len(words) > 25 and len(set(w.lower() for w in words)) / len(words) < 0.4:
        return True

    # The same clause repeated three or more times.
    segments = [s.strip().lower() for s in re.split(r"[.!?\n]+", body) if len(s.strip()) > 8]
    if segments and len(segments) - len(set(segments)) >= 2:
        return True
    return False


# --- Formatting -------------------------------------------------------------

# WhatsApp replies must not look like a document. The model periodically answers
# with markdown headings, bold runs and numbered lists — a plumber directory
# with "### Where to Find..." went out during testing.
_MARKDOWN = re.compile(r"^#{1,6}\s|\*\*|^\s*[-*•]\s+|^\s*\d+\.\s+", re.MULTILINE)


def looks_like_document(text: str) -> bool:
    return len(_MARKDOWN.findall(text or "")) >= 2


# --- Repetitive openers ----------------------------------------------------

# Filler acknowledgements the model reaches for every single turn. Eight
# consecutive messages beginning "Noted ..." is what gives the bot away.
_FILLER_OPENERS = {
    "noted", "got", "understood", "sure", "okay", "ok", "alright", "great",
    "thanks", "acknowledged", "certainly", "absolutely", "perfect", "right",
}
_OPENER = re.compile(r"^\s*([A-Za-z']+)[,.!:]?\s+")

# The whole acknowledgement, not just its first word. Removing one word turned
# "Got it, thanks for the details." into "It, thanks for the details." and sent
# that to a client — the filler is a phrase, so it has to be cut as a phrase.
# Anything not listed here is left alone: "Got the passport number" opens with a
# filler word but is a real sentence.
_FILLER_PHRASE = re.compile(
    r"^\s*(?:noted|got\s+it|got\s+that|understood|sure\s+thing|sure|okay|ok|alright|"
    r"all\s+right|great|thanks|thank\s+you|acknowledged|certainly|absolutely|perfect|"
    r"right)\s*[,.!:;—-]+\s*",
    re.IGNORECASE,
)


# Thanking the client twice running is the most obvious tell that nothing is
# listening, and it takes several shapes the opener guards above all miss:
#
#   "Hi! Thanks for contacting Ming Hwee."  -> opener word is "hi", not "thanks"
#   "Thanks for reaching out!"              -> a clause, not the bare "Thanks,"
#                                              that _FILLER_PHRASE matches
#   "Got it, thanks! Let me pull ..."       -> the thanks is not at the front
#
# All three went to real clients on the same test number, in pairs.
_THANKS = re.compile(r"\bthank(?:s|\s*you)\b", re.IGNORECASE)

# A whole opening sentence that is nothing but an acknowledgement. Deliberately
# strict, and a whitelist rather than a length limit: "Thanks for the permit
# photo which shows a March expiry" is nine words and cutting it would throw
# away the only thing the message said.
_PURE_THANKS_SENTENCE = re.compile(
    r"^\W*"
    r"(?:(?:hi+|hey+|hello+|good\s+(?:morning|afternoon|evening|day))(?:\s+there)?\b[\s,!.\-]*)?"
    r"(?:(?:noted|got\s+it|got\s+that|understood|sure|okay|ok|alright|great|perfect)"
    r"\b[\s,!.\-]*)?"
    r"(?:many\s+)?thank(?:s|\s*you)\b(?:\s+(?:so|very)\s+much)?(?:\s+again)?"
    r"(?:\s+for\s+(?:"
    r"contacting|reaching\s+out|getting\s+in\s+touch|messaging|writing|sharing|waiting|"
    r"letting\s+me\s+know|that|this|"
    r"your\s+(?:message|patience|time|reply|details|note|info(?:rmation)?)|"
    r"the\s+(?:details|update|info(?:rmation)?)"
    r")(?:\s+(?:to\s+)?(?:us|me|ming\s+hwee(?:\s+agency)?))?)?"
    r"[\s,!.\-]*$",
    re.IGNORECASE,
)

# "Hi!" punctuated as its own sentence, which puts the thanks in the second one.
_BARE_GREETING_SENTENCE = re.compile(
    r"^\W*(?:hi+|hey+|hello+|good\s+(?:morning|afternoon|evening|day))(?:\s+there)?[\s,!.\-]*$",
    re.IGNORECASE,
)

# Secondary bound, in case the pattern above ever matches more than intended.
_MAX_THANKS_SENTENCE_WORDS = 10


def thanks_the_client(text: str) -> bool:
    """Whether a message thanks the client anywhere in it."""
    return bool(_THANKS.search(text or ""))


def strip_repeated_gratitude(reply: str, *previous: str) -> str:
    """Drop an opening thank-you when a recent message already thanked them.

    Only the first sentence goes, and only when it is short enough to be an
    acknowledgement rather than content, and there is a real message left
    behind it. A thank-you further into the reply is left alone — it is part of
    a sentence that is saying something.
    """
    body = (reply or "").strip()
    if not body or not any(thanks_the_client(line) for line in previous if line):
        return body

    parts = _SENTENCE_SPLIT.split(body, maxsplit=1)
    # "Hi! Thanks for your message. Which country are you from?" — the greeting
    # is its own sentence, so look past it. It goes with the thanks: the
    # conversation is already running, so it is a repeat greeting either way.
    if len(parts) == 2 and _BARE_GREETING_SENTENCE.match(parts[0].strip()):
        parts = _SENTENCE_SPLIT.split(parts[1].strip(), maxsplit=1)
    if len(parts) != 2:
        return body
    opening, rest = parts[0].strip(), parts[1].strip()
    if not _PURE_THANKS_SENTENCE.match(opening):
        return body
    if len(opening.split()) > _MAX_THANKS_SENTENCE_WORDS or len(rest.split()) < 4:
        return body
    logger.info("Dropped a second thank-you in a row: %r", opening)
    return rest[0].upper() + rest[1:]


def opener_word(text: str) -> str:
    match = _OPENER.match(text or "")
    return match.group(1).lower() if match else ""


def last_bot_line(history_text: str) -> str:
    """The bot's most recent message out of a rendered transcript."""
    lines = recent_bot_lines(history_text, count=1)
    return lines[0] if lines else ""


def recent_bot_lines(history_text: str, count: int = 3) -> list[str]:
    """The bot's last few messages, most recent first."""
    found: list[str] = []
    for line in reversed((history_text or "").splitlines()):
        if line.startswith("You:"):
            found.append(line[4:].strip())
            if len(found) >= count:
                break
    return found


def near_duplicate(reply: str, previous: str, threshold: float = 0.8) -> bool:
    """Whether a reply says essentially the same thing as the last one.

    Asking "How many years of experience would you prefer?" twice in a row —
    observed live, word for word — makes the client feel unheard and is an
    unmistakable tell that nothing is listening.
    """
    a, b = normalize_text_local(reply), normalize_text_local(previous)
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def normalize_text_local(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def strip_repeated_opener(reply: str, *previous: str) -> str:
    """Drop a filler opener already used in any of the recent messages.

    The prompt asks for variety and mostly gets it; this catches the rest,
    because the repetition is the single most robotic-sounding trait.

    Comparing against several previous messages rather than just the last one
    matters: the model alternates. Checking only the immediately preceding reply
    lets "Got it." through on every other message, which reads exactly as
    mechanically as saying it every time.

    A repeated thank-you is handled first: it is the same fault, but it hides
    behind a greeting and spans a whole clause, so the word-level check below
    never sees it.
    """
    body = strip_repeated_gratitude(reply, *previous)
    first = opener_word(body)
    if not first or first not in _FILLER_OPENERS:
        return body
    if first not in {opener_word(line) for line in previous if line}:
        return body

    match = _FILLER_PHRASE.match(body)
    if not match:
        # A filler word opening a real sentence — leave it alone rather than
        # decapitating it.
        return body
    stripped = body[match.end():].strip()
    if len(stripped.split()) < 4:  # nothing meaningful would be left
        return body
    logger.info("Dropped repeated opener %r", match.group(0).strip())
    return stripped[0].upper() + stripped[1:]


def clamp_reply(text: str, max_sentences: int = 4) -> str:
    """Trim self-directed notes and runaway length from an otherwise good reply."""
    body = _TRAILING_NOTES.sub("", (text or "").strip()).strip()
    sentences = re.split(r"(?<=[.!?])\s+", body)
    if len(sentences) > max_sentences:
        body = " ".join(sentences[:max_sentences]).strip()
    return body


def strip_handover_talk(reply: str) -> str:
    """Remove sentences that announce a handover, keeping the rest intact.

    Dropping only the offending sentence preserves the question the collector
    asked in the same message, which a wholesale replacement would lose.
    """
    text = (reply or "").strip()
    if not text or not mentions_handover(text):
        return text

    kept = [s for s in _SENTENCE_SPLIT.split(text) if s.strip() and not mentions_handover(s)]
    cleaned = " ".join(kept).strip()
    if not cleaned:
        cleaned = NEUTRAL_FOLLOW_UP
    logger.warning("Removed handover announcement from reply: %r -> %r", text, cleaned)
    return cleaned
