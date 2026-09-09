"""Node 2 — pull supporting context out of cb_knowledge_base_updated."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import settings
from app.graph.guards import FEE_STATED_SERVICES, last_bot_line
from app.graph.state import ConversationState, effective_contact_type
from app.services import contact as contact_service
from app.services import rag
from app.services import ticket as ticket_service
from app.services.lead import nationality_code

logger = logging.getLogger(__name__)


# Below this a message is too short to embed meaningfully on its own: "how
# much?", "and the levy?", "how long ah" carry almost no signal, so every
# similarity comes back near zero and a knowledge base full of the answer looks
# empty. What the client is asking about is in the question we just asked them.
_SHORT_QUERY_WORDS = 6


# Intents whose NAME describes the shape of a question rather than its subject.
# For these the in-flight service is the subject, and without it the query goes
# to the embedder effectively untagged. See the note inside _search_query.
_SUBJECTLESS_INTENTS = {
    "other",
    "process_question",
    "document_question",
    "general_question",
    # Joined 2026-09-08. "fee enquiry" names the shape of the question exactly
    # as "process question" did - what it is a price FOR is the service in
    # flight. `salary_enquiry` deliberately stays OUT: what a helper earns is
    # about the helper, not about the service, and service-tagging it measured
    # WORSE (0.505 -> 0.446 on "what salary should I budget", swapping a direct
    # answer for a general one).
    "fee_enquiry",
}

# A money question whose service is ALSO money is the whole conversation, and
# there is no other subject to tag it with - tagging it "(fee enquiry)" tags it
# with itself, which is the defect this set exists to fix.
_MONEY_SERVICES = {"fee_enquiry", "salary_enquiry"}


# The turn that explains a whole service is not searching for what the client
# just said - they said "Indonesian" - it is searching for everything they are
# about to be told. Measured 2026-09-08 against both nationalities: this exact
# phrasing is the one that brings back all four kinds of row (process,
# documents, cost AND timing); the shorter "the full process, the documents,
# the cost and how long it takes" returned no timing row for either.
# Reworded 2026-09-09 with the flow itself: the client asked for the process
# and the embassy/appointment detail to come OUT of what we tell them, so the
# briefing turn no longer goes looking for it. Asking for "the process" put the
# process rows at the top of the retrieved set, which is the model's strongest
# hint about what to write.
# Reworded again 2026-09-09, the same afternoon: the agency read the first
# briefing that went out and asked for the process back - "after the document,
# tell the user, this is the further process you have to follow". That is not
# the process this query stopped asking for. What came out was OUR processing
# (the appointment, the runner); what they want is THEIRS - confirm, send the
# documents, sign the forms, hear back. So the query asks for what happens next
# from the client's side, and deliberately still never says "process", which is
# the word that pulls the embassy rows to the top of the set.
BRIEFING_QUERY = (
    "how long does it take, how much does it cost, what documents are "
    "needed from me, and what happens next once I confirm"
)

# FIVE kinds of answer have to arrive together now, and the nationality-specific
# document row has to survive alongside them. At the ordinary 5 the timing row
# was the one that fell off the end; at 8, once the query started asking what
# happens next as well, the COST row fell off it instead - measured 2026-09-09
# at rank 9 (0.461) for all three nationalities, i.e. the briefing would have
# had to say the price was not in our records, which is the 2026-09-08 defect
# over again. 10 keeps every one of the five for PH, ID and MM.
BRIEFING_MATCH_COUNT = 10


def _briefing_turn(state: ConversationState) -> bool:
    """Whether THIS turn is the one that lays the whole service out.

    ticket_service.briefing_due() answers "has the service reached the point
    where it can explain itself". Two more things have to be true before we
    replace the client's own words with a briefing query:

    1. The topic is not PARKED. Only the collector gives a briefing, and a
       parked topic goes to blocked_topic_responder instead - which never sets
       briefed_services, so without this test the briefing query would be used
       for every remaining turn of the conversation and every specific question
       ("what is the cost", "do you need the original passport") would be
       answered out of a general briefing set rather than its own rows.
    2. The client did not just ask us something. Their question gets the
       retrieval, and the briefing waits one turn - answering what somebody
       asked comes before telling them what we planned to tell them.
    """
    if not ticket_service.briefing_due(
        state.get("service_type"),
        state.get("collected_info"),
        state.get("briefed_services"),
    ):
        return False
    topic_key = ticket_service.topic_key_for(
        state.get("service_type"),
        effective_contact_type(state),
        state.get("intent"),
    )
    if topic_key and topic_key in (state.get("blocked_topics") or {}):
        return False
    return not _ASKS_US_SOMETHING.search(state.get("incoming_text") or "")


# Narrow on purpose: this only decides whether the briefing waits a turn.
_ASKS_US_SOMETHING = re.compile(
    r"\?|^\s*(?:what|when|where|which|who|why|how|can|could|do|does|is|are)\b",
    re.IGNORECASE,
)


def _search_query(state: ConversationState) -> str:
    """Bias the query with the last thing the client said plus the topic."""
    message = (state.get("incoming_text") or "").strip()
    intent = state.get("intent") or ""

    if _briefing_turn(state):
        service = (state.get("service_type") or "").replace("_", " ")
        return f"{BRIEFING_QUERY}\n({service})"

    # A greeting or a piece of small talk is searched as it stands: it is not a
    # question, and biasing it towards whatever is in flight would go looking
    # for an answer nobody asked for.
    if intent in {"greeting", "smalltalk"}:
        return message

    topic = intent
    if intent in _SUBJECTLESS_INTENTS:
        # `other` is the classifier's shrug — a real question it could not
        # label. It used to mean the query went to the embedder completely
        # bare, and a short question then matched NOTHING: three questions into
        # a passport renewal, "what is the process" scored 0.000 filtered AND
        # 0.000 unfiltered, so even the widening retry had nothing to widen to
        # and the client got the holding line on a question the records answer
        # outright. The same four words tagged "(passport renewal)" score 0.57.
        #
        # The subject was never missing — it just was not in the intent. The
        # collection in flight IS the subject, and `_service_filter` below
        # already trusts `service_type` for exactly this reason. Measured
        # 2026-09-07 against the agency's process table.
        #
        # The same hole was open one level up, and it is why the SAME question
        # was answered in one flow and refused in another on the same
        # afternoon. `other` is not the only label that carries no subject:
        # process_question, document_question and general_question name the
        # SHAPE of the question, never what it is about. Tagging "what is the
        # process" with "(process question)" is tagging it with itself - the
        # embedder gets nothing, the score lands under the soft floor,
        # _answerable() reads False and blocked_topic_responder answers "a live
        # agent is handling this" to a question the records answer outright.
        # Live, 2026-09-07: "What is the process" and "What are the documents
        # needed?" both got the parked line inside direct_hiring and new_hiring
        # (the rows they wanted score 0.661 and 0.473), while the identical
        # words landed on `other` in a passport renewal and were answered in
        # full. fee_enquiry and salary_enquiry stay out of this set - money IS
        # a subject, and _MONEY_TALK below already widens those turns.
        topic = state.get("service_type") or ""
        if topic in _MONEY_SERVICES and intent in _MONEY_SERVICES:
            topic = ""
        if not topic:
            return message

    readable_topic = topic.replace("_", " ")
    # A price question is tagged with the PRICE of the service, not just the
    # service. The knowledge base phrases these rows "How much does it cost
    # to renew my helper's passport?", and a terse "what is cost" tagged only
    # "(passport renewal)" landed at 0.367 - under the floor, so _answerable()
    # read False and the client got a holding line for a figure we hold.
    # Measured 2026-09-08 across six phrasings and every service: "(cost of
    # passport renewal)" scores 0.503-0.566 against 0.362-0.465, and the right
    # row is top every time. Renewal, transfer, replacement and direct hire all
    # improve too; home leave and replacement move by less than 0.02 and keep
    # the same top row.
    if intent == "fee_enquiry" and topic:
        readable_topic = f"cost of {readable_topic}"
    if not message:
        return readable_topic

    parts = [message]
    if len(message.split()) < _SHORT_QUERY_WORDS:
        previous = last_bot_line(state.get("history_text") or "")
        if previous:
            parts.append(previous)
    parts.append(f"({readable_topic})")
    return "\n".join(parts)


# Money words. A message carrying one is asking or talking about figures, and
# those figures are spread across the knowledge base under their own service
# labels (salary_enquiry, fee_enquiry, general) — not under whichever flow the
# client happens to be in the middle of.
#
# Live, 2026-09-02: mid new_hiring the client asked "is there any approximate
# range?" three separate times. Retrieval was filtered to service=new_hiring
# each time, the salary chunks sat under other labels, so the model had nothing
# grounded to quote — it answered "I don't have a specific range to share" twice
# and once tried to invent $500-700, which ungrounded_figures correctly threw
# away. The figures were in the KB the whole time. Dropping the service filter
# on these turns is what makes them reachable; nationality still narrows it.
_MONEY_TALK = re.compile(
    r"\b(salary|salaries|wage|wages|pay|paid|payment|fee|fees|cost|costs|price|"
    r"pricing|charge|charges|package|levy|deposit|budget|range|quotation|quote|"
    r"afford|expensive|cheap)\b",
    re.IGNORECASE,
)


# The fields whose own question is about money. When one of them is next, the
# figures have to be in front of the model BEFORE it writes, not after.
_MONEY_FIELDS = {"budget", "salary_expectation", "salary", "fee"}


def _service_filter(state: ConversationState) -> str | None:
    """Which service to narrow retrieval to — None means search everything."""
    # Widening a money question is right when the figures live somewhere else
    # (see below) and wrong when THIS service states its own price: the widened
    # search then hands back another service's fee, which is not a vague answer
    # but a false one. Measured 2026-09-08, "how much does it cost" inside a
    # passport renewal returned the WORK PERMIT renewal row - $695 to a client
    # whose answer is $450 - and the same on "what is the fee" and "how much do
    # you charge". None of these flows collects a money field, so the two rules
    # below cannot want the filter dropped here either.
    if state.get("service_type") in FEE_STATED_SERVICES:
        return state.get("service_type")

    if _MONEY_TALK.search(state.get("incoming_text") or ""):
        return None

    # The client's own words are not the only money turn. Retrieval runs before
    # the collector, so the field we are ABOUT to ask sits in last turn's
    # outstanding list — the first entry is whatever was just asked, the second
    # is what comes next. Live, 2026-09-02 19:13: the turn that asked "do you
    # have a monthly salary budget in mind?" retrieved under service=new_hiring,
    # the model reached for a $500-$700 range from nowhere, and
    # ungrounded_figures correctly binned the whole reply and sent the bare
    # question instead ("quoted unstated figure(s) ['700', '500', '600']"). The
    # figures it needed were in the knowledge base the whole time, filed under
    # salary_enquiry. Only the first two are checked: budget is outstanding from
    # the first turn of a hiring flow, and testing the whole list would switch
    # the service filter off for the entire conversation.
    if any(key in _MONEY_FIELDS for key in (state.get("missing_field_keys") or [])[:2]):
        return None

    # And our own last line counts: "what's your monthly budget?" -> "around 600"
    # is a money exchange in which the client's words carry no money word at all.
    if _MONEY_TALK.search(last_bot_line(state.get("history_text") or "")):
        return None

    return state.get("service_type")


# A question about what WE charge, as opposed to money in general. Timing
# words are deliberately absent: "how much time does it take" is not a price
# question and must keep the widening retry it has had since 2026-09-03.
_PRICE_QUESTION = re.compile(
    r"\bcosts?\b|\bprices?\b|\bfees?\b|\bcharges?\b"
    r"|\bhow\s+much\s+(?:is|are|does|do|would|will|for|to)\b"
    r"|\bhow\s+much\s*[?.!]*\s*$",
    re.IGNORECASE,
)


def _nationality(state: ConversationState) -> str | None:
    """The PH/ID/MM code this conversation is about, if we know it.

    Both the client's own nationality (a helper) and their preference (an
    employer) narrow the same way. 'none' is what nationality_code() returns
    for "no preference", which is not a filter — it is the absence of one.
    """
    collected = state.get("collected_info") or {}
    code = nationality_code(
        str(collected.get("nationality") or "")
    ) or nationality_code(str(collected.get("preferred_nationality") or ""))
    return code if code and code != "none" else None


async def rag_retriever(state: ConversationState) -> dict[str, Any]:
    case_summary = None
    if state.get("intent") == "case_enquiry" and state.get("matched_case_id"):
        case_summary = await contact_service.get_case_summary(state["matched_case_id"])

    # Routing labels are per chunk, so the filter runs before the vector search.
    # Each one is inclusive of its catch-all bucket inside the match function;
    # anything we cannot determine is passed as None and simply not filtered on.
    query = _search_query(state)
    contact = effective_contact_type(state)
    nationality = _nationality(state)
    service = _service_filter(state)
    briefing = _briefing_turn(state)
    matches = await rag.search(
        query,
        service_type=service,
        contact_type=contact,
        nationality=nationality,
        match_count=BRIEFING_MATCH_COUNT if briefing else None,
    )
    best = rag.best_similarity(matches)

    # A service filter can starve a question the knowledge base can answer.
    # Live, 2026-09-03: with a passport_renewal ticket parked, "How much time it
    # takes in renewal" was filtered to the four passport_renewal rows, scored
    # 0.385 against them and fell under the soft floor, so the client got the
    # holding line. The rows that answer it — work permit renewal, filed under
    # 'renewal' — score 0.464 and were excluded by the filter, not by the
    # question. The next message, which happened to say the word "passport",
    # scored 0.506 and was answered, so from the client's side we ignored one
    # question and then answered it a message late.
    #
    # Same shape as the money-question carve-out in _service_filter above, and
    # handled here instead of by adding another keyword to it, because the
    # trigger is not the wording of the question — it is the filtered search
    # coming back empty-handed. Only ever widens: the narrow result is kept
    # unless the wider one genuinely scores better. The nationality filter is
    # deliberately NOT dropped — the KB holds per-nationality passport timings,
    # and a confident answer about the wrong country is worse than a holding
    # line.
    # ...but NOT when this service states its own price and the client is
    # asking for it. _service_filter keeps the filter for those three exactly
    # so another service's fee cannot be quoted; widening below the floor would
    # hand it straight back. Measured: a terse "what is cost" inside a PASSPORT
    # renewal scores 0.361 filtered, so it widens - and unfiltered the top row
    # is the WORK PERMIT renewal at $695, where the answer is $450. A wrong
    # price is worse than a vague one, so this turn keeps the narrow result and
    # falls to the holding line if it is genuinely too weak.
    #
    # Scoped to a PRICE question, not to money generally: the case this retry
    # was written for on 2026-09-03 was "How much time it takes in renewal" - a
    # TIMING question inside passport_renewal that needed the `renewal` rows,
    # and it still widens exactly as it did.
    fee_question = state.get("intent") == "fee_enquiry" or _PRICE_QUESTION.search(
        state.get("incoming_text") or ""
    )
    if service in FEE_STATED_SERVICES and fee_question:
        logger.info(
            "Retrieval under service=%s scored %.3f but it is a price question on a "
            "service that states its own fee - not widening, so another service's "
            "figure cannot be quoted",
            service,
            best,
        )
    elif service and best < settings.rag_soft_floor:
        wider = await rag.search(
            query,
            service_type=None,
            contact_type=contact,
            nationality=nationality,
        )
        wider_best = rag.best_similarity(wider)
        if wider_best > best:
            logger.info(
                "Retrieval under service=%s scored %.3f (floor %.2f); searching the whole "
                "knowledge base found %.3f — using the wider result",
                service,
                best,
                settings.rag_soft_floor,
                wider_best,
            )
            matches, best = wider, wider_best

    return {
        "rag_matches": matches,
        "rag_context": rag.format_context(matches),
        "rag_best_score": best,
        "case_summary": case_summary,
    }
