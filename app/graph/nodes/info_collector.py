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
    CANDIDATE_BRIEFING_NOTE,
    SERVICE_BRIEFING_NOTE,
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
    "assistant. One short sentence, your own words, then the rest of your reply. Do "
    "not skip it because you have an answer to give; give the answer after it."
    f"{chr(10)}{chr(10)}"
    "Do NOT promise a consultant, a colleague or a live agent in this message. "
    "Nobody has asked for one and nothing has gone wrong yet, so offering one "
    "unprompted reads as hedging before you have even started — the client called "
    "it weird. Rule 1 already covers what to say if they ask what you are, and a "
    "real handover is announced when it actually happens."
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


# A passport-renewal question whose ANSWER changes with the helper's
# nationality. The agency's process flow (2026-09-07) is explicit that the
# routes genuinely differ and that the bot must not assume one fits all:
#
#     Philippines  -> holds an embassy contract; SHE reports to the embassy
#                     herself and meets the runner there
#     Indonesia    -> holds an embassy contract; the runner collects her from
#                     the employer's home and brings her back
#     Myanmar      -> NO embassy contract, so three more forms are signed
#                     first; runner collects her from the home
#
# Retrieval cannot protect us here on its own. The nationality filter is
# dropped when we do not know the nationality — deliberately, because for most
# services a nationality-labelled row is still useful — so every route is in
# scope at once. Measured 2026-09-07 with nationality unknown: "what is the
# process" returned the MYANMAR row top (0.472) and "does someone go with her
# to the embassy" returned the FILIPINO one (0.609). Answer either confidently
# to an employer of the other nationality and we have told them to prepare the
# wrong forms.
#
# So when the route matters and we do not yet know which route it is, the model
# is told to say so and ask, rather than pick whichever row scored highest.
_NATIONALITY_DEPENDENT = re.compile(
    r"\b(process|procedure|step|steps|document|documents|paperwork|form|forms|"
    r"requirement|requirements|need(?:ed)?|require[ds]?|submit|sign|embassy|"
    r"contract|appointment|how\s+does\s+it\s+work|what\s+happens)\b",
    re.IGNORECASE,
)


# Home leave runs the same two-route problem one service along, and it bites
# harder: for a passport renewal the nationality changes the paperwork, but for
# home leave it changes the paperwork AND the lead time AND the price. The
# Philippines needs her ORIGINAL passport, a ticket itinerary and six embassy
# forms returned with original signatures, takes about 4 weeks and costs $400.
# Indonesia needs copies and one form we provide, takes about 2 weeks and costs
# $250. Answer the wrong one and the client has budgeted the wrong amount
# against the wrong deadline and gathered the wrong documents.
#
# So this pattern deliberately carries what _NATIONALITY_DEPENDENT leaves out -
# the timing and the money words. It is a separate regex rather than a widening
# of that one for exactly the reason recorded there: the passport durations come
# from rows that are NOT nationality-split, and a cost question on a passport
# renewal has one answer ($450 either way), so making the passport note fire on
# "how long" or "how much" would suppress answers it can safely give.
_HOME_LEAVE_ROUTE_DEPENDENT = re.compile(
    r"\b(process|procedure|step|steps|document|documents|paperwork|form|forms|"
    r"requirement|requirements|need(?:ed)?|require[ds]?|submit|sign|embassy|"
    r"endorsement|itinerary|contract|appointment|"
    r"how\s+long|how\s+soon|how\s+much|how\s+quickly|how\s+early|"
    r"when\s+can|when\s+should|when\s+would|when\s+do\s+i|"
    r"lead\s?time|timeline|time\s?frame|duration|"
    r"cost|costs|price|prices|fee|fees|charge|charges|"
    r"how\s+does\s+it\s+work|what\s+happens)\b",
    re.IGNORECASE,
)


# Which services answer differently depending on the helper's nationality, and
# what counts as a question whose answer would change. Per-service rather than
# one shared pattern, because the two services are route-split on different
# things - see the note above _HOME_LEAVE_ROUTE_DEPENDENT.
#
# The second half of each entry is what the model is told NOT to name. That is
# also per-service: a passport renewal must not name a form belonging to one
# route, and a home leave must not name a fee or a lead time either.
_ROUTE_BY_NATIONALITY: dict[str, tuple[re.Pattern[str], str]] = {
    "passport_renewal": (
        _NATIONALITY_DEPENDENT,
        "a nationality, an embassy, or a form that only one route needs",
    ),
    "home_leave": (
        _HOME_LEAVE_ROUTE_DEPENDENT,
        "a nationality, an embassy, a form, a fee, or a lead time that only "
        "one route needs",
    ),
}


# A direct hire runs two routes and the client's question almost never says
# which. A helper already in Singapore on a valid permit skips the embassy and
# the flight entirely; one overseas goes through both. Unlike the passport
# branch this changes the TIMELINE as well as the paperwork - 2 to 3 weeks
# against 4 to 6 - so answering the wrong one hands the client a delivery date
# they will plan around. Timing words are in this pattern and not in
# _NATIONALITY_DEPENDENT for exactly that reason.
_LOCATION_DEPENDENT = re.compile(
    r"\b(process|procedure|step|steps|document|documents|paperwork|form|forms|"
    r"requirement|requirements|need(?:ed)?|require[ds]?|submit|sign|embassy|"
    r"contract|travel|flight|ticket|arrive|arrival|"
    r"how\s+long|how\s+soon|how\s+quickly|when\s+can|when\s+would|timeline|"
    r"time\s?frame|duration|how\s+does\s+it\s+work|what\s+happens)\b",
    re.IGNORECASE,
)


