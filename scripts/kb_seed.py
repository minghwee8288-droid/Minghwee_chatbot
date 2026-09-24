"""The rows scripts/sql/kb_rules_002_seed.sql inserts, parsed without a database.

So the offline checks (smoke_nodes.py, selfcheck_kb_prep.py --offline) can
prove the SEED FILE equals kb_rules.DEFAULTS - the file that goes to
production - rather than only the database it was once applied to.

Deliberately narrow: it reads exactly the VALUES tuples that file is written
in, and fails loudly (a row count that is not 17) if the file's shape changes,
rather than returning a partial rule set that would fail parse_rows for a
reason nobody could see.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SEED_FILE = Path(__file__).resolve().parent / "sql" / "kb_rules_002_seed.sql"
EXPECTED_ROWS = 17

_TUPLE = re.compile(
    r"\('(\w+)',\s*'(\w+)',\s*'(\w+)',\s*"
    r"'((?:[^']|'')*)',\s*(true|false)\)",
    re.S,
)


def seed_rows(path: Path = SEED_FILE) -> list[dict[str, Any]]:
    sql = path.read_text(encoding="utf-8")
    rows = [
        {
            "rule_type": m[1],
            "service_type": m[2],
            "nationality": m[3],
            "value": json.loads(m[4].replace("''", "'")),
            "locked": m[5] == "true",
        }
        for m in _TUPLE.finditer(sql)
    ]
    if len(rows) != EXPECTED_ROWS:
        raise ValueError(
            f"{path.name}: parsed {len(rows)} rows, expected {EXPECTED_ROWS} - "
            "the file's shape changed; update scripts/kb_seed.py with it"
        )
    return rows
