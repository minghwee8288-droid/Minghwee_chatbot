"""Apply scripts/ticket_multiservice.sql to SUPABASE_DB_URL.

    .venv/Scripts/python.exe scripts/apply_ticket_multiservice.py

The SQL is idempotent, so re-running is harmless. Synchronous psycopg on
purpose — async psycopg cannot run on Windows' default event loop (see run.py)
and a one-shot DDL script has no reason to care.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.config import settings  # noqa: E402

SQL_PATH = Path(__file__).with_name("ticket_multiservice.sql")


def main() -> None:
    if not settings.supabase_db_url:
        sys.exit("SUPABASE_DB_URL is not set")

    sql = SQL_PATH.read_text(encoding="utf-8")
    with psycopg.connect(settings.supabase_db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                """
                select data_type,
                       (select column_name from information_schema.columns
                         where table_schema='public' and table_name='cb_tickets'
                           and column_name='description')
                  from information_schema.columns
                 where table_schema='public' and table_name='cb_tickets'
                   and column_name='service_type'
                """
            )
            service_type, description = cur.fetchone()
            print(f"service_type -> {service_type}")
            print(f"description   -> {description or 'MISSING'}")

            cur.execute("select ticket_number, service_type from cb_tickets order by created_at")
            for row in cur.fetchall():
                print("  ", row)


if __name__ == "__main__":
    main()