def _known_helper_location(state: ConversationState) -> str | None:
    """Which of the two direct-hire routes applies, if we have been told yet.

    Read straight off the collected field rather than guessed from the message:
    `direct_hiring` asks "Where is she at the moment", so the answer is either
    on the record or it is not.
    """
    value = str((state.get("collected_info") or {}).get("helper_location") or "").strip()
    return value or None


def _known_nationality(state: ConversationState) -> str | None:
    """The PH/ID/MM code this conversation is about, if we have it yet.

    Same source and same normalisation as rag_retriever._nationality, so the
    note below fires on exactly the turns where the retrieval filter was
    dropped. 'none' is "no preference", which is the absence of an answer, not
    an answer.
    """
    collected = state.get("collected_info") or {}
    code = lead_service.nationality_code(str(collected.get("nationality") or ""))
    return code if code and code != "none" else None


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


def _undecidable_gate_keys(service_type: str, collected: dict[str, Any]) -> list[str]:
    """Gate keys holding a value that opens NOTHING. The question was not answered.

    A disambiguating question exists to open one of two branches. Gate.state()
    ends `return "open" if _mentions(value, matches) else "closed"`, so a value
    matching neither list CLOSES the gate - and where two opposing gates key off
    the same field, a value recognised by neither closes BOTH and the flow has
    nowhere left to go.

    Live, 2026-09-07, ticket CB-2026-0004. "Hi I'm looking for a transfer
    helper" was extracted as transfer_direction='transfer' - true, useless, and
    matching neither _TAKING_ON_TRANSFER nor _RELEASING_HELPER. Both gates
    closed, which shut all six of the fields that would have qualified the
    request, and the only ungated field left in transfer_employer was
    `timeline`. So the entire conversation was "when are you hoping to have the
    transfer arranged?" -> "Asap" -> collection complete -> live agent, and the
    ticket that reached the agent read:

        {'timeline': 'as soon as possible - within 2 weeks',
         'transfer_direction': 'transfer'}

    Nothing about what the client needs, their household, or a nationality -
    the client's own words: "Live agent won't be able to do any candidate
    matching just based on [that]".

    This is deliberately generic: every gated service (transfer_employer,
    insurance, direct_hiring, new_hiring) can hit it the moment the extractor
    returns a plausible-sounding value the gate does not recognise.
    """
    gated: dict[str, list[Any]] = {}
    for field in ticket_service.fields_for(service_type):
        if field.gate:
            gated.setdefault(field.gate.field, []).append(field.gate)

    stuck = []
    for key, gates in gated.items():
        value = str((collected or {}).get(key) or "").strip()
        if not value or value.lower() == ticket_service.UNANSWERED:
            continue  # genuinely unanswered - the gates are undecided, which is right
        if not _gates_are_exhaustive(service_type, key, gates):
            continue
        if not any(gate.state(collected) == "open" for gate in gates):
            stuck.append(key)
    return stuck


def _gates_are_exhaustive(
    service_type: str, key: str, gates: list[Any]
) -> bool:
    """Whether opening NO gate really means the question was not answered.

    It only means that where the gates cover the whole answer space. On
    `transfer_direction` they do: two opposing gates, two options, every valid
    answer opens one of them, so a value opening neither is a value we did not
    understand.

    On `requirement` they emphatically do not, and shipping the rule without
    this test broke a live conversation on 2026-09-08. Its gates are
    `children_detail` (childcare) and `elderly_detail` (eldercare) - partial
    branches off a field whose own options include "general housework and
    cooking", which is a complete, correct answer that opens neither. So the
    client said "General house work", the value was blanked as undecidable and
    the question re-asked; they answered "Only general housework" and it was
    blanked again; the third time they wrote "I have tell several time I need
    general housework" and the flow only moved on because max_asks ran out.
    Their words, on the second re-ask, are the whole bug report.

    Derived from the field's own declared options rather than a list of
    exempt keys, so a new gate or a reworded option cannot leave this stale.
    A field with no options cannot be checked and is left alone - the rule is
    off unless it can be shown to be safe.
    """
    field = next(
        (f for f in ticket_service.fields_for(service_type) if f.key == key), None
    )
    if not field or not field.options:
        return False
    return all(
        any(gate.state({key: option}) == "open" for gate in gates)
        for option in field.options
    )


def _known_fields(
    state: ConversationState, service_type: str | None = None
) -> dict[str, str]:
    known: dict[str, str] = {}

    # The name on their FILE. Filled before the lead and the push name, because
    # a master record is the strongest thing we have and the one the agency
    # means by "if the name is in the database". Absent for a number we have
    # never matched, which is exactly when the flow must ask.
    record_name = str(state.get("record_name") or "").strip()
    if record_name:
        known["full_name"] = record_name[:300]

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

    # ... and a client we have placed a helper for did not find us on Google.
    # Asking them how they heard about us is the same question as asking
    # whether they are returning, one step removed, and the client called it
    # out on 2026-09-08. Only filled when there IS a placement: zero means "no
    # placement on record", which is not evidence of how a first-timer found
    # us, so a first-timer is still asked.
    if prior_hires:
        known["referral_source"] = "returning client - placed with us before"

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
        return _with_push_name(state, known, service_type)

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
    return _with_push_name(state, known, service_type)


