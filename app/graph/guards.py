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
from typing import Any

from app.services import kb_rules

logger = logging.getLogger(__name__)

# --- Grounded figures ------------------------------------------------------

_FIGURE = re.compile(r"\d[\d,\.]*")
# Small numbers are ordinary prose ("2 types", "3-4 weeks") and are ignored.
_MIN_GROUNDED_FIGURE = 100

# A ticket reference, CB-YYYY-NNNN (app/graph/reference.py). One definition:
# reference.py imports it from here.
TICKET_REFERENCE = re.compile(r"\bCB-\d{4}-\d{4}\b")
_TICKET_REFERENCE = TICKET_REFERENCE


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


# Services where the client has told us NOT to put a price in front of anyone
# before a salesperson has spoken to them. Their reasoning, 2026-09-04: a new
# hire is a high-ticket commitment ("approximately S$14,000-17,500" for the
# first year, per the knowledge base), and a number that size landing in a
# WhatsApp message with no consultant attached loses the client rather than
# informing them. The small-ticket services are the deliberate opposite — a
# renewal, a passport, an insurance policy are quoted freely.
# `replacement`, `transfer` and `transfer_employer` joined on 2026-09-08, when
# the agency sent the consolidated cost + timeline table. It states a fee for
# work permit renewal ($695), passport renewal ($450) and home leave ($400 /
# $250) and explicitly does NOT state one for these three or for hiring - their
# instruction: "the service which do not have the timeline and cost ... live
# agent will handle that". The knowledge base is not empty of figures for them
# (the Client Service Agreement's refund clauses sit under `replacement`, and
# the $5,000 bond under `transfer`), so a mechanical net is worth having on top
# of the deferral rows. The bond and the salary are still not caught - see
# quotes_hiring_package_cost.
#
# 2026-09-22: `new_hiring`, `transfer` and `transfer_employer` LEAVE this set,
# because the agency has now given their prices. This reverses the half of the
# 2026-09-04 instruction that covered them, and it is their reversal rather
# than an inference from it - the fee schedule they sent states an agency fee
# for each of the six service+nationality combinations and asks in as many
# words that the bot answer "how much does Philippines transfer helper cost?"
# with $1,688. Section 9 has carried "may a new hire's cost be quoted at all?"
# as an open question for them since 2026-09-17, against their own 2026-09-17
# flow which puts Cost/Fee immediately after Process & Timeline. This is the
# answer, and the open item closes with it.
#
# `direct_hiring` and `replacement` STAY. The schedule gives no figure for
# either, and the rule that a service with no stated price defers to a person
# is the agency's own ("the service which do not have the timeline and cost ...
# live agent will handle that"). `fee_enquiry` stays too, and it is the load-
# bearing one: it is what a bare "how much do you charge?" resolves to when no
# service has been established, and their rule 12 is that an ambiguous fee
# question is answered only once the service and the nationality are known.
#
# 2026-09-24: the set now lives in cb_kb_rules (price_policy = 'withheld') so
# the KB Admin UI can change it, read through kb_rules.cost_withheld_services().
# The code default there is exactly {direct_hiring, fee_enquiry, replacement},
# and fee_enquiry is locked in the table - it cannot be edited to 'stated'.

# The other half of the agency's 2026-09-08 cost table: the services whose fee
# they DID give us, and which therefore quote it. The two sets are exact
# opposites and must stay disjoint - a service cannot both withhold its price
# and state it - which selfcheck_flows.py asserts.
#
# rag_retriever reads this as well, and that is the point. A price question
# inside one of these is a question about THAT service's price, so the service
# filter is kept rather than dropped. Measured 2026-09-08, unfiltered: "how
# much does it cost" inside a PASSPORT renewal returned the WORK PERMIT renewal
# row (0.510) - $695 quoted to a client whose answer is $450 - on all three
# phrasings tried. Filtered it returns the right row every time (0.419-0.486).
#
# The three that joined on 2026-09-22 are the ones that matter most here, and
# membership does more than permit a figure: `_service_filter` KEEPS the
# service filter for these, and the widening retry is refused, so a fee
# question inside a transfer cannot reach a new hire's fee and vice versa.
# That is the agency's own first rule - "never answer a user's fee question
# using information from another service simply because the nationality or fee
# category is similar" - enforced by retrieval rather than by asking the model
# nicely. `transfer` and `transfer_employer` are BOTH here on purpose: the
# employer's key is what _subject_service returns and the candidate's key is
# what _aliased() rewrites it to, and the no-widening test reads the aliased
# value.
#
# 2026-09-24: the set now lives in cb_kb_rules (price_policy = 'stated'), read
# through kb_rules.fee_stated_services(). The code default is exactly {renewal,
# passport_renewal, home_leave, new_hiring, transfer, transfer_employer}, and a
# constraint trigger keeps transfer and transfer_employer equal in the table.


# A figure presented as the cost of the engagement, rather than a figure that
# happens to be money. The pairing is what matters: a bare "$650" is a salary
# and must still go out, and "the $5,000 security bond" is a legal fact a client
# is entitled to. What is caught is a total, a package, or one of our own fees
# with an amount beside it.
_PACKAGE_COST_TERMS = (
    r"total|altogether|all[- ]?in|overall|upfront|first[- ]?year|package|"
    r"placement\s+fee|service\s+fee|agency\s+fee|our\s+fee|deposit"
)
_PACKAGE_COST = re.compile(
    rf"(?:{_PACKAGE_COST_TERMS})[^.!?]{{0,90}}?[$]\s?[\d,]{{3,}}"
    rf"|[$]\s?[\d,]{{3,}}[^.!?]{{0,90}}?(?:{_PACKAGE_COST_TERMS})",
    re.IGNORECASE,
)


# Sent instead of a price when the guard above fires. Vetted, so it bypasses
# nothing: no figure, no named colleague, no promised time, and it does not
# claim the cost is unknowable — only that a person will do the costing.
COST_DEFERRAL_REPLY = (
    "The cost depends on nationality, experience and how the package is put "
    "together, so I would rather one of our agents take you through the "
    "full breakdown than give you half a figure. I will get that arranged."
)


# A question about what WE charge, as opposed to money in general. Timing
# words are deliberately absent: "how much time does it take" is not a price
# question and must keep the widening retry it has had since 2026-09-03.
#
# Moved here from rag_retriever on 2026-09-22, when a second caller appeared.
# It decides two things that have to agree about the same sentence: whether
# retrieval may widen off this service, and whether the reply may put a figure
# on a service whose price depends on a nationality we have not established.
# Two copies of it would be section 9.8 in the one place it costs a client a
# wrong price.
_PRICE_QUESTION = re.compile(
    r"\bcosts?\b|\bprices?\b|\bfees?\b|\bcharges?\b|\bpackages?\b"
    r"|\bhow\s+much\s+(?:is|are|does|do|would|will|for|to)\b"
    r"|\bhow\s+much\s*[?.!]*\s*$",
    re.IGNORECASE,
)


def asks_about_price(text: str) -> bool:
    """Whether this message is asking what something costs."""
    return bool(_PRICE_QUESTION.search(text or ""))


def quotes_hiring_package_cost(reply: str) -> bool:
    """Whether the reply puts a price on the hire itself.

    Only meaningful for kb_rules.cost_withheld_services() — the caller decides. Salary
    figures, the levy and the security bond are deliberately NOT caught: they
    are not what the client meant by "the cost", and withholding them would
    make the bot evasive about facts a client is entitled to.
    """
    return bool(_PACKAGE_COST.search(reply or ""))


# --- One nationality's fee is not another's (Phase 3 Step 4, 2026-10-13) ----
#
# Measured on main 2026-09-24 (section 9.28): "How much is passport renewal for
# my Myanmar helper?" was answered "For a Myanmar helper, the passport renewal
# fee is approximately $450" about one run in ten - the 2026-09-08 defect. On
# that path the Myanmar protection was a PROMPT note
# (FEE_NOT_HELD_FOR_NATIONALITY_NOTE) and nothing mechanical: $450 really is
# in the retrieved records, as the Filipino and Indonesian price, so
# ungrounded_figures passes it. Grounded is not the same as hers.
#
# Two rules, both keyed on the service and the nationality the turn is about:
#   * we hold NO fee for this nationality on this service (kb_rules says so):
#     no fee figure may go out at all;
#   * we DO hold one: a figure the records (or our own earlier messages) give
#     only to ANOTHER nationality may not go out. A figure two nationalities
#     share ($450 PH and ID) is hers too and passes.
# Only the offending sentence is replaced, with the deferral wording the
# briefing note has used since 2026-09-23; the timing and the documents in
# the rest of the reply are unaffected.
#
# Which nationalities have a fee is read through kb_rules and nowhere else.
# The FIGURES are never written here: kb_rules holds the policy, not the
# prices, so who owns a figure is read off the text the model was given - a
# figure beside a nationality word, or inside a row the KB labels with one.
NATIONALITY_FEE_GUARD = True

