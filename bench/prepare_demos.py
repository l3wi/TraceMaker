#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare KiCad demo projects (and test boards) as bench/run.py fixtures.
Tracks/arcs and track-contact vias are removed; no-track vias and zone definitions
are kept. Stale fills are discarded and KiCad refills/saves the unrouted input.

  bench/prepare_demos.py [--out build/demos] [--set planes | NAME ...]

A name is a board file stem looked up in bench/data/kicad/demos/*/ (scripts/fetch_fixtures.sh kicad-demos) and
tests/boards/*/. Prints the fixture directory for bench/run.py --fixtures. The open-hardware `modern-planes` set has
its own fetcher, bench/prepare_modern.py.
"""
import argparse
import pathlib
import subprocess
import sys

from prepare_inputs import invalidate_fixture, prepare_board, set_boards

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH = [ROOT / "bench/data/kicad/demos", ROOT / "tests/boards"]


def find(name: str) -> pathlib.Path:
    for base in SEARCH:
        hits = sorted(base.glob(f"*/{name}.kicad_pcb"))
        if hits:
            return hits[0]
    raise FileNotFoundError(f"board not found: {name} (looked in {', '.join(str(s) for s in SEARCH)})")


def prepare(name: str, out: pathlib.Path) -> pathlib.Path:
    dst = out / name / "unrouted.kicad_pcb"
    invalidate_fixture(dst.parent, 'preparation in progress')
    src = find(name)
    prepare_board(src, dst)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "build/demos"))
    ap.add_argument("--set", help="board set file name in bench/sets (e.g. planes)")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    names = list(a.names)
    if a.set:
        names += set_boards(ROOT / "bench/sets" / f"{a.set}.txt")
    if not names:
        ap.error("give --set or board names")
    out = pathlib.Path(a.out).resolve()
    failed = False
    for n in names:
        try:
            prepare(n, out)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            invalidate_fixture(out / n, error)
            print(f"FAILED {n}: {error}", file=sys.stderr)
            failed = True
    print(out)
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
