"""Node 4 — collect the basic info a service request needs.

Asks one question at a time, in the agency's own voice. The client should not
feel like they are filling in a form.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.graph.guards import (
    COST_DEFERRAL_REPLY,
    COST_WITHHELD_SERVICES,
    clamp_reply,
    quotes_hiring_package_cost,
    is_degenerate,
    last_bot_line,
    leaks_internal_reasoning,
    looks_like_document,
    near_duplicate,
    recent_bot_lines,
    same_opening,
    speaks_of_us_as_a_third_party,
    strip_handover_talk,
    strip_meta_commentary,
    strip_repeated_opener,
    ungrounded_figures,
)
from app.graph.llm import complete, complete_json
from app.graph.prompts.system import build_system_prompt
from app.graph.prompts.templates import (
    ACKNOWLEDGE_ONLY_INSTRUCTION,
    ANSWER_THEN_ASK_INSTRUCTION,
    COLLECTOR_INSTRUCTION,
    EXTRACTION_SYSTEM,
    EXTRACTION_USER,
    FEE_HANDOVER_INSTRUCTION,
    HANDOVER_CLOSER_INSTRUCTION,
)
from app.graph.state import RESET_KEY, ConversationState, effective_contact_type
from app.services import lead as lead_service
from app.services import ticket as ticket_service
# SERVICE_LABELS/service_label live in app.services.ticket now — the SAME_ISSUE
# merge-candidate check needs them from a services module, which cannot import
# graph.nodes. Re-exported here so nothing else in the graph package has to
# change its import.
from app.services.ticket import SERVICE_LABELS, service_label
from app.utils import redact_nric

logger = logging.getLogger(__name__)

ENQUIRY_SERVICES = {"fee_enquiry", "salary_enquiry"}

# §0: phone, source, tenant, branch, temperature, status and lead_number are all
# known before the conversation starts and are never asked. The transfer flow's
# only phone question is which number to use, which is a different thing.
#
# Beyond those, anything this number already told us is on its lead row. §1B
# makes that row permanent — one phone, one lead, never updated — so a returning
# client was being asked their name, nationality and email again every time,
# while the prompt sat above the question saying "they have spoken to us before,
# so do not start their details again". The prompt cannot win that: the field is
# chosen in code before the model is called.

# leads.preferred_nationality / leads_candidate.nationality store the code
# nationality_code() produced. Reading it back as 'MM' would put that in the
# ticket and in the reply, so it is turned back into words.
_NATIONALITY_NAMES = {
    "PH": "Philippines",
    "ID": "Indonesia",
    "MM": "Myanmar",
    "none": "no preference",
}

# lead column -> the collector field key it answers.
_LEAD_FIELD_SOURCES = (
    ("full_name", "full_name"),
    ("full_name", "helper_name"),
    ("email", "email"),
    ("nationality", "nationality"),
    ("preferred_nationality", "preferred_nationality"),
    ("requirement", "requirement"),
    ("salary_expectation", "salary_expectation"),
)


# WhatsApp push names that are not a person. The push name is whatever the
# client set on their own profile, so it is a shop, a role, or an emoji at least
# as often as it is a name — which is why has_real_name() will not open a lead
# on one. It is still worth using for full_name when it plainly IS a name: the
# prompt already prints it and the model already greets with it, so asking "may
# I know your name?" straight after "Hi Gurdeep!" reads as a machine that is not
# listening. Anything that fails this test falls through and is asked for.
_NOT_A_PERSON = {
    # businesses
    "pte", "ltd", "llp", "inc", "co", "company", "agency", "agencies",
    "employment", "service", "services", "trading", "enterprise", "enterprises",
    "shop", "store", "cleaning", "catering", "transport", "construction",
    "renovation", "contractor", "maid", "maids", "helper", "helpers",
    # roles and relationships people use as a display name
    "mummy", "mommy", "mum", "mom", "mama", "daddy", "dad", "papa", "boss",
    "sir", "madam", "mdm", "maam", "auntie", "aunty", "uncle", "bro", "sis",
    "me", "myself", "home", "house", "wife", "husband",
}

# Letters in any script (so 陈美玲 and Nurul both pass) plus the punctuation that
# appears inside real names. Digits and emoji are what this is here to reject.
_NAME_PUNCTUATION = set(" .'-")


def _looks_like_a_person(name: str) -> bool:
    """Whether a WhatsApp push name can stand in for the client's own name."""
    cleaned = " ".join((name or "").split())
    if not 2 <= len(cleaned) <= 60:
        return False
    if not all(char.isalpha() or char in _NAME_PUNCTUATION for char in cleaned):
        return False
    words = cleaned.split()
    if not 1 <= len(words) <= 4:
        return False
    return not any(word.strip(".'-").lower() in _NOT_A_PERSON for word in words)


# A requirement, condition or house rule the client states off their own bat, as
# opposed to an answer to whatever we asked. The prompt is told to acknowledge
# these; this is the half that does not depend on the model noticing. Live,
# 2026-09-03: "She shouldn't do smoke and drinks not allowed in my home please"
# was met with the next question and no reaction, the client asked "Did you read
# this?", and the model answered "Yes, I read it" while paraphrasing a
# completely different message.
#
# Deliberately narrow. "can't" and "don't" are left out because they are far
# more often about the CLIENT ("I can't say yet", "I don't have a case ID") than
# a requirement about the helper, and a note that fires on every other turn
# would put the echoing back that the no-repeat rule exists to stop.
_VOLUNTEERED_REQUIREMENT = re.compile(
    r"\b(?:should|must|shall|will)\s+not\b"
    r"|\b(?:shouldn|mustn|wouldn|won|isn|aren)\'?t\b"
    r"|\bnot\s+(?:allowed|permitted|acceptable)\b"
    r"|\bno\s+(?:smoking|smoke|drinking|drinks|alcohol|boyfriend|boyfriends|"
    r"handphone|tattoo|pork|beef)\b"
    r"|\b(?:must|should|needs?\s+to|has\s+to)\s+(?:be\s+able\s+to|know\s+how)\b"
    r"|\bprefer(?:ably)?\s+(?:someone|a\s+helper|her\s+to|non[- ])\b"
    r"|\bi\s+(?:want|need)\s+(?:someone|a\s+helper|her)\s+(?:who|to\s+be)\b",
    re.IGNORECASE,
)


