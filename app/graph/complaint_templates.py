"""The helper complaint messages, word for word.

Approved by Thomas on 2 Nov 2026 ("Helper Complaint Severity and Response
Guidelines"), except the lines marked PENDING THOMAS APPROVAL. Change these only
with his wording. They go out as written: run_turn does not pass them through
the reply guards (reply_is_fixed), and only a translation may change them
(app/graph/nodes/complaint_triage.py checks that 999, 995, A&E and the reference
survive it).

No phone number is written here. The recruiter's number comes from the
FCC_CONTACT_NUMBER setting, and without it the clause or sentence naming it is
dropped.
"""

from __future__ import annotations

import re

from app.config import settings

P1_EMERGENCY = (
    "I'm very sorry this is happening. Your safety comes first.\n\n"
    "Please call the police on 999 now if you are in danger. Call 995 or go to your "
    "nearest hospital A&E if you are hurt or very unwell.\n\n"
    "If you cannot call, go to a safe place or ask someone nearby for help if you can. "
    "We are a helper placement company, so we cannot provide police or medical help. "
    "After you have contacted them, message us here{recruiter_clause}. We have issued a "
    "ticket, and the next available Family Care Consultant will reach out to you during "
    "the next working day.\n\n"
    'Can you reply "safe" or "not safe"?{ref_sentence}'
)

P1_SUICIDE = (
    "I'm really glad you told me. Are you thinking about ending your life right now?\n\n"
    "If you might hurt yourself or are in danger, please call 995 now or go to your "
    "nearest hospital A&E.\n\n"
    "We are not a medical service, so please contact them first. We have issued a ticket, "
    "and the next available Family Care Consultant will reach out to you during the next "
    "working day. Is there someone safe near you?"
)

P2_SLEEP = (
    "I'm very sorry this is happening. {sleep_sentence}\n\n"
    "Are you feeling very unwell, or do you feel unsafe right now?\n\n"
    "Call 995 or go to your nearest hospital A&E if you feel very unwell. Call 999 if you "
    "are in danger.\n\n"
    "We have issued a ticket. The next available Family Care Consultant will reach out to "
    "you during the next working day.{recruiter_sentence}{ref_sentence}"
)
SLEEP_WITH_DURATION = "Going {duration} without sleep is serious and can affect your health."
SLEEP_NO_DURATION = "Going without sleep is serious and can affect your health."

P2_GENERAL = (
    "Thank you for telling us. This sounds serious, and we have flagged your case.\n\n"
    "{safety_line}"
    "If you are hurt, call 995 or go to your nearest hospital A&E. If you are in danger, "
    "call 999. We have issued a ticket, and the next available Family Care Consultant will "
    "reach out to you during the next working day.{recruiter_sentence} We will not contact "
    "your employer unless you agree, except in an emergency.{ref_sentence}"
)
ASK_SAFE = "Are you safe right now?"
ASK_SAFE_TO_CHAT = "Is it safe to chat?"

P3 = (
    "I'm sorry you are dealing with this. We can explain your options and, if it is "
    "suitable and you agree, offer mediation.\n\n"
    "Please tell us {ask}. If you have messages, photos, or payment records, and it is "
    "safe to keep them, please keep them.\n\n"
    "We have issued a ticket. The next available Family Care Consultant will reach out to "
    "you during the next working day.{ref_sentence}"
)
P3_ASK = {
    "salary": "which salary months are affected",
    "rest_day": "which rest days were missed",
}
P3_ASK_DEFAULT = "what happened"

P4 = "Thanks for your message. {answer}\n\nIs there anything else about this that is worrying you?"

NOT_SAFE_TO_CHAT = (
    "I understand. Please reply when it is safe. Tell us the best time to message you. "
    "If you are in danger at any time, call 999."
)

# 4 Nov 2026, the PM's test: the questions and follow-ups read as cold. The lines this
# switch selects are PENDING THOMAS APPROVAL. The P1, P2, P3, P4 and NOT-SAFE template
# bodies above are his and do not change. False = the wording he approved on 2 Nov.
WARM_WORDING = True

