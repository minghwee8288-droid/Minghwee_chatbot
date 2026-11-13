"""Replay scripts/help_golden.json against a running Help service. Prints only.

    HELP_SECRET=<the value the server was started with> \
    python scripts/help_replay.py [--url http://127.0.0.1:8001] [--show id,id,...] [--only id,id,...]

Writes nothing to any database. It reads the source rows it is given back
(read-only, inside app.readonly.read_only()) to check, independently of the
service, that every figure >= 100 in an "ok" answer is in those rows.
The secret is read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import readonly  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.graph import guards  # noqa: E402

GOLDEN = Path(__file__).with_name("help_golden.json")


def digits(figure: str) -> str:
    return figure.replace(",", "")


def has_figure(text: str, figure: str) -> bool:
    return digits(figure) in {d.replace(",", "") for d in re.findall(r"\d[\d,]*", text or "")}


async def source_texts(ids: list[str]) -> str:
    if not ids:
        return ""
    with readonly.read_only() as refused:
        query = db.table("cb_knowledge_base_updated").select("question,answer,content").in_("id", ids)
        rows = (await db.execute(query)).data or []
    assert not refused
    return " ".join(" ".join(str(r.get(k) or "") for k in ("question", "answer", "content")) for r in rows)


async def ask(client: httpx.AsyncClient, url: str, secret: str, messages: list[dict]) -> tuple[int, dict, float]:
    started = time.monotonic()
    resp = await client.post(
        f"{url}/internal/help/answer",
        headers={"X-Help-Secret": secret},
        json={"request_id": str(uuid.uuid4()), "user_role": "replay", "messages": messages},
        timeout=60,
    )
    elapsed = (time.monotonic() - started) * 1000
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body, elapsed


def grade(item: dict, status_code: int, body: dict) -> tuple[str, str]:
    """('yes'|'no'|'hedge', reason)."""
    if status_code != 200:
        return "no", f"HTTP {status_code} {body.get('error', {}).get('code', '')}"
    status, answer = body.get("status"), body.get("answer") or ""
    for figure in item.get("forbid_figures", []):
        if has_figure(answer, figure):
            return "no", f"forbidden figure {figure}"
    if item["expected"] == "not_found":
        if status == "not_found":
            return "yes", ""
        big = [f for f in re.findall(r"\d[\d,]*", answer) if len(digits(f)) >= 3]
        return ("hedge", "answered without a figure") if status == "ok" and not big else ("no", f"status {status}")
    if status != "ok":
        return "no", f"status {status}"
    for figure in item.get("expect_figures", []):
        if not has_figure(answer, figure):
            return "no", f"missing {figure}"
    for word in item.get("expect_words", []):
        if not any(w.lower() in answer.lower() for w in word.split("|")):
            return "no", f"missing '{word}'"
    return "yes", ""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--show", default="", help="comma-separated ids whose full answers to print")
    parser.add_argument("--only", default="", help="comma-separated ids to replay (default: all)")
    args = parser.parse_args()
    secret = os.environ.get("HELP_SECRET", "")
    if not secret:
        print("HELP_SECRET is not set in this shell")
        return 2
    show = {s for s in args.show.split(",") if s}
    items = json.loads(GOLDEN.read_text(encoding="utf-8"))["items"]
    only = {s for s in args.only.split(",") if s}
    if only:
        items = [i for i in items if i["id"] in only]

    rows_out = []
    latencies: list[float] = []
    flags: Counter = Counter()
    statuses: Counter = Counter()
    invented: list[str] = []
    violations = 0
    samples: list[tuple[str, str, dict]] = []

    async with httpx.AsyncClient() as client:
        for item in items:
            turns = item.get("pair") or [item["question"]]
            history: list[dict] = []
            for turn_index, question in enumerate(turns):
                history.append({"role": "user", "content": question})
                code, body, ms = await ask(client, args.url, secret, history)
                latencies.append(ms)
                if code == 500 and body.get("error", {}).get("code") == "read_only_violation":
                    violations += 1
                answer = body.get("answer") or ""
                history.append({"role": "assistant", "content": answer or "(no answer)"})
                last = turn_index == len(turns) - 1
                status = body.get("status") or f"http{code}"
                statuses[status] += 1
                for flag in body.get("guard_flags") or []:
                    flags[(flag, status)] += 1
                if status == "ok":
                    texts = await source_texts([s["kb_id"] for s in body.get("sources") or []])
                    bad = guards.ungrounded_figures(answer, texts)
                    if bad:
                        invented.append(f"{item['id']}#{turn_index + 1}: {bad}")
                if item["id"] in show:
                    samples.append((item["id"], question, body))
                if last:
                    correct, why = grade(item, code, body)
                    top = (body.get("retrieval") or {}).get("top_score")
                    rows_out.append((
                        item["id"] + (" (turn 2)" if item.get("pair") else ""), status,
                        f"{top:.3f}" if isinstance(top, float) else "-",
                        ",".join(body.get("guard_flags") or []) or "-", correct, why, f"{ms:.0f}",
                    ))

    print(f"{'id':38} {'status':10} {'top':6} {'flags':28} {'ok?':6} {'ms':>6}  why")
    for r in rows_out:
        print(f"{r[0]:38} {r[1]:10} {r[2]:6} {r[3]:28} {r[4]:6} {r[6]:>6}  {r[5]}")

    graded = Counter(r[4] for r in rows_out)
    lat = sorted(latencies)
    p90 = lat[max(0, int(round(0.9 * len(lat))) - 1)]
    print(f"\nitems: {len(rows_out)}  requests: {len(latencies)}")
    print(f"correct: {graded['yes']}/{len(rows_out)} ({100 * graded['yes'] / len(rows_out):.0f}%), "
          f"hedge (not_found expected, answered with no figure): {graded['hedge']}, wrong: {graded['no']}")
    print(f"statuses over all requests: {dict(statuses)}")
    print(f"flags (flag, status): {dict(flags)}")
    print(f"invented figures (answer figure >= 100 not in its own sources): {len(invented)} {invented}")
    print(f"latency ms over all requests: p50 {statistics.median(lat):.0f}  p90 {p90:.0f}")
    print(f"read_only_violation responses: {violations}")

    for item_id, question, body in samples:
        print(f"\n--- {item_id}\nQ: {question}\nstatus: {body.get('status')}  flags: {body.get('guard_flags')}")
        print(f"A: {body.get('answer')}")
        for s in body.get("sources") or []:
            print(f"   [{s['n']}] {s['source_document']} — {s['section_heading']} "
                  f"({s['service_type']}/{s['nationality']}, {s['similarity']})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
