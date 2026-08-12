"""Node 4 — collect the basic info a service request needs.

Asks one question at a time, in the agency's own voice. The client should not
feel like they are filling in a form.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.graph.guards import (
    clamp_reply,
    is_degenerate,
    last_bot_line,
    looks_like_document,
    near_duplicate,
    recent_bot_lines,
    same_opening,
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


def _known_fields(state: ConversationState) -> dict[str, str]:
    lead = state.get("matched_lead")
    if not isinstance(lead, dict):
        return {}

    # leads.full_name is the EMPLOYER's name; leads_candidate.full_name is the
    # helper's. The same column answers helper_name only on a candidate lead —
    # mapped unconditionally it filed a replacement ticket reading "Helper:
    # Vaidik Dubey", which is the employer, and the client was never asked who
    # the helper actually is.
    is_candidate_lead = (state.get("lead_kind") or "").strip().lower() == "candidate"

    known: dict[str, str] = {}
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
    return known


FALLBACK_QUESTION = "Sorry, could you tell me a bit more about what you need?"
FALLBACK_CLOSING = "Noted, thanks for the details. Let me pull this together and come back to you shortly."
FALLBACK_ACKNOWLEDGEMENT = "Got it. Let me look into this and come back to you shortly."

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
_ASKS_SOMETHING = re.compile(
    r"\?|\b(how\s+(much|long|many|do|does)|what\s+(documents?|do\s+i|is|are)|"
    r"which\s+documents?|when\s+(can|will|do)|cost|price|fee|fees|charge|"
    r"salary|levy|deposit|require[ds]?|needed)\b",
    re.IGNORECASE,
)


def _states_a_care_type(text: str) -> bool:
    """Whether a requirement value says anything beyond 'I want a helper'.

    Deliberately a subtractive test rather than a list of care types: the
    agency's own vocabulary keeps growing, and a whitelist would silently drop
    'post-natal' or 'stroke recovery' the first time someone said it.
    """
    remainder = _CARE_TYPE_FILLER.sub(" ", text or "")
    return bool(re.sub(r"[^a-z0-9]+", "", remainder.lower()))


async def _extract(
    state: ConversationState, service_type: str, asked: dict[str, int]
) -> dict[str, Any]:
    fields = ticket_service.fields_for(service_type)
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
    previous = {} if switched else (state.get("collected_info") or {})
    asked = {} if switched else dict(state.get("asked_field_counts") or {})
    if switched:
        logger.info(
            "Conversation %s switched from %s to %s — clearing collected info",
            state.get("conversation_id"),
            state.get("collected_service"),
            service_type,
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

    missing = ticket_service.missing_fields(service_type, collected)

    # The lead is opened the moment the client has given a name, not at the end
    # of collection. A client who answers two questions and then stops used to
    # leave nothing behind at all — no lead, no ticket, no record that anyone
    # had enquired. The requirements gathered afterwards are written onto this
    # same row by the ticket node when collection completes.
    lead_fields = await _open_lead_early(state, service_type, collected)

    carry: dict[str, Any] = dict(extracted)
    counts: dict[str, Any] = {}
    if switched:
        carry[RESET_KEY] = True
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

    if missing:
        next_field = missing[0]
        previous = last_bot_line(state.get("history_text", ""))
        instruction = COLLECTOR_INSTRUCTION.format(
            service_label=label,
            field_label=next_field.label,
            previous_message=previous or "(this is your first message)",
        ) + dropped_note + answer_first
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
            # Answering their question and then asking ours does not fit in two.
            max_sentences=3 if answer_first else 2,
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
        max_sentences=3 if answer_first else 2,
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
