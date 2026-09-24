"""Which Supabase project is this run about to write to? Refuse the wrong one.

Every script that writes calls `require_ref(expected)` first. It prints the ref
it is connected to and exits unless it matches - read from BOTH the REST URL
and the Postgres connection string, because a script that writes SQL uses the
second and one that writes rows uses the first, and a half-edited env file can
point them at different projects.

The ref is in the URL either way:
    https://<ref>.supabase.co                                  (REST)
    postgresql://postgres.<ref>:...@...pooler.supabase.com     (session pooler)
    postgresql://postgres:...@db.<ref>.supabase.co:5432/...    (direct)
"""

from __future__ import annotations

import re
import sys
from urllib.parse import urlsplit

from app.config import settings

PRODUCTION_REF = "qizcnyuzgylzoyfvymfo"

_REST_REF = re.compile(r"^https?://([a-z0-9]{20})\.supabase\.co", re.IGNORECASE)


def rest_ref() -> str | None:
    match = _REST_REF.match(settings.supabase_url or "")
    return match.group(1).lower() if match else None


def db_ref() -> str | None:
    url = settings.supabase_db_url or ""
    if not url:
        return None
    parts = urlsplit(url)
    user, host = (parts.username or ""), (parts.hostname or "")
    if user.startswith("postgres.") and len(user) > len("postgres."):
        return user.split(".", 1)[1].lower()
    match = re.match(r"^db\.([a-z0-9]{20})\.supabase\.co$", host, re.IGNORECASE)
    return match.group(1).lower() if match else None


def require_ref(expected: str, *, need_db: bool = False) -> None:
    """Print the connected ref(s); exit(2) unless they are `expected`."""
    expected = (expected or "").strip().lower()
    rest, db = rest_ref(), db_ref()
    print(f"[target] SUPABASE_URL ref = {rest}   SUPABASE_DB_URL ref = {db}   "
          f"expected = {expected}", flush=True)
    problems = []
    if not expected:
        problems.append("no expected ref given")
    if rest != expected:
        problems.append(f"REST ref {rest!r} != expected {expected!r}")
    if need_db and db != expected:
        problems.append(f"DB ref {db!r} != expected {expected!r}")
    if db is not None and db != rest:
        problems.append(f"REST ref {rest!r} and DB ref {db!r} disagree")
    if expected != PRODUCTION_REF and PRODUCTION_REF in {rest, db}:
        problems.append("connected to PRODUCTION while a non-production ref was expected")
    if problems:
        print("[target] REFUSING TO WRITE: " + "; ".join(problems), flush=True)
        sys.exit(2)
    print(f"[target] OK - writing to {expected}", flush=True)
