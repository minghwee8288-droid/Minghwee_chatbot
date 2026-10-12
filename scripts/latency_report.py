"""p50 / p90 of each step of a turn, from the TURN_TIMING log lines.

    docker compose logs chatbot | python scripts/latency_report.py
    python scripts/latency_report.py chatbot.log [more.log ...]

Reads any text and uses only the lines app/timing.py writes
("TURN_TIMING {...}"), so a whole container log can be piped in as it is.
Those lines carry a conversation id and durations, never message text or a
phone number, so the report has none either.

A step's figure is per TURN: the time that step took in that turn, summed
over its calls (a turn that classifies twice counts both). Turns where a step
did not happen are left out of that step's percentiles, and the "turns"
column says how many it is based on. Steps overlap by design (a RAG search
contains its own database call), so they do not add up to the total; the
phases (setup, graph, send) do not overlap.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from typing import Iterable

PREFIX = "TURN_TIMING "

# The headline numbers, printed first, in this order.
HEADLINE = [
    ("client_to_reply_ms", "client's first message -> reply sent"),
    ("waited_before_start_ms", "client's first message -> processing starts"),
    ("debounce_ms", "client's last message -> processing starts (debounce)"),
    ("total_ms", "processing (setup + graph + send)"),
]


def parse(lines: Iterable[str]) -> list[dict]:
    turns = []
    for line in lines:
        at = line.find(PREFIX)
        if at < 0:
            continue
        try:
            turns.append(json.loads(line[at + len(PREFIX):].strip()))
        except json.JSONDecodeError:
            continue
    return turns


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; values need not be sorted."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _row(label: str, values: list[float], width: int) -> str:
    return (
        f"  {label:<{width}} {len(values):>6} {percentile(values, 50):>9.0f} "
        f"{percentile(values, 90):>9.0f} {max(values):>9.0f}"
    )


def report(turns: list[dict]) -> str:
    if not turns:
        return "No TURN_TIMING lines found."
    out = [f"{len(turns)} turn(s)\n"]
    width = max(len(text) for _, text in HEADLINE)
    out.append(f"  {'':<{width}} {'turns':>6} {'p50 ms':>9} {'p90 ms':>9} {'max ms':>9}")
    for key, text in HEADLINE:
        values = [t[key] for t in turns if isinstance(t.get(key), (int, float))]
        if values:
            out.append(_row(text, values, width))

    phases: dict[str, list[float]] = defaultdict(list)
    for t in turns:
        for name, ms in (t.get("phases") or {}).items():
            phases[name].append(ms)
    if phases:
        out.append("\nphases")
        width = max(len(n) for n in phases) + 2
        for name in ("setup", "graph", "send"):
            if name in phases:
                out.append(_row(name, phases[name], width))

    steps: dict[str, list[float]] = defaultdict(list)
    for t in turns:
        for name, entry in (t.get("steps") or {}).items():
            steps[name].append(entry.get("ms", 0))
    if steps:
        out.append("\nsteps (slowest p90 first)")
        width = max(len(n) for n in steps)
        for name in sorted(steps, key=lambda n: -percentile(steps[n], 90)):
            out.append(_row(name, steps[name], width))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="log files; stdin when none")
    args = parser.parse_args(argv)
    if args.files:
        lines: list[str] = []
        for path in args.files:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines.extend(fh)
    else:
        lines = list(sys.stdin)
    print(report(parse(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
