"""Self-checks for the KB Admin UI preparation (2026-09-24).

    python scripts/selfcheck_kb_prep.py --offline
        No database, no network. Safe anywhere, including CI.

    APP_ENV_FILE=.env.test python scripts/selfcheck_kb_prep.py \\
        --expect-ref cupwomqvevxravkppuxt [--preview]
        Everything above, then READ-ONLY checks against that database - and
        refuses to run against any other project. --preview also calls the
        real /admin/preview handler (real model calls) and proves it wrote
        nothing, by counting rows before and after.

What it proves, and why each one matters:

PREP 1 - the rules table
  * the seed file, kb_rules.DEFAULTS and (live) the table are IDENTICAL. This
    is the drift check that must pass before RULES_FROM_DB goes on anywhere:
    with it passing, switching on changes nothing.
  * a read failure, an empty table and an invalid table all fall back - to the
    last good snapshot, else the defaults - and never to "quote everything"
    or "withhold everything".
  * with the switch off, the table is never read at all.

PREP 2 - ownership
  * every one of the loader's four write paths skips a row owned by the UI,
    proved by RUNNING the loader against a stub database holding one, not by
    reading its source.
  * (live) every knowledge-base row carries metadata.managed_by.

PREP 3 - preview
  * the read-only RPC list names the knowledge-base search exactly.
  * (live, --preview) a preview writes no row anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import readonly  # noqa: E402
from app.services import kb_rules  # noqa: E402
from scripts.kb_seed import seed_rows  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""),
          flush=True)


# --- PREP 1, offline -------------------------------------------------------

async def rules_offline() -> None:
    seed = seed_rows()
    check("the seed file parses to exactly kb_rules.DEFAULTS",
          kb_rules.parse_rows(seed) == kb_rules.DEFAULTS)

    def mutate(match, **new):
        return [
            {**r, **new} if all(r.get(k) == v for k, v in match.items()) else r
            for r in seed
        ]

    invalid = {
        "an empty table": [],
        "fee_enquiry set to stated":
            mutate({"rule_type": "price_policy", "service_type": "fee_enquiry"},
                   value="stated"),
        "fee_enquiry unlocked":
            mutate({"rule_type": "price_policy", "service_type": "fee_enquiry"},
                   locked=False),
        "transfer and transfer_employer split":
            mutate({"rule_type": "price_policy", "service_type": "transfer"},
                   value="withheld"),
        "a nationality-priced service dropped":
            [r for r in seed if not (r["rule_type"] == "price_nationality"
                                     and r["service_type"] == "home_leave")],
        "an unknown nationality":
            mutate({"rule_type": "price_nationality", "service_type": "home_leave"},
                   value=["PH", "XX"]),
        "a salary floor of 6500":
            mutate({"rule_type": "salary_floor"}, value=6500),
        "insurance given a price policy":
            seed + [{"rule_type": "price_policy", "service_type": "insurance",
                     "nationality": "all", "value": "stated", "locked": False}],
    }
    for name, rows in invalid.items():
        try:
            kb_rules.parse_rows(rows)
            check(f"an invalid table is refused: {name}", False, "ACCEPTED")
        except kb_rules.InvalidRules as exc:
            check(f"an invalid table is refused: {name}", True, str(exc)[:70])

    # The fallback chain, run through refresh() with the database stubbed.
    good = kb_rules.parse_rows(seed)
    changed = kb_rules.Rules(
        fee_stated=good.fee_stated - {"renewal"},
        cost_withheld=good.cost_withheld | {"renewal"},
        fee_by_nationality=good.fee_by_nationality,
        salary_floor=good.salary_floor,
        contact=good.contact,
    )
    s = kb_rules.settings
    old_flag = s.rules_from_db
    try:
        s.rules_from_db = False
        kb_rules.reset()
        boom = AsyncMock(side_effect=AssertionError("read while switched off"))
        with patch.object(kb_rules, "fetch", boom):
            await kb_rules.refresh(force=True)
        check("with the switch OFF the table is never read", boom.await_count == 0)
        check("...and the defaults are in force", kb_rules.rules() is kb_rules.DEFAULTS
              and kb_rules.source() == "defaults")

        s.rules_from_db = True
        kb_rules.reset()
        with patch.object(kb_rules, "fetch", AsyncMock(side_effect=ConnectionError("down"))):
            await kb_rules.refresh(force=True)
        check("switch ON, table unreachable, never loaded -> the code defaults",
              kb_rules.rules() == kb_rules.DEFAULTS and kb_rules.source() == "defaults")

        with patch.object(kb_rules, "fetch", AsyncMock(return_value=changed)):
            await kb_rules.refresh(force=True)
        check("switch ON, table readable -> the table's values",
              kb_rules.rules() == changed and kb_rules.source() == "db")

        with patch.object(kb_rules, "fetch", AsyncMock(side_effect=ConnectionError("down"))):
            await kb_rules.refresh(force=True)
        check("table goes down after a good read -> the LAST GOOD values, not the defaults",
              kb_rules.rules() == changed)

        with patch.object(kb_rules, "fetch",
                          AsyncMock(side_effect=kb_rules.InvalidRules("half a table"))):
            await kb_rules.refresh(force=True)
        check("table turns invalid -> still the last good values",
              kb_rules.rules() == changed)

        r = kb_rules.rules()
        check("no fallback ever quotes everything or withholds everything",
              r.fee_stated and r.cost_withheld and "fee_enquiry" in r.cost_withheld)

        calls = AsyncMock(return_value=changed)
        with patch.object(kb_rules, "fetch", calls):
            await kb_rules.refresh()
            await kb_rules.refresh()
        check("a fresh snapshot is not re-read inside the 60s window",
              calls.await_count == 0)
    finally:
        s.rules_from_db = old_flag
        kb_rules.reset()


# --- PREP 2, offline: the loader against a stub database --------------------

class _StubDB:
    """Just enough of app.db.supabase.db for load_service_notes.main()."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.writes: list[tuple[str, Any]] = []

    @staticmethod
    def _match(row, filters):
        return all(row.get(k) == v for k, v in filters.items())

    @staticmethod
    def _project(row, columns):
        # Only what was asked for, as PostgREST does - or a caller reading a
        # column it never selected (the 2026-09-22 UPDATES defect) passes here
        # and fails against the real table.
        if columns.strip() == "*":
            return dict(row)
        return {c.strip(): row.get(c.strip()) for c in columns.split(",")}

    async def select_one(self, table, columns="*", **filters):
        return next((self._project(r, columns) for r in self.rows
                     if self._match(r, filters)), None)

    async def select_many(self, table, columns="*", limit=100, **filters):
        return [self._project(r, columns) for r in self.rows
                if self._match(r, filters)][:limit]

    async def insert(self, table, payload):
        self.writes.append(("insert", payload.get("question")))
        return payload

    async def update(self, table, payload, **filters):
        self.writes.append(("update", filters.get("id")))
        return [payload]


