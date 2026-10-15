"""Does Claire recognise an existing customer? Four contacts, no network.

Phase 3 Step 8. Four contacts, each looked up and answered the way a real
message would be:

  1. an employer with an active case and a named helper on that placement;
  2. an employer with one past placement only, which names no candidate -
     the shape of most live `placements` rows (candidate_id is null on most);
  3. a helper / candidate on our file;
  4. a number we have never seen.

The database is an in-memory fake holding rows shaped like production's
(`employers`, `placements`, `candidates`, `cases`, `wp_chat_conversations`,
`wp_chat_messages`, ...). Everything between the phone number and the
prompt is the REAL code: `contact.identify`, the webhook's own
`_process_locked` (its parallel reads and the payload it builds, captured at
`run_turn`), then the real `info_collector`, `rag_retriever`,
`response_generator` and `run_turn`'s post-guards, with the model stubbed and
its system prompt captured. So a check reads what the model would be handed,
and a fixture that is missing a column fails the same way production would.

Each failure is labelled:
  code          - the data is there and the code does not use it;
  missing data  - the code would work, but the record it needs is not held
                  (in production as in these fixtures).

Exit status: 0 when the run itself is sound (findings are printed, not
fatal); 1 on a network attempt or a contact that crashed.

No network by default: every socket that is not loopback is refused and
counted, and the run fails if anything tried. Writes to the fake database are
recorded, never applied, and listed at the end.

    python scripts/selfcheck_recognition.py
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import re
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- no network ------------------------------------------------------------
# Loopback stays open: asyncio on Windows builds its self-pipe with a local
# socket pair. Anything else is refused before a byte leaves.
NETWORK_ATTEMPTS: list[str] = []
_LOCAL = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guard(address: Any) -> None:
    host = address[0] if isinstance(address, tuple) else str(address)
    if str(host) not in _LOCAL:
        NETWORK_ATTEMPTS.append(str(host))
        raise ConnectionRefusedError(f"selfcheck_recognition: no network ({host})")


def _connect(self, address):  # noqa: ANN001
    _guard(address)
    return _real_connect(self, address)


def _connect_ex(self, address):  # noqa: ANN001
    _guard(address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host, *a, **kw):  # noqa: ANN001
    if host is not None and str(host) not in _LOCAL:
        NETWORK_ATTEMPTS.append(str(host))
        raise socket.gaierror(f"selfcheck_recognition: no network ({host})")
    return _real_getaddrinfo(host, *a, **kw)


socket.socket.connect = _connect  # type: ignore[method-assign]
socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
socket.getaddrinfo = _getaddrinfo  # type: ignore[assignment]

from app.api import webhook  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.graph import graph as graph_module  # noqa: E402
from app.services import contact as contact_service  # noqa: E402
from app.whapi.parser import IncomingMessage  # noqa: E402

NOW = datetime.now(timezone.utc)

# --- fixtures, shaped like production rows -----------------------------------
# Reserved test numbers (+65 9000 04xx), never a real client's. employers.phone
# is stored spaced, as it is live; wp_chat_conversations.customer_number is bare
# digits, as it is live.
EMP_CASE = "e1000000-0000-4000-8000-000000000001"
EMP_PAST = "e2000000-0000-4000-8000-000000000002"
CAND_HELPER = "c1000000-0000-4000-8000-000000000001"
CAND_SELF = "c3000000-0000-4000-8000-000000000003"
PLACE_CASE = "p1000000-0000-4000-8000-000000000001"
PLACE_PAST = "p2000000-0000-4000-8000-000000000002"
CASE_ID = "k1000000-0000-4000-8000-000000000001"
CASE_NUMBER = "MH-CS-0042"

CONTACTS = {
    "1 employer, active case + named helper": {
        "phone": "+6590000401", "push": "MeiMei", "record": "Tan Mei Ling", "conv": 9401},
    "2 employer, past placement only": {
        "phone": "+6590000402", "push": "Raj K", "record": "Rajesh Kumar", "conv": 9402},
    "3 helper / candidate": {
        "phone": "+6590000403", "push": "Mimi", "record": "Maria Santos", "conv": 9403},
    "4 new number": {
        "phone": "+6590000404", "push": "Kenny", "record": "", "conv": 9404},
}

TABLES: dict[str, list[dict[str, Any]]] = {
    "employers": [
        {"id": EMP_CASE, "display_name": "Tan Mei Ling", "phone": "+65 9000 0401",
         "salesperson_profile_id": None, "archived_at": None},
        {"id": EMP_PAST, "display_name": "Rajesh Kumar", "phone": "+65 9000 0402",
         "salesperson_profile_id": None, "archived_at": None},
    ],
    "placements": [
        {"id": PLACE_CASE, "employer_id": EMP_CASE, "candidate_id": CAND_HELPER,
         "archived_at": None},
        # Past placement only, and like most live rows it names no candidate.
        {"id": PLACE_PAST, "employer_id": EMP_PAST, "candidate_id": None,
         "archived_at": None},
    ],
    "candidates": [
        {"id": CAND_HELPER, "full_name": "Siti Rahayu", "nationality": "Indonesian",
         "phone": "+62 812 0000 0001",
         "biodata": {"passportExpiry": "2027-03-14", "passportNo": "NEVER-READ"}},
        {"id": CAND_SELF, "full_name": "Maria Santos", "nationality": "Filipino",
         "phone": "+65 9000 0403", "biodata": {"passportExpiry": "2029-01-02"}},
    ],
    "cases": [
        {"id": CASE_ID, "case_number": CASE_NUMBER, "case_type": "Renewal",
         "status": "active", "current_stage_key": "documents", "country": "ID",
         "opened_at": (NOW - timedelta(days=12)).isoformat(),
         "placement_id": PLACE_CASE, "archived_at": None},
    ],
    "profiles": [], "leads": [], "leads_candidate": [], "employer_service_requests": [],
    "cb_tickets": [], "wp_chat_messages": [],
    "wp_chat_conversations": [
        {"id": c["conv"], "customer_number": c["phone"].lstrip("+"),
         "customer_name": c["push"], "bot_status": "bot_active", "status": "open",
         "langgraph_thread_id": f"t-{c['conv']}"}
        for c in CONTACTS.values()
    ],
}
WRITES: list[str] = []


# --- the fake database ---------------------------------------------------------
class FakeQuery:
    def __init__(self, table: str):
        self.table, self.filters, self._limit, self._order = table, [], None, None
        self._count, self.write = False, None
        self.http_method, self.path = "GET", f"/rest/v1/{table}"

    def select(self, _cols="*", count=None):  # noqa: ANN001
        self._count = count == "exact"
        return self

    def eq(self, c, v): self.filters.append(lambda r, c=c, v=v: str(r.get(c)) == str(v)); return self  # noqa: E704,E501
    def neq(self, c, v): self.filters.append(lambda r, c=c, v=v: str(r.get(c)) != str(v)); return self  # noqa: E704,E501

    def in_(self, c, vals):
        vals = {str(v) for v in vals}
        self.filters.append(lambda r: str(r.get(c)) in vals)
        return self

    def is_(self, c, v):
        want_null = str(v).lower() == "null"
        self.filters.append(lambda r: (r.get(c) is None) == want_null)
        return self

    def ilike(self, c, pattern):
        rx = re.compile("^" + ".*".join(map(re.escape, str(pattern).split("%"))) + "$", re.I)
        self.filters.append(lambda r: bool(rx.match(str(r.get(c) or ""))))
        return self

    like = ilike

    def match(self, mapping):
        for c, v in mapping.items():
            self.eq(c, v)
        return self

    def order(self, c, desc=False, **_):  # noqa: ANN001
        self._order = (c, desc)
        return self

    def limit(self, n): self._limit = n; return self  # noqa: E704
    def range(self, a, b): self._limit = b - a + 1; return self  # noqa: E704

    def _write(self, kind):
        self.write, self.http_method = kind, kind
        return self

    def insert(self, *_a, **_k): return self._write("POST")  # noqa: E704
    def upsert(self, *_a, **_k): return self._write("POST")  # noqa: E704
    def update(self, *_a, **_k): return self._write("PATCH")  # noqa: E704
    def delete(self, *_a, **_k): return self._write("DELETE")  # noqa: E704

    def run(self) -> SimpleNamespace:
        if self.write:
            WRITES.append(f"{self.write} {self.table}")
            return SimpleNamespace(data=[], count=None)
        rows = [r for r in TABLES.get(self.table, []) if all(f(r) for f in self.filters)]
        if self._order:
            col, desc = self._order
            rows.sort(key=lambda r: str(r.get(col) or ""), reverse=desc)
        count = len(rows)
        if self._limit is not None:
            rows = rows[: self._limit]
        return SimpleNamespace(data=copy.deepcopy(rows), count=count)


async def _execute(query):  # noqa: ANN001
    return query.run()


async def _rpc(name, params=None):  # noqa: ANN001
    WRITES.append(f"RPC {name}")
    return None


# --- driving one contact ----------------------------------------------------------
def _history(conv_id: int, hours_ago: float | None) -> None:
    """One earlier exchange on this thread, ending `hours_ago` hours ago."""
    TABLES["wp_chat_messages"] = [m for m in TABLES["wp_chat_messages"]
                                  if m["conversation_id"] != conv_id]
    if hours_ago is None:
        return
    at = NOW - timedelta(hours=hours_ago)
    TABLES["wp_chat_messages"] += [
        {"conversation_id": conv_id, "direction": "inbound", "body": "Hello",
         "is_bot": False, "sent_by": None, "whapi_message_id": f"h{conv_id}a",
         "created_at": (at - timedelta(minutes=1)).isoformat()},
        {"conversation_id": conv_id, "direction": "outbound",
         "body": "Hi, I'm Claire, Ming Hwee's AI assistant. How can I help you?",
         "is_bot": True, "sent_by": "bot", "whapi_message_id": f"h{conv_id}b",
         "created_at": at.isoformat()},
    ]


async def payload_for(name: str, text: str, hours_ago: float | None) -> dict[str, Any]:
    """identify() then the webhook's own _process_locked, stopped at run_turn."""
    c = CONTACTS[name]
    identity = await contact_service.identify(c["phone"])
    row = next(r for r in TABLES["wp_chat_conversations"] if r["id"] == c["conv"])
    row.update({k: identity.get(k) for k in (
        "contact_type", "matched_employer_id", "matched_candidate_id",
        "matched_supplier_id", "matched_case_id")})
    _history(c["conv"], hours_ago)
    captured: dict[str, Any] = {}

    async def _capture(_thread, payload):  # noqa: ANN001
        captured.update(payload)
        return {"reply": ""}

    message = IncomingMessage(
        whapi_message_id=f"in-{c['conv']}-{len(WRITES)}", chat_id=f"{c['phone'][1:]}@s.whatsapp.net",
        customer_number=c["phone"], customer_name=c["push"], body=text,
        message_type="text", from_me=False, timestamp=NOW)
    with patch.object(webhook, "run_turn", new=_capture):
        await webhook._process_locked(c["phone"], [message], allow_hold=False)
    captured["_identity"] = identity
    return captured


