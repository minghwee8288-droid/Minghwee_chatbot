"""Assert that reset-ui/ still clears everything reset_conversation.py clears.

    python scripts/selfcheck_reset_ui.py

The web UI is a SECOND implementation of the reset, in TypeScript, in its own
deployable folder. That is a deliberate trade: the alternative was making the
chatbot service a runtime dependency of a page on Vercel. The cost is the one
this codebase already knows well -- two copies of a rule drift apart, and
nobody notices until it matters (CLAUDE.md section 9.8, where
EMPLOYER_LEAD_SERVICES is defined twice and the copies DISAGREE).

So the drift is caught here instead of in production. Add a table to
WIPE_TABLES in the Python and forget the TypeScript, and this fails.

Three things are checked, and they are the three that would hurt:

  1. The tables wiped by conversation_id.
  2. The LangGraph checkpoint tables wiped by thread_id.
  3. The columns blanked on wp_chat_conversations. A field missed here is the
     bug that had a number branded 'candidate' route "I am looking for a maid"
     down the helper flow.

Phone normalisation is checked separately and by EXECUTION, not by reading:
lib/phone.ts is compiled and run against the same inputs as app/utils.py. A
mismatch there does not clear the wrong rows -- it fails to find the client at
all, or finds somebody else.

Read-only. Touches no database. Safe to run anywhere, any time.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_RESET = ROOT / "scripts" / "reset_conversation.py"
TS_RESET = ROOT / "reset-ui" / "lib" / "reset.ts"
TS_PHONE = ROOT / "reset-ui" / "lib" / "phone.ts"

sys.path.insert(0, str(ROOT))

from app.utils import digits_only, normalize_phone, phone_variants  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  PASS  {label:<52} {detail}")
    else:
        print(f"  FAIL  {label:<52} {detail}")
        failures.append(label)


def _tuple_literal(source: str, name: str) -> set[str]:
    """The string members of `NAME = (...)` or `const NAME = [...]`."""
    match = re.search(rf"{name}\s*(?::[^=]+)?=\s*[\(\[](.*?)[\)\]]", source, re.S)
    if not match:
        return set()
    return set(re.findall(r"['\"]([a-z_]+)['\"]", match.group(1)))


def main() -> int:
    py = PY_RESET.read_text(encoding="utf-8")
    ts = TS_RESET.read_text(encoding="utf-8")

    print("Tables cleared")
    for name in ("WIPE_TABLES", "CHECKPOINT_TABLES"):
        py_tables = _tuple_literal(py, name)
        ts_tables = _tuple_literal(ts, name)
        check(
            f"{name} identical",
            bool(py_tables) and py_tables == ts_tables,
            f"{len(py_tables)} table(s)" if py_tables == ts_tables else f"py={sorted(py_tables)} ts={sorted(ts_tables)}",
        )

    print("\nConversation columns blanked")
    py_update = re.search(r"await conversation_service\.update\((.*?)\n    \)", py, re.S)
    ts_update = re.search(r"\.update\(\{(.*?)\n      \}\)", ts, re.S)
    py_fields = set(re.findall(r"^\s+([a-z_]+)=", py_update.group(1), re.M)) if py_update else set()
    ts_fields = set(re.findall(r"^\s+([a-z_]+):", ts_update.group(1), re.M)) if ts_update else set()
    # The Python's conversation_service.update() stamps updated_at itself; the
    # TypeScript writes it inline because it talks to PostgREST directly.
    ts_fields.discard("updated_at")

    check("the Python update was found", bool(py_fields), f"{len(py_fields)} field(s)")
    check("the TypeScript update was found", bool(ts_fields), f"{len(ts_fields)} field(s)")
    missing = py_fields - ts_fields
    extra = ts_fields - py_fields
    check("no field the script blanks is left set by the UI", not missing, f"missing: {sorted(missing)}" if missing else "")
    check("the UI blanks nothing the script does not", not extra, f"extra: {sorted(extra)}" if extra else "")

    print("\nWhat the UI may delete beyond the script")
    # Deliberate and required by the client: the script never touches `leads`.
    # The UI clears every lead on the number automatically -- but each DELETE
    # is still keyed on a single resolved lead id, never on the phone. That is
    # what stops a loose phone match (the ilike '%last4%' fallback) from
    # sweeping a lead belonging to somebody else off the same four digits.
    check(
        "the script still refuses to touch `leads`",
        "leads`` is NEVER touched" in py or "leads` is NEVER touched" in py,
        "unchanged",
    )
    check(
        "each lead DELETE is keyed on one lead id",
        ".delete()\n    .eq('id', id)" in ts or re.search(r"\.delete\(\)\s*\.eq\('id', id\)", ts) is not None,
        "no phone-scoped lead delete",
    )
    check(
        "the UI has no unscoped delete",
        all(
            ".eq(" in segment[:200]
            for segment in ts.split(".delete()")[1:]
        ),
        f"{ts.count('.delete()')} delete(s), all filtered",
    )

    print("\nPhone matching (compiled and executed, not read)")
    if shutil.which("npx") is None:
        check("phone.ts parity", False, "npx not found — cannot compile")
    else:
        cases = [
            "+6591234567", "6591234567", "91234567", "65 9123 4567",
            "6591234567@s.whatsapp.net", "+917970027379", "917970027379",
            "+65 9188 4442", "9713339155", "", "abc", "+1 (415) 555-2671",
        ]
        expected = {
            case: {
                "digits": digits_only(case),
                "normalized": normalize_phone(case),
                "variants": sorted(phone_variants(case)),
            }
            for case in cases
        }

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            compile_result = subprocess.run(
                ["npx", "tsc", str(TS_PHONE), "--outDir", str(out),
                 "--module", "commonjs", "--target", "es2018"],
                capture_output=True, text=True, cwd=ROOT / "reset-ui", shell=(sys.platform == "win32"),
            )
            compiled = out / "phone.js"
            if not compiled.exists():
                check("phone.ts compiles", False, compile_result.stderr.strip()[:120])
            else:
                driver = out / "run.js"
                driver.write_text(
                    "const p = require('./phone.js');\n"
                    f"const cases = {json.dumps(cases)};\n"
                    "const out = {};\n"
                    "for (const c of cases) out[c] = {\n"
                    "  digits: p.digitsOnly(c),\n"
                    "  normalized: p.normalizePhone(c),\n"
                    "  variants: p.phoneVariants(c).sort(),\n"
                    "};\n"
                    "console.log(JSON.stringify(out));\n",
                    encoding="utf-8",
                )
                run = subprocess.run(
                    ["node", str(driver)], capture_output=True, text=True,
                    shell=(sys.platform == "win32"),
                )
                if run.returncode != 0:
                    check("phone.js runs", False, run.stderr.strip()[:120])
                else:
                    actual = json.loads(run.stdout)
                    mismatched = [c for c in cases if actual.get(c) != expected[c]]
                    check(
                        "lib/phone.ts matches app/utils.py exactly",
                        not mismatched,
                        f"{len(cases)} inputs" if not mismatched else f"differs on {mismatched}",
                    )
                    if mismatched:
                        for case in mismatched:
                            print(f"          {case!r}\n            python: {expected[case]}\n            node  : {actual.get(case)}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        return 1
    print(f"ALL PASS ({checks} assertions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
