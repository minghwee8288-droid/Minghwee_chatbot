"""Evaluate the suggested-replies drafts against what our staff actually sent.

READ-ONLY. Finds recent conversations where a human agent answered a client,
drafts replies AS OF the client's message (later messages and the saved
LangGraph state are not used - context.build(as_of=True)), and writes a
Markdown report comparing the drafts with the real staff reply.

    python scripts/eval_suggest.py                       # 5 conversations, SUGGEST_MODEL / the bot's model
    python scripts/eval_suggest.py --model openai/gpt-5.6-luna --limit 3
    python scripts/eval_suggest.py --conversations 1381 2409

The report goes OUTSIDE the repo (default: Desktop\\suggest_eval\\
eval_suggest_<model>_<date>.md) because it holds client conversation text.
Phone numbers and email addresses are masked; NRIC/FIN numbers are redacted
before anything reaches the model.

Every draft runs inside readonly.read_only(); a refused write aborts the run.
Calls the real model, so each conversation costs one LLM call (about 9k
prompt tokens). Testers' numbers (BOT_ALLOWED_NUMBERS) are skipped unless
--include-testers.

Only client conversations are drafted: an EMPLOYER or a HELPER/candidate. A
group chat or one of our own numbers is left out (audience.skip_reason, as the
endpoint does), and so is a supplier, partner or an unrecognised number. The
conversation's contact_type decides when it says employer or helper; when it
is empty (most portal threads) the number is matched against our records with
contact.identify(), which only reads.

Known limits: contact, ticket and case context is today's, not as it was at
the anchor; the intent and service come from retrieval alone, since the saved
state is not used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import readonly  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.services import contact as contact_service  # noqa: E402
from app.services import conversation as conversation_service  # noqa: E402
from app.suggest import audience, context  # noqa: E402
from app.suggest import llm as suggest_llm  # noqa: E402
from app.suggest import settings as suggest_settings  # noqa: E402
from app.suggest.router import run_suggest  # noqa: E402
from app.suggest.validate import figures_in  # noqa: E402
from app.utils import normalize_phone, redact_nric  # noqa: E402

DEFAULT_OUT_DIR = Path.home() / "OneDrive" / "Desktop" / "suggest_eval"
COLUMNS = "id, conversation_id, direction, body, is_bot, sent_by, created_at"
_PHONE = re.compile(r"\+?\d[\d\s-]{6,}\d")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_FILENAME = re.compile(r"\.(?:pdf|jpe?g|png|docx?|xlsx?|heic|mp4|opus|ogg)\s*$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9$]+")
_STOP = set("""a an and are as at be but by can do for from have i if in is it its me my of
on or our please so that the this to we will with you your yes no ok okay""".split())


class WriteAttempted(RuntimeError):
    pass


# contact_type values on wp_chat_conversations, as the bot and the portal write them.
_EMPLOYER_TYPES = {"employer"}
_CANDIDATE_TYPES = {"candidate", "helper", "domestic_helper"}
_EXCLUDED_TYPES = {"supplier", "partner", "recruiter", "test"}


async def audience_of(conversation: dict[str, Any]) -> str:
    """"employer", "candidate", or why it is left out."""
    skip = await audience.skip_reason(conversation)
    if skip:
        return skip
    kind = str(conversation.get("contact_type") or "").strip().lower()
    if kind in _EMPLOYER_TYPES:
        return "employer"
    if kind in _CANDIDATE_TYPES:
        return "candidate"
    if kind in _EXCLUDED_TYPES:
        return kind
    if conversation.get("matched_supplier_id"):
        return "supplier"
    identified = await contact_service.identify(normalize_phone(conversation.get("customer_number")))
    found = identified.get("contact_type")
    return found if found in ("employer", "candidate") else (found or "unknown")


def mask(text: str) -> str:
    text = _EMAIL.sub("[email]", text or "")
    return _PHONE.sub(lambda m: "[phone ***" + re.sub(r"\D", "", m.group())[-2:] + "]", text)


def overlap(a: str, b: str) -> float:
    """Content-word overlap (Jaccard). Rough: a different wording of the same
    answer scores low, so read the texts, not just the number."""
    wa = {w for w in _WORD.findall(a.lower()) if w not in _STOP}
    wb = {w for w in _WORD.findall(b.lower()) if w not in _STOP}
    return round(len(wa & wb) / len(wa | wb), 2) if wa and wb else 0.0


def _staff_reply(row: dict[str, Any], min_chars: int) -> bool:
    body = (row.get("body") or "").strip()
    return (
        row.get("direction") == "outbound"
        and context.label_of(row) == context.STAFF
        and len(body) >= min_chars
        and not _FILENAME.search(body)
        and not context.is_known_auto_reply(body)
    )


def _time_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first by created_at, as the endpoint reads them. Ids are not time
    order: the portal backfills old messages with new ids (conversation 194
    showed January after October)."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(rows, key=lambda r: (context._parse_time(r.get("created_at")) or epoch, r.get("id") or 0))


async def _rows(conversation_id: int, *, up_to: dict[str, Any] | None = None, after: int | None = None,
                limit: int = 30) -> list[dict[str, Any]]:
    """With `up_to` (a row), the messages up to it in TIME; else those after id `after`."""
    query = db.table("wp_chat_messages").select(COLUMNS).eq("conversation_id", conversation_id)
    if up_to is not None:
        query = query.lte("created_at", up_to["created_at"]).order("created_at", desc=True)
    else:
        query = query.gt("id", after).order("id")
    result = await db.execute(query.limit(limit))
    return _time_order(result.data or [])


async def _case_for(staff_row: dict[str, Any], min_chars: int) -> dict[str, Any] | None:
    """The client message a staff reply answered, and the full staff reply."""
    cid = staff_row["conversation_id"]
    before = await _rows(cid, up_to=staff_row)
    await context.mark_auto_replies(before, cid)
    index = next((i for i, r in enumerate(before) if r["id"] == staff_row["id"]), None)
    if index is None:
        return None
    previous = [r for r in before[:index]
                if context.label_of(r) is not None and (r.get("body") or "").strip()]
    if not previous or previous[-1].get("direction") != "inbound":
        return None  # the agent was not answering a client message
    reply = [staff_row["body"].strip()]
    for row in await _rows(cid, after=staff_row["id"], limit=8):
        if row.get("direction") == "inbound":
            break
        if context.label_of(row) == context.STAFF and (row.get("body") or "").strip():
            reply.append(row["body"].strip())
    return {
        "conversation_id": cid,
        "anchor": previous[-1]["id"],
        "staff_reply": "\n".join(reply),
        "staff_at": staff_row["created_at"],
        "transcript": [{"who": context._PREFIX[context.label_of(r)], "at": r["created_at"][:16],
                        "body": r["body"].strip()} for r in previous[-8:]],
    }


async def find_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    testers = set() if args.include_testers else set(settings.allowed_numbers)
    query = (db.table("wp_chat_messages").select(COLUMNS)
             .eq("direction", "outbound").eq("is_bot", False))
    if args.conversations:
        query = query.in_("conversation_id", args.conversations)
    result = await db.execute(query.order("id", desc=True).limit(args.scan))

    cases: list[dict[str, Any]] = []
    seen: set[int] = set()
    excluded: dict[str, int] = args.excluded
    for row in result.data or []:
        cid = row["conversation_id"]
        if cid in seen or not _staff_reply(row, args.min_reply_chars):
            continue
        conversation = await conversation_service.get_by_id(cid)
        if not conversation:
            continue
        if normalize_phone(conversation.get("customer_number")) in testers:
            seen.add(cid)
            excluded["tester"] = excluded.get("tester", 0) + 1
            continue
        who = await audience_of(conversation)
        if who not in ("employer", "candidate"):
            seen.add(cid)
            excluded[who] = excluded.get(who, 0) + 1
            continue
        case = await _case_for(row, args.min_reply_chars)
        if case is None:
            continue
        seen.add(cid)
        case["audience"] = who
        cases.append(case)
        if len(cases) >= args.limit:
            break
    return cases


async def draft_case(case: dict[str, Any], model: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    real_draft = suggest_llm.draft

    async def spy(*a: Any, **k: Any) -> Any:
        raw, usage = await real_draft(*a, **k)
        captured["raw"] = raw
        return raw, usage

    suggest_llm.draft = spy
    try:
        with readonly.read_only() as refused:
            body = await run_suggest(case["conversation_id"], case["anchor"], model=model, as_of=True)
            writes = list(refused)
    except suggest_llm.SuggestLLMError as exc:
        return {"status": "llm_error", "error": str(exc), "suggestions": [], "_dropped": [],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "latency_ms": 0}
    finally:
        suggest_llm.draft = real_draft
    if writes:
        raise WriteAttempted(f"conversation {case['conversation_id']}: {[w.get('target') for w in writes]}")

    # The dropped drafts' text, matched back by label and order, for the report.
    raw_items = [i for i in ((captured.get("raw") or {}).get("suggestions") or []) if isinstance(i, dict)]
    kept_texts = {s["text"] for s in body["suggestions"]}
    leftovers = [i for i in raw_items if str(i.get("text", "")).strip() not in kept_texts]
    for drop in body["_dropped"]:
        match = next((i for i in leftovers if i.get("label") == drop.get("label")), None)
        if match is not None:
            leftovers.remove(match)
            drop["text"] = redact_nric(str(match.get("text") or ""), context="eval")
    return body


def _render(cases: list[dict[str, Any]], model: str, started: datetime,
            excluded: dict[str, int]) -> str:
    done = [c for c in cases if "result" in c]
    results = [c["result"] for c in done]
    latencies = [r["latency_ms"] for r in results if r.get("latency_ms")]
    drops = [d for r in results for d in r.get("_dropped", [])]
    grounding = [d for d in drops if d["reason"].startswith("figure not")]
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    prompt = sum(r["usage"]["prompt_tokens"] for r in results)
    completion = sum(r["usage"]["completion_tokens"] for r in results)

    out = [
        f"# Suggested replies eval - {model}",
        "",
        f"Run {started:%Y-%m-%d %H:%M}, prompt version "
        f"{results[0].get('prompt_version', '?') if results else '?'}, "
        f"{len(done)} conversation(s). Read-only: every draft ran in read-only mode, 0 writes attempted.",
        "",
        "Drafted as of the client's message: later messages and the saved bot state are not used. "
        "Contact, ticket and case context is today's.",
        "",
        "Employer and helper/candidate conversations only. Conversations left out while selecting: "
        + (", ".join(f"{k} {v}" for k, v in sorted(excluded.items())) or "none") + ".",
        "",
        "## Summary",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Status | {', '.join(f'{k} {v}' for k, v in sorted(statuses.items())) or '-'} |",
        f"| Audience | {', '.join(f'{k} {v}' for k, v in sorted(_count(c['audience'] for c in done).items()))} |",
        f"| Drafts kept | {sum(len(r['suggestions']) for r in results)} "
        f"(low confidence {sum(s['confidence'] == 'low' for r in results for s in r['suggestions'])}) |",
        f"| Flags | {', '.join(f'{k} {v}' for k, v in sorted(_count(f for r in results for s in r['suggestions'] for f in s.get('flags', [])).items())) or 'none'} |",
        f"| Drafts dropped | {len(drops)} (grounding: {len(grounding)}) |",
        f"| kb_gap | {sum(bool(r.get('kb_gap')) for r in results)} of {len(results)} |",
        f"| Sensitive | {sum(bool(r.get('sensitive')) for r in results)} of {len(results)} |",
        f"| Latency ms | median {int(statistics.median(latencies)) if latencies else '-'}, "
        f"max {max(latencies) if latencies else '-'} |",
        f"| Tokens | prompt {prompt}, completion {completion} "
        f"(mean {prompt // max(len(results), 1)} / {completion // max(len(results), 1)}) |",
        f"| Best word overlap with staff reply | mean "
        f"{round(statistics.mean([c['best_overlap'] for c in done]), 2) if done else '-'} |",
        "",
    ]
    reasons: dict[str, int] = {}
    for d in drops:
        key = "figure not in the cited records or the conversation" if d["reason"].startswith(
            "figure not in the cited") else d["reason"]
        reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        out += ["Drop reasons: " + "; ".join(f"{k} ({v})" for k, v in sorted(reasons.items())), ""]

    for c in cases:
        r = c.get("result")
        out += [f"## Conversation {c['conversation_id']} - {c.get('audience', '?')} (anchor {c['anchor']})", ""]
        if r is None:
            out += [f"Not drafted: {c.get('error', 'unknown error')}", ""]
            continue
        out += ["**Conversation up to the client's message**", ""]
        out += [f"- `{t['at']}` **{t['who']}:** {mask(t['body'])[:400]}" for t in c["transcript"]]
        out += ["", f"**What our staff replied** (`{c['staff_at'][:16]}`)", "",
                "> " + mask(c["staff_reply"]).replace("\n", "\n> "), ""]
        out += [
            f"Status **{r['status']}** - kb_gap {r.get('kb_gap')} - sensitive {r.get('sensitive')} - "
            f"language {r.get('language') or '-'} - {r['latency_ms']} ms - "
            f"tokens {r['usage']['prompt_tokens']}/{r['usage']['completion_tokens']}",
            "",
        ]
        if r.get("error"):
            out += [f"Error: {r['error']}", ""]
        for i, s in enumerate(r["suggestions"], 1):
            figs = figures_in(s["text"])
            flag_text = f", flags {'/'.join(s['flags'])}" if s.get("flags") else ""
            out += [f"{i}. **{s['label']}** ({s['confidence']}{flag_text}, overlap {overlap(s['text'], c['staff_reply'])}"
                    f"{', figures ' + ', '.join(sorted(figs)) if figs else ''}"
                    f"{', refs ' + str(len(s['kb_refs'])) if s['kb_refs'] else ''}): {mask(s['text'])}"]
        for d in r.get("_dropped", []):
            out += [f"- dropped **{d.get('label') or '-'}**: {d['reason']}"
                    + (f" - \"{mask(d['text'])}\"" if d.get("text") else "")]
        staff_figs = figures_in(c["staff_reply"])
        if staff_figs:
            draft_figs = set().union(*(figures_in(s["text"]) for s in r["suggestions"])) \
                if r["suggestions"] else set()
            out += ["", f"Figures in staff reply: {', '.join(sorted(staff_figs))}; "
                    f"in drafts: {', '.join(sorted(draft_figs)) or 'none'}"]
        out += [""]
    return "\n".join(out)


def _count(items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def _out_path(out_dir: Path, model: str, started: datetime) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-")
    path = out_dir / f"eval_suggest_{slug}_{started:%Y-%m-%d}.md"
    if path.exists():
        path = out_dir / f"eval_suggest_{slug}_{started:%Y-%m-%d_%H%M%S}.md"
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=None, help="default: SUGGEST_MODEL, else the bot's model")
    parser.add_argument("--limit", type=int, default=5, help="conversations to draft (default 5)")
    parser.add_argument("--conversations", type=int, nargs="*", default=None,
                        help="only these conversation ids (the latest staff reply in each)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--min-reply-chars", type=int, default=15,
                        help="ignore staff replies shorter than this (\"Okay.\")")
    parser.add_argument("--scan", type=int, default=2000, help="recent staff messages to scan")
    parser.add_argument("--include-testers", action="store_true",
                        help="include BOT_ALLOWED_NUMBERS conversations")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    model = args.model or suggest_settings.model()
    started = datetime.now()
    args.excluded = {}

    with readonly.read_only() as refused:
        cases = await find_cases(args)
        if list(refused):
            raise WriteAttempted("while selecting conversations")
    if not cases:
        print("No conversation with a staff reply to a client message was found.")
        return 1
    print(f"{len(cases)} conversation(s), model {model}; left out: "
          + (", ".join(f"{k} {v}" for k, v in sorted(args.excluded.items())) or "none"))

    for case in cases:
        try:
            result = await draft_case(case, model)
        except context.ConversationNotFound:
            case["error"] = "conversation not found"
            continue
        case["result"] = result
        case["best_overlap"] = max((overlap(s["text"], case["staff_reply"]) for s in result["suggestions"]),
                                   default=0.0)
        print(f"  {case['conversation_id']}: {result['status']}, kept {len(result['suggestions'])}, "
              f"dropped {len(result['_dropped'])}, {result['latency_ms']} ms")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = _out_path(args.out_dir, model, started)
    path.write_text(_render(cases, model, started, args.excluded), encoding="utf-8")
    # The machine-readable copy, for comparing models: masked like the report.
    records = []
    for c in cases:
        record = {k: v for k, v in c.items() if k != "transcript"}
        record["staff_reply"] = mask(c["staff_reply"])
        if "result" in c:
            result = json.loads(json.dumps(c["result"], default=str))
            for item in [*result["suggestions"], *result.get("_dropped", [])]:
                if item.get("text"):
                    item["text"] = mask(item["text"])
            record["result"] = result
        records.append(record)
    json_path = path.with_suffix(".json")
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(asyncio.run(main()))