# Services the client calls "small-ticket": short, well-defined jobs we can
# describe end to end rather than qualify and hand off. For these the first
# collector message leads with what the records say the work involves, instead
# of opening on a question and leaving the client with "a live agent will
# connect with you shortly" as the only thing they ever learn. Client
# instruction, 2026-09-04: "provide relevant information instead of immediately
# pushing the customer to a live agent".
# Why we are about to ask a run of questions, per flow. Said once, at the top of
# a qualification, because a question-answer-question-answer march with no
# reason given reads as an interrogation and is the single biggest driver of
# people simply not replying — the client's own feedback, 2026-09-04: "this
# feels like an interrogation and will increase the drop-off rate".
#
# The PURPOSE is supplied, never the wording. A fixed sentence used at the top
# of every conversation becomes the formula that the no-repeat rules exist to
# stop, so the model is told what the reason is and left to say it in its voice.
COLLECTOR_INTRO_NOTE = (
    f"{chr(10)}{chr(10)}This is the client's first message and your introduction is "
    "NOT optional: before anything else, say who you are — Claire, Ming Hwee's AI "
    "assistant — and that you bring in one of our consultants whenever one is needed. "
    "One sentence, your own words, then the rest of your reply. Do not skip it "
    "because you have an answer to give; give the answer after it."
)


_COLLECTION_PURPOSE = {
    "new_hiring": "so we can match a helper who actually suits their household",
    "candidate_new_hiring": "so we can put her in front of the right employers",
    "direct_hiring": "so we can check the paperwork is in order before it goes to MOM",
    "replacement": "so the right person picks this up and we find a suitable replacement",
    "transfer_employer": "so we can shortlist transfer helpers who actually fit",
    # The helper's own transfer. Six questions about her permit, her employer's
    # consent and her availability is exactly the march this note exists for,
    # and she has less patience for it than an employer does.
    "transfer": "so we can find you an employer who suits",
}


_SMALL_TICKET_SERVICES = frozenset({"renewal", "passport_renewal", "insurance"})


def _is_first_contact(state: ConversationState) -> bool:
    """Whether we have never said anything to this client before.

    A function rather than a local, because it was a local: the intro note read
    `first_contact` eighty lines above the line that assigned it, and every
    single turn raised UnboundLocalError. Live 2026-09-04 14:02, and the graph
    caught it as "bot_confused" and handed each message to a human, so the
    client got silence rather than an error — the failure looked like the bot
    ignoring them.
    """
    return not (state.get("history_text") or "").strip()


def _prior_hires(state: ConversationState) -> int:
    """Placements on file for this number, coerced safely to an int."""
    try:
        return int(state.get("prior_hires") or 0)
    except (TypeError, ValueError):
        return 0


def _known_fields(state: ConversationState) -> dict[str, str]:
    known: dict[str, str] = {}

    # Whether they are a returning client is a matter of record, not a question,
    # and as of 2026-09-04 it is NEVER asked either way. The webhook counts this
    # number's non-archived `placements` rows every turn and the answer is
    # filled from that count, so the field reaches the ticket without a question
    # ever going out.
    #
    # Zero is read as "first time with us" on the client's explicit instruction:
    # not being in the database IS the answer, and asking a brand-new client
    # whether they have hired with us before is a question whose answer we
    # already have. The cost, accepted knowingly: someone who hired through us
    # under a different number, or long enough ago that no placement was
    # recorded, is filed as a first-timer. The agent sees the wording below and
    # can tell the two apart — "no placement on record" is a statement about our
    # records, not about the client.
    prior_hires = _prior_hires(state)
    if prior_hires:
        known["first_time_hire"] = (
            f"hired through us before - {prior_hires} placement"
            f"{'s' if prior_hires > 1 else ''} on record"
        )
    else:
        known["first_time_hire"] = "first time with us - no placement on record"

    # The helper on their file, when our records name exactly one. Ahead of the
    # lead below because a placement is a harder fact than an enquiry: `leads`
    # records what somebody once said they wanted, `placements` records a helper
    # we actually placed. Both are filtered by allowed_keys at the call site, so
    # this only ever reaches a flow that asks about the employer's own helper
    # (passport_renewal, replacement, transfer_employer) — never new_hiring,
    # which has no helper_name, and never a candidate flow, where the contact is
    # the helper and matched_employer_id is None.
    placed = state.get("placed_helper")
    if isinstance(placed, dict):
        for field_key, value in placed.items():
            text = str(value or "").strip()
            if not text:
                continue
            if field_key == "nationality":
                text = _NATIONALITY_NAMES.get(text, text)
            known.setdefault(field_key, text[:300])

    lead = state.get("matched_lead")
    if not isinstance(lead, dict):
        return _with_push_name(state, known)

    # leads.full_name is the EMPLOYER's name; leads_candidate.full_name is the
    # helper's. The same column answers helper_name only on a candidate lead —
    # mapped unconditionally it filed a replacement ticket reading "Helper:
    # Vaidik Dubey", which is the employer, and the client was never asked who
    # the helper actually is.
    is_candidate_lead = (state.get("lead_kind") or "").strip().lower() == "candidate"

    for column, field_key in _LEAD_FIELD_SOURCES:
        if field_key == "helper_name" and not is_candidate_lead:
            continue
        value = str(lead.get(column) or "").strip()
        # 'not provided' is what the collector records for a field the client
        # would not answer. Reusing it would make the refusal permanent.
        if not value or value.lower() == UNANSWERED:
            continue
        if column in {"nationality", "preferred_nationality"}:
            value = _NATIONALITY_NAMES.get(value, value)
        # §23.7 — a client who would not give a name got "WhatsApp Lead +65...".
        # That is a placeholder, not something to tell them we already know.
        if field_key in {"full_name", "helper_name"} and value.lower().startswith("whatsapp lead"):
            continue
        known.setdefault(field_key, value[:300])
    return _with_push_name(state, known)


def _with_push_name(state: ConversationState, known: dict[str, str]) -> dict[str, str]:
    """Fall back to the WhatsApp push name for full_name, when it is one.

    Applied last, so a name the client actually gave us on an earlier enquiry
    (the lead row above) always wins over whatever they set on their profile.
    Only ever full_name — never helper_name: the push name belongs to whoever
    holds the phone, and on a replacement or transfer enquiry that is the
    employer, not the helper being asked about.
    """
    push_name = str(state.get("customer_name") or "").strip()
    if not known.get("full_name") and _looks_like_a_person(push_name):
        known["full_name"] = push_name[:300]
    return known