# The triage questions, one per message, and what each is without its lead-in. The
# first question of a case carries the one acknowledgement (question() below).
FIRST_QUESTION_LEAD = "I'm so sorry to hear this, and thank you for telling me."
BARE_QUESTIONS = {
    "Q1": "Are you safe right now?",
    "Q2": "Are you hurt or feeling very unwell?",
    "Q3": "Is it safe to chat now, or could someone else see this phone?" if WARM_WORDING
    else "Is it safe to chat, or could someone see this phone?",
    "Q4": "Is the person who hurt you nearby right now?" if WARM_WORDING
    else "Is the person who is hurting you nearby?",
    "Q5": "Can you leave the house if you need to?",
    "Q6": "If you feel comfortable, could you tell me what happened, and when it last "
    "happened? Take your time." if WARM_WORDING
    else "What happened, and when did it last happen?",
}
if WARM_WORDING:
    Q1 = f"{FIRST_QUESTION_LEAD} {BARE_QUESTIONS['Q1']}"
    Q2 = f"Thank you for letting me know. {BARE_QUESTIONS['Q2']}"
    Q3 = f"Thank you. {BARE_QUESTIONS['Q3']}"
    Q4 = f"I want to make sure you're okay. {BARE_QUESTIONS['Q4']}"
    Q5 = BARE_QUESTIONS["Q5"]
    Q6 = BARE_QUESTIONS["Q6"]
    # never ask for the details
    Q6_SEXUAL = "When did it last happen? You don't need to share any more than that."
else:
    Q1, Q2, Q3 = BARE_QUESTIONS["Q1"], BARE_QUESTIONS["Q2"], BARE_QUESTIONS["Q3"]
    Q4, Q5, Q6 = BARE_QUESTIONS["Q4"], BARE_QUESTIONS["Q5"], BARE_QUESTIONS["Q6"]
    Q6_SEXUAL = "When did it last happen?"  # never ask for the details
QUESTIONS = {"Q1": Q1, "Q2": Q2, "Q3": Q3, "Q4": Q4, "Q5": Q5, "Q6": Q6}

REFERENCE_ONLY = "Your reference is {ref}."

# PENDING THOMAS APPROVAL: the three follow-ups on an open P1/P2 case.
FOLLOW_SAFE = (
    ("I'm glad you're safe right now." if WARM_WORDING else "Thank you for letting us know.")
    + " Your reference is {ref}. The next available Family "
    "Care Consultant will reach out to you during the next working day. If you are in "
    "danger at any time, call 999."
)
FOLLOW_NOT_SAFE = (
    "Please call the police on 999 now. If you are hurt or very unwell, call 995 or go to "
    "your nearest hospital A&E. Your reference is {ref}."
)
FOLLOW_OTHER = (
    "Thank you, we have added this to your case {ref}. The next available Family Care "
    "Consultant will reach out to you during the next working day. If you are in danger, "
    "call 999. If you are hurt or very unwell, call 995 or go to your nearest hospital A&E."
)

# PENDING THOMAS APPROVAL: she gave a time after the NOT-SAFE template.
FOLLOW_BEST_TIME = (
    "Thank you, we have noted that. Your reference is {ref}. The next available Family "
    "Care Consultant will reach out to you at the time you gave, during working hours. "
    "If you are in danger at any time, call 999."
)

# PENDING THOMAS APPROVAL: a bare "ok" or "thanks" on an open case, and any follow-up
# that would otherwise repeat the one before it word for word. The opening goes when
# she did not thank us: "You're welcome" does not answer something she told us.
SHORT_OPENING = "You're welcome. "
SHORT_REPLY = (
    "We're here for you. If anything changes, call 999 if you are in danger, or 995 if "
    "you are hurt or very unwell."
)

# First message of a conversation: the AI disclosure, then the P4 template.
INTRO = "Hi, I'm Claire, Ming Hwee's AI assistant."

# Her reply to the P1 suicide template does not clearly say she is safe. Built only
# from that template's own sentences (instructions of 4 Nov 2026).
FOLLOW_SUICIDE_NOW = (
    "Please call 995 now or go to your nearest hospital A&E. We are not a medical "
    "service, so please contact them first. Your reference is {ref}."
)

# PENDING THOMAS APPROVAL: the doc gives no wording for these two. Built from its
# own sentences; no safety numbers, because neither case has a safety concern.
P3_ADDED = (
    ("Thank you for sharing this with us. I've added it to your case {ref}." if WARM_WORDING
     else "Thank you, we have added this to your case {ref}.")
    + " The next available Family Care "
    "Consultant will reach out to you during the next working day."
)
# PENDING THOMAS APPROVAL: she already said how long, so she is not asked for it.
P3_REST_DAY_REFLECTED = "You mentioned you haven't had a rest day for {span}."
P4_TICKET = (
    "We have issued a ticket. The next available Family Care Consultant will reach out to "
    "you during the next working day. Your reference is {ref}."
)

