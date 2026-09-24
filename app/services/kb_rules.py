"""The pricing and contact rules, read from cb_kb_rules - or from the code.

These used to be five Python constants in four files:

    guards.FEE_STATED_SERVICES          which services may quote their fee
    guards.COST_WITHHELD_SERVICES       which defer the fee to an agent
    ticket.FEE_BY_NATIONALITY           which nationalities we hold a fee for
    info_collector._SALARY_FLOOR_BY_NATIONALITY
    (and the agency's WhatsApp number and office address, which no code read)

They moved to a table so the KB Admin UI can change a price policy without a
deploy. The reasons each value is what it is have NOT moved - they are still
written beside the call sites, and in CLAUDE.md section 5.

HOW IT IS READ, and why it is shaped like this:

* Every caller is a plain synchronous function (`_service_filter`,
  `_effective_options`, ...) on the hot path of a turn. None of them may wait
  on a database. So callers read a SNAPSHOT held in memory, and the snapshot
  is refreshed OFF the hot path: `refresh()` is awaited once at the top of each
  turn (graph.run_turn) and does nothing unless 60 seconds have passed.

* RULES_FROM_DB=false (the default) means the table is never read and every
  getter returns DEFAULTS - today's values, byte for byte. With the switch off
  the bot behaves exactly as it did before this module existed.

* A snapshot from the table is VALIDATED WHOLE before it replaces the old one.
  A read that fails, or returns a set that breaks any rule below, is logged and
  ignored: the bot keeps the last snapshot that was good, and if there has
  never been one, DEFAULTS. It never falls back to "quote everything" or
  "withhold everything" - an empty or half-read table is invalid, not a policy.
  Measured reason (2026-09-08): withholding everything would ALSO switch off
  the retrieval protection that keeps a passport renewal from being quoted the
  work permit's $695, because FEE_STATED_SERVICES decides that filter too. A
  wrong fallback here produces a wrong PRICE, not a vague one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.config import settings

logger = logging.getLogger(__name__)

TABLE = "cb_kb_rules"
TTL_SECONDS = 60.0

NATIONALITIES = frozenset({"PH", "ID", "MM"})

# The nine services a price policy exists for. `insurance` and
# `candidate_new_hiring` are in NEITHER list today and must stay out of both -
# the table's CHECK constraint refuses them too.
PRICED_SERVICES = frozenset({
    "renewal", "passport_renewal", "home_leave", "new_hiring",
    "transfer", "transfer_employer", "direct_hiring", "fee_enquiry",
    "replacement",
})

# Which services are priced per nationality is a DEVELOPER decision, not a UI
# edit: removing one of these would make the bot quote one nationality's price
# for another (the 2026-09-08 Myanmar $450), and adding one needs the fee rows
# to exist first. The UI edits WHICH nationalities, never the set of services.
NATIONALITY_PRICED_SERVICES = frozenset({
    "passport_renewal", "home_leave", "new_hiring", "transfer", "transfer_employer",
})

CONTACT_KEYS = frozenset({"whatsapp_number", "office_address"})


@dataclass(frozen=True)
class Rules:
    fee_stated: frozenset[str]
    cost_withheld: frozenset[str]
    fee_by_nationality: dict[str, frozenset[str]]
    salary_floor: dict[str, int]
    contact: dict[str, str] = field(default_factory=dict)


# Today's values, exactly. scripts/sql/kb_rules_002_seed.sql seeds the table
# with the same, and selfcheck_kb_prep.py fails if the two differ - which is the
# check that has to pass before RULES_FROM_DB may be switched on anywhere.
DEFAULTS = Rules(
    fee_stated=frozenset({
        "renewal", "passport_renewal", "home_leave",
        "new_hiring", "transfer", "transfer_employer",
    }),
    cost_withheld=frozenset({"direct_hiring", "fee_enquiry", "replacement"}),
    fee_by_nationality={
        "passport_renewal": frozenset({"PH", "ID"}),
        "home_leave": frozenset({"PH", "ID"}),
        "new_hiring": frozenset({"PH", "ID", "MM"}),
        "transfer": frozenset({"PH", "ID", "MM"}),
        "transfer_employer": frozenset({"PH", "ID", "MM"}),
    },
    salary_floor={"PH": 650},
    contact={
        "whatsapp_number": "+65 6534 2277",
        "office_address": (
            "101 Upper Cross Street, #03-54, People's Park Centre, Singapore 058357"
        ),
    },
)


class InvalidRules(ValueError):
    """The table's contents do not form a usable, safe rule set."""