PROMPT = {"text": ""}


async def _stub_complete(system_prompt, *a, **kw):  # noqa: ANN001
    PROMPT["text"] = system_prompt or ""
    return "Thanks. May I know a little more?"


def _state(payload: dict[str, Any], intent: str, service: str | None) -> dict[str, Any]:
    state = {k: v for k, v in payload.items() if not k.startswith("_")}
    state.update(intent=intent, service_type=service, confidence=0.95,
                 collected_info={}, asked_field_counts={}, rag_matches=[],
                 rag_context="", rag_best_score=0.0, briefed_services=[])
    return state


async def run_collector(payload, intent, service, **extra):  # noqa: ANN001
    ic = importlib.import_module("app.graph.nodes.info_collector")
    PROMPT["text"] = ""
    state = {**_state(payload, intent, service), **extra}
    with patch.object(ic, "complete", new=_stub_complete, create=True), \
            patch.object(ic, "complete_json", new=AsyncMock(return_value={}), create=True), \
            patch.object(ic, "_open_lead_early", new=AsyncMock(return_value={})):
        out = await ic.info_collector(state)
    return out, PROMPT["text"]


async def run_answer(payload, intent, service=None, reply=None):  # noqa: ANN001
    """rag_retriever (search stubbed empty) then response_generator."""
    # By module path: app.graph.nodes re-exports each node FUNCTION under the
    # module's own name (the Step 2c trap), so `from ... import` gets the function.
    rr = importlib.import_module("app.graph.nodes.rag_retriever")
    rg = importlib.import_module("app.graph.nodes.response_generator")
    from app.services import rag
    state = _state(payload, intent, service)
    with patch.object(rag, "search", new=AsyncMock(return_value=[])), \
            patch.object(rag, "embed_query", new=AsyncMock(return_value=[0.0] * 8)):
        state.update(await rr.rag_retriever(state))

    async def _reply(system_prompt, *a, **kw):  # noqa: ANN001
        PROMPT["text"] = system_prompt or ""
        return reply or "Thanks for checking in."

    with patch.object(rg, "complete", new=_reply, create=True):
        out = await rg.response_generator(state)
    return {**state, **out}, PROMPT["text"]