NATIONALITY_FEE_DEFERRAL = "Our agent will confirm the exact fee based on your requirements."

_MONEY = re.compile(r"(?:S\$|SGD\s?|\$)\s?(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
# Money that is not our fee for the service: what she is paid, the government
# levy, the security bond. A sentence about one of those is left alone by both
# rules - a Filipino salary floor beside an Indonesian budget is not a fee.
_NOT_OUR_FEE = re.compile(r"\b(?:salary|salaries|wages?|levy|levies|bond)\b", re.IGNORECASE)
_NATIONALITY_CODES = frozenset({"PH", "ID", "MM"})

# Step 5 item 17 (2026-10-13): where we hold no fee, the cost is not called
# "approximate" - there is nothing to approximate. Its own switch.
NO_APPROXIMATE_WITHOUT_FIGURE = True
_APPROXIMATE_COST = re.compile(
    r"\b(?:fees?|costs?|prices?|charges?)\b[^.!?]*\bapproximat\w*"
    r"|\bapproximat\w*[^.!?]*\b(?:fees?|costs?|prices?|charges?)\b",
    re.IGNORECASE,
)
_ALREADY_DEFERRED = re.compile(
    r"\bconfirm the (?:exact )?(?:\w+ )?(?:fee|cost|amount|price)\b", re.IGNORECASE
)
_NATIONALITY_WORD = {"PH": "a Filipino", "ID": "an Indonesian", "MM": "a Myanmar"}
_FEE_SERVICE_WORD = {
    "passport_renewal": "passport renewal", "home_leave": "home leave",
    "new_hiring": "new hire", "transfer": "transfer", "transfer_employer": "transfer",
}


def nationality_fee_deferral(service: str | None, nationality: str | None) -> str:
    """'Our agent will confirm the cost for a Myanmar helper's passport renewal.'"""
    who = _NATIONALITY_WORD.get(nationality or "")
    what = _FEE_SERVICE_WORD.get(service or "")
    if not who or not what:
        return NATIONALITY_FEE_DEFERRAL
    return f"Our agent will confirm the cost for {who} helper's {what}."


def _money_norm(raw: str) -> str:
    """'$1,428' == 'S$1428' == '1428.00'."""
    value = raw.replace(",", "").rstrip(".")
    head, _, tail = value.partition(".")
    return head if not tail.strip("0") else value


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence, list markers not counted as an end."""
    masked = _LIST_MARKER.sub(lambda m: m.group(1) + " " + m.group(2), text)
    spans: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"(?<=[.!?])\s+|\n+", masked):
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def fee_owners(*sources: str, matches: Any = ()) -> dict[str, set[str]]:
    """Figure -> the nationalities the text gives it to.

    A figure belongs to the nationality named nearest to it in the same
    sentence ("$450 for a Filipino helper", "Philippines $1,428"), and every
    figure in a row the KB labels PH/ID/MM belongs to that nationality.
    """
    from app.services.lead import _NATIONALITY_NAMED  # lazy: lead imports the db

    owners: dict[str, set[str]] = {}
    for text in sources:
        text = text or ""
        for start, end in _sentence_spans(text):
            sentence = text[start:end]
            mentions = [
                (m.start(), m.end(), code)
                for pattern, code in _NATIONALITY_NAMED
                for m in pattern.finditer(sentence)
            ]
            if not mentions:
                continue
            for money in _MONEY.finditer(sentence):
                nearest = min(
                    mentions,
                    key=lambda n: min(abs(n[0] - money.end()), abs(money.start() - n[1])),
                )
                owners.setdefault(_money_norm(money.group(1)), set()).add(nearest[2])
    for match in matches or ():
        code = str((match or {}).get("nationality") or "").strip().upper()
        if code not in _NATIONALITY_CODES:
            continue
        body = " ".join(
            str(match.get(key) or "") for key in ("question", "answer", "content")
        )
        for money in _MONEY.finditer(body):
            owners.setdefault(_money_norm(money.group(1)), set()).add(code)
    return owners


def guard_nationality_fee(
    reply: str,
    service: str | None,
    nationality: str | None,
    *sources: str,
    matches: Any = (),
) -> tuple[str, list[str]]:
    """The reply with any other nationality's fee replaced, and what was cut.

    No-op unless the switch is on, the service is priced per nationality
    (kb_rules) and the nationality is one of the three we place.
    """
    if not NATIONALITY_FEE_GUARD or not reply:
        return reply, []
    held = kb_rules.fee_by_nationality().get(service or "")
    if held is None or nationality not in _NATIONALITY_CODES:
        return reply, []
    known = nationality in held
    owners = fee_owners(*sources, matches=matches) if known else {}
    # Where we hold no fee for her, the deferral names her and the service
    # (Step 5 item 17, the agency's wording); elsewhere the briefing's own.
    deferral = NATIONALITY_FEE_DEFERRAL if known else nationality_fee_deferral(service, nationality)

    offending: list[tuple[int, int]] = []
    for start, end in _sentence_spans(reply):
        sentence = reply[start:end]
        figures = [_money_norm(m.group(1)) for m in _MONEY.finditer(sentence)]
        if not figures:
            # Step 5 item 17: a cost with no figure is not "approximate". Live,
            # 2026-10-13: "The fee for a Myanmar helper's passport renewal is
            # approximate and our agent will confirm the exact amount."
            if not known and NO_APPROXIMATE_WITHOUT_FIGURE and _APPROXIMATE_COST.search(sentence):
                offending.append((start, end))
            continue
        if _NOT_OUR_FEE.search(sentence):
            continue
        if not known:
            offending.append((start, end))
        elif any(owners.get(f) and nationality not in owners[f] for f in figures):
            offending.append((start, end))
    if not offending:
        return reply, []

    cut = [reply[s:e].strip() for s, e in offending]
    rest = reply
    for start, end in reversed(offending):
        rest = rest[:start] + rest[end:]
    already_deferred = bool(_ALREADY_DEFERRED.search(rest))
    out = reply
    for index, (start, end) in reversed(list(enumerate(offending))):
        sentence = out[start:end]
        marker = re.match(r"\s*\d+\.\s+", sentence)
        keep = marker.group(0) if marker else ""
        replacement = keep + deferral if index == 0 and not already_deferred else ""
        out = out[:start] + replacement + out[end:]
    # A list item or line emptied by a removal goes with it.
    out = re.sub(r"(?m)^\s*\d+\.\s*$\n?", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"(?m)^[ \t]+(?=\S)", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return (out or deferral), cut


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
    # A ticket reference (CB-2026-0142) is an identifier, not a figure: read
    # as one, "2026" and "0142" would bin a reply that only repeats it
    # (2026-10-12, app/graph/reference.py).
    reply = _TICKET_REFERENCE.sub(" ", reply or "")
    for figure in _digit_forms(reply):
        head = figure.split(".", 1)[0]
        if not head.isdigit() or int(head) < _MIN_GROUNDED_FIGURE:
            continue
        if figure not in grounded:
            suspect.append(figure)
    return suspect


# --- Handover promises -----------------------------------------------------
#
# Handovers used to be invisible: the client was meant to believe they were
# talking to one person throughout, and this pattern stripped any sentence that
# said otherwise. That policy is gone — Claire introduces herself as Ming Hwee's
# AI assistant and says plainly when a live agent is taking over (rules 1 and 2).
#
# What is still forbidden is a commitment the office has not made. Announcing
# "a live agent will connect with you shortly" is the sanctioned line; naming
# who, or promising when, invents an appointment nobody scheduled. Live, the old
# model wrote "Grace will call you back" about an agent who was on leave.
_HANDOVER_TALK = re.compile(
    # "transfer you to Grace", "connect you with Winston" — a capitalised name
    # that is not the agency's own. The name half is explicitly case-SENSITIVE
    # via (?-i:...): the whole pattern runs IGNORECASE for the verbs, and
    # without scoping that off, [A-Z][a-z]+ happily matched "our team" too.
    r"\b(transfer|connect|refer|forward|pass|put)\s+(you|this)\s+"
    r"(to|with|through\s+to)\s+(?!Ming\b)(?-i:[A-Z][a-z]+)"
    # "in 10 minutes", "within 2 hours", "by 3pm", "at 4 PM", "before 5pm"
    r"|\b(in|within|after)\s+\d+\s*(min|mins|minute|minutes|hour|hours|hr|hrs)\b"
    r"|\b(by|at|before|around)\s+\d{1,2}(:\d{2})?\s*(am|pm)\b"
    # "will call you today/tomorrow at ..."
    r"|\b(call|ring|phone)\s+you\s+(back\s+)?(today|tomorrow|tonight|this\s+\w+)\b",
    re.IGNORECASE,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

NEUTRAL_FOLLOW_UP = (
    "I've passed this to our team and a live agent will connect with you shortly. "
    "In the meantime, is there anything else I can help you with?"
)

# What we say when we cannot answer. Every one of these leads to a handover, so
# each says so and then offers to keep helping (rule 2). A single fixed string
# was going out four and five times in one conversation, word for word, which is
# unmistakably machine-like — a person phrases the same thought differently.
HOLDING_REPLIES = (
    "Let me get a live agent to confirm that for you — they'll connect with you "
    "shortly. Anything else I can help with in the meantime?",
    "I'd rather not guess on that one, so I've passed it to our team and a live "
    "agent will come back to you shortly. Is there anything else I can help with?",
    "That one needs a live agent to check properly — they'll be in touch shortly. "
    "In the meantime, anything else you'd like to ask?",
    "I've handed that to our team so a live agent can give you the right answer, "
    "and they'll connect with you shortly. Anything else I can help with?",
)


def holding_reply(history_text: str = "") -> str:
    """A 'let me check' line we have not just used.

    Falls back to the first phrasing when every option has been used recently,
    which in practice means the conversation has bigger problems than repetition.
    """
    recent = " ".join(recent_bot_lines(history_text, count=4)).lower()
    for line in HOLDING_REPLIES:
        # Compare on the first few words: the guards downstream may have
        # trimmed an opener off the copy that actually went out.
        if " ".join(normalize_text_local(line).split()[:4]) not in normalize_text_local(recent):
            return line
    return HOLDING_REPLIES[0]


def mentions_handover(text: str) -> bool:
    """Whether the reply promises a named colleague or a specific time.

    Not "does this mention a handover" any more — announcing one is now the
    required behaviour. This catches only the commitment the office has not
    made. The name is kept for the call sites that already read well with it.
    """
    return bool(_HANDOVER_TALK.search(text or ""))


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


# The same test with numbered steps permitted, and nothing else. A stepped
# answer IS a numbered list - it is exactly what PROCESS_INSTRUCTION asks for -
# so on that one path the numbered branch is dropped while headings, bold runs
# and bullets stay banned.
#
# Live, 2026-09-08: the first build of the stepped answer was caught by
# looks_like_document on every process question and handed to a human instead,
# which is the opposite of the change's whole purpose. smoke_nodes.py surfaced
# it before it shipped, on the run that first executed response_generator.
_MARKDOWN_NO_STEPS = re.compile(r"^#{1,6}\s|\*\*|^\s*[-*\u2022]\s+", re.MULTILINE)


def looks_like_document(text: str, allow_steps: bool = False) -> bool:
    """Whether the reply is formatted like a document rather than a message.

    `allow_steps` is passed ONLY by the callers that asked for a numbered list.
    Everywhere else a numbered list is still a document: the guard exists
    because a plumber directory headed "### Where to Find..." went out to a
    client during testing.
    """
    pattern = _MARKDOWN_NO_STEPS if allow_steps else _MARKDOWN
    return len(pattern.findall(text or "")) >= 2


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
    # The client's own name, which the voice guide asks for: "Thanks Ping pong."
    # was thanking them twice in four messages and neither guard saw it, because
    # a name is not one of the endings listed below. Bounded at two words and
    # barred from swallowing 'for', so "Thanks for the permit photo which shows
    # a March expiry" still falls through to the whitelist and is left alone.
    r"(?:\s+(?!for\b)[A-Za-z][\w'\-]*){0,2}"
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


def last_client_line(history_text: str) -> str:
    """The client's most recent message before this turn (history excludes it)."""
    for line in reversed((history_text or "").splitlines()):
        if line.startswith("Client:"):
            return line[7:].strip()
    return ""


def recent_bot_lines(history_text: str, count: int = 3) -> list[str]:
    """The bot's last few messages, most recent first."""
    found: list[str] = []
    for line in reversed((history_text or "").splitlines()):
        if line.startswith("You:"):
            found.append(line[4:].strip())
            if len(found) >= count:
                break
    return found


def claire_has_spoken(history_text: str) -> bool:
    """Whether Claire herself has written anything in this transcript yet.

    "First contact" used to mean an EMPTY history, and that is false on any
    number our team already talks to. Live, 2026-10-07, conversation 26: a
    client whose agents had been messaging him all week about insurance and an
    air ticket asked "Hi I will like to find out more about direct hire process
    and costs". The history was full of those agents' lines, so the prompt said
    the conversation was already going and Claire never said who she was - the
    agency: "No greeting", "introduction of agent bot". Agents' lines are now
    rendered "Agent:" (message.format_history), so "You:" is Claire alone, and
    the first message SHE writes introduces her whoever spoke before.
    """
    return any(
        line.startswith("You:") for line in (history_text or "").splitlines()
    )


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


def reasks_previous(reply: str, previous: str, threshold: float = 0.8) -> bool:
    """Whether a reply is our previous question again, cut down.

    `near_duplicate` compares whole messages, so a SHORTER re-ask slips under
    it. Live, 2026-09-23, conversation 1687: "Are there any other house rules
    or preferences I should note, so these can be clearly agreed with the
    helper before she starts?" was answered with the house rule, and the next
    message was "Any other house rules or preferences I should note?" - a
    ratio well under 0.8, and every one of its words taken from the message
    before it. The client read it as a glitch, and it was one: that question
    had been answered and the collector had moved on to the next.

    Containment rather than similarity: what share of the reply's words were
    already in the previous message. Four content words at least, so a bare
    "Got it, thanks." cannot trip it on two words it happens to share.
    """
    words = [w for w in normalize_text_local(reply).split() if len(w) >= 3]
    if len(words) < 4:
        return False
    before = set(normalize_text_local(previous).split())
    return sum(w in before for w in words) / len(words) >= threshold


def normalize_text_local(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def strip_leading_name(reply: str, name: str) -> str:
    """Drop the client's own name when it OPENS the message with nothing else.

    "Amir. Are you sending Farhana home, transferring her to another employer,
    or have you not decided yet?" - the client's name standing alone as a
    sentence, then the next question. It reads as a form calling out a row
    before it reads the next field, and the agency has now sent it in four
    separate transcripts across two services (2026-09-17 passport renewal,
    2026-09-18 replacement twice).

    The prompt rule alone was not enough, which is this repo's whole argument
    for guards: told plainly not to, the model still did it in 1 run of 3. And
    the first version of that prompt rule made it WORSE - it quoted the bad
    example verbatim, so the model was reading the exact sentence it was being
    told not to write, which is section 8's rule about non-Latin script applied
    to a name. Measured 3 runs of 3 before the example came out.

    A GREETING is untouched, because that is the one message where the name
    belongs: "Hi Amir", "Thanks, Amir" and "Good to meet you, Amir" all keep it,
    since the name is not what the message opens with. Only the bare forms go -
    "Amir." and "Amir," at the very start - and only the name we actually hold,
    never a word that merely looks like one.
    """
    body = (reply or "").lstrip()
    first = (name or "").strip().split()[0] if (name or "").strip() else ""
    if not body or len(first) < 2:
        return reply
    match = re.match(
        rf"{re.escape(first)}\s*[.,!]\s+(?=\S)", body, re.IGNORECASE
    )
    if not match:
        return reply
    rest = body[match.end():]
    if not rest:
        return reply
    return rest[0].upper() + rest[1:]


# "(MDW)" tags a name; a possessive cannot hang off the bracket. Live
# 2026-10-13 (3766): "Here is everything for Devi lal singh (MDW)'s home
# leave". The tag goes and the possessive goes back on the name.
_TAG_BEFORE_POSSESSIVE = re.compile(r"\s*\(MDW\)(['’](?:s\b)?)", re.IGNORECASE)


def without_tag_before_possessive(reply: str) -> str:
    """'Devi (MDW)'s leave' -> 'Devi's leave'. Anything else is untouched."""
    return _TAG_BEFORE_POSSESSIVE.sub(r"\1", reply or "")


# Phase 3 Step 5 item 15 (2026-10-13). In the Step 2e replay the model ended
# an Indonesian chase with "天天彩票中奖" - a known glitch string, Chinese for a
# lottery win - and no guard caught it: is_degenerate is weak on unspaced
# scripts, and nothing else reads the script at all. Runs of letters in a
# script that is neither the reply's main script nor one the client (or our
# record of their name) wrote are removed; the glitch strings always are.
# Latin is never removed: Claire, Ming Hwee, the address, a CB reference and
# names written in Latin all stay in a Burmese or Chinese reply. Digits and
# punctuation are not letters, so they are never touched either.
# False = the reply goes out exactly as the node wrote it.
STRAY_SCRIPT_GUARD = True

GLITCH_STRINGS = ("天天彩票中奖",)

# Script buckets. Han and kana are one bucket, so Japanese (kanji + kana) is
# never cut against itself.
_SCRIPT_BUCKETS = (
    ("cjk", "぀-ヿ㐀-䶿一-鿿豈-﫿"),
    ("hangul", "가-힯ᄀ-ᇿ"),
    ("burmese", "က-႟"),
    ("thai", "฀-๿"),
    ("lao", "຀-໿"),
    ("khmer", "ក-៿"),
    ("devanagari", "ऀ-ॿ"),
    ("bengali", "ঀ-৿"),
    ("tamil", "஀-௿"),
    ("telugu", "ఀ-౿"),
    ("sinhala", "඀-෿"),
    ("arabic", "؀-ۿ"),
    ("hebrew", "֐-׿"),
    ("cyrillic", "Ѐ-ӿ"),
    ("greek", "Ͱ-Ͽ"),
)
_SCRIPT_CHAR = {name: re.compile(f"[{chars}]") for name, chars in _SCRIPT_BUCKETS}
# A run: the script's letters with any spaces between them, so a stray phrase
# goes whole and does not leave its spacing behind.
_SCRIPT_RUN = {
    name: re.compile(f"[{chars}](?:[{chars}\\s]*[{chars}])?")
    for name, chars in _SCRIPT_BUCKETS
}
_LATIN_CHAR = re.compile(r"[A-Za-zÀ-ɏ]")


def _scripts_in(text: str) -> set[str]:
    found = {name for name, pat in _SCRIPT_CHAR.items() if pat.search(text or "")}
    if _LATIN_CHAR.search(text or ""):
        found.add("latin")
    return found


def _main_script(text: str) -> str | None:
    counts = {name: len(pat.findall(text)) for name, pat in _SCRIPT_CHAR.items()}
    counts["latin"] = len(_LATIN_CHAR.findall(text))
    name, count = max(counts.items(), key=lambda kv: kv[1])
    return name if count else None


def _tidy_after_cut(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([.,!?;:])", r"\1", text)
    text = re.sub(r"([.!?])[.,;:]+", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def without_stray_script(reply: str, *client_text: str) -> str:
    """Remove glitch strings, and runs in a script that is neither the
    reply's main script nor one in `client_text` (what the client wrote, and
    the names we hold for them). Latin, digits and punctuation are never cut.
    """
    body = reply or ""
    if not body:
        return body
    out = body
    for glitch in GLITCH_STRINGS:
        out = out.replace(glitch, "")
    main = _main_script(out)
    keep = _scripts_in(" ".join(t or "" for t in client_text)) | {"latin"}
    if main:
        keep.add(main)
    for name, run in _SCRIPT_RUN.items():
        if name not in keep:
            out = run.sub("", out)
    if out == body:
        return body
    out = _tidy_after_cut(out)
    logger.warning("Stray-script guard cut %r from a reply",
                   "".join(ch for ch in body if ch not in out)[:80])
    return out


# Phase 3 Step 5 item 9 (2026-10-13). "As this is your first helper, she must
# attend the Settling-In Programme" states a fact nobody gave us. The record
# fill says only "first time with us - no placement on record", which is about
# OUR records: a client who employed a helper through another agency, or on
# another number, is no first-timer. Said as a condition it is true either
# way. Left as written when the client said so themselves.
# False = the reply as the node wrote it.
FIRST_HELPER_CONDITIONAL = True
_ASSUMED_FIRST_HELPER = re.compile(
    r"\b(as|since|because|given\s+that)\s+(?:this\s+is|it\s+is|it'?s|this'?s)\s+"
    r"(your\s+first(?:\s+time\s+(?:hiring|employing|getting|having)\s+a)?\s+"
    r"(?:domestic\s+)?(?:helper|maid|MDW|hire))\b",
    re.IGNORECASE,
)
_ASSUMED_FIRST_TIME_EMPLOYER = re.compile(
    r"\b(as|since|because|given\s+that)\s+you(?:'re|\s+are)\s+(a\s+first[- ]time\s+employer)\b",
    re.IGNORECASE,
)
_SAYS_FIRST_HELPER = re.compile(
    r"\b(?:my|our)\s+first\s+(?:domestic\s+)?(?:helper|maid|hire)\b|"
    r"\bfirst\s+time\s+(?:hiring|employing|to\s+hire|getting|having)\b|"
    r"\bfirst[- ]time\s+employer\b|"
    r"\bnever\s+(?:hired|had|employed)\s+(?:a\s+)?(?:domestic\s+)?(?:helper|maid)",
    re.IGNORECASE,
)


def _as_condition(match: re.Match, subject: str) -> str:
    word = "If" if match.group(1)[0].isupper() else "if"
    return f"{word} {subject} {match.group(2)}"


def without_assumed_first_helper(reply: str, *client_text: str) -> str:
    """'As this is your first helper, ...' -> 'If this is your first helper, ...'
    unless the client said it is."""
    body = reply or ""
    if not body or _SAYS_FIRST_HELPER.search(" ".join(t or "" for t in client_text)):
        return body
    out = _ASSUMED_FIRST_HELPER.sub(lambda m: _as_condition(m, "this is"), body)
    out = _ASSUMED_FIRST_TIME_EMPLOYER.sub(lambda m: _as_condition(m, "you are"), out)
    if out != body:
        logger.info("Reworded an assumed first helper as a condition")
    return out


def one_mention_of_known_helper(reply: str, names: list[str]) -> str:
    """Step 5 item 11: a known helper is named once, inside the confirmation
    question ("Is this home leave for Devi, or for another helper?"). Any
    other sentence naming her - "We have Devi's details on record", or the
    question asked a second time - is dropped. Left alone when no sentence
    is the confirmation question."""
    body = reply or ""
    patterns = [
        re.compile(rf"(?<!\w){re.escape(n.strip())}(?!\w)", re.IGNORECASE)
        for n in names or [] if str(n or "").strip()
    ]
    if not body or not patterns:
        return body
    spans = _sentence_spans(body)

    def names_her(i: int) -> bool:
        return any(p.search(body[spans[i][0]:spans[i][1]]) for p in patterns)

    confirm = [
        i for i in range(len(spans))
        if names_her(i) and "?" in body[spans[i][0]:spans[i][1]]
    ]
    if not confirm:
        return body
    drop = [i for i in range(len(spans)) if i != confirm[0] and names_her(i)]
    if not drop:
        return body
    out = body
    for i in reversed(drop):
        start, end = spans[i]
        out = out[:start] + out[end:]
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"(?m)^[ \t]+(?=\S)", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    logger.info("Known helper named outside the confirmation question - %d sentence(s) dropped",
                len(drop))
    return out


# Phase 3 Step 5 item 17, everywhere (2026-10-13). Step 4b stopped "the cost
# is approximate" with no figure on the nationality-priced services. The same
# sentence on a service we never price (direct hire, replacement) says nothing
# either: there is no figure to approximate. Any sentence that calls a fee or
# cost approximate and carries no amount is replaced with the deferral (or
# dropped, where the reply already says our agent will confirm it). Tight on
# purpose: "approximately 2 weeks ... our agent will confirm the cost" is a
# timing, and stays. False = the reply as the node wrote it.
NO_APPROXIMATE_COST_ANYWHERE = True
_FIGURELESS_APPROXIMATE_COST = re.compile(
    r"\b(?:fees?|costs?|prices?|charges?|amount)\b\s+(?:\w+\s+){0,3}?approximat\w*"
    r"|\bapproximat\w*\s+(?:\w+\s+){0,2}?(?:fees?|costs?|prices?|charges?|amount)\b",
    re.IGNORECASE,
)


def without_figureless_approximate(reply: str) -> str:
    """'The cost is approximate and our agent will confirm it.' -> the deferral."""
    body = reply or ""
    if not body:
        return body
    offending = [
        (start, end) for start, end in _sentence_spans(body)
        if _FIGURELESS_APPROXIMATE_COST.search(body[start:end])
        and not _MONEY.search(body[start:end])
        and not re.search(r"\d", body[start:end])
    ]
    if not offending:
        return body
    rest = body
    for start, end in reversed(offending):
        rest = rest[:start] + rest[end:]
    already = bool(_ALREADY_DEFERRED.search(rest))
    out = body
    for index, (start, end) in reversed(list(enumerate(offending))):
        marker = re.match(r"\s*\d+\.\s+", out[start:end])
        keep = marker.group(0) if marker else ""
        replacement = keep + NATIONALITY_FEE_DEFERRAL if index == 0 and not already else ""
        out = out[:start] + replacement + out[end:]
    out = re.sub(r"(?m)^\s*\d+\.\s*$\n?", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"(?m)^[ \t]+(?=\S)", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    logger.info("Replaced a figure-free 'approximate' cost sentence")
    return out or NATIONALITY_FEE_DEFERRAL


# Phase 3 Step 5 Batch B2 item 2 (2026-10-14). Live: after the renewal closing
# said "The cost is $695", "how much is the fee" got "The approximate fee is
# $695" and "why this much" got "...the exact amount will be confirmed by our
# team". The records say "Work permit renewal is $695" - a fixed price. Prompt
# rule 5 told the model to call EVERY figure approximate and promise to
# confirm it (3 of 3 replays). A figure the records state with no qualifier is
# now stated plainly: "approximately"/"about"/"around" before it and
# "approximate" before its fee word go, and a clause promising to confirm the
# exact amount goes. A figure the records themselves qualify (the passport
# row's "approximately $450", a range, "from", "subject to change") is left
# alone, and so is a reply that also carries such a figure. Salary, levy and
# bond sentences are not our fee and are never touched.
# False = the reply as the node wrote it (the prompt lines swap with it).
FIXED_FEE_STATED_PLAINLY = True
_VARYING_FIGURE = re.compile(
    r"\b(?:approx\w*|about|around|roughly|estimat\w*|from|starts?|starting|subject\s+to\s+change"
    r"|var(?:y|ies|iable|ying)|depend\w*|up\s+to|between|typical\w*|ranges?|minimum|at\s+least"
    r"|or\s+more|guide)\b",
    re.IGNORECASE,
)
_SOFTENER_BEFORE_FIGURE = re.compile(
    r"\b(?:approximately|approximate|approx\.?|about|around|roughly)\s+(?=(?:S\$|SGD\s?|\$)\s?\d)",
    re.IGNORECASE,
)
_APPROXIMATE_FEE_WORD = re.compile(
    r"\b(?:the\s+)?approximate\s+(?=(?:[\w-]+\s+){0,4}?(?:fees?|costs?|prices?|charges?|amount)\b)",
    re.IGNORECASE,
)
_CONFIRMS_THE_FEE = re.compile(
    r"\bconfirm\w*\b[^.!?;,]*?\b(?:fees?|amount|costs?|figure|price|quotation)\b"
    r"|\b(?:exact|final)\s+(?:[\w-]+\s+)?(?:fees?|amount|costs?|figure|price)\b[^.!?;,]*\bconfirm\w*"
    r"|\b(?:fees?|amount|costs?|price)\b[^.!?;,]*\bdepends?\s+on\s+your\s+requirements",
    re.IGNORECASE,
)
_CONFIRM_AND = re.compile(
    r"\bconfirm\w*\s+(?:the\s+)?(?:exact\s+|final\s+)?(?:[\w-]+\s+)?(?:fees?|amount|costs?|figure|price)"
    r"(?:\s+(?:for\s+you|with\s+you|for\s+your\s+case))?\s+and\s+",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(r"(,\s*(?:and|but|so|which|while)?\s*|;\s*|\s+[-–—]\s+)")


# Phase 3 Step 5 Batch B2 item 4 (2026-10-14). Live: "DEVI's home leave" - the
# client typed the helper's name in capitals and every reply read it back that
# way. A name typed all in capitals or all in lower case is shown in title
# case ("Devi", "Mary-Jane", "O'Neil"); a mixed-case one ("McDonald", "de
# Silva") is the client's own spelling and is left exactly as typed. Applied
# where the name is filed (info_collector) and to the reply (run_turn). In a
# reply, an all-lower copy is only rewritten when the name is not also an
# everyday word, so "may" in "may I know" stays. False = names as typed.
TIDY_HELPER_NAME_CASE = True
_NAME_IS_A_WORD = frozenset({
    "may", "joy", "hope", "grace", "faith", "rose", "april", "june", "sunny", "honey",
    "lucky", "precious", "cherry", "lovely", "angel", "baby", "star", "dawn", "mercy",
    "charity", "patience", "summer", "will", "mark", "rich", "an", "ma", "ana",
})


def tidy_name_case(name: str) -> str:
    """'DEVI LAL' / 'devi lal' -> 'Devi Lal'; 'McDonald' unchanged."""
    text = (name or "").strip()
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 2:
        return text
    if not (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        return text
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), text)


def tidy_helper_names_in_reply(reply: str, names: list[str]) -> str:
    """'DEVI's home leave' -> 'Devi's home leave', for the helpers we know."""
    body = reply or ""
    forms = []
    for raw in names:
        full = tidy_name_case(str(raw or ""))
        forms.append(full)
        if " " in full:  # "DEVI" for "Devi Lal Singh"
            forms.append(full.split()[0])
    for tidy in forms:
        if len(tidy) < 2:
            continue
        pattern = re.compile(r"(?<![\w'])" + re.escape(tidy) + r"(?![\w])", re.IGNORECASE)

        def fix(match: re.Match, tidy=tidy) -> str:
            seen = match.group(0)
            if seen == tidy or tidy_name_case(seen) != tidy:
                return seen  # already right, or a mixed-case spelling of its own
            if seen.islower() and seen in _NAME_IS_A_WORD:
                return seen
            return tidy

        body = pattern.sub(fix, body)
    if body != reply:
        logger.info("Helper name case tidied in the reply")
    return body


# Phase 3 Step 5 Batch B2 item 5 (2026-10-14). A home leave for a family matter
# or an emergency gets one short caring line before we move on; a regular home
# leave does not. Only on the turn the client gives that reason, and only where
# the reply does not already carry one. False = the reply as written.
CARING_LINE_FOR_FAMILY_MATTERS = True
CARING_LINE = "I hope everything is alright at home."
_FAMILY_MATTER = re.compile(
    r"\bfamily\s+(?:matters?|emergenc\w*|problems?|issues?|reasons?|crisis)\b|\bemergenc\w*"
    r"|\burgent\w*|\b(?:sick|ill|unwell|hospital\w*|passed\s+away|died|death|dying|funeral"
    r"|accident|surgery|operation|critical|bereave\w*)\b",
    re.IGNORECASE,
)
_ALREADY_CARING = re.compile(
    r"\b(?:hope\s+(?:everything|all|she|he|they|your|her|his)|sorry\s+to\s+hear|take\s+care"
    r"|thinking\s+of)\b",
    re.IGNORECASE,
)


def with_caring_line(reply: str, message: str, leave_reason: str | None) -> str:
    """'I hope everything is alright at home.' before the reply, once, when the
    reason just given for a home leave is a family matter or an emergency."""
    if not reply or not leave_reason or not _FAMILY_MATTER.search(message or ""):
        return reply
    if not _FAMILY_MATTER.search(str(leave_reason)) or _ALREADY_CARING.search(reply):
        return reply
    logger.info("Home leave for a family matter - caring line added")
    return f"{CARING_LINE} {reply}"


# Phase 3 Step 5 Batch B2 item 3 (2026-10-14). Live: "why this much?" after
# the renewal fee was answered with the price again ("...the exact amount will
# be confirmed by our team"). Asking WHY a fee is what it is wants what it
# covers. Answered from the records when they say what it covers, else the
# agent line (blocked_topic_responder.PROBE_REPLY). A sentence that only
# restates a figure we already sent is never the answer.
# False = the reply as the node wrote it.
WHY_THE_PRICE_EXPLAINED = True
_PRICE_WORD = re.compile(
    r"\b(?:much|expensive|costly|pricey|high|fees?|costs?|prices?|charges?|pay|paying|rate)\b"
    r"|\$\s?\d",
    re.IGNORECASE,
)
_ASKS_WHAT_IT_COVERS = re.compile(
    r"\bwhat(?:'s|\s+is|\s+does|\s+do)\s+(?:(?:it|that|this|the\s+fee)\s+)?"
    r"(?:included|covered|cover|include)\b"
    r"|\bbreak(?:\s|-)?(?:it\s+)?down\b|\bbreakdown\b",
    re.IGNORECASE,
)
_WHY = re.compile(r"\bwhy\b|\bhow\s+come\b|\bexplain\b|\bjustif", re.IGNORECASE)
_EXPLAINS_COVERAGE = re.compile(
    r"\bcover(?:s|ed|ing)?\b|\binclud\w*|\bpays?\s+for\b|\bgoes\s+towards\b|\bmade\s+up\s+of\b"
    r"|\bfor\s+(?:us\s+|our\s+)?handling\b",
    re.IGNORECASE,
)


def asks_why_the_price(message: str) -> bool:
    """'why this much?', 'why so expensive', 'what does it cover'."""
    text = message or ""
    if _ASKS_WHAT_IT_COVERS.search(text):
        return True
    return bool(_WHY.search(text) and _PRICE_WORD.search(text))


def without_repeated_price(reply: str, *previous: str) -> str:
    """The reply to 'why this much?' minus every sentence that only restates a
    figure we already sent, or promises to confirm the amount. '' when what is
    left explains nothing - the caller then sends the agent line."""
    body = reply or ""
    sent = {_money_norm(m.group(1)) for line in previous for m in _MONEY.finditer(line or "")}
    kept = []
    for start, end in _sentence_spans(body):
        sentence = body[start:end]
        figures = {_money_norm(m.group(1)) for m in _MONEY.finditer(sentence)}
        if figures & sent and not _EXPLAINS_COVERAGE.search(sentence):
            continue
        if not figures and _CONFIRMS_THE_FEE.search(sentence) and not _EXPLAINS_COVERAGE.search(sentence):
            continue
        kept.append(sentence)
    out = " ".join(kept).strip()
    return out if _EXPLAINS_COVERAGE.search(out) else ""


def _figure_sentences(text: str) -> list[tuple[str, str]]:
    """(normalised figure, the sentence it sits in) for every money figure."""
    out = []
    for start, end in _sentence_spans(text or ""):
        sentence = text[start:end]
        for match in _MONEY.finditer(sentence):
            out.append((_money_norm(match.group(1)), sentence))
    return out


def fixed_fee_figures(records: str) -> set[str]:
    """Figures the records only ever state plainly, as a fee rather than a salary,
    levy or bond: never qualified, never part of a range."""
    seen: dict[str, bool] = {}
    for figure, sentence in _figure_sentences(records):
        if _NOT_OUR_FEE.search(sentence):
            continue
        ranged = re.search(
            r"(?:S\$|SGD\s?|\$)?\s?" + re.escape(figure) + r"(?:\.\d+)?\s*(?:[-–]|to)\s*(?:S\$|\$)?\d"
            r"|\d\s*(?:[-–]|to)\s*(?:S\$|SGD\s?|\$)\s?" + re.escape(figure),
            sentence.replace(",", ""),
        )
        plain = not (_VARYING_FIGURE.search(sentence) or ranged)
        seen[figure] = seen.get(figure, True) and plain
    return {figure for figure, plain in seen.items() if plain}


def state_fixed_fee_plainly(reply: str, records: str) -> str:
    """'The approximate fee is $695, and our team will confirm the exact amount.'
    -> 'The fee is $695.' - only for a figure the records state exactly."""
    body = reply or ""
    if not body or not records:
        return body
    fixed = fixed_fee_figures(records)
    if not fixed:
        return body
    fee_figures = {
        figure for figure, sentence in _figure_sentences(body) if not _NOT_OUR_FEE.search(sentence)
    }
    plain_here = fee_figures & fixed
    if not plain_here:
        return body
    every_figure_fixed = fee_figures <= fixed
    out = []
    cursor = 0
    for start, end in _sentence_spans(body):
        out.append(body[cursor:start])
        sentence = body[start:end]
        figures = {f for f, _ in _figure_sentences(sentence)}
        about_salary = bool(_NOT_OUR_FEE.search(sentence))
        if figures and not about_salary and figures <= fixed:
            sentence = _SOFTENER_BEFORE_FIGURE.sub("", sentence)
            sentence = _APPROXIMATE_FEE_WORD.sub(
                lambda m: "The " if m.group(0)[:1].isupper() else "the ", sentence)
            if every_figure_fixed and _CONFIRMS_THE_FEE.search(sentence):
                parts = _CLAUSE_SPLIT.split(sentence)
                kept = [parts[0]]
                for sep, clause in zip(parts[1::2], parts[2::2]):
                    if _CONFIRMS_THE_FEE.search(clause) and not _MONEY.search(clause):
                        continue
                    kept += [sep, clause]
                sentence = "".join(kept).rstrip(" ,;")
                if sentence and sentence[-1] not in ".!?":
                    sentence += "."
        elif not figures and every_figure_fixed and _CONFIRMS_THE_FEE.search(sentence):
            trimmed = _CONFIRM_AND.sub("", sentence)
            sentence = "" if _CONFIRMS_THE_FEE.search(trimmed) else trimmed
        out.append(sentence)
        cursor = end
    out.append(body[cursor:])
    result = "".join(out)
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"(?m)^[ \t]+(?=\S)", "", result)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if result != body:
        logger.info("Stated a fixed fee plainly: %r -> %r", body[:160], result[:160])
    return result or body


def client_lines(history_text: str) -> list[str]:
    """The client's own lines of a "Client:" / "You:" history."""
    return [line.split(":", 1)[1] for line in (history_text or "").splitlines()
            if line.startswith("Client:")]


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


# "How does this actually run, and what will I have to produce?" - the one
# question whose honest answer does not fit in two sentences. A new hire runs
# through five stages over several weeks, and "we handle the MOM application and
# then the embassy" is not an answer to it, it is a summary of one that leaves
# the client no better informed than before they asked. The callers use this to
# widen the sentence budget, and ONLY ever alongside retrieved records - an
# unanswerable process question still gets the holding line rather than a long
# improvisation.
# "What do I have to give you?" - asked four different ways in one conversation
# and answered none of them.
#
# Live, 2026-09-10, a job seeker with her registration parked. She wrote "Tell me
# the documents I needed" and got "I'll check with the team and come back to you
# shortly."; two messages later she wrote "No i ask for what are the documents I
# required" and was answered in full, from records that had been there the whole
# time. Her own reading of it: "if the bot knows the documents required, then why
# didn't it tell me when I said tell me the documents I needed".
#
# Measured, and it is worse than one unlucky turn: BOTH of those phrasings were
# False on this detector AND on asks_general_info. The old alternations wanted
# "what documents" adjacent ("what ARE THE documents" missed), or "documents
# needed" adjacent ("documents I needed" missed), and the imperative branch in
# _GENERAL_INFO knew only about a process ("tell me the documents" missed). So
# the one turn that DID work was the classifier happening to return
# document_question, and the deterministic net underneath it - the whole reason
# that net exists - caught neither. Same shape as the home-leave "what is the
# FURTHER process" gap on 2026-09-10 and the "what is THE cost" gap on
# 2026-09-08: a pattern written around the exact words somebody reported.
#
# One definition, read by both callers rather than copied into each (§9.8): it
# decides whether a numbered list is allowed HERE, and whether a parked topic
# answers rather than holding, and those two must never disagree about the same
# sentence again.
_DOCUMENT_NOUN = r"(?:documents?|papers?|paperwork|forms?|certificates?)"

_DOCUMENTS_QUESTION = re.compile(
    # "what documents", "which forms", "what all paperwork"
    rf"\b(?:what|which)\s+(?:all\s+)?{_DOCUMENT_NOUN}\b"
    # "what are the documents", "what's the paperwork"
    rf"|\bwhat(?:'?s|\s+is|\s+are)\s+(?:all\s+)?(?:the\s+)?{_DOCUMENT_NOUN}\b"
    # "tell me the documents", "just tell the forms"
    rf"|\btell\s+(?:me|us)?\s*(?:the\s+)?{_DOCUMENT_NOUN}\b"
    # "documents I needed", "documents are required", "papers I have to provide".
    # The pronoun and the auxiliary are optional and SEPARATE, which is the whole
    # difference between "documents needed" (matched before) and "documents I
    # needed" (did not).
    rf"|\b{_DOCUMENT_NOUN}\s+(?:i\s+|you\s+|we\s+|she\s+)?"
    r"(?:am\s+|are\s+|is\s+|will\s+|would\s+|should\s+|must\s+|"
    r"have\s+to\s+|need\s+to\s+)?"
    r"(?:need(?:ed|s)?|require[ds]?|provide|submit|bring|give|send)\b"
    # "do you need any documents from me", "do i need to send any papers"
    rf"|\bneed\s+(?:any\s+|some\s+)?{_DOCUMENT_NOUN}\b",
    re.IGNORECASE,
)


def asks_for_documents(text: str) -> bool:
    """Whether the client is asking what paperwork they have to produce.

    Deliberately shared by asks_for_process (may this reply be a list?) and by
    blocked_topic_responder.asks_general_info (does a parked topic answer this?)
    - see the note above for the conversation where those two disagreed.
    """
    return bool(_DOCUMENTS_QUESTION.search(text or ""))


_ASKS_FOR_PROCESS = re.compile(
    r"\bprocess(?:es)?\b|\bprocedures?\b|\bsteps?\b|\bstages?\b|\bphases?\b"
    r"|\bwalk\s+me\s+through\b|\bstep[-\s]?by[-\s]?step\b"
    r"|\bwhat\s+happens\b|\bhow\s+does\s+it\s+(?:work|go)\b"
    r"|\bwhat\s+(?:documents?|papers?|paperwork|forms?)\b"
    r"|\bdocuments?\s+(?:are\s+)?(?:needed|required)\b"
    r"|\bwhat\s+do\s+i\s+need\s+to\s+(?:do|provide|prepare|submit|bring|sign)\b",
    re.IGNORECASE,
)

# "process" is a verb far more often than a noun in this inbox - "can you help
# me process her paperwork" is a direct-hire request, not a question about how
# a hire runs. Excludes are tested FIRST, the same way _STILL_EMPLOYED does it,
# so a message cannot match on the very word that disqualifies it.
_PROCESS_AS_VERB = re.compile(
    r"\bprocess(?:ing|ed|es)?\s+(?:her|him|his|my|the|this|that|it|them|"
    r"a\b|an\b|for\b)",
    re.IGNORECASE,
)


# The plural counts: "can you explain to me the cost and the processes?" was
# not read as a process question (conversation 26, 2026-10-09), so a
# replacement enquiry skipped the explain-first path and either ignored the
# question or dropped the intake question.
#
# A determiner in front of it makes "process" a noun whatever follows, which
# is what separates "the process for hiring a helper" from "please process her
# application". Tested first, so the verb exclusion below cannot claim it.
_PROCESS_AS_NOUN = re.compile(
    r"\b(?:the|a|any|full|whole|entire|complete|further|next|overall|"
    r"hiring|application|renewal|transfer)\s+process(?:es)?\b",
    re.IGNORECASE,
)


def asks_for_process(text: str) -> bool:
    """Whether the client asked how something runs, or what they must produce.

    Narrow on purpose. It decides only whether a reply may run past the
    two-sentence cap, and the caller additionally requires that we actually
    retrieved something - so a false positive costs a slightly longer answer,
    never an invented one.

    The noun test runs BEFORE the verb test, because a determiner settles the
    question and the word after it does not: "the full process for hiring a
    helper" is a noun that happens to be followed by "for", and testing the
    verb first threw it out. Caught by smoke_nodes on 2026-09-08 - the most
    natural phrasing of the very question this function exists for was the one
    phrasing it rejected.
    """
    body = text or ""
    if _PROCESS_AS_NOUN.search(body):
        return True
    # Tested BEFORE the verb exclusion, not after: that exclusion is about the
    # word "process", and "can you process the documents I need to send" is
    # still a question about her documents. Vetoing it on somebody else's word
    # is how "the full process FOR hiring" was thrown out on 2026-09-08.
    if asks_for_documents(body):
        return True
    if _PROCESS_AS_VERB.search(body):
        return False
    return bool(_ASKS_FOR_PROCESS.search(body))


# A question about a helper's rights AS A WHOLE. Live 2026-10-12, conversation
# 4443: "i am helper i want to know my rights" was answered in one long
# sentence of seven rights, because only a process or documents question may
# run past two sentences. The agency asked for all ten as a list. "rights" on
# its own, beside my/her/your/what/helper, so "can my employer keep my
# passport" (one right, its own row) stays a short answer.
_ASKS_FOR_RIGHTS = re.compile(
    r"\b(?:my|her|your|our|their|what|which|the|helpers?'?s?|maids?'?s?|workers?'?s?)"
    # Plural only: "is this the right form" is not a rights question.
    r"\s+(?:basic\s+|main\s+|legal\s+)?rights\b",
    re.IGNORECASE,
)


def asks_for_rights(text: str) -> bool:
    """Whether the client is asking for a helper's rights as a whole."""
    return bool(_ASKS_FOR_RIGHTS.search(text or ""))


# The client telling us they have already asked, or that we never answered.
#
# Live, 2026-09-18 12:05: a work permit renewal collected four fields, raised a
# ticket and closed on the handover line without ever stating the fee the
# client opened with. They wrote "i have asked for the fees" and got "I've
# noted that you're asking about the renewal fees, and a live agent is handling
# it" - the second non-answer to the same question.
#
# It is deliberately NOT a question detector. `asks_something` already catches
# the word "fees"; what none of the existing patterns can see is that this is a
# SECOND attempt, which is the fact that decides whether acknowledging is a
# reasonable reply or the worst possible one. Nothing here anchors on a
# question mark or an interrogative opener, because a client re-asking rarely
# uses either - the whole point is that they have stopped asking and started
# complaining.
#
# First person only, and only about asking or being answered: "i asked" and
# "you never told me", never a bare "again". "My employer asked me to" carries
# "asked" and is not this, so the subject has to be "i" or the verb has to be
# aimed at "you".
_ASKS_AGAIN = re.compile(
    r"\bi\s+(?:have\s+|already\s+|had\s+|just\s+)?asked\b"
    r"|\bi\s+(?:have\s+)?(?:already\s+)?(?:told|said)\s+(?:you|u)\b"
    r"|\bas\s+i\s+(?:said|asked|mentioned)\b"
    r"|\byou\s+(?:never|did\s*n[o']?t|have\s*n[o']?t|has\s*n[o']?t|still\s+have\s*n[o']?t)"
    r"\s+(?:told|answer(?:ed)?|said|given|give|reply|replied|respond(?:ed)?)\b"
    r"|\bstill\s+(?:waiting|no\s+(?:answer|reply|response))\b"
    r"|\bmy\s+question\s+(?:is\s+)?(?:still\s+)?(?:un)?answer",
    re.IGNORECASE,
)


def asks_again(text: str) -> bool:
    """Whether the client is restating something we did not answer."""
    return bool(_ASKS_AGAIN.search(text or ""))


# Our own last line ended in a question, and what came back is not itself a
# question - so it is an ANSWER to us, whatever the classifier made of it.
#
# Live, 2026-09-07 (client screenshot). Mid new-hiring intake the bot asked
# "How many people live in your household, roughly 1-2, 3-4, 5-6, or 7 or
# more?" and the client replied "3-4". That turn came back:
#
#     "Hi, I'm Claire, Ming Hwee's AI assistant. Could you share what your
#      question is about?"
#
# - a re-introduction and a request for the question, in answer to the answer
# we had just asked for. The client had to retype "i would like to hire an
# helper" to restart the intake, and wrote: "chatbot seems to be glitching
# because i stated my request and it replied me with the correct follow up
# question of residence type, but once i respond it asks me for what my
# request is".
#
# closure.needs_no_reply() has held the mirror image of this rule from the
# start - it never silences a bare yes/no when our own last line contained a
# question mark. This is the same fact used the other way round.
def answering_our_question(history_text: str, message: str) -> bool:
    """Whether this message is an answer to a question we just asked."""
    previous = last_bot_line(history_text or "")
    if "?" not in (previous or ""):
        return False
    body = (message or "").strip()
    if not body:
        return False
    # A question back at us is a question, not an answer. Those are already
    # handled: the collector answers and then re-asks (ANSWER_THEN_ASK).
    return "?" not in body


# A numbered list marker at the start of a line. Only the marker's own full
# stop is masked, and only while counting sentences.
_LIST_MARKER = re.compile(r"(?m)^(\s*\d+)\.(\s)")


def clamp_reply(text: str, max_sentences: int = 2) -> str:
    """Trim self-directed notes and runaway length from an otherwise good reply.

    The truncation is logged with what was cut. A reply reading "Transfer helper.
    Let me check who's available locally and get back to you shortly." went to a
    client, and there was no way afterwards to tell whether the model wrote that
    fragment or this function made one by keeping two sentences out of a longer
    thought. Without the discarded tail in the log the question cannot be
    settled, and a fix chosen without settling it is a guess.
    """
    original = (text or "").strip()
    body = _TRAILING_NOTES.sub("", original).strip()

    # "1. We prepare your documents." carries a full stop after the marker as
    # well as at the end of the line, so a plain sentence split scores a
    # six-step answer as twelve sentences and the clamp deletes half of it.
    # Mask the marker's own dot for the COUNT only. The mask is the same length
    # as what it replaces, so offsets into it are offsets into `body`.
    masked = _LIST_MARKER.sub(lambda m: m.group(1) + " " + m.group(2), body)

    # Sliced rather than re-joined: joining on " " flattened every newline out
    # of a clamped reply, which turned a stepped answer into a wall of text on
    # exactly the replies that most needed the line breaks.
    breaks = [m.end() for m in re.finditer(r"(?<=[.!?])\s+", masked)]
    if len(breaks) >= max_sentences:
        cut = breaks[max_sentences - 1]
        dropped = body[cut:].strip()
        body = body[:cut].strip()
        logger.info(
            "Clamped reply to %d sentence(s): kept %r, dropped %r",
            max_sentences,
            body,
            dropped,
        )
    return body


# Claire talking about Ming Hwee as though it were somebody else. She is Ming
# Hwee's assistant, so "we sent it over to the agency, they have passed it up"
# is not a rephrasing — it is a different speaker — and "as instructed" is the
# prompt talking out loud.
#
# Live, 2026-09-02 19:44, after a client had asked three times why a question
# had been skipped: "When we took down the details you gave, we sent it all
# over to the agency as instructed. They have passed them up to a live agent to
# handle." Nothing else catches it: it is fluent, it is not degenerate, it
# names no colleague and promises no time, and it is not in brackets, so
# strip_meta_commentary never looks at it.
#
# "our team", "our agent" and "I've passed this to the team" are deliberately
# NOT matched — those are the first person and are exactly what rule 2 asks for.
_THIRD_PARTY_SELF = re.compile(
    r"\bas\s+instructed\b"
    r"|\b(?:sent|send|passed|forwarded|forward|escalated|escalate|gave|given|"
    r"handed|submitted|relayed)\b[^.!?]{0,40}\bto\s+(?:the\s+)?"
    r"(?:agency|agencies|office|company|firm)\b"
    r"|\bthe\s+agency\s+(?:has|have|will|then|they)\b",
    re.IGNORECASE,
)


def speaks_of_us_as_a_third_party(reply: str) -> bool:
    """Whether the reply describes Ming Hwee as somebody other than the speaker."""
    return bool(_THIRD_PARTY_SELF.search(reply or ""))


# The model narrating its own process to the client, UNBRACKETED — so
# strip_meta_commentary (which only cuts brackets) never sees it, and it is
# fluent so is_degenerate passes it too.
#
# Live, 2026-09-03: a salary-range question with nothing in the records. When
# retrieval is empty, format_context used to hand the model a block reading
# "(no relevant records found) ... You do not have information to answer this."
# Kimi copied it straight to the client: "The client asked about salary range.
# Records say 'no relevant records found', so I can't give a figure ... never
# quote a figure that isn't there. Should I ask them the range, or handle this
# together?" — its whole reply was internal monologue.
#
# Every marker below is language that can ONLY appear when the model is
# describing its own reasoning or reading an instruction back; a genuine reply
# to a client never contains any of them. So a match discards the whole reply in
# favour of the holding line — there is no good sentence to salvage out of it.
_INTERNAL_MONOLOGUE = re.compile(
    r"\bno\s+relevant\s+records?\s+found\b"
    r"|\byou\s+do\s+not\s+have\s+(?:the\s+)?information\s+to\s+answer\b"
    r"|\bthe\s+client\s+(?:asked|wants|said|mentioned|is\s+asking|needs|requested)\b"
    r"|\bshould\s+i\b[^?.!]*\bhandle\s+this\b"
    r"|\bnever\s+quote\b"
    r"|\bfigure\s+that\s+(?:isn'?t|is\s+not)\s+there\b",
    re.IGNORECASE,
)


def leaks_internal_reasoning(reply: str) -> bool:
    """Whether the reply is the model narrating its process rather than talking to the client."""
    return bool(_INTERNAL_MONOLOGUE.search(reply or ""))


# The name half on its own. A colleague we have named is never acceptable,
# wherever it appears - unlike the time half, which has a legitimate use.
_HANDOVER_NAME = re.compile(
    r"\b(transfer|connect|refer|forward|pass|put)\s+(you|this)\s+"
    r"(to|with|through\s+to)\s+(?!Ming\b)(?-i:[A-Z][a-z]+)",
    re.IGNORECASE,
)

# All that is left of a step whose only sentence was stripped: "2." on its own.
# _LIST_MARKER cannot be reused - it requires whitespace after the dot.
_BARE_MARKER = re.compile(r"^\s*\d+[.)]\s*$")

# Somebody getting in touch with them. What makes a TIME a promise the office
# has not made is that it is attached to one of these.
_CONTACT_PROMISE = re.compile(
    r"\b(call|ring|phone|contact|reach)\s+you\b"
    r"|\bget\s+back\s+to\s+you\b|\blive\s+agent\b|\b(?:consultant|agent)\s+will\b",
    re.IGNORECASE,
)


# A deadline the CLIENT has to meet, stated by a regulator, is not a callback
# time we are promising. Live 2026-09-29, replaying the runaway report: "please
# report it to the police and MOM within 24 hours" was deleted as though it
# were "a live agent will call you within 2 hours", and the employer was told
# nothing but "May I know your helper's name?" - the exact reply the agency had
# just objected to as giving up. The contact promise and the named colleague
# are still caught in the same sentence.
_REPORTING_DEADLINE = re.compile(r"\breport\w*\b[^.?!]*\b(?:police|MOM)\b"
                                 r"|\b(?:police|MOM)\b[^.?!]*\breport", re.IGNORECASE)


def strip_handover_talk(reply: str) -> str:
    """Remove sentences promising a named colleague or a specific time.

    Dropping only the offending sentence preserves the question the collector
    asked in the same message, which a wholesale replacement would lose. A
    sentence that merely says a live agent will pick this up is left alone —
    that is what we now want it to say.

    LINE STRUCTURE SURVIVES. It used to re-join on " ", which is the same
    defect clamp_reply had until 2026-09-08 and it lands on the same replies:
    live 2026-09-17, a first-contact process answer came back as
    "1. Consultation ... 2. 3. Interview ..." with every line break gone,
    because one step was dropped and the rest were glued into a paragraph.
    A numbered list a client cannot read is not a safer reply than the one
    this guard was protecting them from.

    AND A NUMBERED STEP KEEPS ITS TIMING, unless the step is about somebody
    contacting them. The same run lost "Shortlist — you'll receive 3 to 5
    matched profiles within 48 hours" — the agency's own published turnaround,
    grounded in the records and passed by ungrounded_figures, deleted as though
    it were an invented callback time. This guard exists for "Grace will call
    you back at 3pm", not for how long our own service takes. A step that DOES
    promise contact ("a live agent will call you within 2 hours") still goes.
    """
    text = (reply or "").strip()
    if not text or not mentions_handover(text):
        return text

    lines: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            lines.append(line)  # a blank line is layout, not a sentence
            continue
        step = bool(_LIST_MARKER.match(line))
        kept = []
        for sentence in _SENTENCE_SPLIT.split(line):
            if not sentence.strip():
                continue
            if step:
                offends = bool(_HANDOVER_NAME.search(sentence)) or (
                    mentions_handover(sentence)
                    and bool(_CONTACT_PROMISE.search(sentence))
                )
            elif _REPORTING_DEADLINE.search(sentence):
                offends = bool(_HANDOVER_NAME.search(sentence)) or bool(
                    _CONTACT_PROMISE.search(sentence)
                )
            else:
                offends = mentions_handover(sentence)
            if not offends:
                kept.append(sentence)
        rebuilt = " ".join(kept).strip()
        # A step whose content was stripped leaves a bare "2." behind,
        # which reads worse than the missing step. Drop the line.
        if step and _BARE_MARKER.match(rebuilt):
            continue
        lines.append(rebuilt)

    cleaned = "\n".join(lines).strip()
    if not cleaned.strip():
        cleaned = NEUTRAL_FOLLOW_UP
    if cleaned == text:
        return cleaned
    logger.warning("Removed handover announcement from reply: %r -> %r", text, cleaned)
    return cleaned


# --- Is this message ONLY a greeting? ----------------------------------------
#
# Lives here, not in the node that first needed it, because two different
# places have to agree about it: `response_generator`, which picks the reply,
# and `system.build_system_prompt`, which tells the model whether it may greet
# at all. They disagreed - the prompt said "the conversation is already going,
# do NOT greet again" on every turn with history, which is right when the
# client asked something and wrong when the client's whole message IS "Hi".
# §9.8's rule: one definition, two readers, never a second copy.
#
# Beyond this length it is a real message whatever the wording.
_NO_REQUEST_MAX_WORDS = 8

_GREETING = re.compile(
    r"^\W*(hi+|hey+|hello+|yo|halo|helo|greetings|"
    r"good\s+(morning|afternoon|evening|day))\b(\s+there)?[\s,.!\-]*",
    re.IGNORECASE,
)


def without_greeting(body: str) -> str:
    """What is left once every leading greeting is taken off.

    Stripped in a LOOP, not once: "hello, good morning" left "good morning"
    behind, which matches no announcement, so a client who greeted us twice
    fell through as though they had asked something.
    """
    rest = (body or "").strip()
    while True:
        stripped = _GREETING.sub("", rest, count=1).strip()
        if stripped == rest:
            return rest
        rest = stripped


def greeting_only(text: str) -> bool:
    """A greeting and nothing else - nothing asked, no question announced.

    What is left may be an emoji and still be nothing: "Hi 👋" is how people
    greet on WhatsApp, and live on 2026-09-29 it read as a request because the
    wave survived the strip. A question mark is NOT nothing - "hello??" is
    somebody asking whether anyone is there, which has its own reply.
    """
    body = (text or "").strip()
    if not body or len(body.split()) > _NO_REQUEST_MAX_WORDS:
        return False
    return bool(_GREETING.match(body)) and not re.search(r"[\w?]", without_greeting(body))