async def loader_offline() -> None:
    import scripts.load_service_notes as loader

    def row(rid, owner, **kw):
        base = {"id": rid, "question": None, "answer": None, "content": "",
                "service_type": "general", "nationality": "all",
                "is_active": True, "namespace": "ns", "embedding": None,
                "metadata": {"managed_by": owner}}
        return {**base, **kw}

    rows = [
        # one UI-owned row in the way of EACH write path, and one loader row
        # beside it that must still be written - or a loader that skips
        # everything would pass too.
        row("ui-insert", "ui", question="Q insert", service_type="general"),
        row("ui-update", "ui", question="Q update", answer="old"),
        row("lo-update", "loader", question="Q update 2", answer="old"),
        row("ui-replace", "ui", content="call OLDNUM now"),
        row("lo-replace", "loader", content="call OLDNUM today"),
        row("ui-retire", "ui", content="NEEDLE fee table"),
        row("lo-retire", "loader", content="NEEDLE fee table again"),
        # already carries what its UPDATES entry sets, on a column other than
        # `answer` - the shape of the 2026-09-22 transfer-cost entry
        row("lo-correct", "loader", question="Q correct", answer="same",
            contact_type="employer", section_heading="H"),
    ]
    stub = _StubDB(rows)
    fakes = dict(
        ROWS=[
            {"service_type": "general", "nationality": "all", "section_heading": "h",
             "question": "Q insert", "answer": "a"},
            {"service_type": "general", "nationality": "all", "section_heading": "h",
             "question": "Q brand new", "answer": "a"},
        ],
        UPDATES=[
            {"where": {"question": "Q update"}, "set": {"answer": "new"}, "reason": "t"},
            {"where": {"question": "Q update 2"}, "set": {"answer": "new"}, "reason": "t"},
            {"where": {"question": "Q correct"},
             "set": {"answer": "same", "contact_type": "employer", "section_heading": "H"},
             "reason": "t"},
        ],
        TEXT_REPLACEMENTS=[{"old": "OLDNUM", "new": "NEWNUM", "reason": "t"}],
        RETIRED=[{"needle": "NEEDLE", "reason": "t"}],
        _RELOCATED={},
    )
    with patch.object(loader, "db", stub), \
         patch.object(loader, "embed_query", AsyncMock(return_value=[0.0])), \
         patch.multiple(loader, **fakes):
        loader._ui_skipped.clear()
        await loader.main(dry_run=False)

    touched = {target for _, target in stub.writes}
    for rid, path in (("ui-update", "UPDATES"), ("ui-replace", "TEXT_REPLACEMENTS"),
                      ("ui-retire", "RETIRED")):
        check(f"the loader never touches a UI row through {path}", rid not in touched)
    check("the loader never inserts over a UI row (ROWS)",
          ("insert", "Q insert") not in stub.writes)
    check("...while every LOADER row beside them is still written",
          {"lo-update", "lo-replace", "lo-retire"} <= touched
          and ("insert", "Q brand new") in stub.writes, f"writes={stub.writes}")
    check("...and each UI row is reported, once per path",
          sorted(loader._ui_skipped) == ["ui-insert", "ui-replace", "ui-retire", "ui-update"],
          f"reported={sorted(loader._ui_skipped)}")
    check("an UPDATES entry that is already applied is not re-applied, on ANY "
          "column it sets (the loader stays idempotent)",
          "lo-correct" not in touched)
    src = Path(loader.__file__).read_text(encoding="utf-8")
    check("a row the loader inserts is tagged managed_by=loader",
          '"metadata": {OWNER_KEY: LOADER_OWNER}' in src)


