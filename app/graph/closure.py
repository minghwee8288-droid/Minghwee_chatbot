"""When the right reply is no reply at all.

A real consultant does not answer "ok". They read it and carry on. The bot
answered every single inbound message, so a client acknowledging four things in
a row got four reassurances back — which is both unlike a person and the fastest
way to make a WhatsApp thread feel automated.

Everything here is deliberately deterministic and conservative. Staying silent
on a message that actually needed an answer is the worst failure mode available,
so the tests below fire only on short messages that say nothing but "received"
or "we're done", and never on anything carrying a question, a figure, or an
unfamiliar word.
"""

from __future__ import annotations

import re

# Beyond this a message is saying something, whatever words it uses.
_MAX_ACK_WORDS = 7

# A bare acknowledgement: the client confirming they read the last message.
_ACK = re.compile(
    r"^\W*(?:"
    r"ok(?:ay|ie)?|k|kk|alright|all\s+right|sure|fine|good|great|nice|cool|"
    r"noted|understood|got\s+it|got\s+that|i\s+see|makes\s+sense|"
    r"yes|yeah|yep|ya|yup|right|correct|exactly|true|"
    r"no\s+problem|no\s+worries|nvm|never\s+mind|"
    r"thank(?:s|\s*you)(?:\s+(?:so|very)\s+much)?|thx|tq|ty|"
    r"welcome|you'?re\s+welcome"
    r")\b[\s,.!…\-]*",
    re.IGNORECASE,
)

# The client winding the conversation up.
_CLOSING = re.compile(
    r"\b("
    r"bye+|goodbye|good\s*night|gd\s*nite|nite|"
    r"see\s+(?:you|ya)(?:\s+(?:later|soon|then))?|talk\s+(?:to\s+you\s+)?later|"
    r"catch\s+you\s+later|"
    r"i'?ll\s+(?:get\s+back|revert|come\s+back|let\s+you\s+know|check|think\s+about\s+it)|"
    r"let\s+me\s+(?:think|check|discuss|confirm)(?:\s+(?:about\s+)?(?:it|this|first))?|"
    r"will\s+(?:get\s+back|revert|let\s+you\s+know|update\s+you)|"
    r"(?:i'?m\s+)?(?:done|all\s+set|good\s+for\s+now)|"
    r"that'?s\s+(?:all|it)|nothing\s+else|no\s+more\s+question"
    r")\b",
    re.IGNORECASE,
)

# Anything that makes a short message a real message after all.
_QUESTION = re.compile(
    r"\?|\b(what|when|where|which|who|whose|why|how|can|could|would|should|"
    r"do|does|did|is|are|was|were|will|may|any|need|want|send|share|tell|"
    # "help" is deliberately absent: every real ask for it already carries
    # another word here ("can you help", "please help"), while "thanks for your
    # help" is a sign-off that this word alone turned into a message needing an
    # answer.
    r"please|pls|update|status|check)\b",
    re.IGNORECASE,
)

# Emoji, ticks and punctuation with no letters or digits at all: 👍 / 🙏 / ok✅
_NO_CONTENT = re.compile(r"^[^\w]*$", re.UNICODE)


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", (text or "").strip()) if w]


def is_pure_acknowledgement(text: str) -> bool:
    """Whether a message says nothing beyond 'received'.

    Subtractive rather than a whitelist of exact strings: the acknowledgement is
    stripped off the front and whatever is left decides it. "ok thanks" and
    "noted, thank you 🙂" are acknowledgements; "ok how much" is not, because
    "how much" survives the strip.
    """
    body = (text or "").strip()
    if not body:
        return False
    if _NO_CONTENT.match(body):  # a bare 👍
        # ...but "?" and "??" are punctuation-only too, and they are the exact
        # opposite of an acknowledgement: a client who has had no answer and is
        # prompting for one. Live, "??" was silenced as if it were a thumbs-up,
        # which is the single most irritating thing we can do to someone already
        # waiting.
        return "?" not in body
    if len(_words(body)) > _MAX_ACK_WORDS:
        return False
    if _QUESTION.search(body):
        return False

    # Peel acknowledgements off the front until nothing changes.
    rest = body
    while True:
        stripped = _ACK.sub("", rest, count=1).strip()
        if stripped == rest:
            break
        rest = stripped
    return bool(_NO_CONTENT.match(rest)) or not rest


# An interrogative anywhere in the message means it is asking, whatever else it
# contains. Deliberately narrower than _QUESTION above, which includes "check"
# and "let" — words that appear in the closings this guard exists to catch
# ("let me check first").
_INTERROGATIVE = re.compile(
    r"\?|\b(what|when|where|which|who|whom|whose|why|how|"
    r"tell\s+me|explain|send\s+me|share)\b",
    re.IGNORECASE,
)


def is_closing(text: str) -> bool:
    """Whether the client is ending the conversation for now."""
    body = (text or "").strip()
    if not body or len(_words(body)) > _MAX_ACK_WORDS + 4:
        return False
    # "But tell me what would be done / What's the process" — eleven words, no
    # question mark, and "done" matched the closing pattern, so the bot read a
    # direct question as a sign-off and said nothing. The client then sent "?",
    # then "Are you there". A message that asks something is never a closing,
    # however it ends.
    if _INTERROGATIVE.search(body):
        return False
    return bool(_CLOSING.search(body))


# A bare yes or no, and nothing else. These sit inside _ACK above, which is
# right when they follow a statement ("noted, yes") and wrong when they follow a
# question — then they are the answer we asked for.
_BARE_ANSWER = re.compile(
    r"^\W*(?:"
    r"yes|yeah|yep|yup|ya|yah|correct|exactly|true|right|confirm(?:ed)?|"
    r"no|nope|nah|not\s+yet|negative"
    r")\W*$",
    re.IGNORECASE,
)


def _we_just_asked(history_text: str) -> bool:
    """Whether our own last message put a question to the client."""
    for line in reversed((history_text or "").splitlines()):
        if line.startswith("You:"):
            return "?" in line
    return False


def needs_no_reply(text: str, *, history_text: str = "") -> bool:
    """Whether this message should be read and left unanswered.

    Never silent on the opening message of a conversation: a lone "hi" is
    somebody starting a conversation, not closing one, and answering it is the
    whole job.

    Never silent on a bare yes or no when we were the ones who just asked. The
    acknowledgement pattern lists "yes" — correct after a statement, and twice
    live it swallowed an answer instead: we asked "is she currently with you?"
    and "has her current employer agreed to the transfer?", the client replied
    "Yes" to each, and got nothing back both times. They had to rephrase
    themselves ("She is with me", "Iam the current employer of her") to move a
    flow that was already waiting on exactly that answer.
    """
    if not (history_text or "").strip():
        return False
    if _BARE_ANSWER.match((text or "").strip()) and _we_just_asked(history_text):
        return False
    return is_pure_acknowledgement(text) or is_closing(text)