async def through_post_guards(reply: str, payload: dict[str, Any]) -> str:
    """What run_turn's post-guards leave of a reply the model wrote."""
    class _Graph:
        async def ainvoke(self, *_a, **_kw):
            return {**payload, "reply": reply, "rag_matches": []}

    with patch.object(graph_module.kb_rules, "refresh", new=AsyncMock()), \
            patch.object(graph_module, "get_graph", new=lambda: _Graph()):
        return (await graph_module.run_turn("t", dict(payload)))["reply"]


# --- the checks ---------------------------------------------------------------------
RESULTS: list[tuple[str, str, bool, str, str]] = []


def check(contact: str, label: str, ok: bool, on_fail: str, detail: str = "") -> None:
    RESULTS.append((contact, label, bool(ok), on_fail, detail))


def asked(out: dict[str, Any]) -> set[str]:
    return {k for k, v in (out.get("asked_field_counts") or {}).items() if v and v > 0}


def welcomes(prompt: str) -> bool:
    return "is back after a break" in prompt


async def contact_1() -> None:
    n = "1 employer, active case + named helper"
    p = await payload_for(n, "Hi", hours_ago=1)
    check(n, "identified as an employer, with its case", p["contact_type"] == "employer"
          and p["_identity"].get("matched_case_id") == CASE_ID, "missing data")
    check(n, "record name read from employers", p["record_name"] == "Tan Mei Ling", "code")
    _, prompt = await run_answer(p, "greeting")
    check(n, "greeting: the record name is in the prompt", "Tan Mei Ling" in prompt, "code")
    check(n, "greeting: the WhatsApp push name is not", "MeiMei" not in prompt, "code")

    # The client already chose one-by-one on an earlier visit (Step 6c keeps
    # the choice), so this turn is the ordinary opening of the collection.
    p = await payload_for(n, "I want to renew my helper's work permit", hours_ago=1)
    out, prompt = await run_collector(p, "renewal", "renewal", question_style="one")
    check(n, "renewal: her name is not asked (held on file)", "full_name" not in asked(out),
          "code", f"asked {sorted(asked(out))}")
    check(n, "renewal: the push name is not in the prompt", "MeiMei" not in prompt, "code")
    check(n, "renewal: the known helper is confirmed BY NAME",
          "Siti Rahayu" in prompt and "Do NOT ask for the helper's name" in prompt, "code")
    check(n, "renewal: no welcome back one hour after the last message",
          not welcomes(prompt), "code")

    for hours, want, why in ((10, True, "10 h"), (5.9, False, "5.9 h"),
                             (6.0, False, "exactly 6 h is not 'more than 6'")):
        p = await payload_for(n, "I want to renew my helper's work permit", hours_ago=hours)
        _, prompt = await run_collector(p, "renewal", "renewal", question_style="one")
        check(n, f"welcome back only after more than 6 h ({why})",
              welcomes(prompt) == want, "code",
              f"hours_since_last_message={p.get('hours_since_last_message')}")

    # Back after a break and the one-by-one choice never made: the opening
    # collector turn is where both the welcome and the question-style offer fall.
    p = await payload_for(n, "I want to renew my helper's work permit", hours_ago=10)
    out, prompt = await run_collector(p, "renewal", "renewal")
    reply = out.get("reply") or ""
    check(n, "back after 10 h, style not yet chosen: welcomed back, helper confirmed",
          welcomes(prompt) and "Siti Rahayu" in (prompt + reply), "code",
          f"the question-style offer took the turn: {reply[:70]!r}"
          if out.get("question_style") == "offered" else "")

    p = await payload_for(n, "any update on my case?", hours_ago=1)
    state, prompt = await run_answer(p, "case_enquiry")
    check(n, "case update: the case row is read", bool(state.get("case_summary")), "missing data")
    check(n, "case update: answered from the record, not handed over",
          not state.get("needs_handover") and "documents" in prompt, "code",
          f"needs_handover={state.get('needs_handover')}")
    check(n, "case update: the prompt forbids reading the case number out",
          "Do NOT read a case number" in prompt, "code")
    leaked = await through_post_guards(
        f"Your case {CASE_NUMBER} is at the documents stage.", p)
    check(n, "case update: a reply that reads the case number out is stopped",
          CASE_NUMBER not in leaked, "code",
          "prompt rule only; no guard removes it" if CASE_NUMBER in leaked else "")