FALLBACK_QUESTION = "Sorry, could you tell me a bit more about what you need?"
FALLBACK_CLOSING = (
    "Noted, thanks for the details. I've passed this to our team and a live agent "
    "will connect with you shortly. Anything else I can help you with?"
)
FALLBACK_ACKNOWLEDGEMENT = (
    "Got it. A live agent will pick this up and connect with you shortly. "
    "Is there anything else I can help with in the meantime?"
)

# Values that only mean anything as an answer to a question that was asked.
#
# The extractor is told to record "no preference" when a client says "any" or
# "up to you", which is right — but it also applies it to fields nobody has
# raised. One message, "can you find 3 myanmar maid for my elderly mother",
# produced a complete new_hiring ticket claiming the client had no preference on
# experience, budget and timeline. Sales then works a lead whose requirements
# were never established, and the client is never asked.
_NO_PREFERENCE = re.compile(
    r"^(any|anything|any\s?one|no\s+preference|no\s+pref|up\s+to\s+you|you\s+decide|"
    r"doesn'?t\s+matter|does\s+not\s+matter|whatever|either|both|flexible|open)\b",
    re.IGNORECASE,
)

# After this many attempts at the same field, take the client at their word and
# move on. "I don't have this kind of stuff" is an answer; asking a ninth time —
# which is what happened live on a replacement case reference — is not
# persistence, it is a machine that cannot hear.
MAX_ASKS_PER_FIELD = 3
UNANSWERED = "not provided"

# Fields that must name the work, not the enquiry.
_CARE_TYPE_FIELDS = {"requirement", "care_type"}

# Facts about the CLIENT that stay true when the service changes, and so survive
# the switch-reset below.
#
# The reset exists to stop one service's answers being filed as another's — a
# case ID given for a renewal is not a passport renewal's case ID. But it wiped
# everything, including things that do not belong to a service at all. Live
# (2026-09-02 18:31): a client finished a hiring enquiry having said "Filipino",
# then asked about salary; the service switched to salary_enquiry, the reset
# cleared the lot, and the bot asked "which nationality are you looking at?" —
# they had to answer "I have tell you the nationality before".
#
# Only identity and preference keys are listed. Anything tied to a specific
# helper, case or document (helper_name, case_id, permit_expiry,
# passport_expiry, transfer_direction) is deliberately absent: those DO belong
# to one service and carrying them across is the contamination the reset is for.
_PORTABLE_ACROSS_SERVICES = {
    "full_name",
    "email",
    "contact_number",
    "nationality",
    "preferred_nationality",
    "requirement",
    "care_type",
    "household",
    "home_type",
    "languages",
    "budget",
    "referral_source",
}

# A value the extractor handed back that is itself a QUESTION, not an answer.
# Live (screenshot, 2026-09-02): a client mid-hiring asked "is there a monthly
# salary budget in mind?" back to us, and the extractor recorded it as the
# `budget` answer — so the field looked filled, the salary question was never
# answered, and the client had to ask again. A field answer is never a
# question: "how much", "what is", "is there", a trailing "?" — none of that is
# the client stating their own budget/nationality/timeline. Dropping it lets the
# question fall to the ANSWER_THEN_ASK path (which answers it) and leaves the
# field genuinely open so it is still asked.
_VALUE_IS_QUESTION = re.compile(
    r"\?|^\s*(?:how\s+(?:much|long|many|do|does|about)|what(?:'?s|\s+is|\s+are|\s+do)?|"
    r"which|when|where|why|who|is\s+there|are\s+there|do\s+you|can\s+(?:i|you|we)|"
    r"could\s+you|would\s+(?:it|you)|any\s+idea|tell\s+me)\b",
    re.IGNORECASE,
)

# Words that describe wanting a helper rather than what the helper is for.
# "I want to hire a helper" is entirely made of these; "helper for my elderly
# mother" is not, and neither is "cooking and cleaning".
_CARE_TYPE_FILLER = re.compile(
    r"\b(i|we|my|our|me|us|a|an|the|to|for|of|in|is|am|are|and|"
    r"want(?:ed|ing|s)?|need(?:ed|ing|s)?|look(?:ing)?|hire|hiring|get(?:ting)?|"
    r"find(?:ing)?|new|another|one|first|time|please|kindly|"
    r"helper|helpers|maid|maids|domestic|worker|mdw|fdw|house\s*help|"
    r"service|services|enquiry|enquire|interested)\b",
    re.IGNORECASE,
)


# A message that puts a question to us in the middle of a collection flow.
# Answering it before asking the next thing is the difference between a
# consultant and a form: live, "How much is your agency fee?" asked during a
# hiring flow got "Thanks, I'll pull the details together and come back to you"
# — the question was never even acknowledged, let alone answered.
#
# Widened after a live round on 2026-09-02: "In 2 weeks can you provide" was
# answered by moving straight on to the next question. It carries no question
# mark and none of the words listed here, so ANSWER_THEN_ASK never fired —
# while _VALUE_IS_QUESTION, which is far broader, DID match and threw the "in 2
# weeks" half away as well. The client's answer was lost and their question
# ignored in the same turn. The two patterns have to agree about what reads as
# a question, so the openers below now mirror that one.
_ASKS_SOMETHING = re.compile(
    r"\?|\b(how\s+(much|long|many|do|does)|what\s+(documents?|do\s+i|is|are)|"
    r"which\s+documents?|when\s+(can|will|do)|cost|price|fee|fees|charge|"
    r"salary|levy|deposit|require[ds]?|needed|"
    r"can\s+(you|we|i)|could\s+you|do\s+you\s+(have|provide|offer|know)|"
    r"are\s+you\s+able|will\s+you|is\s+it\s+possible|is\s+there|are\s+there|"
    r"any\s+(idea|chance)|possible\s+to)\b",
    re.IGNORECASE,
)


