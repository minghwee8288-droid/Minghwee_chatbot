"""Checks every draft before it reaches a consultant.

A draft is kept only if it passes all of these, in order:

1. schema - a label, a non-empty text, kb_refs as a list, confidence high|low;
2. no "Claire" and no self-identification as an AI, bot or assistant in the
   model's own words; NRIC/FIN numbers redacted;
3. the bot's own node-level guards (response_generator), where a guard that
   would replace the bot's reply with a holding line DROPS the draft instead;
4. every kb_ref is the id of a row actually retrieved for this request;
5. grounding: every S$ amount and every day/week/month/year count appears in
   the cited records or the conversation;
5b. no phone number and no "contact Ming Hwee" (they are talking to us); no
   nationality, language, religion, age or skill nobody in the conversation
   mentioned (a Clarify that guesses a nationality is replaced by a neutral
   "Which helper are you referring to?"); no case status or completed action
   ("we received", "approved", "in order") that our messages or the cited
   records do not state - the client's own words do not count;
6. the bot's post-graph rewriting guards (run_turn), in the same order;
7. the identity check again, on the final text - the drafts are sent by a
   human consultant - and no handover to "a live agent" (the consultant is
   one), including the line a bot guard swaps in for a reply it rejects;
8. not cut by the guards: shortened by more than 40%, or ending mid-thought;
9. at most 700 characters, not a duplicate of another draft.

Kept drafts then carry "flags", and any flag sets confidence "low":
- "unverified_figures": a number neither a cited record nor our own messages
  state (one only the client wrote, or one nobody wrote);
- "weak_support": an Answer whose key claim words are mostly not in the
  records it cites (a simple keyword check);
- "nationality_unconfirmed": it cites a record labelled for one helper
  nationality while the conversation's nationality is unknown or another one.
  A draft QUOTING a fee that belongs to another (or an unconfirmed)
  nationality is dropped instead, and a Clarify asking which helper is offered;
- "unverified_commitment": a day or deadline ("tomorrow", "by Friday", "within
  3 days") that the client's latest message (for a relative day) or our
  messages and the cited records (for a deadline) do not give.

At most three survive. Drops are returned with their reason and never logged
with their text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.graph import guards
from app.graph import language
from app.graph.prompts.system import IDENTITY
from app.services import kb_rules
from app.services.lead import nationality_code, nationality_for_fee
from app.suggest import nationality as nat
from app.suggest.retrieval import ref_of, text_of
from app.utils import redact_nric

MAX_CHARS = 700
MAX_DRAFTS = 3
LABELS = {"Answer", "Clarify", "Next step"}

# --- Identity ---------------------------------------------------------------

_CLAIRE = re.compile(r"\bclaire\b", re.IGNORECASE)
_AI_SELF = re.compile(
    r"\bartificial intelligence\b"
    r"|\bchat\s?-?bot\b|\bbot\b"
    r"|\blanguage model\b"
    r"|\b(?:virtual|digital|automated|ai)\s+assistant\b"
    r"|\bI(?:'m|\s+am)\s+(?:an?\s+|ming hwee'?s\s+|your\s+)?(?:\w+\s+)?assistant\b",
    re.IGNORECASE,
)
# "AI" only in capitals: lower-case "ai" is a word in some of our clients'
# languages, and no consultant needs the abbreviation.
_AI_CAPS = re.compile(r"\bA\.?I\b")


def identifies_as_claire_or_ai(text: str) -> bool:
    """A draft a human sends must not name Claire or claim to be an AI/bot/assistant."""
    if _CLAIRE.search(text) or _AI_CAPS.search(text):
        return True
    return bool(_AI_SELF.search(text))


# Claire's handover lines. The consultant sending the draft IS the live agent,
# so "I've passed this to our team and a live agent will connect with you
# shortly" is nonsense from them. Live, 2026-10-27 (eval, conversation 59): the
# bot's strip_handover_talk replaced a draft naming a colleague and a time with
# guards.NEUTRAL_FOLLOW_UP, and that line was kept as a draft.
_HANDOVER = re.compile(
    r"\blive agent\b"
    r"|\bconnect with you shortly\b"
    r"|\b(?:passed|handed)\s+(?:this|it|that)\s+(?:on\s+)?to\s+our\s+team\b",
    re.IGNORECASE,
)


def announces_handover(text: str) -> bool:
    return bool(_HANDOVER.search(text))


# --- Cut by a guard -----------------------------------------------------------
# Live, 2026-10-27 (eval, conversation 4633): a guard trimmed a draft to
# "Hi Geraldine, thank you for completing our form." - a greeting with the
# substance gone, still offered as a "Next step". A cut draft is dropped, never
# offered in its cut form.

MAX_SHRINK = 0.4
_MID_THOUGHT = re.compile(
    r"(?:[,;:\-–—(]|\b(?:and|or|but|so|to|the|a|an|of|for|with|that|which|because|if|as))\s*$",
    re.IGNORECASE,
)


def cut_by_guards(before: str, after: str) -> bool:
    """The guards removed more than 40% of the draft, or left it ending mid-thought."""
    before, after = before.strip(), after.strip()
    if before and len(after) < (1 - MAX_SHRINK) * len(before):
        return True
    return after != before and bool(_MID_THOUGHT.search(after))


# --- Grounding --------------------------------------------------------------

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_MONEY = re.compile(
    rf"(?:S\$|SGD\s?|\$)\s?({_NUMBER})|({_NUMBER})\s?(?:SGD|dollars?)\b", re.IGNORECASE
)
_DURATION = re.compile(
    rf"({_NUMBER})(?:\s*(?:-|–|to)\s*({_NUMBER}))?\s*(?:working\s+|business\s+|calendar\s+)?"
    r"(?:day|week|month|year)s?\b",
    re.IGNORECASE,
)
_ANY_NUMBER = re.compile(_NUMBER)


def _norm(number: str) -> str:
    value = number.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def figures_in(text: str) -> set[str]:
    """Every S$ amount and every day/week/month/year count in a text, normalised."""
    found: set[str] = set()
    for match in _MONEY.finditer(text or ""):
        found.add(_norm(match.group(1) or match.group(2)))
    for match in _DURATION.finditer(text or ""):
        found.add(_norm(match.group(1)))
        if match.group(2):
            found.add(_norm(match.group(2)))
    return {f for f in found if f}


def numbers_in(text: str) -> set[str]:
    return {_norm(n) for n in _ANY_NUMBER.findall(text or "")}


def money_in(text: str) -> set[str]:
    return {_norm(m.group(1) or m.group(2)) for m in _MONEY.finditer(text or "")}


def ungrounded(text: str, sources: str) -> set[str]:
    """Figures in the draft that no source states."""
    return figures_in(text) - numbers_in(sources)


# --- Flags: kept, but marked for the consultant ---------------------------------

UNVERIFIED_FIGURES = "unverified_figures"
WEAK_SUPPORT = "weak_support"

_SPEAKER = re.compile(r"^(Client|You|Agent):\s?")


def split_speakers(history_text: str, incoming_text: str) -> tuple[str, str]:
    """(what the client wrote, what we wrote) from the rendered transcript. A
    line with no prefix continues the message above it."""
    client: list[str] = [incoming_text or ""]
    ours: list[str] = []
    current = client
    for line in (history_text or "").splitlines():
        match = _SPEAKER.match(line)
        if match:
            current = client if match.group(1) == "Client" else ours
            line = line[match.end():]
        current.append(line)
    return "\n".join(client), "\n".join(ours)


def unverified_numbers(text: str, cited: str, ours: str) -> set[str]:
    """Every number in the draft that neither a cited record nor one of our own
    messages (Claire's or staff's) states. That covers a number only the client
    wrote - live, 2026-10-27, a draft "explained" the client's "$52 or $78" -
    and a count nobody wrote at all ("2 rest days" in the same draft), which
    the money/duration grounding check does not see."""
    return numbers_in(text) - numbers_in(cited) - numbers_in(ours)


_CONTENT = re.compile(r"[a-z][a-z']{3,}")
_COMMON = set("""
about after again also been before being both cannot come could does done each even every
from give going have here into just know like made make many more most much need once only
other over please same should some such than that their them then there these they this those
through very want well were what when where which while will with within would your yours
yeah sure okay noted thank thanks help check confirm update let know able
""".split())


def _stem(word: str) -> str:
    return word.rstrip("'s")[:5]


def claim_words(text: str, client_text: str) -> set[str]:
    """The draft's key claim words: content words that are not filler and not
    simply the client's own words handed back to them."""
    echoed = {_stem(w) for w in _CONTENT.findall(client_text.lower())}
    return {
        _stem(w) for w in _CONTENT.findall(text.lower())
        if w not in _COMMON and _stem(w) not in echoed
    }


