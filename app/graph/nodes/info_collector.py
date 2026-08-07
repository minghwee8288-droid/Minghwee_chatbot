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
    COLLECTOR_INSTRUCTION,
    EXTRACTION_SYSTEM,
    EXTRACTION_USER,
    FEE_HANDOVER_INSTRUCTION,
    HANDOVER_CLOSER_INSTRUCTION,
)
from app.graph.state import RESET_KEY, ConversationState
from app.services import ticket as ticket_service
from app.utils import redact_nric

logger = logging.getLogger(__name__)

SERVICE_LABELS = {
    "new_hiring": "hiring a new helper",
    "candidate_new_hiring": "finding work as a helper",
    "direct_hiring": "direct hire processing",
    "replacement": "replacing their current helper",
    "transfer": "a helper transfer",
    "renewal": "a work permit renewal",
    "home_leave": "home leave for their helper",
    "passport_renewal": "a passport renewal",
    "fee_enquiry": "our fees",
    "salary_enquiry": "helper salary",
    "dispute_salary": "a salary or leave issue",
    "candidate_registration": "registering a helper for placement",
}

ENQUIRY_SERVICES = {"fee_enquiry", "salary_enquiry"}

# §0: phone, source, tenant, branch, temperature, status and lead_number are all
# known before the conversation starts and are never asked. The transfer flow's
# only phone question is which number to use, which is a different thing.
def _known_fields(state: ConversationState) -> dict[str, str]:
    return {}

FALLBACK_QUESTION = "Sorry, could you tell me a bit more about what you need?"
FALLBACK_CLOSING = "Noted, thanks for the details. Let me pull this together and come back to you shortly."

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


def service_label(service_type: str | None) -> str:
    return SERVICE_LABELS.get(service_type or "", "their enquiry")


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
        cleaned[key] = redact_nric(text, context=f"captured:{key}")[:300]
    return cleaned


async def info_collector(state: ConversationState) -> dict[str, Any]:
    # §3 and the routing rule: a helper looking for work produces intent
    # new_hiring exactly as an employer does, and must not be asked an
    # employer's questions.
    contact_type = (state.get("contact_type") or "unknown").strip()
    if contact_type == "unknown":
        contact_type = (state.get("detected_contact_type") or "").strip()
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

    extracted = await _extract({**dict(state), "collected_info": previous}, service_type, asked)

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

    carry: dict[str, Any] = dict(extracted)
    counts: dict[str, Any] = {}
    if switched:
        carry[RESET_KEY] = True
        counts[RESET_KEY] = True

    label = service_label(service_type)
    system_prompt_state = {**dict(state), "collected_info": collected}

    if missing:
        next_field = missing[0]
        previous = last_bot_line(state.get("history_text", ""))
        instruction = COLLECTOR_INSTRUCTION.format(
            service_label=label,
            field_label=next_field.label,
            previous_message=previous or "(this is your first message)",
        ) + dropped_note
        if next_field.optional:
            # §2 step 4 / §23.6: asked once, and a no is taken as an answer.
            instruction += (
                "\n\nThis one is optional. Ask lightly, once. If they say no, do not "
                "have it, or simply move past it, accept that without comment and "
                "never raise it again."
            )
        # If generation degenerates, fall back to the field's own hand-written
        # question from SERVICE_FIELDS — less warm, but always correct.
        reply = await _write(state, system_prompt_state, instruction, fallback=next_field.question)
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

    instruction_template = (
        FEE_HANDOVER_INSTRUCTION if service_type in ENQUIRY_SERVICES else HANDOVER_CLOSER_INSTRUCTION
    )
    instruction = instruction_template.format(
        service_label=label,
        enquiry_label="our fees" if service_type == "fee_enquiry" else "helper salary",
    ) + dropped_note
    reply = await _write(state, system_prompt_state, instruction, fallback=FALLBACK_CLOSING)
    logger.info(
        "Conversation %s finished collection for %s: %s",
        state.get("conversation_id"),
        service_type,
        ticket_service.summarize(service_type, collected),
    )
    return {
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
) -> str:
    system_prompt = build_system_prompt(prompt_state, extra_instructions=instruction)
    user_prompt = (
        f"Conversation so far:\n{state.get('history_text') or '(this is the first message)'}\n\n"
        f"Client's latest message(s):\n{state.get('incoming_text', '')}\n\n"
        "Your reply:"
    )
    try:
        reply = await complete(system_prompt, user_prompt, temperature=0.45, max_tokens=200)
    except Exception:  # noqa: BLE001
        logger.exception("Collector reply generation failed")
        return FALLBACK_QUESTION

    reply = strip_meta_commentary(reply.strip().strip('"'))
    if is_degenerate(reply) or looks_like_document(reply):
        logger.error("Discarded malformed collector reply: %r", reply[:200])
        return fallback or FALLBACK_QUESTION

    # The bot must never quote a price. Figures are allowed only when echoing
    # back what the client said ("Noted, $650 budget") — it was caught inventing
    # "salaries range from $600 to $800" while asking about budget.
    allowed = " ".join(
        [state.get("incoming_text", ""), state.get("history_text", "")]
        + [str(v) for v in (prompt_state.get("collected_info") or {}).values()]
    )
    invented = ungrounded_figures(reply, allowed)
    if invented:
        logger.warning("Collector reply quoted unstated figure(s) %s — using the plain question", invented)
        return fallback or FALLBACK_QUESTION

    # "Our consultant will share the package details" is a handover announcement.
    reply = clamp_reply(strip_handover_talk(reply), max_sentences=3)
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
            max_tokens=200,
        )
        retry = clamp_reply(
            strip_handover_talk(strip_meta_commentary(retry.strip().strip('"'))),
            max_sentences=3,
        )
        if (
            retry
            and not is_degenerate(retry)
            and not near_duplicate(retry, previous)
            and not same_opening(retry, previous)
        ):
            reply = retry

    return strip_repeated_opener(reply, *recent_bot_lines(state.get("history_text", "")))