async def contact_2() -> None:
    n = "2 employer, past placement only"
    p = await payload_for(n, "Hi", hours_ago=1)
    check(n, "identified as an employer, one placement counted",
          p["contact_type"] == "employer" and p["prior_hires"] == 1, "missing data")
    check(n, "record name read from employers", p["record_name"] == "Rajesh Kumar", "code")
    _, prompt = await run_answer(p, "greeting")
    check(n, "greeting: the record name is in the prompt", "Rajesh Kumar" in prompt, "code")
    check(n, "greeting: the WhatsApp push name is not", "Raj K" not in prompt, "code")

    p = await payload_for(n, "I want to hire a new helper", hours_ago=10)
    out, prompt = await run_collector(p, "new_hiring", "new_hiring", question_style="one")
    held = {"full_name", "first_time_hire", "referral_source"} & asked(out)
    check(n, "new hire: name, 'hired before' and referral are not asked", not held, "code",
          f"asked {sorted(asked(out))}")
    check(n, "new hire: welcomed back after 10 h, as a past client",
          welcomes(prompt) and "hired a helper through us before" in prompt, "code")

    p = await payload_for(n, "I want to hire a new helper", hours_ago=10)
    out, prompt = await run_collector(p, "new_hiring", "new_hiring")
    reply = out.get("reply") or ""
    check(n, "back after 10 h, style not yet chosen: welcomed back", welcomes(prompt), "code",
          f"the question-style offer took the turn: {reply[:70]!r}"
          if out.get("question_style") == "offered" else "")

    p = await payload_for(n, "my helper wants to go home for a month", hours_ago=1)
    out, prompt = await run_collector(p, "home_leave", "home_leave", question_style="one")
    check(n, "home leave: the known helper is confirmed BY NAME",
          "Do NOT ask for the helper's name" in prompt, "missing data",
          "placements.candidate_id is null, so we cannot say who she is"
          if not p.get("known_helpers") else "")

    p = await payload_for(n, "any update on my case?", hours_ago=1)
    state, _ = await run_answer(p, "case_enquiry")
    check(n, "case update with no case on file: handed over, nothing invented",
          state.get("needs_handover") and not state.get("case_summary"), "code")