def weakly_supported(text: str, cited: str, client_text: str) -> bool:
    """An Answer whose claim words are mostly absent from the records it cites.
    A simple keyword check, not a judgement of truth: it catches the "Yes, we
    can start the renewal while..." shape, where the records hold the topic but
    not the claim."""
    claims = claim_words(text, client_text)
    if not claims:
        return False
    if not cited.strip():
        return True
    present = {_stem(w) for w in _CONTENT.findall(cited.lower())}
    return len(claims & present) / len(claims) < SUPPORT_THRESHOLD


SUPPORT_THRESHOLD = 0.5

NATIONALITY_UNCONFIRMED = "nationality_unconfirmed"
CLARIFY_NATIONALITY = (
    "Which helper is this for, and what is her nationality? The fees differ by "
    "nationality, so I'll confirm the right amount once I know."
)


def nationality_issue(text: str, cited: str, cited_matches: list[dict[str, Any]],
                      in_play: str | None) -> str:
    """"drop", "flag" or "". Drop: the draft quotes a fee the cited records give
    to a nationality, and the conversation's nationality is unknown or not that
    one. Flag: it cites a nationality-specific record under the same doubt but
    quotes no fee from it."""
    owners = {_norm(k): v for k, v in guards.fee_owners(cited, matches=cited_matches).items()}
    quoted = [owners[f] for f in money_in(text) if f in owners]
    if quoted and (in_play is None or any(in_play not in codes for codes in quoted)):
        return "drop"
    specific: set[str] = set()
    for match in cited_matches:
        specific |= nat.specific_codes(match)
    if specific and (in_play is None or in_play not in specific):
        return "flag"
    return ""


