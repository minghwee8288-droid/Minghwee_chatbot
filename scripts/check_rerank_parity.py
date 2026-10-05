"""The bot's post-search steps in Python (app/services/rag.py) against kb-admin's
TypeScript port (kb-admin/lib/retrieval.ts), on the same inputs. Must match exactly.

    python scripts/check_rerank_parity.py [--cases N] [--seed S]

Writes nothing, reads no database. For each generated search result (10 rows, as
search() over-fetches) it runs, in Python, exactly what search() runs:

    _rerank(_enforce_row_floor(_drop_internal(_drop_non_evidence(rows))))[:5]

and, in Node, kb-admin's botTop(rows, 5), then compares the id lists. The rows
are built to hit every branch: internal source names in their separator
variants, style_example rows, floors as numbers, numeric strings, junk and None,
priorities 1-3 as ints, strings, floats and junk, similarity ties (so the
stable-sort order is tested), and similarities equal to a floor.
"""
import argparse, json, os, random, subprocess, sys, tempfile
from decimal import Decimal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("APP_ENV_FILE", ".env.test")
os.chdir(REPO)
from app.services import rag  # noqa: E402

SOURCES = [
    "Ming_Hwee_Client_Service_Agreement_SOURCE.docx", "minghwee FAQs and Overview.md",
    "MingHwee Hiring Pipelines Brief.docx", "MingHwee_Hiring-Pipelines_Brief.docx", "hiring pipelines brief",
    "MHOS-100_Product_Blueprint", "MHOS100 notes", "Notes for Vendor", "for_vendor", "Version Control log",
    "Blueprint", "blueprints v2", "Ming Hwee Service Notes", "", None, "KB admin test document.md",
]
CHUNK_TYPES = ["qa_pair", "document_chunk", "table_unit", "style_example", "faq", None, " style_example "]
FLOORS = [None, None, None, 0.42, "0.420", "0.35", 0.5, "", "abc", 0, 0.3]
PRIORITIES = [None, 1, 2, 3, "1", "2", " 3 ", 2.7, 4, 0, "x", "2.0", True, -1]


def rows(rnd):
    sims = [round(rnd.uniform(0.3, 0.8), rnd.choice([2, 3, 6])) for _ in range(10)]
    if rnd.random() < 0.5:  # ties
        sims[rnd.randrange(10)] = sims[rnd.randrange(10)]
    out = []
    for i, s in enumerate(sorted(sims, reverse=True)):
        floor = rnd.choice(FLOORS)
        if rnd.random() < 0.1 and floor is not None and not isinstance(floor, str):
            s = float(floor)  # exactly at the floor
        meta = rnd.choice([None, {}, {"priority": rnd.choice(PRIORITIES)}, {"priority": rnd.choice(PRIORITIES), "x": 1}, "notadict"])
        out.append({"id": f"r{i}", "chunk_type": rnd.choice(CHUNK_TYPES), "source_document": rnd.choice(SOURCES),
                    "similarity": rnd.choice([s, s, s, str(s), None]), "rag_score_floor": floor, "metadata": meta})
    return out


def python_top(case):
    # The database hands Python a numeric floor as Decimal; strings stay strings.
    prepared = [dict(r, rag_score_floor=Decimal(str(r["rag_score_floor"])) if isinstance(r["rag_score_floor"], float) else r["rag_score_floor"])
                for r in case]
    kept = rag._enforce_row_floor(rag._drop_internal(rag._drop_non_evidence(prepared)))
    return [r["id"] for r in rag._rerank(kept)[:5]]


NODE = r"""
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const { botTop } = await import(pathToFileURL(process.argv[2]).href);
const cases = JSON.parse(readFileSync(process.argv[3], 'utf8'));
process.stdout.write(JSON.stringify(cases.map((c) => botTop(c, 5).map((r) => r.id))));
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261005)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    cases = [rows(rnd) for _ in range(a.cases)]
    py = [python_top(c) for c in cases]
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "cases.json")
        script = os.path.join(d, "run.mjs")
        open(inp, "w", encoding="utf-8").write(json.dumps(cases))
        open(script, "w", encoding="utf-8").write(NODE)
        out = subprocess.run(["node", "--no-warnings", script, os.path.join(REPO, "kb-admin", "lib", "retrieval.ts"), inp],
                             capture_output=True, text=True, encoding="utf-8")
        if out.returncode:
            print(out.stderr[-2000:])
            sys.exit(2)
        ts = json.loads(out.stdout)
    diffs = [i for i, (p, t) in enumerate(zip(py, ts)) if p != t]
    dropped = sum(10 - len(p) for p in py)
    print(f"{len(cases)} cases, {len(cases) * 10} rows; Python kept {sum(len(p) for p in py)} in the top 5s ({dropped} slots empty or filtered)")
    for i in diffs[:5]:
        print(f"  MISMATCH case {i}: python {py[i]} ts {ts[i]}\n    {json.dumps(cases[i])[:600]}")
    print("PARITY:", "EXACT" if not diffs else f"{len(diffs)} MISMATCHES")
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