def preview_offline() -> None:
    from app.services import rag
    check("the read-only RPC list is exactly the knowledge-base search",
          readonly.READ_RPCS == {rag.KB_MATCH_FUNCTION})


# --- live, read-only --------------------------------------------------------

async def _count(table: str) -> int | None:
    from app.db.supabase import db
    try:
        res = await db.execute(db.table(table).select("id", count="exact").limit(1))
        return res.count
    except Exception as exc:  # noqa: BLE001
        print(f"      (could not count {table}: {type(exc).__name__})")
        return None


def _pg_counts(tables: list[str]) -> dict[str, int]:
    from app.config import settings
    if not settings.supabase_db_url:
        return {}
    import psycopg
    out = {}
    with psycopg.connect(settings.supabase_db_url) as conn:
        for t in tables:
            try:
                out[t] = conn.execute(f"select count(*) from public.{t}").fetchone()[0]
            except Exception:  # noqa: BLE001 - table may not exist on this project
                conn.rollback()
    return out


async def live(preview: bool) -> None:
    from app.db.supabase import db
    from app.services.rag import KB_TABLE

    fetched = await kb_rules.fetch()
    check("DRIFT: cb_kb_rules == kb_rules.DEFAULTS (safe to switch on)",
          fetched == kb_rules.DEFAULTS,
          "" if fetched == kb_rules.DEFAULTS else f"table={fetched}")

    rows = await db.select_many(KB_TABLE, "id,metadata,is_active,content,answer", limit=10000)
    owners: dict[str, int] = {}
    for r in rows:
        owner = (r.get("metadata") or {}).get("managed_by") if isinstance(
            r.get("metadata"), dict) else None
        owners[str(owner)] = owners.get(str(owner), 0) + 1
    check("every knowledge-base row carries metadata.managed_by",
          set(owners) <= {"loader", "ui"}, f"{len(rows)} rows: {owners}")

    active_text = " ".join((r.get("content") or "") + " " + (r.get("answer") or "")
                           for r in rows if r.get("is_active"))
    for key in sorted(kb_rules.CONTACT_KEYS):
        value = fetched.contact.get(key, "")
        check(f"the {key} rule matches what the knowledge base says",
              bool(value) and value.replace("+65 ", "") in active_text, value)

    if not preview:
        return

    from fastapi import HTTPException

    from app.api.admin import PreviewRequest
    from app.api.admin import preview as preview_handler
    from app.config import settings

    rest_tables = ["cb_tickets", "cb_handovers", "leads", "leads_candidate",
                   "wp_chat_conversations", "wp_chat_messages", KB_TABLE, "cb_kb_rules"]
    pg_tables = ["cb_checkpoints", "cb_checkpoint_writes", "cb_checkpoint_blobs",
                 "cb_round_robin_state"]
    before = {t: await _count(t) for t in rest_tables} | _pg_counts(pg_tables)

    if not settings.admin_preview_secret:
        check("preview: ADMIN_PREVIEW_SECRET is set for this run", False)
        return
    bodies = [
        PreviewRequest(messages=["how much is a passport renewal for a filipino helper?"]),
        # opens a hiring intake: the collector tries to open a lead early
        PreviewRequest(messages=["hi i want to hire a helper", "Vaidik Dubey"],
                       contact_type="employer"),
    ]
    refused_any: list[dict[str, str]] = []
    for body in bodies:
        try:
            out = await preview_handler(body, settings.admin_preview_secret)
        except HTTPException as exc:
            check(f"preview answered: {body.messages[0]!r}", False, str(exc.detail))
            continue
        replies = [t["reply"] for t in out["turns"]]
        check(f"preview answered: {body.messages[0]!r}", all(replies),
              f"{replies[-1][:60]!r} refused={len(out['refused_writes'])}")
        refused_any += out["refused_writes"]

    after = {t: await _count(t) for t in rest_tables} | _pg_counts(pg_tables)
    moved = {t: (before[t], after.get(t)) for t in before if before[t] != after.get(t)}
    check("preview wrote NOTHING: every row count is unchanged",
          not moved, f"{len(before)} tables counted" if not moved else f"changed={moved}")
    check("...and the guard did intercept the writes the graph attempted",
          bool(refused_any), f"{refused_any[:3]}")


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--expect-ref")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()

    print("PREP 1 - rules (offline)")
    await rules_offline()
    print("PREP 2 - loader ownership (offline, stub database)")
    await loader_offline()
    print("PREP 3 - preview (offline)")
    preview_offline()

    if not args.offline:
        if not args.expect_ref:
            parser.error("--expect-ref is required for the live checks (or pass --offline)")
        from scripts.target_guard import require_ref
        require_ref(args.expect_ref)
        print("LIVE - read-only checks against the database")
        await live(args.preview)

    failed = [label for label, ok, _ in results if not ok]
    print("\nALL PASS" if not failed else f"\n{len(failed)} FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