async def contact_3() -> None:
    n = "3 helper / candidate"
    p = await payload_for(n, "Hi", hours_ago=1)
    check(n, "identified as a candidate", p["contact_type"] == "candidate", "missing data")
    _, prompt = await run_answer(p, "greeting")
    check(n, "greeting: the WhatsApp push name is not in the prompt", "Mimi" not in prompt, "code")
    check(n, "greeting: her name on file (candidates.full_name) is used",
          "Maria Santos" in prompt, "code",
          "get_record_name reads employers only" if "Maria Santos" not in prompt else "")

    p = await payload_for(n, "I am looking for a job as a helper", hours_ago=10)
    out, prompt = await run_collector(p, "candidate_registration", "candidate_new_hiring",
                                      question_style="one")
    check(n, "registration: the push name is not in the prompt", "Mimi" not in prompt, "code")
    reasked = {"full_name", "nationality"} & asked(out)
    check(n, "registration: name and country held on file are not asked", not reasked,
          "code", f"asked {sorted(asked(out))}")
    check(n, "registration: welcomed back after 10 h (she is on our file)",
          welcomes(prompt), "code",
          "existing_client() never looks at matched_candidate_id" if not welcomes(prompt) else "")
    check(n, "not treated as a new number", "New number, not in our system" not in prompt, "code")