# --- Invented specifics (Thomas's review, 2026-10-28) ------------------------
# A draft a consultant may send unchanged must not claim what nobody said.
# The client's own words do not count as evidence for a status or a deadline
# (the same rule as a number only the client wrote): "Please confirm the
# documents are in order" is a question, not a fact (739).

UNVERIFIED_COMMITMENT = "unverified_commitment"
CLARIFY_WHICH_HELPER = "Which helper are you referring to?"

_STATUS_CLAIM = re.compile(
    r"\b(?:(?:we|i)(?:'ve|\s+have)?\s+(?:already\s+)?(?P<a>received|approved|processed|submitted|"
    r"completed|sent)"
    r"|(?:has|have|had|was|were|is|are)\s+(?:already\s+|now\s+)?(?:been\s+)?(?P<b>received|approved|"
    r"processed|submitted|completed|issued|endorsed)"
    r"|(?:documents?|papers?|forms?|everything)\s+(?:are\s+|is\s+)?(?:all\s+)?(?P<c>in\s+order))\b",
    re.IGNORECASE,
)

# A day relative to "now" is only safe when the client's latest message says
# it: an earlier one may already have passed (194: staff wrote "tomorrow" the
# day before, and a draft repeated it).
_RELATIVE_DAY = re.compile(r"\b(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|week))\b",
                           re.IGNORECASE)
_DAYS = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_COUNT = r"(?:\d+|a|an|one|two|three|four|five|six|seven|ten|a\s+few|few)"
_DEADLINE = re.compile(
    rf"\b(?:(?:by|before|on|this|next)\s+{_DAYS}|by\s+(?:the\s+)?end\s+of\s+(?:the\s+)?(?:day|week|month)"
    rf"|next\s+week|(?:within|in)\s+(?:about\s+|around\s+|approximately\s+)?(?P<n>{_COUNT}\s+"
    rf"(?:working\s+|business\s+)?(?:days?|hours?|weeks?)))\b",
    re.IGNORECASE,
)

_LANGUAGES = ("tamil", "mandarin", "chinese", "cantonese", "hokkien", "teochew", "malay", "hindi",
              "tagalog", "bahasa", "burmese", "bengali", "punjabi")
