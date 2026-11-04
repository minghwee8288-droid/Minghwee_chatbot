"""The complaint flow's side effects: the ticket and the message.

The ticket is raised BEFORE the first triage question, so a helper who goes
silent still has a record. It goes to the portal's Complaints tab because its
service_type holds a dispute value (tickets_repo._CB_IS_COMPLAINT, the portal's
rule) and carries the level in complaint_level. Fields with no column live on the
ticket's captured_info, which the portal's drawer shows: "complaint" for the
agent, "triage" for where the flow has got to. Raising a ticket does not pause the
chat, and the ticket has no owner (the FCC pool).

At P1 the message does not wait on the database: 3 seconds, then the template goes
without its reference while the ticket keeps retrying, and the reference follows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings
from app.graph import complaint_templates as templates
from app.graph import complaint_triage as triage
from app.graph import guards
from app.graph.llm import complete
from app.graph.state import ConversationState, conversation_ref, effective_contact_type
from app.services import complaint_case
from app.services import handover as handover_service
from app.services import message as message_service
from app.services import ticket as ticket_service

logger = logging.getLogger(__name__)

P1_TICKET_SECONDS = 3.0
TRANSLATE_SECONDS = 8.0
_LATE_TASKS: set[asyncio.Task] = set()
# A P1 ticket still being raised, so her next message waits for it, not a second one.
_RAISING: dict[Any, asyncio.Task] = {}

TRANSLATE_SYSTEM = """Translate this message from a maid agency to a domestic helper into {language}.
Translate only: add nothing, remove nothing and give no advice of your own. Keep these exactly as \
written: the numbers 999 and 995, the letters A&E, any reference like CB-2026-0001, and any phone \
number. Reply with the translation and nothing else."""

P4_SYSTEM = """A domestic helper in Singapore asked a question. Answer it in 1-2 short sentences in \
plain English, from the records below ONLY. Give no legal or medical advice, promise no outcome, \
do not tell her to confront anyone. If the records do not answer it, reply exactly: NO_ANSWER