async def contact_4() -> None:
    n = "4 new number"
    p = await payload_for(n, "Hi", hours_ago=None)
    check(n, "identified as unknown, nothing held",
          p["contact_type"] == "unknown" and not p["record_name"] and not p["prior_hires"]
          and not p["known_helpers"] and not p["matched_cases"], "code")
    _, prompt = await run_answer(p, "greeting")
    check(n, "greeting: told it is a new number", "New number, not in our system" in prompt, "code")
    check(n, "greeting: the WhatsApp push name is not in the prompt", "Kenny" not in prompt, "code")

    p = await payload_for(n, "I want to hire a helper", hours_ago=10)
    out, prompt = await run_collector(p, "new_hiring", "new_hiring", question_style="one")
    check(n, "new hire: asked for their name", "full_name" in asked(out), "code",
          f"asked {sorted(asked(out))}")
    check(n, "new hire: no welcome back, even 10 h after an earlier chat",
          not welcomes(prompt), "code")
    check(n, "new hire: no helper to confirm", "Do NOT ask for the helper's name" not in prompt, "code")


async def main() -> int:
    with patch.object(db, "table", new=lambda name: FakeQuery(name)), \
            patch.object(db, "execute", new=_execute), patch.object(db, "rpc", new=_rpc), \
            patch.object(webhook, "_predates_our_last_reply", new=lambda *a, **k: False):
        for run in (contact_1, contact_2, contact_3, contact_4):
            try:
                await run()
            except Exception as exc:  # noqa: BLE001 - a crash is a result, not an exit
                check(run.__name__, f"ran without raising ({type(exc).__name__}: {exc})",
                      False, "code")
    check("all", "no network call was attempted", not NETWORK_ATTEMPTS, "code",
          ", ".join(sorted(set(NETWORK_ATTEMPTS))))

    current = None
    for contact, label, ok, on_fail, detail in RESULTS:
        if contact != current:
            print(f"\n{contact}")
            current = contact
        tag = "PASS" if ok else f"FAIL [{on_fail}]"
        print(f"  {tag:<20} {label}" + (f"  ({detail})" if detail and not ok else ""))
    failed = [r for r in RESULTS if not r[2]]
    print(f"\nwrites refused by the fake database: {len(WRITES)} "
          f"({', '.join(sorted(set(WRITES))) or 'none'})")
    by = {k: sum(1 for r in failed if r[3] == k) for k in ("code", "missing data")}
    print(f"{len(RESULTS) - len(failed)} of {len(RESULTS)} pass; "
          f"{by['code']} code, {by['missing data']} missing data")
    # A finding is reported, not an exit: several are known and recorded in
    # CLAUDE.md section 9. Exit 1 only when the harness itself is not to be
    # trusted - a network attempt, or a contact that crashed.
    broken = NETWORK_ATTEMPTS or any(r[1].startswith("ran without raising") for r in failed)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