_RELIGIONS = ("christian", "muslim", "islam", "catholic", "hindu", "buddhist")
_SKILLS = ("cooking", "baking", "infant care", "newborn", "elderly care", "eldercare", "childcare",
           "pet care", "driving")
_AGE = re.compile(r"\b(?:\d{2}\s*(?:years?|yrs?)\s*old|aged\s+\d{2})\b", re.IGNORECASE)

_PHONE = re.compile(r"\[phone|\+?\d[\d\s().-]{6,}\d")
_CONTACT_US = re.compile(
    r"\b(?:contact|call|whatsapp|message|text|reach(?:\s+out\s+to)?|get\s+in\s+touch\s+with|"
    r"speak\s+(?:to|with))\s+(?:ming\s+hwee|the\s+agency|our\s+(?:office|hotline|agency)|"
    r"us\s+(?:at|on|via))\b",
    re.IGNORECASE,
)


def _has(word: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(word)}", text, re.IGNORECASE) is not None


def invented_status(text: str, ours: str, cited: str) -> list[str]:
    """Status or completed-action claims that neither our own messages nor the
    cited records make."""
    sources = f"{ours}\n{cited}".lower()
    missing = []
    for match in _STATUS_CLAIM.finditer(text):
        key = " ".join((match.group("a") or match.group("b") or match.group("c")).lower().split())
        stem = key[:-2] if key.endswith("ed") else key
        if stem not in " ".join(sources.split()):
            missing.append(match.group(0))
    return missing


def unverified_commitments(text: str, incoming: str, ours: str, cited: str) -> list[str]:
    """Days and deadlines the draft commits to that nothing gives: a relative
    day the client's latest message does not say, or a deadline our messages
    and the records do not state."""
    found = [m.group(0) for m in _RELATIVE_DAY.finditer(text) if not _has(m.group(0), incoming)]
    sources = " ".join(f"{incoming}\n{ours}\n{cited}".lower().split())
    for match in _DEADLINE.finditer(text):
        core = match.group("n") or match.group(0)
        if " ".join(core.lower().split()) not in sources:
            found.append(match.group(0))
    return found


def added_attributes(text: str, conversation: str) -> list[str]:
    """Nationality, language, religion, age or skill words in the draft that
    nobody in the conversation used."""
    added = [code for code in sorted(nat.named(text) - nat.named(conversation))]
    for word in _LANGUAGES + _RELIGIONS + _SKILLS:
        if _has(word, text) and not _has(word, conversation):
            added.append(word)
    added += [m.group(0) for m in _AGE.finditer(text) if m.group(0).lower() not in conversation.lower()]
    return added


# 4615: staff answered "Sorry, we do nit have" (a Tamil speaker), and the
# draft promised to check and come back anyway.
_WE_HAVE_NONE = re.compile(r"\b(?:do\s+n\w*t|don'?t|doesn'?t)\s+have\b|\bnot\s+available\b|"
                           r"\bnone\s+available\b|\bno\s+(?:such\s+|suitable\s+)?helpers?\b",
                           re.IGNORECASE)
_WILL_LOOK = re.compile(r"\b(?:check|see|look)\s+(?:if|whether)\s+(?:we|there)\b|"
                        r"\blet\s+you\s+know\s+(?:if|when|once)\b|\blook\s+out\s+for\b|"
                        r"\bkeep\s+(?:this|it|you)\s+in\s+mind\b",
                        re.IGNORECASE)


def last_message_of_ours(history_text: str) -> str:
    """Our most recent message (staff or Claire) in the rendered transcript."""
    block: list[str] = []
    for line in reversed((history_text or "").splitlines()):
        match = _SPEAKER.match(line)
        if not match:
            block.insert(0, line)
            continue
        if match.group(1) in ("Agent", "You"):
            return "\n".join([line[match.end():]] + block)
        block = []
    return ""


def promises_after_we_said_no(text: str, history_text: str) -> bool:
    return bool(_WE_HAVE_NONE.search(last_message_of_ours(history_text)) and _WILL_LOOK.search(text))


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


_AT_PHONE = re.compile(r"\s+(?:at|on)\s+(\+?\d[\d\s().-]{6,}\d)")


