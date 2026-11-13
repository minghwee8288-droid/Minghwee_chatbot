"""Offline self-check for the Help service (app/help/, POST /internal/help/answer).

Runs the real Help app over an in-process ASGI transport. rag.search,
rag.embed_query, rag.column_vocabulary and the model are fakes; nothing touches
the network (no_network is imported first and the run fails if anything tried).

    python scripts/selfcheck_help.py

Exit 1 on any failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import no_network  # noqa: E402,F401 - first: this suite cannot reach production

import httpx  # noqa: E402

from app.db.supabase import db  # noqa: E402
from app.help import answer as h_answer  # noqa: E402
from app.help import llm as h_llm  # noqa: E402
from app.help import prompt as h_prompt  # noqa: E402
from app.help import retrieve as h_retrieve  # noqa: E402
from app.help import settings as h_settings  # noqa: E402
from app.help.main import create_app  # noqa: E402
from app.services import rag  # noqa: E402

SECRET = "selfcheck-help-secret"
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if (detail and not ok) else ''}")


# --- Fakes -------------------------------------------------------------------

def row(n: int, *, sim: float, service: str = "transfer", nat: str = "PH",
        question: str = "", answer: str = "", content: str = "", chunk: str = "qa_pair") -> dict:
    return {
        "id": f"00000000-0000-0000-0000-00000000000{n}", "chunk_type": chunk,
        "question": question, "answer": answer, "content": content,
        "source_document": "Ming Hwee Service Notes", "section_heading": f"Heading {n}",
        "service_type": service, "contact_type": "employer", "nationality": nat,
        "similarity": sim, "metadata": {},
    }


FEE_ROWS = [
    row(1, sim=0.82, question="How much is the transfer agency fee for a Filipino helper?",
        answer="The agency fee for a Filipino transfer helper is $1,688."),
    row(2, sim=0.70, nat="ID", question="How much is the transfer agency fee for an Indonesian helper?",
        answer="The agency fee for an Indonesian transfer helper is $1,588."),
]


class Fake:
    """Records every search and model call; answers from a script."""

    def __init__(self) -> None:
        self.searches: list[dict] = []
        self.calls: list[dict] = []
        self.embedded: list[str] = []
        self.search_results: list[list[dict]] = [FEE_ROWS]
        self.plan: dict | Exception = {"standalone_query": "What is the transfer fee for a Filipino helper?",
                                       "service_type": "transfer", "nationality": "PH"}
        self.reply: str | Exception = "The agency fee for a Filipino transfer helper is $1,688 [1]."
        self.embed_error: Exception | None = None
        self.answer_delay = 0.0
        self.on_search = None  # optional async hook

    async def search(self, query: str, **kwargs: Any) -> list[dict]:
        self.searches.append({"query": query, **kwargs})
        if self.on_search:
            await self.on_search()
        index = min(len(self.searches) - 1, len(self.search_results) - 1)
        return [dict(r) for r in self.search_results[index]]

    async def embed(self, text: str) -> list[float]:
        self.embedded.append(text)
        if self.embed_error:
            raise self.embed_error
        return [0.0] * 4

    async def vocabulary(self, column: str) -> set[str]:
        return {"general", "new_hiring", "direct_hiring", "transfer", "replacement",
                "renewal", "passport_renewal", "home_leave"}

    async def complete(self, system: str, user: str, *, json_mode: bool = False, **kw: Any) -> str:
        self.calls.append({"system": system, "user": user, "json": json_mode})
        if json_mode:
            if isinstance(self.plan, Exception):
                raise self.plan
            return json.dumps(self.plan)
        if self.answer_delay:
            await asyncio.sleep(self.answer_delay)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    @property
    def answer_calls(self) -> list[dict]:
        return [c for c in self.calls if not c["json"]]


def body(messages: list[dict] | None = None, **over: Any) -> dict:
    data = {
        "request_id": str(uuid.uuid4()),
        "user_role": "consultant",
        "messages": messages or [{"role": "user", "content": "What is the transfer fee for a Filipino helper?"}],
    }
    data.update(over)
    return data


async def post(client: httpx.AsyncClient, data: Any, *, secret: str | None = SECRET, raw: bytes | None = None):
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers["X-Help-Secret"] = secret
    content = raw if raw is not None else json.dumps(data).encode()
    return await client.post("/internal/help/answer", content=content, headers=headers)


def env(**values: str):
    base = {"HELP_SECRET": SECRET, "HELP_MAX_CONCURRENCY": "4", "HELP_TIMEOUT_SECONDS": "5",
            "HELP_HISTORY_TURNS": "10", "HELP_MATCH_COUNT": "8", "HELP_MODEL": "test/model"}
    base.update(values)
    return patch.dict(os.environ, base)


async def run(fake: Fake, data: Any = None, *, secret: str | None = SECRET, raw: bytes | None = None,
              **envs: str) -> httpx.Response:
    with env(**envs), patch.object(rag, "search", fake.search), \
            patch.object(rag, "embed_query", fake.embed), \
            patch.object(rag, "column_vocabulary", fake.vocabulary), \
            patch.object(h_llm, "complete", fake.complete):
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://help") as client:
            return await post(client, data if data is not None else body(), secret=secret, raw=raw)


def code(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["code"]
    except Exception:  # noqa: BLE001
        return ""


# --- The checks --------------------------------------------------------------

async def checks() -> None:
    # Happy path.
    fake = Fake()
    resp = await run(fake)
    data = resp.json()
    check(resp.status_code == 200 and data["status"] == "ok" and "[1]" in data["answer"]
          and data["sources"][0]["n"] == 1 and data["retrieval"]["rows"] == 2,
          "ok: a grounded answer with numbered sources", str(data)[:300])
    check(set(data) == {"status", "answer", "sources", "retrieval", "guard_flags", "model", "latency_ms"}
          and set(data["sources"][0]) == {"n", "kb_id", "source_document", "section_heading",
                                          "service_type", "contact_type", "nationality", "similarity"}
          and set(data["retrieval"]) == {"search_query", "service_type", "nationality", "price_question",
                                         "widened", "top_score", "floor", "rows"},
          "ok: the response has exactly the agreed fields")
    check(all(s.get("contact_type") is None for s in fake.searches),
          "retrieval searches BOTH audiences (contact_type never filtered)")

    # 404 / 401.
    resp = await run(Fake(), HELP_SECRET="")
    unknown = None
    with env(HELP_SECRET=""):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://h") as c:
            unknown = await c.get("/no/such/path")
            health = await c.get("/internal/help/health")
            get_answer = await c.get("/internal/help/answer")
    check(resp.status_code == 404 and code(resp) == "not_found",
          "404 when HELP_SECRET is empty", f"{resp.status_code} {resp.text[:100]}")
    check(unknown.status_code == 404 and unknown.json() == resp.json(),
          "404 with the secret empty looks exactly like an unknown path")
    check(get_answer.status_code == 404 and get_answer.json() == resp.json(),
          "GET on the answer path is the same 404 (405 is not revealed)")
    check(health.status_code == 200 and health.json() == {"ok": True},
          "health answers {ok: true} even with HELP_SECRET empty")
    resp = await run(Fake(), secret="wrong")
    check(resp.status_code == 401 and code(resp) == "unauthorized", "401 on a wrong secret")
    resp = await run(Fake(), secret=None)
    check(resp.status_code == 401, "401 on a missing secret")
    fake = Fake()
    resp = await run(fake, raw=b"{not json", secret="wrong")
    check(resp.status_code == 401, "the secret is checked BEFORE the body (bad body + bad secret = 401)")

    # 413 / 422.
    resp = await run(Fake(), raw=b"{" + b" " * (33 * 1024) + b"}")
    check(resp.status_code == 413 and code(resp) == "body_too_large", "413 on a body over 32 KB")
    big_ok = body([{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 1990} for i in range(9)])
    resp = await run(Fake(), big_ok)
    check(resp.status_code == 200, "a 9-turn conversation of ~2,000-char messages (~18 KB) is accepted",
          f"{resp.status_code}")
    cases = {
        "last message not user": body([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]),
        "more than 10 turns": body([{"role": "user", "content": "a"}] * 11),
        "content over 2000 chars": body([{"role": "user", "content": "x" * 2001}]),
        "request_id not a UUID": body(request_id="abc"),
        "no messages": body([]) | {"messages": []},
        "bad role": body([{"role": "system", "content": "a"}]),
    }
    for name, data in cases.items():
        resp = await run(Fake(), data)
        check(resp.status_code == 422 and code(resp) == "invalid_request", f"422: {name}",
              f"{resp.status_code}")
    resp = await run(Fake(), raw=b"{not json")
    check(resp.status_code == 422, "422: not JSON")

    # 429: hold one request inside the run, then send another.
    fake = Fake()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    fake.on_search = hold
    with env(HELP_MAX_CONCURRENCY="1"), patch.object(rag, "search", fake.search), \
            patch.object(rag, "embed_query", fake.embed), \
            patch.object(rag, "column_vocabulary", fake.vocabulary), \
            patch.object(h_llm, "complete", fake.complete):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://h") as c:
            first = asyncio.create_task(post(c, body()))
            await asyncio.wait_for(entered.wait(), 5)
            second = await post(c, body())
            release.set()
            first_resp = await first
    check(second.status_code == 429 and code(second) == "busy" and first_resp.status_code == 200,
          "429 at once when the semaphore is full (never queued)", f"{second.status_code}")

    # 504.
    fake = Fake()
    fake.answer_delay = 3.0
    resp = await run(fake, HELP_TIMEOUT_SECONDS="0.3")
    check(resp.status_code == 504 and code(resp) == "timeout", "504 on timeout", f"{resp.status_code}")

    # not_found makes no answer call.
    fake = Fake()
    fake.search_results = [[row(1, sim=0.30, answer="x", question="y")]]
    resp = await run(fake)
    data = resp.json()
    check(data.get("status") == "not_found" and data["answer"] == h_answer.NOT_FOUND_ANSWER
          and not fake.answer_calls and data["sources"] == [],
          "not_found below the floor makes NO answer call", str(data)[:200])
    fake = Fake()
    fake.search_results = [[]]
    resp = await run(fake)
    check(resp.json().get("status") == "not_found" and not fake.answer_calls,
          "not_found with no rows makes NO answer call")

    # Price question: filter kept, never widened.
    fake = Fake()
    fake.search_results = [[row(1, sim=0.30, answer="The fee is $1,688.", question="fee?")]]
    resp = await run(fake)
    check(len(fake.searches) == 1 and fake.searches[0]["service_type"] == "transfer"
          and resp.json()["retrieval"]["price_question"] is True and resp.json()["retrieval"]["widened"] is False,
          "a price question with a service keeps the filter and never widens",
          str(fake.searches))

    # Non-price low score widens once, nationality kept.
    fake = Fake()
    fake.plan = {"standalone_query": "How long does a transfer take?", "service_type": "transfer", "nationality": "PH"}
    fake.search_results = [[row(1, sim=0.30, answer="a", question="q")],
                           [row(2, sim=0.60, service="general", answer="About 1 to 2 weeks.", question="How long?")]]
    fake.reply = "About 1 to 2 weeks [1]."
    resp = await run(fake, body([{"role": "user", "content": "How long does a transfer take?"}]))
    check(len(fake.searches) == 2 and fake.searches[1]["service_type"] is None
          and fake.searches[1]["nationality"] == "PH" and resp.json()["retrieval"]["widened"] is True,
          "a non-price question below the floor widens ONCE, nationality kept", str(fake.searches))
    fake = Fake()
    fake.plan = {"standalone_query": "How long does a transfer take?", "service_type": "transfer", "nationality": "PH"}
    fake.search_results = [[row(1, sim=0.30, answer="a", question="q")], [row(2, sim=0.20, answer="b", question="q")]]
    resp = await run(fake, body([{"role": "user", "content": "How long does a transfer take?"}]))
    check(resp.json()["retrieval"]["widened"] is False and resp.json()["status"] == "not_found",
          "the wider result is used only if it scores better")

    # Nationality unknown on a price question: search with nationality None.
    fake = Fake()
    fake.plan = {"standalone_query": "What is the transfer fee?", "service_type": "transfer", "nationality": None}
    fake.reply = "Philippines: $1,688 [1]. Indonesia: $1,588 [2]."
    resp = await run(fake, body([{"role": "user", "content": "What is the transfer fee?"}]))
    check(fake.searches[0]["nationality"] is None and fake.searches[0]["service_type"] == "transfer"
          and resp.json()["status"] == "ok",
          "a price question with no nationality searches nationality=None", str(fake.searches))

    # Planner output validated.
    fake = Fake()
    fake.plan = {"standalone_query": "q", "service_type": "dispute_assault", "nationality": "SG"}
    await run(fake)
    check(fake.searches[0]["service_type"] is None and fake.searches[0]["nationality"] is None,
          "planner: an unknown service or nationality becomes null")
    check(h_retrieve.allowed_services() == frozenset({"renewal", "passport_renewal", "home_leave",
                                                      "new_hiring", "transfer", "direct_hiring", "replacement"}),
          "planner: the service list comes from kb_rules (KB labels, no fee_enquiry)")

    # Planner failure falls back to the last user message.
    for exc in (h_llm.HelpLLMError("x"), RuntimeError("boom")):
        fake = Fake()
        fake.plan = exc
        last = "and for Indonesia?"
        resp = await run(fake, body([{"role": "user", "content": "transfer fee for a Filipino?"},
                                     {"role": "assistant", "content": "It is $1,688 [1]."},
                                     {"role": "user", "content": last}]))
        check(resp.status_code == 200 and fake.searches and fake.searches[0]["query"] == last
              and fake.searches[0]["service_type"] is None,
              f"planner failure ({type(exc).__name__}) falls back to the last user message")

    # Guards.
    async def guarded(reply: str, **fake_over: Any) -> dict:
        fake = Fake()
        fake.reply = reply
        for k, v in fake_over.items():
            setattr(fake, k, v)
        return (await run(fake)).json()

    data = await guarded("The fee is $1,999 [1].")
    check(data["status"] == "refused" and "ungrounded_figure" in data["guard_flags"]
          and data["answer"] == h_answer.REFUSED_ANSWER and data["sources"],
          "an ungrounded figure is refused (sources still returned)", str(data)[:200])
    data = await guarded("Is it $1,999? The fee is $1,688 [1].".replace("Is it $1,999? ", "") + " Not $1,999.")
    check("ungrounded_figure" in data["guard_flags"], "a figure only in the staff question does not ground")
    fake = Fake()
    fake.reply = "Yes, it is $2,500 [1]."
    resp = await run(fake, body([{"role": "user", "content": "Is the transfer fee $2,500?"}]))
    check(resp.json()["status"] == "refused",
          "figures are grounded on the records ONLY (a figure the staff member typed is not)")
    data = await guarded(" ".join(["fee fee fee fee"] * 10) + " [1]")
    check(data["status"] == "refused" and "degenerate" in data["guard_flags"], "degenerate output is refused")
    # Live shape (replay 2026-11-13): "[1][2][3]" is 9 characters, over is_degenerate's 8.
    data = await guarded("- Copy of the NRIC. [1][2][1]\n- Renewal Notification. [1][2][1]\n"
                         "- Her passport. [1][2][1]\n- Her new salary. [1][2][1]")
    check(data["status"] == "ok" and "degenerate" not in data["guard_flags"],
          "a list whose lines all end with the same citations is not 'degenerate'", str(data)[:200])
    # NOT_COVERED: the records scored above the floor but do not answer.
    data = await guarded("The records do not state the staff annual leave policy. [[NOT_COVERED]]")
    check(data["status"] == "not_found" and "not_covered" in data["guard_flags"]
          and "[[" not in data["answer"] and "annual leave" in data["answer"] and data["sources"],
          "the NOT_COVERED marker gives not_found, keeps the text and sources, strips the marker",
          str(data)[:200])
    data = await guarded("The fee is $1,688 [1].")
    check(data["status"] == "ok" and "not_covered" not in data["guard_flags"],
          "an answer without the marker stays ok")
    check("[[NOT_COVERED]]" in h_prompt.STAFF_PROMPT and h_answer.NOT_COVERED_MARKER == "[[NOT_COVERED]]",
          "the staff prompt asks for the exact marker")
    data = await guarded("The client asked about fees, so the fee is $1,688 [1].")
    check(data["status"] == "refused" and "internal_reasoning" in data["guard_flags"],
          "internal-reasoning leak is refused")
    data = await guarded("The fee is $1,688 [1][7] and [3, 1].")
    check(data["status"] == "ok" and "bad_citation" in data["guard_flags"] and "[7]" not in data["answer"]
          and "[3" not in data["answer"] and "[1]" in data["answer"],
          "a citation pointing at no source is removed", data["answer"])
    # Dummy numbers only (all zeros and ones), never a real one.
    data = await guarded("The fee is $1,688 [1]. Call us on +65 0000 1111 or 0000 1111 or 1800-000-1111.")
    check(data["status"] == "ok" and "phone_number" in data["guard_flags"]
          and not any(d in data["answer"] for d in ("0000", "1111")),
          "phone numbers are removed and flagged", data["answer"])
    kept_text = "The fee is $1,688 (S$60,000 cover), valid 2025-2026, from 2026-10-15, CB-2026-0100; fees 350 250 60."
    stripped, phoned = h_answer.strip_phone_numbers(kept_text)
    check(not phoned and stripped == kept_text,
          "money, dates, year ranges, references and short number lists are not taken for phone numbers",
          stripped)
    # Nationality fee guard, with the staff line.
    fake = Fake()
    fake.plan = {"standalone_query": "Indonesian transfer fee?", "service_type": "transfer", "nationality": "ID"}
    fake.reply = "The fee for an Indonesian helper is $1,688 [1]."
    resp = await run(fake, body([{"role": "user", "content": "Indonesian transfer fee?"}]))
    data = resp.json()
    check("nationality_fee_guard" in data["guard_flags"] and "$1,688" not in data["answer"]
          and h_answer.STAFF_FEE_LINE in data["answer"] and "Our agent" not in data["answer"],
          "another nationality's fee is cut and replaced with the STAFF line", data["answer"])
    fake = Fake()
    fake.plan = {"standalone_query": "Myanmar passport renewal fee?", "service_type": "passport_renewal",
                 "nationality": "MM"}
    fake.search_results = [[row(1, sim=0.8, service="passport_renewal", nat="all",
                                question="Passport renewal fee?", answer="$450 for a Filipino or Indonesian helper.")]]
    fake.reply = "It is $450 [1]."
    resp = await run(fake, body([{"role": "user", "content": "Myanmar passport renewal fee?"}]))
    data = resp.json()
    check("$450" not in data["answer"] and h_answer.STAFF_FEE_LINE in data["answer"],
          "a fee the KB does not hold for that nationality is replaced with the staff line", data["answer"])

    # Errors.
    fake = Fake()
    fake.embed_error = RuntimeError("provider down")
    resp = await run(fake)
    check(resp.status_code == 500 and code(resp) == "search_unavailable" and not fake.searches,
          "an embedding failure is 500 search_unavailable (not not_found)")
    fake = Fake()
    fake.reply = h_llm.HelpLLMError("x")
    resp = await run(fake)
    check(resp.status_code == 500 and code(resp) == "llm_error", "a failed answer call is 500 llm_error")

    # Read-only: a write through the real database door during the run.
    for kind in ("table", "rpc"):
        fake = Fake()

        async def write(kind: str = kind) -> None:
            if kind == "table":
                query = SimpleNamespace(http_method="POST", path="/rest/v1/cb_tickets",
                                        execute=lambda: (_ for _ in ()).throw(AssertionError("executed")))
                await db.execute(query)
            else:
                await db.rpc("cb_get_next_agent", {})

        fake.on_search = write
        resp = await run(fake)
        check(resp.status_code == 500 and code(resp) == "read_only_violation",
              f"a refused {kind} write inside the run is 500 read_only_violation", f"{resp.status_code}")

    # NRIC never reaches the model or the search.
    fake = Fake()
    nric = "S1234567D"
    await run(fake, body([{"role": "user", "content": f"Helper {nric}: what is the transfer fee?"}]))
    seen = json.dumps(fake.calls) + json.dumps(fake.searches)
    check(fake.calls and nric not in seen and "[REDACTED-ID]" in seen,
          "an NRIC in the input never reaches the model")

    # Routes.
    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    check(paths == {"/internal/help/answer", "/internal/help/health"},
          "Help has exactly two routes (no webhook, no docs, no admin, no suggest)", str(paths))
    with env():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://h") as c:
            gone = [await c.get(p) for p in ("/docs", "/redoc", "/openapi.json", "/", "/health",
                                              "/webhook/whapi/x")]
    check(all(r.status_code == 404 for r in gone), "no /docs, /redoc, /openapi.json, /, /health or webhook")


def import_check() -> None:
    """Importing app.help.main in a fresh process starts nothing of Claire's."""
    code_ = r"""
import sys, threading, asyncio, json
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + '/scripts')
import no_network
threads = {t.ident for t in threading.enumerate()}
loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
async def tasks(): return {id(t) for t in asyncio.all_tasks() if t is not asyncio.current_task()}
before_tasks = loop.run_until_complete(tasks())
import app.help.main
after_tasks = loop.run_until_complete(tasks())
print(json.dumps({
  "new_threads": len({t.ident for t in threading.enumerate()} - threads),
  "new_tasks": len(after_tasks - before_tasks),
  "network": list(no_network.ATTEMPTS),
  "app_main": "app.main" in sys.modules,
  "webhook": "app.api.webhook" in sys.modules,
  "suggest": any(m.startswith("app.suggest") for m in sys.modules),
  "graph_built": getattr(sys.modules.get("app.graph.graph"), "_graph", None) is not None,
}))
"""
    out = subprocess.run([sys.executable, "-I", "-c", code_, str(ROOT)], capture_output=True, text=True, timeout=120)
    try:
        facts = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        check(False, "import of app.help.main in a fresh process", out.stderr[-500:])
        return
    check(facts["new_threads"] == 0, "importing app.help.main starts no thread", str(facts))
    check(facts["new_tasks"] == 0, "importing app.help.main starts no asyncio task", str(facts))
    check(not facts["network"], "importing app.help.main makes no network call", str(facts))
    check(not facts["app_main"] and not facts["webhook"],
          "importing app.help.main loads neither app.main nor app.api.webhook", str(facts))
    check(not facts["suggest"], "importing app.help.main loads nothing of app.suggest", str(facts))
    check(not facts["graph_built"], "the conversation graph is never built", str(facts))


def main() -> int:
    asyncio.run(checks())
    import_check()
    failed = [name for ok, name in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return no_network.exit_code(1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