# An answer that confirms something EXISTS without ever saying what it is.
#
# Live, 2026-09-02 19:38. Asked "anything else we should know — like if your
# grandmother has any medical conditions or mobility issues?", the client
# answered "Yes grandmother has medical condition". The field filled, the flow
# moved on to how they heard about us, and four messages later they were
# writing "You didnt asked for which medical condition my grandmother is
# suffering from is that looks normal for you tell me you are ignoring that" —
# then asking twice more. missing_fields() only asks whether the key is set,
# and by that test this was answered.
#
# Anchored at the end on purpose: the vague word has to be the LAST thing said.
# "a heart condition", "some mobility issues" and "diabetes and high blood
# pressure" all name the thing and are left alone; "has medical condition",
# "has a condition" and "there are some issues" do not.
_ASSERTS_WITHOUT_DETAIL = re.compile(
    r"\b(?:has|have|had|is|are|got|some|any|a|an|the|with|of|medical|health)\s+"
    r"(?:medical\s+|health\s+)?"
    r"(?:conditions?|illnesses?|issues?|problems?|ailments?|difficulties|"
    r"disabilit(?:y|ies)|requirements?|preferences?|needs?|allerg(?:y|ies))"
    r"\s*[.!,]*\s*$",
    re.IGNORECASE,
)

# An address that parses but is almost certainly mistyped. Live, same round:
# "Vd@gmail.con" went onto the lead exactly as written. Email is the only
# channel the office has for sending helper profiles, so a wrong one does not
# fail loudly — it fails silently, forever, and nobody finds out.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_TYPO_DOMAINS = re.compile(
    r"@(?:gmial|gmai|gmal|gnail|hotmial|hotmai|homail|yahooo|yaho|outlok|outllok)\.|"
    r"\.(?:con|cmo|ocm|xom|vom|c0m|comm|cim|cpm|om)$",
    re.IGNORECASE,
)


def _unfinished(
    service_type: str, collected: dict[str, Any], asked: dict[str, int]
) -> tuple[list[ticket_service.Field], dict[str, str]]:
    """Fields holding a value that does not actually answer them.

    missing_fields() asks one question — is the key set? That is the right test
    for most answers and the wrong one for two shapes that have both now gone
    out to a client: an answer that says something exists without saying what
    it is, and a contact address that is plainly mistyped.

    The value is KEPT either way. It is real and it belongs on the ticket; it
    is simply not finished, so the field goes back to the front of the queue
    and is asked once more for the part that is missing. The ask counters still
    apply, so this adds at most one more question and can never loop.
    """
    fields: list[ticket_service.Field] = []
    notes: dict[str, str] = {}
    for field in ticket_service.fields_for(service_type):
        value = str(collected.get(field.key) or "").strip()
        if not value or value.lower() == UNANSWERED:
            continue
        note = ""
        if field.key == "email":
            # email is max_asks=1, so the ordinary limit would rule out ever
            # querying a typo. Exactly one confirmation is allowed instead.
            if asked.get(field.key, 0) <= field.max_asks and (
                not _EMAIL_SHAPE.match(value) or _TYPO_DOMAINS.search(value)
            ):
                note = (
                    f'\n\nThey gave their email as "{value}", which looks like it may '
                    "have a typo in it. Read it back to them exactly as they wrote it "
                    "and ask if that is right. Do NOT correct it yourself and do NOT "
                    "say what you think it should be — just check."
                )
        elif asked.get(field.key, 0) < min(
            field.max_asks, MAX_ASKS_PER_FIELD
        ) and _ASSERTS_WITHOUT_DETAIL.search(value):
            note = (
                f'\n\nThey have told you "{value}" — that something is there, but not '
                "what it is, and what it is, is the part that matters. Ask them for it "
                "directly, warmly, now. Do not thank them and change the subject: "
                "skipping past the one thing a client has just raised is what makes "
                "them feel unheard."
            )
        if note:
            fields.append(field)
            notes[field.key] = note
    return fields, notes


def _states_a_care_type(text: str) -> bool:
    """Whether a requirement value says anything beyond 'I want a helper'.

    Deliberately a subtractive test rather than a list of care types: the
    agency's own vocabulary keeps growing, and a whitelist would silently drop
    'post-natal' or 'stroke recovery' the first time someone said it.
    """
    remainder = _CARE_TYPE_FILLER.sub(" ", text or "")
    return bool(re.sub(r"[^a-z0-9]+", "", remainder.lower()))


def _field_guidance(
    service_type: str, collected: dict[str, Any], field: ticket_service.Field
) -> str:
    """What to tell the model about the specific detail it is asking for.

    Two things the field label alone cannot carry: the answers the office
    actually works with, and whether this question changes the subject.

    The options are given as examples to steer the question, never as a list to
    read out. A client asked "1-2, 3-4, 5-6 or 7 or more?" is filling in a form;
    a client asked "how many of you are there at home?" is having a
    conversation, and both produce the same value.
    """
    parts: list[str] = []

    previous_group = ticket_service.preceding_group(service_type, collected, field)
    if field.group and previous_group and field.group != previous_group:
        parts.append(
            f"\n\nYou have finished with {previous_group} and are moving on to "
            f"{field.group}. A handful of words to mark the turn is fine "
            '("got it — and about the home itself,") but it is not required, and it '
            "must never become a formula you use at every change of subject."
        )
    elif field.group and not previous_group:
        parts.append(f"\n\nThis is the first thing you are asking about {field.group}.")

    # The office's own wording for this field. Without it the model sees only
    # the LABEL — "the elderly family member" — and asks the thinnest question
    # that fits it. Live, 2026-09-02 19:00: a field whose written question is
    # "their age, how mobile they are, and any medical conditions" went out as
    # "May I know who the care is for?". "For my grandmother" filled it, the
    # flow moved on, and the medical condition was never asked about at all —
    # which is the complaint the client spent the next four messages making.
    # The label says WHICH field; only the question says what it has to get.
    parts.append(
        f'\n\nThe office asks this as: "{field.question}" Put it in your own words, '
        "but ask for everything that question asks for — if it names three things, a "
        "question that gets one of them is not this question. Never read it out "
        "verbatim like a form."
    )

    if field.options:
        parts.append(
            "\n\nThe answers the office works with here are: "
            + ", ".join(field.options)
            + ". Use them to shape the question — dropping two or three in as examples "
            "is how a person asks it. Never read the whole set out, never number them, "
            "and never present them as a menu to choose from. Whatever the client "
            "answers is their answer, listed or not."
        )

    return "".join(parts)


