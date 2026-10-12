#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare the DAC 2020 PCB benchmarks (bench/data/dac2020) in the PCBench fixture layout used by bench/compare.py.

For each benchmark bmN with a Specctra file in Freerouting's fixture set (DAC2020_bmNN.dsn), writes
bench/data/dac2020_prepared/bmN/ with:
  raw.kicad_pcb       the human-routed original
  unrouted.kicad_pcb  the same board with its tracks, track arcs and vias removed (the DAC 2020 authors' intended use)
  unrouted.dsn        Freerouting's Specctra fixture for the board (unrouted; same coordinates as the KiCad file)
"""
import hashlib
import json
import pathlib
import shutil
import sys

from prepare_inputs import copy_rules, fixture_transaction, invalidate_fixture, terminal_signature

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "bench/data/dac2020"
DSN = ROOT / "bench/data/freerouting/scripts/benchmark/fixtures/DAC2020_boards"
OUT = ROOT / "bench/data/dac2020_prepared"


def strip_routing(text: str) -> str:
    """Remove top-level (segment ...), (arc ...) and (via ...) items, keeping everything else byte for byte."""
    out, i, n, depth = [], 0, len(text), 0
    start_keep = 0
    while i < n:
        c = text[i]
        if c == '"':  # skip strings
            j = text.index('"', i + 1)
            while text[j - 1] == "\\":
                j = text.index('"', j + 1)
            i = j + 1
            continue
        if c == "(":
            if depth == 1:
                head = text[i + 1:i + 12].split(None, 1)[0].rstrip(")")
                if head in ("segment", "arc", "via"):
                    # find the matching close paren
                    d, j = 0, i
                    while True:
                        if text[j] == '"':
                            j = text.index('"', j + 1)
                        elif text[j] == "(":
                            d += 1
                        elif text[j] == ")":
                            d -= 1
                            if d == 0:
                                break
                        j += 1
                    # drop the item and its leading indentation / trailing newline
                    k = i
                    while k > start_keep and text[k - 1] in " \t":
                        k -= 1
                    out.append(text[start_keep:k])
                    i = j + 1
                    if i < n and text[i] == "\n":
                        i += 1
                    start_keep = i
                    continue
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    out.append(text[start_keep:])
    return "".join(out)


def prepare(pcb, dsn, destination):
    metadata = {'board': destination.name, 'refilled': False, 'human_available': True,
                'preparation': 'legacy DAC2020 all-vias-stripped', 'report': 'preparation.report.json'}
    with fixture_transaction(destination, metadata) as stage:
        human = stage / 'routed.kicad_pcb'
        shutil.copyfile(pcb, human)
        shutil.copyfile(pcb, stage / 'raw.kicad_pcb')
        shutil.copyfile(dsn, stage / 'unrouted.dsn')
        board = stage / 'unrouted.kicad_pcb'
        board.write_text(strip_routing(pcb.read_text()))
        for target in (human, board, stage / 'raw.kicad_pcb'):
            copy_rules(pcb, target)
        if terminal_signature(board.read_text()) != terminal_signature(human.read_text()):
            raise ValueError('stripping changed nets, footprint references or pads')
        metadata.update(source=str(pcb), source_sha256=hashlib.sha256(human.read_bytes()).hexdigest())
        (stage / 'preparation.report.json').write_text(json.dumps({'terminal_identity_matches': True}) + '\n')
    return metadata


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    failed = False
    for d in sorted(SRC.glob("bm*")):
        n = int(d.name[2:])
        dsn = DSN / f"DAC2020_bm{n:02d}.dsn"
        pcb = next(iter(d.glob("*.kicad_pcb")), None)
        destination = OUT / d.name
        if not dsn.exists() or pcb is None:
            reason = 'no Specctra fixture' if pcb else 'no board'
            invalidate_fixture(destination, reason)
            print(f"skip {d.name}: {reason}")
            failed = True
            continue
        try:
            prepare(pcb, dsn, destination)
            print("prepared", destination)
        except (OSError, ValueError, RuntimeError) as error:
            invalidate_fixture(destination, error)
            print(f"FAILED {d.name}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
