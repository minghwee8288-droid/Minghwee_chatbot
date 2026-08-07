"""Check everything the chatbot depends on at runtime before going live.

    python scripts/preflight.py

Reports on the knowledge base, style config, tenant, agent rotation, the admin
escalation target and the portal user bridge. Read-only — it changes nothing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.services import assignment  # noqa: E402
from app.services import lead as lead_service  # noqa: E402

OK = "PASS"
WARN = "WARN"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []


def record(level: str, check: str, detail: str) -> None:
    results.append((level, check, detail))
    print(f"  [{level}] {check}: {detail}")


async def safe(coro, default=None):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        return exc if default is None else default


async def check_tenant() -> str | None:
    tenants = await safe(db.select_many("tenants", "*", limit=10))
    if isinstance(tenants, Exception):
        record(FAIL, "tenants", f"could not read: {tenants}")
        return None
    if not tenants:
        record(FAIL, "tenants", "no rows — TENANT_ID cannot be resolved")
        return None

    names = {
        str(t["id"]): (t.get("display_name") or t.get("name") or t.get("slug") or "?")
        for t in tenants
    }
    if settings.tenant_id:
        if settings.tenant_id in names:
            record(OK, "TENANT_ID", f"{settings.tenant_id} ({names[settings.tenant_id]})")
            return settings.tenant_id
        record(FAIL, "TENANT_ID", f"{settings.tenant_id} is not in tenants: {names}")
        return None

    if len(tenants) == 1:
        only = str(tenants[0]["id"])
        record(WARN, "TENANT_ID", f"not set — the only tenant is {only} ({names[only]})")
        return only
    record(FAIL, "TENANT_ID", f"not set and {len(tenants)} tenants exist: {names}")
    return None


async def check_knowledge_base() -> None:
    rows = await safe(db.select_many("cb_knowledge_base", "namespace, is_active", limit=5000))
    if isinstance(rows, Exception):
        record(FAIL, "cb_knowledge_base", f"could not read: {rows}")
        return
    active = [r for r in rows if r.get("is_active")]
    namespaces: dict[str, int] = {}
    for row in active:
        key = row.get("namespace") or "(null)"
        namespaces[key] = namespaces.get(key, 0) + 1
    if not active:
        record(FAIL, "cb_knowledge_base", "no active rows")
        return
    record(OK, "cb_knowledge_base", f"{len(active)} active rows {namespaces}")

    if settings.rag_namespace and settings.rag_namespace not in namespaces:
        record(
            FAIL,
            "RAG_NAMESPACE",
            f"'{settings.rag_namespace}' has no rows — searches will return nothing",
        )


def check_transcription() -> None:
    """Voice notes only reach the bot as text if transcription is configured."""
    if not settings.transcription_enabled:
        record(WARN, "transcription", "disabled — voice notes reach the bot as '[voice]'")
        return
    if not settings.resolved_transcription_key:
        record(
            FAIL,
            "transcription",
            "enabled but TRANSCRIPTION_API_KEY/OPENROUTER_API_KEY is empty — voice "
            "notes will not be transcribed",
        )
        return
    record(
        OK,
        "transcription",
        f"{settings.transcription_model} via {settings.transcription_base_url}",
    )


async def check_leads() -> None:
    """Leads are only creatable if a branch resolves — branch_id is NOT NULL."""
    if not settings.lead_enabled:
        record(WARN, "leads", "disabled — new contacts produce a ticket but no lead")
        return
    for table in (lead_service.EMPLOYER_TABLE, lead_service.CANDIDATE_TABLE):
        rows = await safe(db.select_many(table, "id", limit=1))
        if isinstance(rows, Exception):
            record(FAIL, table, f"unreachable: {str(rows)[:120]}")
            return
    branch = await safe(lead_service.resolve_branch_id())
    if isinstance(branch, Exception) or not branch:
        record(
            FAIL,
            "leads",
            "no branch resolved — every lead insert will fail (branch_id is NOT NULL). "
            "Set LEAD_BRANCH_ID or add a branch for this tenant.",
        )
        return
    number = await safe(lead_service.next_lead_number(lead_service.EMPLOYER_TABLE))
    record(OK, "leads", f"branch {branch}, next lead number {number}")


async def check_agents(tenant_id: str | None) -> None:
    rotation = await safe(db.select_many("cb_round_robin_state", "*", limit=100))
    if isinstance(rotation, Exception):
        record(FAIL, "cb_round_robin_state", f"could not read: {rotation}")
    else:
        active = [r for r in rotation if r.get("is_active")]
        if not active:
            record(FAIL, "cb_round_robin_state", "no active agents — round robin cannot assign")
        else:
            record(OK, "cb_round_robin_state", f"{len(active)} active agent(s) in rotation")

    if tenant_id:
        admins = await safe(
            db.select_many("profiles", "*", limit=10, tenant_id=tenant_id,
                           archetype_key="admin", status="active")
        )
        if isinstance(admins, Exception):
            record(FAIL, "admin escalation target", f"could not read profiles: {admins}")
        elif not admins:
            record(FAIL, "admin escalation target", "no active admin profile — assault escalation has nowhere to go")
        else:
            # Name whoever would actually be paged. "2 active admins" told you
            # nothing about which one, and an unordered LIMIT 1 could pick
            # either — a safety escalation must not be a coin flip.
            target = await safe(assignment.resolve_agent(intent="dispute_assault"))
            chosen = None if isinstance(target, Exception) else target[0]
            named = next((a for a in admins if a["id"] == chosen), None)
            label = (named or {}).get("display_name") or chosen or "unknown"
            pinned = bool(settings.escalation_profile_id)
            level = OK if (pinned or len(admins) == 1) else WARN
            record(
                level,
                "admin escalation target",
                f"assault and partner enquiries go to {label}"
                + (" (pinned via ESCALATION_PROFILE_ID)" if pinned else
                   f" — {len(admins)} admins exist and none is pinned, so this "
                   "can change; set ESCALATION_PROFILE_ID"),
            )

        next_agent = await safe(db.rpc("cb_get_next_agent", {"p_tenant_id": tenant_id}))
        if isinstance(next_agent, Exception):
            record(FAIL, "cb_get_next_agent()", f"call failed: {next_agent}")
        elif not next_agent:
            record(FAIL, "cb_get_next_agent()", "returned nothing — no agent would be assigned")
        else:
            record(OK, "cb_get_next_agent()", f"returned {next_agent}")


async def check_portal_bridge() -> None:
    users = await safe(db.select_many("wp_chat_users", "id, name, profile_id, is_active", limit=200))
    if isinstance(users, Exception):
        record(FAIL, "wp_chat_users", f"could not read: {users}")
        return
    if not users:
        record(FAIL, "wp_chat_users", "no rows — the portal cannot show assignments")
        return
    bridged = [u for u in users if u.get("profile_id")]
    if not bridged:
        record(FAIL, "wp_chat_users.profile_id", f"0 of {len(users)} rows bridged to profiles")
    elif len(bridged) < len(users):
        unbridged = [str(u.get("name")) for u in users if not u.get("profile_id")]
        record(WARN, "wp_chat_users.profile_id", f"{len(bridged)}/{len(users)} bridged; missing: {', '.join(unbridged)}")
    else:
        record(OK, "wp_chat_users.profile_id", f"all {len(users)} rows bridged")


async def check_writable_tables() -> None:
    for table in ("wp_chat_conversations", "wp_chat_messages", "cb_tickets", "cb_handovers"):
        rows = await safe(db.select_many(table, "*", limit=1))
        if isinstance(rows, Exception):
            record(FAIL, table, f"could not read: {rows}")
        else:
            record(OK, table, f"reachable ({len(rows)} row sampled)")


async def check_checkpointer_dsn() -> None:
    """Actually connect with the checkpointer's DSN — a wrong password or the
    wrong pooler port only shows up at runtime otherwise."""
    if not settings.supabase_db_url:
        record(WARN, "SUPABASE_DB_URL", "missing — LangGraph falls back to an in-memory checkpointer")
        return
    try:
        import psycopg

        conn = await asyncio.to_thread(psycopg.connect, settings.supabase_db_url, connect_timeout=15)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user")
                database, user = cur.fetchone()
        finally:
            conn.close()
        record(OK, "SUPABASE_DB_URL", f"connected as {user} to {database}")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "SUPABASE_DB_URL", f"set but cannot connect: {str(exc).strip()[:200]}")


async def check_env() -> None:
    await check_checkpointer_dsn()
    for name, value, required in (
        ("WHAPI_API_TOKEN", settings.whapi_api_token, True),
        ("WHAPI_WEBHOOK_SECRET", settings.whapi_webhook_secret, True),
        ("WHAPI_SENDER_PHONE", settings.whapi_sender_phone, True),
        ("OPENROUTER_API_KEY", settings.openrouter_api_key, True),
    ):
        if value:
            record(OK, name, "set")
        elif required:
            record(FAIL, name, "missing")
        else:
            record(WARN, name, "missing — LangGraph falls back to an in-memory checkpointer")


async def main() -> None:
    print("=" * 78)
    print("Ming Hwee chatbot preflight")
    print("=" * 78)

    print("\nEnvironment")
    await check_env()

    print("\nKnowledge base")
    await check_knowledge_base()
    check_transcription()
    await check_leads()

    print("\nTenant and agents")
    tenant_id = await check_tenant()
    await check_agents(tenant_id)
    await check_portal_bridge()

    print("\nRuntime tables")
    await check_writable_tables()

    failures = [r for r in results if r[0] == FAIL]
    warnings = [r for r in results if r[0] == WARN]
    print("\n" + "=" * 78)
    print(f"{len(results) - len(failures) - len(warnings)} passed, {len(warnings)} warnings, {len(failures)} failures")
    for _, check, detail in failures:
        print(f"  BLOCKER  {check}: {detail}")


if __name__ == "__main__":
    asyncio.run(main())