def phone_to_reference(text: str, client_text: str) -> str:
    """A number the client just gave us, written back as "at/on <number>",
    becomes "on that number" (194: "call my wife number ..."). Dropping such a
    draft left the consultant with nothing; any other number still drops it."""
    client_digits = _digits(client_text)

    def swap(match: re.Match[str]) -> str:
        digits = _digits(match.group(1))
        return " on that number" if len(digits) >= 8 and digits in client_digits else match.group(0)

    return _AT_PHONE.sub(swap, text)


def has_phone_number(text: str) -> bool:
    return any(sum(ch.isdigit() for ch in m.group(0)) >= 8 or m.group(0).startswith("[phone")
               for m in _PHONE.finditer(text)
               if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", m.group(0).strip()))


def tells_client_to_contact_us(text: str) -> bool:
    return _CONTACT_US.search(text) is not None


# --- Result -----------------------------------------------------------------

@dataclass
class Validation:
    kept: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)  # {label, reason}


# The records are shown to the model as "[ref <id>]", and it cites them in
# that shape ("ref <id>", "[ref <id>]") as often as bare. Live, 2026-10-27:
# every draft on two conversations cited "ref 6c143f15-..." and was dropped
# as citing an unknown record.
_REF_WRAPPER = re.compile(r"^\[?\s*(?:ref\s*[:#]?\s*)?(.*?)\s*\]?$", re.IGNORECASE)


def _ref_id(ref: Any) -> str:
    match = _REF_WRAPPER.match(str(ref).strip())
    return match.group(1).strip() if match else ""


