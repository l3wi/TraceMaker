#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clean pass split by configured escape-search findings (doc 05 §12): exact witnessed/satisfied
obligations vs finite-domain exhaustion or unknown work/unsupported results; never physical impossibility.

  bench/feasibility.py RUN [RUN ...] [--analysis build/escape_all]

The per-board analysis JSONs come from `tracemaker escape <board> --json build/escape_all/<board>.json`
(see build/escape_all/run.sh); boards without one are listed as unknown.
"""
import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--analysis", default=str(ROOT / "build/escape_all"))
    a = ap.parse_args()
    an = pathlib.Path(a.analysis)
    print("| Run | Boards | Clean | Witnessed boards | Clean (witnessed) | Domain exhausted / unknown boards |")
    print("|---|--:|--:|--:|--:|---|")
    for rid in a.runs:
        rows = [json.loads(l) for l in (ROOT / "bench/results" / rid / "boards.jsonl").read_text().splitlines() if l.strip()]
        rows = [r for r in rows if "clean" in r]
        states = {}
        for r in rows:
            path = an / f"{r['board']}.json"
            if r.get("escape_unknown") is not None and r.get("dead_pins") is not None:
                states[r["board"]] = (r["dead_pins"], r["escape_unknown"])
            elif path.exists():
                report = json.loads(path.read_text())
                status = report.get("statuses")
                if status is not None:
                    states[r["board"]] = (status["exhausted"], status["unknown"])
        feas = [r for r in rows if states.get(r["board"]) == (0, 0)]
        bad = [f"{r['board']} ({states.get(r['board'], 'unknown legacy/missing domain')})"
               for r in rows if states.get(r["board"]) != (0, 0)]
        cf = sum(r["clean"] for r in feas) / len(feas) if feas else 0
        print(f"| {rid} | {len(rows)} | {sum(r['clean'] for r in rows) / len(rows):.1%} | {len(feas)} | {cf:.1%} | {', '.join(bad)} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