async def _extract(
    state: ConversationState, service_type: str, asked: dict[str, int]
) -> dict[str, Any]:
    # include_undecided: a gate that has not been decided yet still goes to the
    # extractor. The turn the client says "for my mum, she's 82 and bedridden"
    # is the same turn that opens the eldercare gate, and a field left out of
    # this list on that turn is a question asked about something already
    # answered. Gates that are decided and closed stay out — nobody with no
    # children should have children_detail offered to a model at all.
    fields = ticket_service.applicable_fields(
        service_type, state.get("collected_info") or {}, include_undecided=True
    )
    if not fields:
        return {}

    field_lines = "\n".join(f"- {field.key}: {field.label}" for field in fields)
    captured = state.get("collected_info") or {}
    captured_lines = (
        "\n".join(f"- {key}: {value}" for key, value in captured.items() if value) or "(nothing yet)"
    )

    result = await complete_json(
        EXTRACTION_SYSTEM,
        EXTRACTION_USER.format(
            fields=field_lines,
            captured=captured_lines,
            history=state.get("history_text") or "(no earlier messages)",
            message=state.get("incoming_text", ""),
        ),
        default={},
    )

    allowed = {field.key for field in fields}
    cleaned: dict[str, Any] = {}
    for key, value in result.items():
        if key not in allowed:
            continue
        text = str(value).strip()
        if not text or text.lower() in {"unknown", "n/a", "na", "none", "not provided", "null"}:
            continue
        # A question is never a field answer. When the client asks us something
        # ("what's the typical salary?"), the extractor sometimes files it as the
        # very field being asked about — the field then looks answered and the
        # question goes unanswered. Drop it: the ANSWER_THEN_ASK path handles the
        # question, and the field stays open to be asked properly.
        if _VALUE_IS_QUESTION.search(text):
            logger.info(
                "Conversation %s: ignoring '%s' for '%s' — it is a question, not an answer",
                state.get("conversation_id"),
                text[:40],
                key,
            )
            continue
        # "No preference" is only an answer if there was a question. Unasked,
        # it is the model filling in the form on the client's behalf.
        if _NO_PREFERENCE.match(text) and not asked.get(key):
            logger.info(
                "Conversation %s: ignoring '%s' for '%s' — that field was never asked",
                state.get("conversation_id"),
                text[:40],
                key,
            )
            continue
        # "I want to hire a helper" is the enquiry, not the answer to what kind
        # of care they need. Taken as one, the question is never asked and sales
        # opens a lead whose requirement reads "hire a helper".
        if key in _CARE_TYPE_FIELDS and not _states_a_care_type(text):
            logger.info(
                "Conversation %s: ignoring '%s' for '%s' — it restates the enquiry "
                "rather than naming a care type",
                state.get("conversation_id"),
                text[:40],
                key,
            )
            continue
        # An address with no '@' is not one. The client saying "no email" was
        # being recorded as "no preference", which reads on the ticket as though
        # they gave one. Dropping it lets the max_asks rule record it honestly
        # as 'not provided'.
        if key == "email" and "@" not in text:
            logger.info(
                "Conversation %s: ignoring '%s' for 'email' — not an address",
                state.get("conversation_id"),
                text[:40],
            )
            continue
        cleaned[key] = redact_nric(text, context=f"captured:{key}")[:300]
    return cleaned


async def _open_lead_early(
    state: ConversationState, service_type: str | None, collected: dict[str, Any]
) -> dict[str, Any]:
    """Create the lead as soon as the client has given a usable name.

    Returns the state keys to carry, or {} when there is nothing to open —
    the service raises no lead, we have no name yet, or this number already
    has a row (§1B: one phone, one lead, ever).
    """
    if state.get("created_lead_id") or state.get("matched_lead_id"):
        return {}

    kind = lead_service.kind_for(service_type, effective_contact_type(state))
    if not kind or not lead_service.has_real_name(collected):
        return {}

    # No summary yet — it costs an LLM call and the requirements it would
    # summarise have not been asked for. It is written on the update instead.
    lead = await lead_service.create_if_absent(
        kind=kind,
        name=lead_service.best_name(state.get("customer_name"), collected),
        phone=state.get("phone") or "",
        collected=collected,
        service_type=service_type,
    )
    if not lead:
        return {}

    if lead.get("lead_existed"):
        # Someone else's row, or one from a previous enquiry. §1B says leave it
        # alone, so it is recorded as matched rather than created.
        logger.info(
            "Conversation %s: %s already has lead %s — not opening another",
            state.get("conversation_id"),
            state.get("phone"),
            lead.get("lead_number"),
        )
        return {
            "matched_lead_id": lead.get("id"),
            "matched_lead_number": lead.get("lead_number"),
            "lead_kind": kind,
        }

    logger.info(
        "Conversation %s: opened %s lead %s on the client's name, before the rest "
        "of the questions",
        state.get("conversation_id"),
        kind,
        lead.get("lead_number"),
    )
    return {
        "created_lead_id": lead.get("id"),
        "created_lead_number": lead.get("lead_number"),
        # created_lead_kind, not lead_kind: the webhook rewrites lead_kind from
        # the per-turn matched-lead lookup, which finds nothing on the very
        # conversation that just opened one. See ConversationState.
        "created_lead_kind": kind,
        "lead_kind": kind,
    }