def parse_rows(rows: list[dict[str, Any]]) -> Rules:
    """Turn cb_kb_rules rows into Rules, or raise InvalidRules.

    Every rule the table's constraints enforce is checked again here, because
    this is what stands between a bad row and a client: a constraint dropped
    in a future migration must not be the only thing that was protecting them.
    """
    stated: set[str] = set()
    withheld: set[str] = set()
    by_nat: dict[str, frozenset[str]] = {}
    floors: dict[str, int] = {}
    contact: dict[str, str] = {}
    locked_fee_enquiry = False

    for row in rows:
        kind = row.get("rule_type")
        service = row.get("service_type")
        nat = row.get("nationality")
        value = row.get("value")
        if kind == "price_policy":
            if service not in PRICED_SERVICES:
                raise InvalidRules(f"price_policy for unknown service {service!r}")
            if value == "stated":
                stated.add(service)
            elif value == "withheld":
                withheld.add(service)
            else:
                raise InvalidRules(f"price_policy {service}: bad value {value!r}")
            if service == "fee_enquiry":
                locked_fee_enquiry = bool(row.get("locked"))
        elif kind == "price_nationality":
            if not isinstance(value, list) or not set(value) <= NATIONALITIES:
                raise InvalidRules(f"price_nationality {service}: bad value {value!r}")
            by_nat[service] = frozenset(value)
        elif kind == "salary_floor":
            if nat not in NATIONALITIES or isinstance(value, bool) \
                    or not isinstance(value, (int, float)) or not 300 <= value <= 2000:
                raise InvalidRules(f"salary_floor {nat}: bad value {value!r}")
            floors[nat] = int(value)
        elif kind == "contact":
            if service not in CONTACT_KEYS or not isinstance(value, str) or not value.strip():
                raise InvalidRules(f"contact {service}: bad value {value!r}")
            contact[service] = value
        else:
            raise InvalidRules(f"unknown rule_type {kind!r}")

    # An empty or partly-read table is not a policy. All nine must be decided.
    if stated | withheld != PRICED_SERVICES:
        missing = sorted(PRICED_SERVICES - stated - withheld)
        raise InvalidRules(f"price_policy missing for {missing}")
    if stated & withheld:
        raise InvalidRules(f"stated and withheld overlap: {sorted(stated & withheld)}")
    if "fee_enquiry" not in withheld or not locked_fee_enquiry:
        raise InvalidRules("fee_enquiry must be withheld and locked")
    if set(by_nat) != NATIONALITY_PRICED_SERVICES:
        raise InvalidRules(
            f"price_nationality services {sorted(by_nat)} != "
            f"{sorted(NATIONALITY_PRICED_SERVICES)}"
        )
    # One switch. See the constraint trigger in kb_rules_001_create.sql.
    if ("transfer" in stated) != ("transfer_employer" in stated) \
            or by_nat["transfer"] != by_nat["transfer_employer"]:
        raise InvalidRules("transfer and transfer_employer disagree")
    if set(contact) != CONTACT_KEYS:
        raise InvalidRules(f"contact rows {sorted(contact)} != {sorted(CONTACT_KEYS)}")

    return Rules(
        fee_stated=frozenset(stated),
        cost_withheld=frozenset(withheld),
        fee_by_nationality=by_nat,
        salary_floor=floors,
        contact=contact,
    )


# --- The snapshot -----------------------------------------------------------

_from_db: Rules | None = None       # last snapshot that parsed cleanly
_fetched_at: float = 0.0            # monotonic time of the last ATTEMPT
_override: Rules | None = None      # tests only - see use()
_lock = asyncio.Lock()


def enabled() -> bool:
    return bool(settings.rules_from_db)


def rules() -> Rules:
    """The rule set in force right now. Never raises, never does I/O."""
    if _override is not None:
        return _override
    if not enabled():
        return DEFAULTS
    return _from_db or DEFAULTS


def source() -> str:
    """'override', 'defaults' or 'db' - for logs and the preview endpoint."""
    if _override is not None:
        return "override"
    if not enabled() or _from_db is None:
        return "defaults"
    return "db"


async def fetch() -> Rules:
    """Read and parse the table now. Raises on any failure."""
    from app.db.supabase import db  # lazy: guards imports this module

    rows = await db.select_many(
        TABLE, "rule_type,service_type,nationality,value,locked", limit=1000
    )
    return parse_rows(rows)


async def refresh(*, force: bool = False) -> None:
    """Refresh the snapshot if it is stale. Never raises.

    Awaited at the top of every turn. With the switch off it returns at once.
    A failure keeps whatever was in force - the last good snapshot, else
    DEFAULTS - and is retried after the TTL rather than on every message, so a
    table that is down does not add a failed request to every turn.
    """
    global _from_db, _fetched_at
    if not enabled():
        return
    if not force and time.monotonic() - _fetched_at < TTL_SECONDS:
        return
    async with _lock:
        if not force and time.monotonic() - _fetched_at < TTL_SECONDS:
            return
        _fetched_at = time.monotonic()
        try:
            fresh = await fetch()
        except Exception as exc:  # noqa: BLE001 - a rules read must never break a turn
            logger.error(
                "kb_rules: could not use %s (%s: %s) - keeping %s",
                TABLE, type(exc).__name__, exc,
                "the last good snapshot" if _from_db else "the code defaults",
            )
            return
        if fresh != _from_db:
            logger.info("kb_rules: loaded %s (%s)", TABLE, _describe(fresh))
        _from_db = fresh


def _describe(r: Rules) -> str:
    return (
        f"stated={sorted(r.fee_stated)} withheld={sorted(r.cost_withheld)} "
        f"floors={r.salary_floor}"
    )


def reset() -> None:
    """Forget any snapshot (tests)."""
    global _from_db, _fetched_at
    _from_db, _fetched_at = None, 0.0


@contextmanager
def use(r: Rules) -> Iterator[None]:
    """Force a rule set for the duration of a block (tests only)."""
    global _override
    previous, _override = _override, r
    try:
        yield
    finally:
        _override = previous


# --- The getters the bot calls ----------------------------------------------

def fee_stated_services() -> frozenset[str]:
    """Services whose fee the agency gave us, and which therefore quote it.

    Also decides retrieval: `_service_filter` keeps the service filter and
    refuses the widening retry for these (see rag_retriever)."""
    return rules().fee_stated


def cost_withheld_services() -> frozenset[str]:
    """Services whose price is deferred to an agent (quotes_hiring_package_cost)."""
    return rules().cost_withheld


def fee_by_nationality() -> dict[str, frozenset[str]]:
    """Service -> the nationalities we hold a fee for (ticket.fee_is_known_for)."""
    return rules().fee_by_nationality


def salary_floor(nationality_code: str | None) -> int | None:
    """Minimum monthly salary (SGD) for this nationality, or None."""
    return rules().salary_floor.get(nationality_code or "")


def contact(key: str) -> str | None:
    return rules().contact.get(key)