# In helper complaint replies there must never be any of these. Every template above
# is checked by scripts/selfcheck_golive.py; the one model-written answer (P4) is
# checked when it is written.
FORBIDDEN = re.compile(
    r"confront\s+(?:your|the)\s+employer"
    r"|\bwe\s+will\s+contact\s+your\s+employer"
    r"|\bwe\s+(?:will\s+)?(?:promise|guarantee)\b|\bwill\s+be\s+(?:resolved|solved|punished)\b"
    r"|\b(?:you\s+should|you\s+can)\s+(?:sue|file\s+a\s+(?:case|lawsuit))\b|\blegal\s+action\b"
    r"|\btake\s+(?:some\s+)?(?:medicine|medication|panadol|paracetamol|painkillers?)\b"
    r"|\byour\s+employer\s+(?:is|was)\s+(?:wrong|guilty|abusing|breaking)\b"
    r"|\b(?:tell|describe)\b[^.?!]{0,40}\b(?:exactly|in\s+detail|every\s+detail)\b",
    re.IGNORECASE,
)


def _recruiter_clause() -> str:
    number = (settings.fcc_contact_number or "").strip()
    return f" or call your recruiter on {number}" if number else ""


def _recruiter_sentence() -> str:
    number = (settings.fcc_contact_number or "").strip()
    return f" You can also call your recruiter on {number}." if number else ""


def _ref_sentence(ref: str | None) -> str:
    return f" Your reference is {ref}." if ref else ""


def p1_emergency(ref: str | None) -> str:
    return P1_EMERGENCY.format(
        recruiter_clause=_recruiter_clause(), ref_sentence=_ref_sentence(ref)
    )


def p2_sleep(ref: str | None, duration: str | None) -> str:
    sleep = SLEEP_WITH_DURATION.format(duration=duration) if duration else SLEEP_NO_DURATION
    return P2_SLEEP.format(
        sleep_sentence=sleep,
        recruiter_sentence=_recruiter_sentence(),
        ref_sentence=_ref_sentence(ref),
    )


def p2_general(ref: str | None, *, asked_safe: bool = False, asked_chat: bool = False) -> str:
    """Triage has already asked what it asked: only what is left stays in the line."""
    line = " ".join(
        q for q, asked in ((ASK_SAFE, asked_safe), (ASK_SAFE_TO_CHAT, asked_chat)) if not asked
    )
    return P2_GENERAL.format(
        safety_line=f"{line}\n\n" if line else "",
        recruiter_sentence=_recruiter_sentence(),
        ref_sentence=_ref_sentence(ref),
    )


def p3(ref: str | None, complaint_type: str | None, rest_day_span: str | None = None) -> str:
    template = P3
    if rest_day_span and complaint_type == "rest_day":
        template = P3.replace(
            "Please tell us {ask}.", P3_REST_DAY_REFLECTED.format(span=rest_day_span)
        )
    return template.format(
        ask=P3_ASK.get(complaint_type or "", P3_ASK_DEFAULT), ref_sentence=_ref_sentence(ref)
    )


def question(pending: str | None, *, first: bool, sexual: bool = False) -> str:
    """The question to send. The acknowledgement goes on the first of a case only, and
    whichever question that is carries no other lead-in."""
    if pending == "Q6" and sexual:
        return Q6_SEXUAL
    if not WARM_WORDING:
        return QUESTIONS.get(pending or "", "")
    if first:
        return f"{FIRST_QUESTION_LEAD} {BARE_QUESTIONS.get(pending or '', '')}".strip()
    if pending == "Q1":
        return f"Thank you. {BARE_QUESTIONS['Q1']}"
    return QUESTIONS.get(pending or "", "")


def short_reply(ref: str | None, *, thanked: bool) -> str:
    opening = SHORT_OPENING if thanked else ""
    return f"{opening}{SHORT_REPLY}{_ref_sentence(ref)}"


def p4(answer: str) -> str:
    return P4.format(answer=answer.strip())


def follow_up(template: str, ref: str | None) -> str:
    """A follow-up carrying the reference, with the wording tidied when it is not there yet."""
    if ref:
        return template.format(ref=ref)
    return (
        template.replace(" Your reference is {ref}.", "")
        .replace("your case {ref}", "your case")
    )