async def info_collector(state: ConversationState) -> dict[str, Any]:
    # §3 and the routing rule: a helper looking for work produces intent
    # new_hiring exactly as an employer does, and must not be asked an
    # employer's questions.
    contact_type = effective_contact_type(state)
    service_type = ticket_service.resolve_service(state.get("service_type"), contact_type)
    if service_type != state.get("service_type"):
        logger.info(
            "Conversation %s: %s routed to the %s flow for a %s",
            state.get("conversation_id"),
            state.get("service_type"),
            service_type,
            contact_type,
        )

    if not service_type:
        # Nothing to collect — let the responder handle it as a general question.
        return {"info_complete": True, "missing_field_keys": []}

    # A client who switches service starts clean. Their answers about hiring a
    # helper are not answers about renewing a work permit, and keeping them both
    # fills the new service's fields with the old service's values and files a
    # ticket full of irrelevant detail.
    switched = bool(state.get("collected_service")) and state["collected_service"] != service_type
    everything = state.get("collected_info") or {}
    # On a switch, keep the facts that are about the client rather than about the
    # old service, so the new flow never re-asks something they already told us.
    carried_over = (
        {
            key: value
            for key, value in everything.items()
            if key in _PORTABLE_ACROSS_SERVICES and str(value or "").strip()
        }
        if switched
        else {}
    )
    previous = carried_over if switched else everything
    asked = {} if switched else dict(state.get("asked_field_counts") or {})
    if switched:
        logger.info(
            "Conversation %s switched from %s to %s — cleared the old service's "
            "answers, carried over %s",
            state.get("conversation_id"),
            state.get("collected_service"),
            service_type,
            ", ".join(sorted(carried_over)) or "nothing",
        )

    extraction_state = {**dict(state), "collected_info": previous}
    if switched:
        # The pre-switch history is entirely about the abandoned service — on
        # the turn that switches, it is the only thing in state.history_text,
        # and the extractor has nothing relevant to find, so it grabs the
        # nearest plausible-looking short answer instead: a real case, this
        # produced case_id="Mui Hui" (the client's own name, given three turns
        # earlier while hiring a helper — a completely different enquiry).
        # No history for the new service exists yet, so none is given, exactly
        # as a brand-new conversation gets EXTRACTION_USER's own
        # "(no earlier messages)" fallback.
        extraction_state["history_text"] = ""
    extracted = await _extract(extraction_state, service_type, asked)

    # Anything we already know goes in before the gap analysis, so it is never
    # asked for and still reaches the ticket.
    allowed_keys = {field.key for field in ticket_service.fields_for(service_type)}
    known = {
        key: value
        for key, value in _known_fields(state).items()
        if key in allowed_keys and not str(previous.get(key) or "").strip()
    }
    if known:
        logger.info(
            "Conversation %s: filling %s from the conversation instead of asking",
            state.get("conversation_id"),
            ", ".join(sorted(known)),
        )

    extracted = {**known, **extracted}
    collected = {**previous, **extracted}

    # A field the client has been asked about repeatedly and still not answered
    # is recorded as unanswered rather than asked again. Sales sees the gap
    # honestly, which is more use than a value invented to close it — the live
    # loop ended up filing "case reference: singapore branch".
    # A case ID the client does not have has max_asks=1, so it is asked once and
    # then let go (§23.5); email likewise (§2 step 4).
    exhausted = {
        field.key: UNANSWERED
        for field in ticket_service.missing_fields(service_type, collected)
        if asked.get(field.key, 0) >= min(field.max_asks, MAX_ASKS_PER_FIELD)
    }
    if exhausted:
        logger.info(
            "Conversation %s: giving up on %s — recording as unanswered",
            state.get("conversation_id"),
            ", ".join(sorted(exhausted)),
        )
        collected = {**collected, **exhausted}
        extracted = {**extracted, **exhausted}

    # §23.5: a client who cannot give a case ID is reassured and the flow
    # continues. Said once, on the turn the question is dropped.
    dropped_note = ""
    if "case_id" in exhausted:
        dropped_note = (
            "\n\nThey could not give a case ID. Open this message by telling them "
            'that is no problem and you will find their case yourself — in your own '
            "words, close to \"No problem, let me find your case for you.\" Then "
            "carry on. Never ask for the case ID again."
        )

    # Said once, on the turn the database answers first_time_hire for us: `known`
    # only carries a field the collected state does not already hold, so this
    # cannot repeat on later turns of the same service. Skipping the question
    # silently would read as us not knowing them at all.
    # We recognised their helper off their own file. Say so out loud, on the one
    # turn it becomes true. Filling the fields silently — which is all this did
    # at first — is indistinguishable from never having asked: the client sees a
    # bot that skipped straight to question three and has no idea we know who
    # they are. Client instruction, 2026-09-04: "recognize the client by their
    # phone number and say something like 'Hi Thomas, I can see that your
    # helper's passport is expiring next May'".
    #
    # `known` holds only what the records filled THIS turn, so this cannot
    # repeat on later turns of the same service — the same say-once mechanism
    # returning_note relies on.
    recognised = {
        key: known[key]
        for key in ("helper_name", "passport_expiry", "permit_expiry")
        if known.get(key)
    }
    recognised_note = ""
    if recognised and isinstance(state.get("placed_helper"), dict):
        facts = "; ".join(f"{key.replace('_', ' ')} {value}" for key, value in recognised.items())
        recognised_note = (
            f"\n\nOur records already answer this, off their own file: {facts}. Open "
            "this message by telling them what we can see — name the helper and the "
            "date plainly — so they know we recognise them, then ask your question. "
            "Keep it to two sentences. State the date as it is and do NOT describe it "
            "as expiring soon, urgent, or coming up unless the date itself says so; "
            "the client can read a year as well as you can. Never ask them for "
            "anything listed here."
        )

    # Suppressed when we are already showing them something off their file —
    # the two notes give opposite instructions about how much to reveal, and
    # the specific one wins.
    returning_note = ""
    if _prior_hires(state) and "first_time_hire" in known and not recognised_note:
        returning_note = (
            "\n\nOur own records show they have hired a helper through us before, so "
            "that is already established and you must never ask it. Open this message "
            "by welcoming them back in one short clause — no details of who, when or "
            "how many, we are not showing them their file — and then ask your question."
        )

    # A small-ticket service, on its opening turn: say what the job involves
    # before asking about it. Gated on nothing having been asked yet, so it
    # happens once and does not turn every turn into a briefing.
    #
    # Strictly grounded — the model is told to use the records or say nothing.
    # There is no agency fee for either service in the knowledge base (checked
    # 2026-09-04), so a "tell them the cost" instruction here would be an
    # instruction to invent one; ungrounded_figures would bin the reply and the
    # client would get the bare question anyway. Load the fee into the KB and
    # this starts quoting it with no code change.
    small_ticket_note = ""
    if service_type in _SMALL_TICKET_SERVICES and not any(asked.values()):
        small_ticket_note = (
            f"{chr(10)}{chr(10)}This is a short, well-defined job we handle end to end, not "
            "something to hand straight to a colleague. Before your question, tell "
            "them in one sentence what the process involves or how long it takes — "
            "but ONLY what the records above actually state. If the records say "
            "nothing about it, just ask your question and add nothing. Never "
            "estimate a price, a duration or a document list that is not written "
            "there."
        )

    # The very first thing this client has ever heard from us. Rule 1 and the
    # stage line in build_system_prompt both call for the introduction, but on a
    # collector turn they compete with COLLECTOR_INSTRUCTION's "ask for that one
    # detail and nothing else", and the introduction is what loses. Live,
    # 2026-09-04 19:39: a first message ("which nationality is the cheapest to
    # hire") was answered with the salary range and the next question, no
    # introduction at all, and the agency flagged that we never declared what we
    # are. Repeating it here, in the instruction that is actually winning, is
    # what makes it stick.
    intro_note = COLLECTOR_INTRO_NOTE if _is_first_contact(state) else ""

    # Opening a qualification: say why, once. Gated on nothing having been asked
    # yet, the same test the small-ticket briefing uses — and the two sets are
    # disjoint, so a flow gets one or the other, never both.
    purpose_note = ""
    purpose = _COLLECTION_PURPOSE.get(service_type or "")
    if purpose and not any(asked.values()):
        purpose_note = (
            f"{chr(10)}{chr(10)}This is the first of several questions, and a client "
            "marched through question after question with no reason given stops "
            "replying — that is the most common way these conversations die. Before "
            "you ask, give them the reason in one short clause: you are asking "
            f"{purpose}. Put it in your own words, not those ones, and say it ONCE "
            "— here, at the top. Never explain yourself again in this conversation, "
            "and never turn it into a preamble you attach to every question."
        )

    # They stated a requirement rather than answering; say so before asking the
    # next thing. Not conditional on the extractor having found a home for it —
    # the failure this fixes is conversational, and a client whose requirement
    # is silently filed still thinks we ignored them.
    requirement_note = ""
    if _VOLUNTEERED_REQUIREMENT.search(state.get("incoming_text") or ""):
        requirement_note = (
            f"{chr(92)}n{chr(92)}nThe client has just stated a requirement or a house rule of their "
            "own. Acknowledge that one thing in a short clause before your question — "
            "plainly, in your own words, no repeating their sentence back at them and no "
            "promising anything about it — then ask. Do not let it pass without a word, "
            "and do not claim to have read it while responding to something else."
        )

    missing = ticket_service.missing_fields(service_type, collected)

    # A field can hold a value and still not be finished — see _unfinished().
    # Put those back at the FRONT: the client raised it a moment ago, and the
    # whole failure being fixed here is asking about something else instead.
    unfinished, follow_up_notes = _unfinished(service_type, collected, asked)
    if unfinished:
        logger.info(
            "Conversation %s: %s answered but not finished — asking again for the "
            "part that is missing",
            state.get("conversation_id"),
            ", ".join(field.key for field in unfinished),
        )
        missing = unfinished + [f for f in missing if f.key not in follow_up_notes]

    # The lead is opened the moment the client has given a name, not at the end
    # of collection. A client who answers two questions and then stops used to
    # leave nothing behind at all — no lead, no ticket, no record that anyone
    # had enquired. The requirements gathered afterwards are written onto this
    # same row by the ticket node when collection completes.
    lead_fields = await _open_lead_early(state, service_type, collected)

    carry: dict[str, Any] = dict(extracted)
    counts: dict[str, Any] = {}
    if switched:
        # RESET_KEY empties collected_info in the reducer, so the portable facts
        # have to be written back alongside it or they are lost from state even
        # though this turn used them. Anything extracted this turn still wins.
        carry = {**carried_over, **extracted, RESET_KEY: True}
        counts[RESET_KEY] = True

    label = service_label(service_type)
    system_prompt_state = {**dict(state), "collected_info": collected}

    # The client asked something while we were collecting. Retrieval now runs
    # before this node on every turn, so the records are in state and the answer
    # can go out with the next question rather than being ignored.
    answer_first = (
        ANSWER_THEN_ASK_INSTRUCTION
        if _ASKS_SOMETHING.search(state.get("incoming_text") or "")
        else ""
    )

    # The greeting and the AI disclosure are two sentences before a single
    # question has been asked, so a two-sentence budget deletes the question
    # itself. Live, 2026-09-02 19:25: "I need a helper" was answered with
    # "Good Evening! I'm Claire, Ming Hwee's AI assistant." and nothing else —
    # the client had to send "I need helper" again to get a question out of us.
    # It is a coin flip on punctuation: written "Good Evening, I'm Claire..."
    # that is one sentence and the question survives; written with an
    # exclamation mark it is two and clamp_reply cuts the question off.
    first_contact = _is_first_contact(state)

    if missing:
        next_field = missing[0]
        previous = last_bot_line(state.get("history_text", ""))
        instruction = (
            COLLECTOR_INSTRUCTION.format(
                service_label=label,
                field_label=next_field.label,
                field_guidance=_field_guidance(service_type, collected, next_field),
                previous_message=previous or "(this is your first message)",
            )
            + dropped_note
            + intro_note
            + recognised_note
            + small_ticket_note
            + purpose_note
            + returning_note
            + requirement_note
            + follow_up_notes.get(next_field.key, "")
            + answer_first
        )
        if next_field.optional:
            # §2 step 4 / §23.6: asked once, and a no is taken as an answer.
            instruction += (
                "\n\nThis one is optional. Ask lightly, once. If they say no, do not "
                "have it, or simply move past it, accept that without comment and "
                "never raise it again."
            )
        # If generation degenerates, fall back to the field's own hand-written
        # question from SERVICE_FIELDS — less warm, but always correct.
        reply = await _write(
            state,
            system_prompt_state,
            instruction,
            fallback=next_field.question,
            # Answering their question and then asking ours does not fit in two,
            # and neither does introducing yourself before asking anything.
            # Four only where all three are genuinely required: the
            # introduction, the answer to what they asked, and our question.
            max_sentences=4
            if (first_contact and answer_first)
            else 3
            if (answer_first or first_contact or small_ticket_note or purpose_note)
            else 2,
            withhold_cost=service_type in COST_WITHHELD_SERVICES,
        )
        counts[next_field.key] = 1
        logger.info(
            "Conversation %s collecting '%s' for %s (attempt %d, %s field(s) outstanding)",
            state.get("conversation_id"),
            next_field.key,
            service_type,
            asked.get(next_field.key, 0) + 1,
            len(missing),
        )
        return {
            **lead_fields,
            "collected_info": carry,
            # The resolved service, so the ticket and lead use the flow that
            # actually ran rather than the raw classification.
            "service_type": service_type,
            "collected_service": service_type,
            "asked_field_counts": counts,
            "missing_field_keys": [field.key for field in missing],
            "info_complete": False,
            "reply": reply,
            "needs_handover": bool(state.get("needs_handover")),
        }

    # A service that asks nothing at all (direct hiring, a supplier offering a
    # helper) has not "got everything it needs" — it never wanted anything. Told
    # otherwise, the model filled the gap by inventing a question and then went
    # silent behind the handover, which is what a job seeker saw: asked for her
    # name and country, then nothing.
    if not ticket_service.fields_for(service_type):
        instruction_template = ACKNOWLEDGE_ONLY_INSTRUCTION
        fallback = FALLBACK_ACKNOWLEDGEMENT
    elif service_type in ENQUIRY_SERVICES:
        instruction_template = FEE_HANDOVER_INSTRUCTION
        fallback = FALLBACK_CLOSING
    else:
        instruction_template = HANDOVER_CLOSER_INSTRUCTION
        fallback = FALLBACK_CLOSING
    instruction = instruction_template.format(
        service_label=label,
        enquiry_label="our fees" if service_type == "fee_enquiry" else "helper salary",
    ) + dropped_note + answer_first
    reply = await _write(
        state,
        system_prompt_state,
        instruction,
        fallback=fallback,
        # The closing message is three things by design (§8): thank them, say a
        # live agent will connect, offer to help with anything else. At two, the
        # offer was the sentence that got cut — live, 2026-09-02 19:18, the log
        # reads: kept "...a live agent will connect with you shortly.", dropped
        # "In the meantime, is there anything else I can help you with?" — and
        # the client was left at a dead end straight after a handover.
        max_sentences=3,
    )
    logger.info(
        "Conversation %s finished collection for %s: %s",
        state.get("conversation_id"),
        service_type,
        ticket_service.summarize(service_type, collected),
    )
    return {
        **lead_fields,
        "collected_info": carry,
        "service_type": service_type,
        "collected_service": service_type,
        "asked_field_counts": counts,
        "missing_field_keys": [],
        "info_complete": True,
        "reply": reply,
    }