def _with_push_name(
    state: ConversationState,
    known: dict[str, str],
    service_type: str | None = None,
) -> dict[str, str]:
    """Fall back to the WhatsApp push name for full_name, when it is one.

    Applied last, so a name the client actually gave us on an earlier enquiry
    (the lead row above) always wins over whatever they set on their profile.
    Only ever full_name — never helper_name: the push name belongs to whoever
    holds the phone, and on a replacement or transfer enquiry that is the
    employer, not the helper being asked about.
    """
    # ...and on some flows it is not a fallback at all. See
    # ticket.NAME_FROM_RECORD_ONLY: a passport renewal asks for the name unless
    # our own records hold it, because the push name is a profile label and this
    # flow is collecting the name that goes on paperwork.
    if service_type in ticket_service.NAME_FROM_RECORD_ONLY:
        return known
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
# Deferring the choice back to us is not a preference. The optional lead-in
# matters: this was anchored hard at ^, so "whatever you want" matched and
# "you do whatever you want" did not - and the second is how people say it.
# Live, 2026-09-10, on a replacement enquiry (ticket CB-2026-0006).
#
# The prefix list is deliberately pure filler - pronouns and auxiliaries that
# cannot introduce a real answer. "want" is NOT in it: "I want any Filipino"
# is a preference, not the absence of one.
_NO_PREFERENCE = re.compile(
    r"^(?:(?:i|you|we|it|its|it'?s|that|that'?s|thats|just|do|really|honestly|please)\s+){0,3}"
    r"(any|anything|any\s?one|no\s+preference|no\s+pref|up\s+to\s+you|you\s+decide|"
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

# The same trap as _CARE_TYPE_FIELDS, one field along: a value that restates
# the REQUEST rather than describing the helper they want.
#
# Live 2026-09-10, ticket CB-2026-0006. The client wrote "I have not decided
# yet but I don't want her anymore you do whatever you want just replace her",
# the extractor filed `replacement_preferences = 'replace her'`, the field
# looked answered, and so it was NEVER PUT TO THEM - the collection went
# straight from the timeline to the handover. The agent opened a ticket
# reading "Wants in the replacement: replace her", which says nothing about
# who to look for, on the one field that exists to say exactly that.
#
# Its own filler rather than _CARE_TYPE_FILLER, so `requirement` is untouched:
# adding words there makes THAT test stricter and could start dropping real
# care types. The words to strip here are the ones belonging to the request -
# replace/change/new - plus the pronouns standing in for the helper.
_PREFERENCE_FIELDS = {"replacement_preferences"}

_PREFERENCE_FILLER = re.compile(
    r"\b(replace|replaces|replaced|replacing|replacement|change|changing|changed|"
    r"swap|switch|new|another|next|other|else|different|"
    r"her|him|his|she|he|them|they|it|this|that|one|someone|somebody|anyone|"
    r"i|we|my|our|me|us|you|your|a|an|the|to|for|of|in|is|am|are|and|but|just|do|"
    r"want(?:ed|ing|s)?|need(?:ed|ing|s)?|look(?:ing)?|get(?:ting)?|find(?:ing)?|"
    r"like|please|kindly|asap|soon|now|"
    r"helper|helpers|maid|maids|domestic|worker|mdw|fdw)\b",
    re.IGNORECASE,
)


def _states_a_preference(text: str) -> bool:
    """Whether the value says anything about the helper they actually want.

    Subtractive for the same reason as _states_a_care_type: the agency's
    vocabulary keeps growing, and a whitelist would drop the first unfamiliar
    thing anyone asks for.
    """
    remainder = _PREFERENCE_FILLER.sub(" ", text or "")
    return bool(re.sub(r"[^a-z0-9]+", "", remainder.lower()))


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
    "home_size",
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
    # "what if" / "what about" / "what happens" carry no question mark and none
    # of the other openers, so live on 2026-09-08 "what if her work permit
    # expires at the same time" got only the next question back - the client's
    # question was never acknowledged, let alone answered. _VALUE_IS_QUESTION
    # already reads all three as questions (its `what` suffix is optional), so
    # until now the two patterns disagreed about the same words, which is the
    # exact mismatch the note below this one was written about.
    r"\?|\bwhat\s+(?:if|about|happens)\b|"
    r"\b(how\s+(much|long|many|do|does)|what\s+(documents?|do\s+i|is|are)|"
    r"which\s+documents?|when\s+(can|will|do)|cost|price|fee|fees|charge|"
    r"salary|levy|deposit|require[ds]?|needed|"
    r"can\s+(you|we|i)|could\s+you|do\s+you\s+(have|provide|offer|know)|"
    r"are\s+you\s+able|will\s+you|is\s+it\s+possible|"
    r"any\s+(idea|chance)|possible\s+to)\b"
    # "is there" / "are there" only count when they OPEN the message. As an
    # interrogative they always do ("is there a fee?"); trailing, they are the
    # ordinary Singaporean and Indian English way of stating that something
    # exists. Live, 2026-09-08: "6 bedroom and 6 bathrooms are there" matched
    # here, so the collector believed a question had been asked and closed on
    # "Will she have her own room...? I'll confirm your question with the team
    # and come back to you." - a promise to answer a question the client had
    # never asked, which leaves them waiting for a reply that cannot come.
    # _VALUE_IS_QUESTION anchors all of its own word alternatives at ^ for
    # exactly this reason; these two were the pair that got left unanchored.
    r"|^\s*(?:is|are)\s+there\b",
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

# A question that a bare yes or no cannot answer: one that does not open with
# an auxiliary verb. "Will she have her own room?" and "Do you have any pets?"
# are settled by "yes"; "Any preference on her age or how much experience she
# should have?" is not - it is grammatically a yes/no question and carries no
# information at all when answered that way.
#
# Live, 2026-09-08: that exact question was answered "Yes", the field closed on
# it, and the ticket reached the agent saying the client had a preference about
# age and experience without saying what it was. `additional_notes` survived the
# same shape only because the model happened to ask again unprompted.
_YES_NO_QUESTION = re.compile(
    r"^\s*(?:do|does|did|is|are|was|were|will|would|can|could|shall|should|"
    r"have|has|had|may|might)\b",
    re.IGNORECASE,
)

def _yes_no_question(question: str) -> bool:
    """Whether a bare yes or no actually answers this question.

    The auxiliary does not have to be the first word. Live, 2026-09-09:
    `special_duties` is written "Beyond the usual cleaning and cooking, WOULD
    she need to do things like high-rise window cleaning...", which is a
    yes/no question wearing a subordinate clause - and a client who answered
    "no" had it read as an unfinished answer and got the whole question again.
    Exactly the anchoring mistake `_ASKS_SOMETHING` made in the other
    direction on 2026-09-08.
    So: the opening of the question, or the opening of any clause after a
    comma. `helper_profile` ("Any preference on her age or how much experience
    she should have?") still has no auxiliary anywhere and still re-asks,
    which is the case the rule was written for.
    """
    text = (question or "").strip()
    if _YES_NO_QUESTION.match(text):
        return True
    return any(_YES_NO_QUESTION.match(part.strip()) for part in text.split(",")[1:])


# Deliberately a BARE yes or no and nothing else. "Yes all" answers the
# extra-duties question completely and must not be re-asked; "Yes" alone
# answers nothing.
_BARE_YES_NO = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|ya|sure|ok|okay|no|nope|nah)\W*$",
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
        # `<=`, not `<`, and for the same reason the email branch uses it:
        # helper_profile and additional_notes are both max_asks=1, so the
        # ordinary limit would rule out ever querying a bare "Yes" on exactly
        # the two fields this was written for. One extra ask, never more.
        elif (
            asked.get(field.key, 0) <= min(field.max_asks, MAX_ASKS_PER_FIELD)
            and _BARE_YES_NO.match(value)
            and not _yes_no_question(field.question)
        ):
            note = (
                f'\n\nThey answered "{value}", which does not tell you anything '
                "about it. Your question was not a yes-or-no one. Acknowledge "
                "them and ask warmly for the actual detail, in your own words - "
                "do not repeat the question back word for word."
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


# Why we are asking, for the questions where a client would reasonably wonder.
#
# Thomas, 2026-09-09: "Right now Claire fires off questions back-to-back with no
# context ... Customers are more likely to answer fully and feel at ease if she
# briefly explains why she's asking." He named the four - pets, house rules,
# budget, rest days - and was equally clear about the rest: "We don't need this
# on every single question ... Keep the simpler ones (number of children, home
# type) short and direct as they are now." A reason attached to "how many
# bedrooms" is padding, and padding on every turn is its own kind of robotic.
#
# The REASON is supplied and the wording is not, the same rule
# _COLLECTION_PURPOSE follows: a fixed lead-in repeated at four points in one
# conversation is exactly the formula strip_repeated_opener exists to stop.
#
# Keyed on the field key, so the ones shared with transfer_employer through
# _hiring_field get it in both flows without a second copy (§9.8).
_WHY_WE_ASK: dict[str, str] = {
    "pets": "so we only put forward helpers who are genuinely comfortable "
            "around animals - it is one of the things every helper is asked "
            "about, and a mismatch here goes wrong quickly",
    "rest_day": "so we can set the expectation with the helper before she "
                "accepts, which is where most rest-day disagreements start",
    "additional_notes": "so anything that matters to them is agreed with the "
                        "helper up front rather than discovered later",
}

# BUDGET IS DELIBERATELY ABSENT from _WHY_WE_ASK, and Thomas named it. Measured four times on
# 2026-09-09, twice with no retrieval and twice through the real path with the
# salary rows in context: told to explain WHY it wants a budget, the model
# supplies a helpful salary range out of its own knowledge - "$500 to $700" -
# ungrounded_figures bins the entire reply, and the client receives the bare
# "Do you have a monthly salary budget in mind?" with no reason attached at all.
# That is strictly worse than not explaining, and it costs an extra LLM call to
# get there. Rewording the reason to contain no money word did not help; the
# question is about salary, so the model completes it with one.
#
# The way to close this is data, not prompt: with a grounded salary band in the
# knowledge base for the nationality and experience in hand, the figure would
# survive ungrounded_figures and the explanation with it. That is Ming Hwee's to
# supply. Until then the budget question stays short and direct, which is what
# it did before.


def briefs_on_this_turn(service_type: str | None, asked: dict | None) -> bool:
    """Whether this is the turn a small-ticket service explains itself.

    Exactly one turn per collection — the one after the first question, so the
    briefing never shares a message with the introduction (see the note at the
    call site for the 2026-09-04 reason).

    This is a function, and asserted by scripts/selfcheck_flows.py across a
    whole turn sequence, because the condition it replaces COULD NOT EVER BE
    TRUE and nobody noticed for five days:

        brief_on_turn = 1 if _is_first_contact(state) else 0
        if service_type in _SMALL_TICKET_SERVICES
           and sum(asked.values()) == brief_on_turn:

    `_is_first_contact` is only true while the history is empty, which is only
    true while nothing has been asked. So brief_on_turn was 1 exactly when
    sum(asked) was 0, and 0 exactly when sum(asked) was 1 or more — the two
    sides could never meet. Introduced 2026-09-04 in the commit that made the
    briefing "wait a turn"; found 2026-09-09 by walking both small-ticket
    services end to end and noticing that neither of them ever explained
    itself.

    It is the same failure signature as the budget guard: a feature that
    silently does nothing looks exactly like a feature working quietly, because
    what the client gets is a perfectly reasonable question either way.
    """
    if service_type not in _SMALL_TICKET_SERVICES:
        return False

    # A service that briefs at the END does not also brief at the start.
    #
    # passport_renewal is the only one, and letting it do both was a straight
    # regression: measured 2026-09-09 the moment the dead condition above was
    # fixed, the opening overview came back as "We handle the passport renewal
    # from the embassy appointment through to the renewed passport being
    # returned to you" — the embassy and the appointment, which the 2026-09-09
    # meeting removed from this flow by name, arriving before the client has
    # even given the helper's name. The overview query retrieves the process
    # rows, and those rows describe OUR processing.
    #
    # It also has nothing to add: BRIEFING_AFTER gives that flow a full closing
    # briefing with the timeline, the cost, the documents and the client's own
    # next steps, which is what the agency asked for and what they now get.
    if service_type in ticket_service.BRIEFING_AFTER:
        return False

    return sum((asked or {}).values()) == 1


# What a client whose name is already on their file is greeted with, once, at
# the top of a collection. A module constant rather than an inline string so
# selfcheck_flows.py can assert it greets AND forbids re-asking. See the note
# at the call site for the live conversation that produced it.
RECORD_NAME_NOTE = (
    "\n\nUse their name in THIS message: {name}. Open with a short greeting or "
    "acknowledgement that CARRIES the name - \"Thanks, {name}\", \"Hi {name}\", "
    "\"Good to meet you, {name}\" - and then ask your question.\n\n"
    "NOT the bare name with a comma after it. \"{name}, how many people live in "
    "your household?\" is a form calling out a row, not a person saying hello.\n\n"
    "If they gave a full name, their first name on its own is the friendlier "
    "address and the one a colleague would use. Never change the SPELLING of "
    "what they wrote, never expand it, and never invent a fuller version.\n\n"
    "This is the first message since we learned their name, and it is the one "
    "place it belongs - do not go on using it in every later message. And never "
    "ask for a name we are already holding."
)


# What a returning client is told, once, at the top of a collection. A module
# constant rather than an inline string so selfcheck_flows.py can assert its
# two halves: that it DOES refer to the last enquiry, and that it does not
# read their file back at them. See the note at the call site.
RETURNING_NOTE = (
    "\n\nOur own records show they have hired a helper through us before, so "
    "that is already established and you must never ask it. Open by welcoming "
    "them back, by name if you know it.\n\n"
    "If the notes above show a previous enquiry, refer to it in the same "
    "breath — what it was about, in a few words — and ask whether this is a "
    "follow-up on that or something new. It is the difference between being "
    "remembered and being processed, and they have told us so.\n\n"
    "ONE enquiry, the most recent, and only what it was about. Do not give "
    "them dates, do not tell them how many times they have hired, and do not "
    "list their history back at them — that is reading out their file, which "
    "is a different and much worse thing. If the notes show no previous "
    "enquiry, just welcome them back and ask your question; never invent one."
)


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

    why = _WHY_WE_ASK.get(field.key)
    if why:
        parts.append(
            f"\n\nThis is one of the questions a client can reasonably wonder about, "
            f"so say WHY you are asking before you ask it. The reason is: {why}. "
            "Put that in your own words, in a short clause — not a sentence of its "
            "own, and never the same phrasing you used the last time you explained "
            "yourself. Do not do this on the plain questions; it is for this one "
            "because it would otherwise feel intrusive or arbitrary.\n\n"
            "Explaining yourself is NOT an invitation to give examples. Quote no "
            "figure, no range and no salary while you do it. Live, 2026-09-09: the "
            "reason for the budget question ('so we shortlist helpers whose asking "
            "salary is within their range') led straight to an invented "
            "'$500 to $700', ungrounded_figures binned the whole reply, and the "
            "client got the bare question with no reason attached at all — the "
            "exact opposite of what this note is for."
        )

    if field.options:
        # A field whose own written question already spells the options out is
        # asking for all of them ON PURPOSE, and the "drop two or three in"
        # rule below silently undoes that. Live, 2026-09-07: `languages` was
        # rewritten on 2026-09-04 precisely because the bot was "hiding four of
        # its options", and it STILL went out as "such as English, Mandarin or
        # Malay?" - because this instruction told it to. Two prompts pulling
        # opposite ways, and the more specific one lost. Where the option set is
        # something a client cannot guess at, naming them all IS the question: a
        # Tamil-speaking household shown three Chinese and Malay options can
        # only conclude we do not place Tamil speakers.
        listed = sum(
            1 for opt in field.options if opt.lower() in (field.question or "").lower()
        )
        if listed >= 3:
            parts.append(
                "\n\nThe answers the office works with here are: "
                + ", ".join(field.options)
                + ". The written question above names them deliberately, so name them "
                "all - the client cannot choose an option they were never shown. Keep "
                "it one flowing sentence rather than a numbered menu, and make clear "
                "they may give more than one, or something not on the list."
            )
        else:
            parts.append(
                "\n\nThe answers the office works with here are: "
                + ", ".join(field.options)
                + ". Use them to shape the question - dropping two or three in as "
                "examples is how a person asks it. Never read the whole set out, never "
                "number them, and never present them as a menu to choose from. "
                "Whatever the client answers is their answer, listed or not."
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
        # ...and the same test against what the CLIENT actually wrote. The
        # rule above only inspects the VALUE, so it catches a value that
        # restates the enquiry ("hire a helper") and waves through one the
        # extractor invented out of the same words. Live, 2026-09-07: the
        # opening message was "I want to hire a helper" and nothing else, and
        # `requirement` came back "household chores" - plausible, never said.
        # The field was therefore never asked (the collector opened on question
        # six, "how many people live at home"), and the agent was handed
        # "Care type: household chores" as though the client had stated it.
        # Matching a helper against a requirement nobody gave is worse than
        # having no requirement at all.
        #
        # A volunteered care type still lands: the test is on the client's own
        # words, and "I need someone for my mum who is bedridden" passes it
        # while "looking for a maid" does not. Only applied when the field was
        # never put to them - once asked, their answer is their answer.
        if (
            key in _CARE_TYPE_FIELDS
            and not asked.get(key)
            and not _states_a_care_type(state.get("incoming_text") or "")
        ):
            logger.info(
                "Conversation %s: ignoring '%s' for '%s' - the client's message "
                "named no care type, so the value was inferred rather than given",
                state.get("conversation_id"),
                text[:40],
                key,
            )
            continue
        # "replace her" is the request restated, not a description of the
        # helper they want - and a field that already looks answered is never
        # asked. See _PREFERENCE_FIELDS for the ticket this reached.
        if key in _PREFERENCE_FIELDS and not _states_a_preference(text):
            logger.info(
                "Conversation %s: ignoring '%s' for '%s' - it restates the request "
                "rather than describing the helper they want",
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
        for key, value in _known_fields(state, service_type).items()
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
    # Fired off `first_time_hire` alone until 2026-09-08, and that key only
    # exists in new_hiring's field list - `known` is filtered to the current
    # service's own keys - so a returning client asking about a transfer, a
    # renewal or a passport got no acknowledgement at all that we had met them
    # before. The agency's instruction was explicit: "if user is existing then
    # greet them by name and then ask further questions accordingly."
    #
    # The opening-turn test is the same one purpose_note uses, so this still
    # says it ONCE per collection rather than every turn - which is what the
    # `known` test was doing the work of before.
    returning_note = ""
    if (
        _prior_hires(state)
        and not recognised_note
        and ("first_time_hire" in known or not any(asked.values()))
    ):
        # Widened 2026-09-09. It used to forbid ALL detail ("no details of who,
        # when or how many, we are not showing them their file") - written to
        # stop the bot reciting somebody's file back at them. Thomas asked for
        # the opposite of the half that matters: "Recognise them by name if
        # known, reference their last enquiry or helper status (e.g. 'Welcome
        # back, Vaidik! Last time we spoke about a childcare helper - are you
        # following up on that, or is this a new request?') ... This alone will
        # make repeat customers feel remembered rather than processed."
        #
        # So ONE enquiry, the most recent, in a clause - and the rest of the
        # ban stands: no counts, no dates, no listing their history. The
        # previous-enquiry block in the system prompt is the only source; there
        # is nothing to reference when it is empty, and inventing one is worse
        # than a plain welcome.
        returning_note = RETURNING_NOTE

    # Their name is on their file, and nothing above is going to use it.
    #
    # Live, conversation 3766 (2026-09-09): a number matching employer
    # "tunaktun" opened with "Hi, I'm Claire, Ming Hwee's AI assistant. May I
    # know your helper's name?" — no name, straight past the client to the
    # helper. The agency reported it in the same words as the 2026-09-08
    # defect: "it still didn't ask name".
    #
    # It had NOT skipped the question by mistake. `full_name` was correctly
    # pre-filled from employers.display_name, which is the rule the agency
    # themselves gave us on 2026-09-08: "ask for the name first if the name is
    # not in the database. If the name is in the database, greet them before
    # moving forward." The skip was right; the greeting half simply never
    # happened, and from the client's side "never asked my name" and "knows my
    # name and won't say it" are the same bot.
    #
    # Why it needs a note at all: `full_name` reaches the prompt inside
    # "Already confirmed by the client (do not ask again)", which is an
    # instruction NOT to ask. Nothing there says to USE it, and prompt rule 1c
    # (use the client's name when you know it) loses to COLLECTOR_INSTRUCTION's
    # "ask for that one detail and nothing else" — exactly how the introduction
    # was being dropped on 2026-09-04, and fixed the same way, in the
    # instruction that actually wins on a collector turn.
    #
    # The two notes above already open the message when they fire — one
    # welcomes them back by name, the other opens on the helper we can see — so
    # this is gated behind both. One opener, never two.
    # Greet them by name on the FIRST turn we know it, however we learned it,
    # and never again.
    #
    # Two live reports, 2026-09-09 and 2026-09-10, and they are the same defect
    # from opposite ends. An EXISTING client got "Hi, I'm Claire ... Welcome
    # back - may I know how many children you have", with the name on their file
    # never used, and asked outright: "You didn't ask me for my name. What is
    # the reason behind it?" A NEW client who had just typed their name got the
    # next question with no greeting at all - "Vaidik Dubey" -> "How many people
    # live in your household?".
    #
    # The agency's rule covers both: "if user is existing then it should greet
    # by name at starting then move forward to our flow; if user is new then ask
    # the user name, then in next message greet the user with our followup
    # question."
    #
    # So it is no longer gated behind returning_note and recognised_note. This
    # is NOT a competing opener - those two decide what the message opens WITH,
    # and this decides that whatever it opens with carries the client's name.
    # Gating it behind them is exactly what silenced it for every returning
    # client, which is most of them.
    #
    # `previous` is what was collected before this turn, so the test is "the
    # name became known on THIS turn" - true on the opening turn for a client
    # whose name is on file, and true on the turn after a new client types it.
    client_name = str(collected.get("full_name") or "").strip()
    already_greeted = bool(str(previous.get("full_name") or "").strip())
    record_name_note = ""
    if client_name and not already_greeted and client_name != UNANSWERED:
        record_name_note = RECORD_NAME_NOTE.format(name=client_name)

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
    # NOT on the same turn as the introduction. Live 2026-09-04 19:40, the
    # opening message was "Hi Vaidik, I'm Claire, Ming Hwee's AI assistant, and
    # I'll bring in one of our consultants whenever needed. Passport renewal
    # timing depends on your helper's nationality and embassy. May I know your
    # helper's name?" — three things at once, and the middle one said nothing,
    # because on turn one we do not yet know the nationality it depends on. The
    # briefing waits a turn; by then an answer or two is in and it can be
    # concrete.
    small_ticket_note = ""
    if briefs_on_this_turn(service_type, asked):
        small_ticket_note = (
            f"{chr(10)}{chr(10)}This is a short, well-defined job we handle end to end, not "
            "something to hand straight to a colleague. THIS MESSAGE IS THE ONE "
            "EXCEPTION to \"ask for that one detail and nothing else\" above: open "
            "with a single sentence saying what the job involves or how long it "
            "takes, and THEN ask your question. Two sentences, and the first one is "
            "not optional — a client four questions into a form has been told "
            "nothing about what they are buying."
            f"{chr(10)}{chr(10)}Ground that sentence strictly: ONLY what the records "
            "above actually state. If the records say "
            "nothing about it, just ask your question and add nothing. Never "
            "estimate a price, a duration or a document list that is not written "
            "there."
            f"{chr(10)}{chr(10)}And say nothing at all rather than something that "
            "amounts "
            "to \"it depends\". If the records make the answer conditional on "
            "something you have not been told yet — her nationality, where she is "
            "— then you do not have an answer to give: skip the sentence and ask "
            "your question. A client who is told the timing depends on the "
            "nationality, and is then asked for the nationality, has been given "
            "nothing and made to read a sentence for it."
        )

    # Passport renewal branches on nationality and the branches are not
    # cosmetic: a Myanmar helper holds no embassy contract, so three forms a
    # Filipino employer never sees have to be signed before anything is
    # submitted. With the nationality unknown the retrieval filter is dropped
    # and all three routes compete, so the top row is whichever phrasing
    # happened to score best — the Myanmar one, measured, for a bare "what is
    # the process". Naming the route we have not established is how an employer
    # ends up preparing the wrong paperwork.
    nationality_note = ""
    route = _ROUTE_BY_NATIONALITY.get(service_type or "")
    if (
        route
        and not _known_nationality(state)
        and route[0].search(state.get("incoming_text") or "")
    ):
        nationality_note = (
            f"{chr(10)}{chr(10)}The records above describe MORE THAN ONE route, "
            "because the answer differs by the helper's "
            "nationality — and you have not been told hers yet. Do not pick one. "
            f"Do not name {route[1]}"
            ". Give only what is true for all of them, say plainly that it "
            "depends on her nationality, and ask which country she "
            "is from. Once you know it you can be specific."
        )

    # Same shape as nationality_note above, one service along. The retrieval
    # filter does not separate the two direct-hire routes - both are filed
    # under direct_hiring - so with the location unknown the transfer rows and
    # the overseas rows compete, and the top one is whichever phrasing scored
    # best. Quoting "2 to 3 weeks" to an employer whose helper is still in
    # Manila is a promise we cannot keep.
    location_note = ""
    if (
        service_type == "direct_hiring"
        and not _known_helper_location(state)
        and _LOCATION_DEPENDENT.search(state.get("incoming_text") or "")
    ):
        location_note = (
            f"{chr(10)}{chr(10)}The records above describe TWO different routes, "
            "because a helper already in Singapore on a valid work permit skips "
            "the embassy and the travel while one coming from overseas does not "
            "- and you have not been told which applies here. Do not pick one. "
            "Do not quote a timeline, an embassy step or a travel arrangement "
            "that belongs to only one of them. Give only what is true for both, "
            "say plainly that it depends on where she is at the moment, and ask. "
            "Once you know, you can be specific."
        )

    # The turn that stops and explains the whole service. Fires once, on the
    # turn AFTER the field the explanation depends on has been answered - for a
    # passport renewal, her nationality, because every practical answer differs
    # by it and there is nothing honest to say before we know it.
    #
    # Gated on rag_matches, and that is the important half: without records this
    # would be an instruction to improvise a process, a document list and a
    # price, which is the single worst thing this bot can do. With nothing
    # retrieved the turn silently stays an ordinary question.
    # Did the client put a question to us this turn? Read in two places about a
    # hundred lines apart, so it is computed once here rather than derived from
    # `answer_first` below - reading a local before it is assigned is exactly
    # the 2026-09-04 failure that silenced the bot on every single turn, and
    # smoke_nodes.py caught this one the same way.
    client_asked = bool(_ASKS_SOMETHING.search(state.get("incoming_text") or ""))

    # The prompt prints "- WhatsApp name: Vaidik" in its contact block, so
    # suppressing the field alone would still have produced "Hi Vaidik" and
    # then asked for the name. On these flows the model is given the name only
    # when our records hold it.
    hide_push_name = service_type in ticket_service.NAME_FROM_RECORD_ONLY and not str(
        state.get("record_name") or ""
    ).strip()

    briefing_note = ""
    # `answer_first` is the collector's own "they asked us something" flag, and
    # it is the same rule the retriever applies one node earlier: a client who
    # asked a question gets it answered, and the briefing waits a turn. Without
    # this the two instructions would both be in the prompt, pulling opposite
    # ways on the same reply.
    briefing_due = (
        ticket_service.briefing_due(
            service_type, collected, state.get("briefed_services")
        )
        and bool(state.get("rag_matches"))
        and not client_asked
    )
    if briefing_due:
        # A job seeker gets her own version. SERVICE_BRIEFING_NOTE is written
        # for somebody buying a service - it REQUIRES a cost section - and
        # pointed at a registration on 2026-09-10 it quoted the passport
        # renewal's $450 as the price of applying for work. See the note above
        # CANDIDATE_BRIEFING_NOTE.
        briefing_note = (
            CANDIDATE_BRIEFING_NOTE
            if service_type in ticket_service.CANDIDATE_SERVICES
            else SERVICE_BRIEFING_NOTE
        )
        # ...and if we have no price for HER nationality, say so rather than
        # reaching for the one sitting beside it in the same record.
        #
        # Never on a candidate flow: that addendum tells the model to say a
        # consultant will confirm "the cost for her embassy", which is a
        # sentence about an employer's service. CANDIDATE_BRIEFING_NOTE already
        # forbids every figure outright, and bolting this on would reintroduce
        # the word cost to the one message that must not contain it.
        if service_type not in ticket_service.CANDIDATE_SERVICES and not (
            ticket_service.fee_is_known_for(service_type, _known_nationality(state))
        ):
            briefing_note += (
                "\n\nWe do NOT have a fee on record for a helper of this "
                "nationality. The records name a price for other nationalities; "
                "that price is theirs and not hers. Do not quote it, do not "
                "adapt it, and do not give a range. Say in one short sentence "
                "that a consultant will confirm the cost for her embassy, and "
                "carry on with the timing and the process."
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

    # An answer that opened no branch is not an answer. Blanked so the
    # disambiguating question is put again rather than the flow collapsing to
    # whatever field happens to be ungated - see _undecidable_gate_keys.
    # Bounded by max_asks through the normal path, so it cannot loop: once the
    # question has been asked its limit it is left alone and the flow moves on.
    for key in _undecidable_gate_keys(service_type, collected):
        field = next(
            (f for f in ticket_service.fields_for(service_type) if f.key == key), None
        )
        if field and asked.get(key, 0) >= field.max_asks:
            continue
        logger.info(
            "Conversation %s: %s=%r opens no branch - asking it again rather than "
            "letting every gated field close",
            state.get("conversation_id"), key, collected.get(key),
        )
        collected.pop(key, None)

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
    if hide_push_name:
        # Applied where the prompt state is actually BUILT, not a hundred lines
        # above it - the flag is set earlier and read here for the same reason
        # client_asked is. Reading a local before it is assigned is the
        # 2026-09-04 failure, and smoke_nodes.py failed seven states on this
        # exact mistake before it could ship.
        system_prompt_state["customer_name"] = ""

    # The client asked something while we were collecting. Retrieval now runs
    # before this node on every turn, so the records are in state and the answer
    # can go out with the next question rather than being ignored.
    answer_first = ANSWER_THEN_ASK_INSTRUCTION if client_asked else ""

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
            + nationality_note
            + location_note
            + purpose_note
            + returning_note
            + record_name_note
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
            if (answer_first or first_contact or small_ticket_note or purpose_note
                or nationality_note or location_note or record_name_note)
            else 2,
            withhold_cost=service_type in COST_WITHHELD_SERVICES,
            grounded_options=next_field.options or (),
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
    ) + dropped_note + briefing_note + answer_first
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
        #
        # ...unless this is the turn that also explains the whole service, which
        # is a heading, a timing, a cost, a lead-in and a document list, a
        # second lead-in and the client's own next steps, and then the handover
        # close. clamp_reply masks the list MARKER's full stop but not the one
        # at the end of a step, so every step still counts as a sentence - two
        # lists of five is ten of the budget before a word of prose.
        max_sentences=20 if briefing_due else 3,
        stepped=briefing_due,
    )

    # A briefing that was generated and then discarded by a guard leaves the
    # bare fallback, and until 2026-09-08 it was still recorded as GIVEN - so it
    # was never tried again and the client simply never got it. Live: after
    # "myanmar" the reply was "When does her current passport expire?", which is
    # that field's hand-written question verbatim, i.e. the fallback. Only mark
    # it given when the reply that actually goes out is not the fallback.
    briefing_lost = briefing_due and reply.strip() == fallback.strip()
    if briefing_lost:
        logger.error(
            "Conversation %s: the %s briefing was discarded by a guard and the "
            "fallback went out instead - it will be tried again next turn",
            state.get("conversation_id"),
            service_type,
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
        # Said once - _merge_unique accumulates and _TURN_RESET leaves this
        # alone. Never recorded when the briefing did not survive the guards.
        "briefed_services": [service_type] if (briefing_due and not briefing_lost) else [],
    }


async def _write(
    state: ConversationState,
    prompt_state: dict[str, Any],
    instruction: str,
    fallback: str = "",
    max_sentences: int = 2,
    withhold_cost: bool = False,
    stepped: bool = False,
    grounded_options: tuple[str, ...] = (),
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
        reply = await complete(
            system_prompt,
            user_prompt,
            temperature=0.45,
            # A four-part briefing does not fit in 140. Same budget the stepped
            # path in response_generator uses; every other turn is unchanged.
            max_tokens=460 if stepped else 140,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Collector reply generation failed")
        return FALLBACK_QUESTION

    reply = strip_meta_commentary(reply.strip().strip('"'))
    # A numbered list IS the requested format on a briefing turn. Headings,
    # bold and bullets stay banned there as everywhere else - see
    # looks_like_document(allow_steps=).
    if is_degenerate(reply) or looks_like_document(reply, allow_steps=stepped):
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
            # The options of the field being asked. They are OUR figures,
            # written by hand in SERVICE_FIELDS, and _field_guidance tells the
            # model to offer two or three of them as examples - so a reply
            # carrying them is doing as it was told, not inventing.
            #
            # Live, 2026-09-09: `budget`'s options are "below $500, $500-600,
            # $600-700, $700-800, above $800", the model duly asked "are you
            # thinking $500-600 or $600-700?", and this guard discarded the
            # whole reply as ungrounded and sent the bare question instead.
            # Four runs out of four, on every hiring conversation, and nothing
            # surfaced it because a guard falling back to a correct question
            # looks like nothing going wrong.
            " ".join(grounded_options),
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
