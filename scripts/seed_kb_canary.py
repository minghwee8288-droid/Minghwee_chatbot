"""Write the SQL that seeds cb_kb_canary - the vector the BOT makes for one
fixed sentence. kb-admin reproduces it before it stores any vector, and
kb_admin_publish refuses every new vector until this row exists.

    APP_ENV_FILE=.env.test python scripts/seed_kb_canary.py \\
        --expect-ref cupwomqvevxravkppuxt --out <scratchpad>/seed_kb_canary_test.sql

then, after reading the file:

    APP_ENV_FILE=.env.test python scripts/apply_sql.py \\
        --expect-ref cupwomqvevxravkppuxt <scratchpad>/seed_kb_canary_test.sql

This script WRITES NOTHING to any database. It:
  1. proves which project the env file names (scripts/target_guard.py);
  2. reads cb_kb_canary in a READ ONLY transaction and refuses if a row exists;
  3. embeds CANARY_SENTENCE with app.services.rag.embed_query - the bot's own
     function, so its own provider, model and dimensions;
  4. writes one .sql file (begin; refuse-if-seeded; the insert; checks; commit).

The model recorded is settings.embedding_model, exactly as the bot sends it
(e.g. "openai/text-embedding-3-small" through OpenRouter). kb-admin embeds
with that same model name, so its KB_ADMIN_EMBEDDING_BASE_URL must be the
endpoint the bot uses. The vector is not secret: it is the embedding of a
fixed, public sentence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.rag import embed_query  # noqa: E402
from scripts.target_guard import require_ref  # noqa: E402

# Fixed forever: changing it means reseeding the canary (a new reviewed change).
CANARY_SENTENCE = (
    "Ming Hwee knowledge base canary: how long does it take to renew a "
    "domestic helper's work permit, and what documents are needed?"
)
TAG = "$canary$"


def already_seeded() -> bool:
    try:
        conn = psycopg.connect(settings.supabase_db_url, autocommit=True)
    except Exception as exc:  # the URL can carry the password: print the type only
        sys.exit(f"[canary] could not connect ({type(exc).__name__})")
    with conn:
        with conn.transaction():
            conn.execute("set transaction read only")
            if conn.execute("select to_regclass('public.cb_kb_canary')").fetchone()[0] is None:
                sys.exit("[canary] cb_kb_canary does not exist - apply kb_admin_006_guards.sql first")
            return conn.execute("select count(*) from public.cb_kb_canary").fetchone()[0] > 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expect-ref", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    if TAG in CANARY_SENTENCE:
        sys.exit("[canary] the sentence contains the dollar-quote tag")
    if args.out.exists():
        sys.exit(f"[canary] refusing to overwrite {args.out}")

    require_ref(args.expect_ref, need_db=True)
    if already_seeded():
        sys.exit("[canary] REFUSING: cb_kb_canary already has a row. Reseeding is a separate, reviewed change.")

    vector = asyncio.run(embed_query(CANARY_SENTENCE))
    if len(vector) != 1536:
        sys.exit(f"[canary] the bot's embedding returned {len(vector)} dimensions, not 1536")
    model = settings.embedding_model.strip()
    if not model or "'" in model:
        sys.exit("[canary] unusable EMBEDDING_MODEL")

    literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
    digest = hashlib.sha256(literal.encode()).hexdigest()[:16]
    endpoint = settings.embedding_base_url or "https://api.openai.com/v1 (direct)"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sql = f"""-- seed cb_kb_canary for project {args.expect_ref}
-- Generated {now} by scripts/seed_kb_canary.py with the bot's own embed_query.
--   model     {model}
--   endpoint  {endpoint}
--   dims      {len(vector)}
--   sha256    {digest} (of the vector literal below)
-- Apply ONLY with:
--   python scripts/apply_sql.py --expect-ref {args.expect_ref} <this file>

begin;

do $$
begin
    if exists (select 1 from public.cb_kb_canary) then
        raise exception 'cb_kb_canary is already seeded; refusing to replace it';
    end if;
end
$$;

insert into public.cb_kb_canary (id, sentence, embedding, model)
values (1, {TAG}{CANARY_SENTENCE}{TAG}, '{literal}'::vector, '{model}');

do $$
declare
    r record;
begin
    select * into r from public.cb_kb_canary where id = 1;
    if r is null or vector_dims(r.embedding) <> 1536 or r.model <> '{model}' then
        raise exception 'cb_kb_canary check failed after the insert';
    end if;
    raise notice 'PASS: cb_kb_canary seeded (model %, % dims)', r.model, vector_dims(r.embedding);
end
$$;

commit;
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(sql, encoding="utf-8", newline="\n")
    print(f"[canary] wrote {args.out} (model {model}, {len(vector)} dims, sha256 {digest}). Nothing was applied.")


if __name__ == "__main__":
    main()
