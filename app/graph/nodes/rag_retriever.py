"""Node 2 — pull supporting context out of cb_knowledge_base_updated."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import settings
from app.graph.guards import last_bot_line
from app.graph.state import ConversationState, effective_contact_type
from app.services import contact as contact_service
from app.services import rag
from app.services.lead import nationality_code

logger = logging.getLogger(__name__)


# Below this a message is too short to embed meaningfully on its own: "how
# much?", "and the levy?", "how long ah" carry almost no signal, so every
# similarity comes back near zero and a knowledge base full of the answer looks
# empty. What the client is asking about is in the question we just asked them.
_SHORT_QUERY_WORDS = 6


def _search_query(state: ConversationState) -> str:
    """Bias the query with the last thing the client said plus the intent."""
    message = (state.get("incoming_text") or "").strip()
    intent = state.get("intent") or ""
    if intent in {"greeting", "smalltalk", "other"}:
        return message

    readable_intent = intent.replace("_", " ")
    if not message:
        return readable_intent

    parts = [message]
    if len(message.split()) < _SHORT_QUERY_WORDS:
        previous = last_bot_line(state.get("history_text") or "")
        if previous:
            parts.append(previous)
    parts.append(f"({readable_intent})")
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
    matches = await rag.search(
        query,
        service_type=service,
        contact_type=contact,
        nationality=nationality,
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
    if service and best < settings.rag_soft_floor:
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