def _schema(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    label = item.get("label") if isinstance(item.get("label"), str) else ""
    refs = item.get("kb_refs") or []
    if not isinstance(refs, list):
        return None
    confidence = item.get("confidence")
    return {
        "label": label.strip() if label.strip() in LABELS else (label.strip() or "Draft"),
        "text": text.strip(),
        "kb_refs": [_ref_id(r) for r in refs if _ref_id(r)],
        "confidence": confidence if confidence in ("high", "low") else "low",
    }


def _node_guards(text: str, state: dict[str, Any], rag_context: str,
                 ours: str = "") -> tuple[str, str]:
    """(text, drop reason). The checks response_generator runs on a reply
    (response_generator.py L194-L211 and L473-L612 at commit c138f91), with
    "replace with a holding line" turned into "drop the draft".

    One departure: the figure guard also grounds on `ours`, our own earlier
    messages in this conversation. Live, 2026-10-27 (eval, conversation 4641):
    staff had quoted "Agency Fee $1,388", and a draft repeating it was dropped
    as "figure not in the records". A fee we have already told this client is
    one the consultant may repeat."""
    text = guards.strip_meta_commentary(text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    text = guards.strip_handover_talk(text)
    if not text:
        return "", "empty after cleaning"
    if guards.is_degenerate(text):
        return "", "degenerate"
    if guards.leaks_internal_reasoning(text):
        return "", "leaks internal reasoning"
    if guards.speaks_of_us_as_a_third_party(text):
        return "", "speaks of Ming Hwee as a third party"
    service = state.get("service_type")
    if service in kb_rules.cost_withheld_services() and guards.quotes_hiring_package_cost(text):
        return "", "quotes a withheld hiring cost"
    incoming = state.get("incoming_text", "")
    if guards.ungrounded_figures(text, rag_context, IDENTITY, incoming, ours):
        return "", "figure not in the records"
    text, _cut = guards.guard_nationality_fee(
        text,
        service,
        nationality_for_fee(
            state.get("collected_info"), incoming, state.get("blocked_topics"), service
        ),
        rag_context,
        state.get("history_text", ""),
        matches=state.get("rag_matches") or (),
    )
    if guards.looks_like_document(text, allow_steps=guards.asks_for_process(incoming)):
        return "", "formatted as a document"
    return text, ""


def _post_guards(text: str, result: dict[str, Any], payload: dict[str, Any]) -> str:
    """The rewriting guards run_turn applies to every reply.

    Copied from app/graph/graph.py L561-L630 at commit c138f91. Keep in sync.
    """
    from app.graph import graph as graph_module
    from app.graph import reference as _reference

    if graph_module.NO_TAG_BEFORE_POSSESSIVE and text:
        text = guards.without_tag_before_possessive(text)
    if guards.STRAY_SCRIPT_GUARD and text:
        text = guards.without_stray_script(text, *graph_module.stray_script_context(payload))
    if guards.FIRST_HELPER_CONDITIONAL and text:
        text = guards.without_assumed_first_helper(
            text, *graph_module.stray_script_context(payload)
        )
    if guards.NO_APPROXIMATE_COST_ANYWHERE and text:
        text = guards.without_figureless_approximate(text)
    if guards.FIXED_FEE_STATED_PLAINLY and text:
        text = guards.state_fixed_fee_plainly(text, graph_module.records_text(result))
    if text and result.get("service_type") == "direct_hiring":
        text = guards.with_after_mom_approval(
            text, nationality_code(str(
                (result.get("collected_info") or {}).get("helper_nationality") or "")))
    if (guards.WHY_THE_PRICE_EVERY_PATH and text
            and guards.asks_why_the_price(payload.get("incoming_text") or "")):
        text = guards.price_probe_reply(
            text, payload.get("history_text") or "", graph_module.PROBE_REPLY
        )
    if guards.TIDY_HELPER_NAME_CASE and text:
        text = guards.tidy_helper_names_in_reply(
            text, graph_module.helper_names(result, payload)
        )
    if guards.NO_REINTRODUCTION and text:
        text = guards.without_reintroduction(
            text, payload.get("history_text") or "", payload.get("incoming_text") or ""
        )
    if (guards.CONDITIONAL_FEE_ONLY_IF_ASKED and text
            and result.get("service_type") == "home_leave"):
        text = guards.without_unasked_conditional_fee(
            text, *graph_module.stray_script_context(payload)[:-2]
        )
    if (guards.CARING_LINE_FOR_FAMILY_MATTERS and text
            and result.get("service_type") == "home_leave"):
        text = guards.with_caring_line(
            text, payload.get("incoming_text") or "",
            (result.get("collected_info") or {}).get("leave_reason"),
        )
    if (language.INDONESIAN_NOT_MALAY and text
            and language.client_writes_indonesian(
                payload.get("incoming_text") or "", payload.get("history_text") or "")):
        text = language.to_indonesian(text)
    if guards.NO_CASE_NUMBER_READ_OUT and text:
        text = guards.without_case_numbers(text, guards.case_numbers_held(payload, result))
    if _reference.REFERENCE_AS_CODE and text:
        text = guards.references_as_code(text)
    return text


def validate(
    raw: Any,
    *,
    state: dict[str, Any],
    payload: dict[str, Any],
    matches: list[dict[str, Any]],
    rag_context: str,
) -> Validation:
    """Check every draft the model returned. `state` must already carry
    rag_matches / rag_context for this request."""
    result = Validation()
    items = raw.get("suggestions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return result

    by_ref = {ref_of(m): m for m in matches if ref_of(m)}
    conversation = "\n".join([state.get("history_text") or "", state.get("incoming_text") or ""])
    client_text, ours = split_speakers(state.get("history_text") or "", state.get("incoming_text") or "")
    in_play = nat.in_play(state, ours)
    known_by_us = nat.identified_by_us(state, ours)
    nationality_doubt = False
    neutral_clarify = False
    seen: set[str] = set()

    for item in items:
        draft = _schema(item)
        if draft is None:
            result.dropped.append({"label": "", "reason": "invalid schema"})
            continue
        label = draft["label"]
        # Checked on the model's own words first: the bot's re-introduction
        # guard below would quietly cut "Hi, I'm Claire." and keep the rest,
        # and a draft that opened as Claire is not one to hand a consultant.
        if identifies_as_claire_or_ai(draft["text"]):
            result.dropped.append({"label": label, "reason": "names Claire or claims to be an AI"})
            continue
        text = redact_nric(draft["text"], context="suggest:draft")
        written = text

        text, reason = _node_guards(text, state, rag_context, ours)
        if reason:
            result.dropped.append({"label": label, "reason": reason})
            continue

        unknown_refs = [r for r in draft["kb_refs"] if r not in by_ref]
        if unknown_refs:
            result.dropped.append({"label": label, "reason": "cites a record that was not retrieved"})
            continue

        cited = "\n".join(text_of(by_ref[r]) for r in draft["kb_refs"])
        missing = ungrounded(text, cited + "\n" + conversation)
        if missing:
            result.dropped.append({
                "label": label,
                "reason": "figure not in the cited records or the conversation: "
                + ", ".join(sorted(missing)),
            })
            continue

        text = phone_to_reference(text, state.get("incoming_text") or "")
        if has_phone_number(text) or tells_client_to_contact_us(text):
            result.dropped.append({"label": label, "reason": "gives a phone number or tells the client "
                                   "to contact us"})
            continue
        # A Clarify that guesses the nationality (4645: "the Indonesian new
        # hire or transfer?" in a chat about Filipino and Myanmar helpers).
        if label == "Clarify" and in_play is None and nat.named(text):
            neutral_clarify = True
            result.dropped.append({"label": label, "reason": "Clarify assumes a nationality"})
            continue
        added = added_attributes(text, conversation)
        if added:
            if label == "Clarify":
                neutral_clarify = True
            result.dropped.append({"label": label, "reason": "adds a detail nobody mentioned: "
                                   + ", ".join(added)})
            continue
        if promises_after_we_said_no(text, state.get("history_text") or ""):
            result.dropped.append({"label": label, "reason": "promises to look again after our last "
                                   "message said we do not have it"})
            continue
        claimed = invented_status(text, ours, cited)
        if claimed:
            result.dropped.append({"label": label, "reason": "states a status nobody gave: "
                                   + "; ".join(claimed)})
            continue

        cited_matches = [by_ref[r] for r in draft["kb_refs"]]
        issue = nationality_issue(text, cited, cited_matches, in_play)
        if issue == "drop":
            nationality_doubt = True
            result.dropped.append({"label": label, "reason": "quotes a fee for another or an unconfirmed "
                                   "helper nationality"})
            continue

        text = _post_guards(text, state, payload).strip()
        text = redact_nric(text, context="suggest:draft")

        if not text:
            result.dropped.append({"label": label, "reason": "empty after guards"})
            continue
        if cut_by_guards(written, text):
            result.dropped.append({"label": label, "reason": "cut short by the guards"})
            continue
        if identifies_as_claire_or_ai(text):
            result.dropped.append({"label": label, "reason": "names Claire or claims to be an AI"})
            continue
        if announces_handover(text):
            result.dropped.append({"label": label, "reason": "hands over to a live agent"})
            continue
        if len(text) > MAX_CHARS:
            result.dropped.append({"label": label, "reason": f"over {MAX_CHARS} characters"})
            continue
        key = " ".join(text.lower().split())
        if key in seen:
            result.dropped.append({"label": label, "reason": "duplicate"})
            continue
        seen.add(key)

        flags: list[str] = []
        if unverified_numbers(text, cited, ours):
            flags.append(UNVERIFIED_FIGURES)
        if draft["label"] == "Answer" and weakly_supported(text, cited, client_text):
            flags.append(WEAK_SUPPORT)
        if issue == "flag":
            nationality_doubt = True
            flags.append(NATIONALITY_UNCONFIRMED)
        if unverified_commitments(text, state.get("incoming_text") or "", ours, cited):
            flags.append(UNVERIFIED_COMMITMENT)
        confidence = "low" if flags else draft["confidence"]
        result.kept.append({**draft, "text": text, "confidence": confidence, "flags": flags})
        if len(result.kept) == MAX_DRAFTS:
            break

    # Doubt about the nationality: make sure the consultant has the question
    # that settles it. A fixed English line, so it needs no guard.
    # A Clarify was dropped for guessing a nationality: ask neutrally instead.
    if neutral_clarify and not any(d["label"] == "Clarify" for d in result.kept):
        clarify = {"label": "Clarify", "text": CLARIFY_WHICH_HELPER, "kb_refs": [],
                   "confidence": "high", "flags": []}
        if len(result.kept) == MAX_DRAFTS:
            result.kept[-1] = clarify
        else:
            result.kept.append(clarify)
    # Not when our own messages already identified the helper (4641).
    if nationality_doubt and not known_by_us and not any(d["label"] == "Clarify" for d in result.kept):
        clarify = {"label": "Clarify", "text": CLARIFY_NATIONALITY, "kb_refs": [],
                   "confidence": "low", "flags": [NATIONALITY_UNCONFIRMED]}
        if len(result.kept) == MAX_DRAFTS:
            result.kept[-1] = clarify
        else:
            result.kept.append(clarify)
    return result
