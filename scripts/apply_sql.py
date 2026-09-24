"""Run a .sql migration file against the configured database - after proving
which database that is.

    APP_ENV_FILE=.env.test python scripts/apply_sql.py \\
        --expect-ref cupwomqvevxravkppuxt scripts/sql/kb_rules_001_create.sql

Refuses unless SUPABASE_URL and SUPABASE_DB_URL both name --expect-ref
(scripts/target_guard.py). The file runs as written, in one session; the files
under scripts/sql/ carry their own begin/commit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.config import settings  # noqa: E402
from scripts.target_guard import require_ref  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expect-ref", required=True)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    for path in args.files:
        if not path.is_file():
            sys.exit(f"no such file: {path}")

    require_ref(args.expect_ref, need_db=True)
    with psycopg.connect(settings.supabase_db_url, autocommit=True) as conn:
        for path in args.files:
            print(f"[apply_sql] running {path}", flush=True)
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"[apply_sql] done    {path}", flush=True)


if __name__ == "__main__":
    main()