async def _write(
    state: ConversationState,
    prompt_state: dict[str, Any],
    instruction: str,
    fallback: str = "",
    max_sentences: int = 2,
    withhold_cost: bool = False,
) -> str:
    system_prompt = build_system_prompt(
        prompt_state,
        # Retrieval runs ahead of this node now, so a question asked mid-flow can
        # be answered from our own material instead of deferred.
        rag_context=state.get("rag_context", ""),
        extra_instructions=instruction,
    )
    user_prompt = (
        f"Conversation so far:\n{state.get('history_text') or '(this is the first message)'}\n\n"
        f"Client's latest message(s):\n{state.get('incoming_text', '')}\n\n"
        "Your reply:"
    )
    try:
        reply = await complete(system_prompt, user_prompt, temperature=0.45, max_tokens=140)
    except Exception:  # noqa: BLE001
        logger.exception("Collector reply generation failed")
        return FALLBACK_QUESTION

    reply = strip_meta_commentary(reply.strip().strip('"'))
    if is_degenerate(reply) or looks_like_document(reply):
        logger.error("Discarded malformed collector reply: %r", reply[:200])
        return fallback or FALLBACK_QUESTION

    # The model reading its own reasoning back at the client instead of asking —
    # e.g. copying the empty-records instruction. Same failure the response
    # generator now guards; the collector answers mid-flow questions too, so it
    # can hit it on the same salary turn.
    if leaks_internal_reasoning(reply):
        logger.error("Collector reply leaked internal reasoning: %r", reply[:200])
        return fallback or FALLBACK_QUESTION

    # Claire IS Ming Hwee. A reply narrating what "the agency" did with the
    # client's details is a different speaker; the field's own question is not.
    if speaks_of_us_as_a_third_party(reply):
        logger.warning(
            "Collector reply spoke of us in the third person (%r) - using the plain question",
            reply[:160],
        )
        return fallback or FALLBACK_QUESTION

    # The bot must never INVENT a price. A figure is allowed when it is echoing
    # what the client said ("Noted, $650 budget") or when it comes out of the
    # retrieved records — it was caught inventing "salaries range from $600 to
    # $800" while asking about budget, and that is what this stops. The records
    # are included now that the collector can answer a fee or salary question
    # from them; without that, every correct answer it gave would be thrown away
    # and replaced with the bare question.
    allowed = " ".join(
        [
            state.get("incoming_text", ""),
            state.get("history_text", ""),
            state.get("rag_context", ""),
        ]
        + [str(v) for v in (prompt_state.get("collected_info") or {}).values()]
    )
    invented = ungrounded_figures(reply, allowed)
    if invented:
        logger.warning("Collector reply quoted unstated figure(s) %s — using the plain question", invented)
        return fallback or FALLBACK_QUESTION

    # Grounded is not the same as wanted. The knowledge base really does hold
    # "approximately S$14,000-17,500" and the $1,568 service fee off Form A, so
    # ungrounded_figures passes them happily — and the client's instruction
    # (2026-09-04) is that a new hire's cost is never put in front of anyone
    # before a salesperson has. Small-ticket services are the opposite and are
    # not in COST_WITHHELD_SERVICES.
    if withhold_cost and quotes_hiring_package_cost(reply):
        logger.info("Collector reply priced the hire — deferring the cost to a consultant instead")
        return COST_DEFERRAL_REPLY

    # "Our consultant will share the package details" is a handover announcement.
    reply = clamp_reply(strip_handover_talk(reply), max_sentences=max_sentences)
    previous = last_bot_line(state.get("history_text", ""))

    # Asking the same thing twice in identical words makes the client feel
    # unheard; opening seven messages running with "May I know" is the same tell
    # in slower motion. One rewrite attempt, told explicitly what it just said.
    repeated = previous and near_duplicate(reply, previous)
    echoed_opener = previous and same_opening(reply, previous)
    if repeated or echoed_opener:
        logger.info(
            "Collector %s — rewriting once",
            "repeated its previous message" if repeated else "reused its opening words",
        )
        retry_instruction = (
            f'{instruction}\n\nYou just sent this and it was NOT answered:\n"{previous}"\n'
            "Do not send it again. Acknowledge what the client actually told you, "
            "then ask for the missing detail a different way.\n"
            "Do not begin with the same words you began that message with — vary how "
            "you open, or open with nothing at all and just ask."
        )
        retry = await complete(
            build_system_prompt(prompt_state, extra_instructions=retry_instruction),
            user_prompt,
            temperature=0.6,
            max_tokens=140,
        )
        retry = clamp_reply(
            strip_handover_talk(strip_meta_commentary(retry.strip().strip('"'))),
            max_sentences=2,
        )
        if (
            retry
            and not is_degenerate(retry)
            and not near_duplicate(retry, previous)
            and not same_opening(retry, previous)
        ):
            reply = retry

    return strip_repeated_opener(reply, *recent_bot_lines(state.get("history_text", "")))