Records:
{records}"""


def _text_for(kind: str, case: dict[str, Any], ref: str | None) -> str:
    asked = case.get("asked") or []
    if kind == "P1":
        return templates.p1_emergency(ref)
    if kind == "P1_SUICIDE":
        return templates.P1_SUICIDE
    if kind == "P2_SLEEP":
        return templates.p2_sleep(ref, case.get("duration"))
    if kind == "P2_GENERAL":
        return templates.p2_general(ref, asked_safe="Q1" in asked, asked_chat="Q3" in asked)
    if kind == "P3":
        return templates.p3(ref, case.get("complaint_type"))
    if kind == "NOT_SAFE":
        return templates.NOT_SAFE_TO_CHAT
    if kind == "FOLLOW_SAFE":
        return templates.follow_up(templates.FOLLOW_SAFE, ref)
    if kind == "FOLLOW_NOT_SAFE":
        return templates.follow_up(templates.FOLLOW_NOT_SAFE, ref)
    if kind == "FOLLOW_SUICIDE_NOW":
        return templates.follow_up(templates.FOLLOW_SUICIDE_NOW, ref)
    if kind == "FOLLOW_OTHER":
        return templates.follow_up(templates.FOLLOW_OTHER, ref)
    if kind == "P3_ADDED":
        return templates.follow_up(templates.P3_ADDED, ref)
    if kind == "P4_TICKET":
        return templates.follow_up(templates.P4_TICKET, ref)
    if kind == "QUESTION":
        pending = case.get("pending")
        if pending == "Q6" and case.get("complaint_type") == "sexual":
            return templates.Q6_SEXUAL
        return templates.QUESTIONS.get(pending or "", "")
    return ""


def translated_ok(english: str, translated: str) -> bool:
    """999, 995, A&E and any reference survived, or the English goes instead."""
    if not translated.strip():
        return False
    for token in ("999", "995", "A&E"):
        if token in english and token not in translated:
            return False
    return all(ref in translated for ref in guards.TICKET_REFERENCE.findall(english))


async def _in_her_language(text: str, language: str | None) -> str:
    if not text or not language or language.lower() in {"english", "en"}:
        return text
    try:
        translated = await asyncio.wait_for(
            complete(TRANSLATE_SYSTEM.format(language=language), text, temperature=0.0, max_tokens=700),
            TRANSLATE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - the English still gets through
        logger.warning("Could not translate a complaint message (%s) - sending English", type(exc).__name__)
        return text
    translated = (translated or "").strip().strip('"')
    if not translated_ok(text, translated):
        logger.warning("A complaint translation lost a number or the reference - sending English")
        return text
    return translated


async def _kb_answer(state: ConversationState) -> str | None:
    """1-2 short sentences from the records, or None when they do not answer it."""
    from app.graph.nodes.rag_retriever import rag_retriever

    message = state.get("incoming_text") or ""
    found = await rag_retriever({
        **state, "intent": "general_question", "service_type": None,
        "contact_type": "candidate", "detected_contact_type": "candidate",
    })
    records = found.get("rag_context") or ""
    if not found.get("rag_matches") or (found.get("rag_best_score") or 0) < settings.rag_soft_floor:
        return None
    try:
        answer = (await complete(
            P4_SYSTEM.format(records=records), message, temperature=0.2, max_tokens=160
        )).strip().strip('"')
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write a P4 answer (%s)", type(exc).__name__)
        return None
    if (not answer or "NO_ANSWER" in answer or templates.FORBIDDEN.search(answer)
            or guards.ungrounded_figures(answer, records, message)):
        return None
    return answer


async def _late_reference(
    create: asyncio.Task, conversation: dict[str, Any], language: str | None
) -> None:
    """A P1 sent without its reference: the follow-up, once the ticket exists."""
    ticket = await create
    number = (ticket or {}).get("ticket_number")
    if not number:
        logger.critical("P1 complaint on conversation %s has no ticket after retries", conversation.get("id"))
        return
    text = guards.references_as_code(templates.REFERENCE_ONLY.format(ref=number))
    try:
        await message_service.send_bot_reply(
            conversation, await _in_her_language(text, language)
        )
    except Exception:  # noqa: BLE001 - the ticket exists; only the reference line was lost
        logger.exception("Could not send the late P1 reference on %s", conversation.get("id"))


def _in_background(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _LATE_TASKS.add(task)
    task.add_done_callback(_LATE_TASKS.discard)


async def _ticket_for(
    state: ConversationState, case: dict[str, Any], kind: str, message: str
) -> tuple[str | None, str | None]:
    """(ticket id, number), both None while a P1's is still being raised. An existing
    ticket is written through."""
    if case.get("ticket_id"):
        await complaint_case.write_through(case, message, _transcript(state, message))
        return case["ticket_id"], case.get("ticket_number")

    conversation_id = state.get("conversation_id")
    raising = _RAISING.get(conversation_id)
    if raising is not None:
        ticket = await asyncio.wait_for(asyncio.shield(raising), P1_TICKET_SECONDS)
        if ticket:
            case.update(ticket_id=ticket.get("id"), ticket_number=ticket.get("ticket_number"))
            await complaint_case.write_through(case, message, _transcript(state, message))
            return ticket.get("id"), ticket.get("ticket_number")
        return None, None

    create = asyncio.ensure_future(complaint_case.raise_ticket(
        conversation_ref(state), case, message,
        contact_type=effective_contact_type(state),
        whatsapp_number=state.get("phone"),
        whatsapp_name=state.get("customer_name"),
        transcript=_transcript(state, message),
    ))
    is_p1 = case.get("level") == "P1"
    try:
        ticket = await (asyncio.wait_for(asyncio.shield(create), P1_TICKET_SECONDS) if is_p1 else create)
    except asyncio.TimeoutError:
        _RAISING[conversation_id] = create
        create.add_done_callback(lambda _task: _RAISING.pop(conversation_id, None))
        _in_background(_late_reference(create, conversation_ref(state), case.get("language")))
        return None, None
    ticket = ticket or {}
    return ticket.get("id"), ticket.get("ticket_number")


def _transcript(state: ConversationState, message: str) -> str:
    history = state.get("history_text") or ""
    return f"{history}\nClient: {message}".strip()


async def complaint_triage_node(state: ConversationState) -> dict[str, Any]:
    step = state.get("complaint_step") or {}
    kind = step.get("kind")
    message = step.get("message") or state.get("incoming_text") or ""
    case = dict(step.get("case") or {})
    update: dict[str, Any] = {"complaint_step": None, "reply_is_fixed": True}

    if kind == "P4":
        answer = await _kb_answer(state)
        if answer:
            language = (step.get("assessment") or {}).get("language")
            return {
                **update,
                "reply": await _in_her_language(templates.p4(answer), language),
                "complaint_p4": {"question": message[:500], "answer": answer[:500]},
                "detected_contact_type": "candidate",
            }
        # The records hold no answer: a P4 ticket, as the guidelines say.
        case, _ = triage.open_case(triage.Assessment(
            **{**step.get("assessment", {}), "level": "P4",
               "complaint_type": (step.get("assessment") or {}).get("complaint_type") or "general"}
        ))
        kind = "P4_TICKET"

    ticket_id, number = await _ticket_for(state, case, kind, message)
    case["ticket_id"] = ticket_id or case.get("ticket_id")
    case["ticket_number"] = number or case.get("ticket_number")
    new_ticket = bool(step.get("new") or kind == "P4_TICKET") and ticket_id
    if new_ticket:
        await handover_service._log(
            state.get("conversation_id"),
            handover_service.BOT_TO_HUMAN,
            handover_service.REASON_DISPUTE,
            ticket_id=ticket_id,
        )

    if kind == "P3_KB":
        # A rights question on an open P3 case: answered from the records.
        answer = await _kb_answer(state)
        text = templates.p4(answer) if answer else _text_for("P3_ADDED", case, number)
    else:
        text = _text_for(kind, case, number)
    reply = await _in_her_language(text, case.get("language"))

    extra: list[str] = []
    if kind == "P1_SUICIDE" and number:
        extra.append(await _in_her_language(
            templates.REFERENCE_ONLY.format(ref=number), case.get("language")
        ))

    return {
        **update,
        "reply": reply,
        "extra_replies": extra,
        "reply_must_send": kind in {"P1", "P1_SUICIDE", "FOLLOW_NOT_SAFE", "FOLLOW_SUICIDE_NOW", "NOT_SAFE"},
        "complaint_case": case,
        "complaint_p4": None,
        "ticket_id": ticket_id or case.get("ticket_id"),
        "ticket_number": number or case.get("ticket_number"),
        "handover_done": bool(ticket_id),
        "detected_contact_type": "candidate",
    }


async def emergency_reply(conversation: dict[str, Any], text: str) -> str | None:
    """A chat a human holds, and a report of harm from a helper or unknown sender.

    One safety message and a complaint ticket, the same words as any other hour.
    No triage: a person has this chat. None for an employer's chat, whose safety
    reply stays what it was.
    """
    if not triage.enabled() or (conversation.get("contact_type") or "").lower() in {
        "employer", "supplier", "partner"
    } or conversation.get("matched_employer_id"):
        return None
    a = await triage.assess(text)
    level = a.level if a.level in {"P1", "P2"} else "P2"
    a.level, a.complaint_type = level, a.complaint_type or "general"
    case, _ = triage.open_case(a)
    case["stage"], case["pending"], case["asked"] = "open", None, []
    ticket = await complaint_case.raise_ticket(
        conversation, case, text,
        contact_type=conversation.get("contact_type") or "unknown",
        whatsapp_number=conversation.get("customer_number"),
        whatsapp_name=conversation.get("customer_name"),
        transcript=f"Client: {text}",
    )
    number = (ticket or {}).get("ticket_number")
    if level == "P1":
        kind = triage._p1_kind(case)
    else:
        kind = "P2_GENERAL"
    english = _text_for(kind, case, number)
    if kind == "P1_SUICIDE" and number:
        english = f"{english}\n\n{templates.REFERENCE_ONLY.format(ref=number)}"
    return guards.references_as_code(await _in_her_language(english, case.get("language")))
