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
6. the bot's post-graph rewriting guards (run_turn), in the same order;
7. the identity check again, on the final text - the drafts are sent by a
   human consultant;
8. at most 700 characters, not a duplicate of another draft.

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


def ungrounded(text: str, sources: str) -> set[str]:
    """Figures in the draft that no source states."""
    return figures_in(text) - numbers_in(sources)


# --- Result -----------------------------------------------------------------

@dataclass
class Validation:
    kept: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)  # {label, reason}


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
        "kb_refs": [str(r) for r in refs if str(r).strip()],
        "confidence": confidence if confidence in ("high", "low") else "low",
    }


def _node_guards(text: str, state: dict[str, Any], rag_context: str) -> tuple[str, str]:
    """(text, drop reason). The checks response_generator runs on a reply
    (response_generator.py L194-L211 and L473-L612 at commit c138f91), with
    "replace with a holding line" turned into "drop the draft"."""
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
    if guards.ungrounded_figures(text, rag_context, IDENTITY, incoming):
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

        text, reason = _node_guards(text, state, rag_context)
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

        text = _post_guards(text, state, payload).strip()
        text = redact_nric(text, context="suggest:draft")

        if not text:
            result.dropped.append({"label": label, "reason": "empty after guards"})
            continue
        if identifies_as_claire_or_ai(text):
            result.dropped.append({"label": label, "reason": "names Claire or claims to be an AI"})
            continue
        if len(text) > MAX_CHARS:
            result.dropped.append({"label": label, "reason": f"over {MAX_CHARS} characters"})
            continue
        key = " ".join(text.lower().split())
        if key in seen:
            result.dropped.append({"label": label, "reason": "duplicate"})
            continue
        seen.add(key)

        result.kept.append({**draft, "text": text})
        if len(result.kept) == MAX_DRAFTS:
            break
    return result
