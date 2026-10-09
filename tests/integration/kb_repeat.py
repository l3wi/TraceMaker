#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent history must not change a budgeted route or its portfolio (D99).

Ten short connections precede one expensive connection on a fixture-free board. The budget fits the ten short
ones (16 work units each at the 0.065 mm pitch of KiCad's default 0.2 mm class, D100) but not the long one. With the
pitch of the old 0.25 mm default, at 240 work units the old failed-first policy routed 10, then only 1, then 10.
Also check historical variant wins cannot replace a configured portfolio, and
that editing the input changes its history key even when its filename is unchanged.
"""
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile


def board_text():
    text = ['(kicad_pcb (version 20240108) (generator pcbnew)',
            '(general (thickness 1.6))',
            '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
            '(setup (pad_to_mask_clearance 0))', '(net 0 "")']
    for net in range(1, 12):
        text.append(f'(net {net} "N{net}")')
        for end, x in (("A", 2), ("B", 4 if net < 11 else 48)):
            ref = f"P{net}{end}"
            text.append(f'(footprint "T:{ref}" (layer "F.Cu") (at {x} {net * 3}) '
                        f'(property "Reference" "{ref}" (at 0 -1) (layer "F.SilkS")) '
                        f'(pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net {net} "N{net}")))')
    text.append('(gr_rect (start 0 0) (end 50 36) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts")))')
    return "\n".join(text) + "\n"


def main():
    tm, work = sys.argv[1], pathlib.Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="repeat-", dir=work) as td:
        directory = pathlib.Path(td)
        src = directory / "input.kicad_pcb"
        src.write_text(board_text())

        def route(name, variants, kb=None):
            out = directory / f"{name}.kicad_pcb"
            report = out.with_suffix(".json")
            command = [tm, "route", str(src), "-o", str(out), "--work", "150", "--time", "3600",
                       "--variants", str(variants), "--threads", "1", "--no-gpu", "--json", str(report)]
            command += ["--kb", str(kb)] if kb else ["--no-kb"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            assert result.returncode in (0, 3), result.stdout + result.stderr
            return json.loads(report.read_text()), out.read_bytes(), result.stdout + result.stderr

        for variants in (1, 2):
            kb = directory / f"v{variants}.sqlite"
            baseline, copper, _ = route(f"v{variants}-reference", variants)
            assert baseline["routed"] > 0, baseline
            if variants == 1:
                assert baseline["routed"] < baseline["connections"], baseline
            for run in range(1, 4):
                summary, output, log = route(f"v{variants}-run{run}", variants, kb)
                for key in ("routed", "connections", "expansions", "variant"):
                    assert summary[key] == baseline[key], (variants, run, key, baseline[key], summary[key], log)
                assert output == copper, (variants, run, "board bytes changed")
                with sqlite3.connect(kb) as database:
                    assert database.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == run
                    if variants == 1:
                        assert database.execute("SELECT COUNT(*) FROM failed_connections").fetchone()[0] > 0
                    if run == 1 and variants == 2:
                        # A formerly winning arm outside the requested first two must not replace them.
                        database.execute("INSERT OR REPLACE INTO variant_stats VALUES("
                                         "(SELECT bucket FROM runs LIMIT 1), 7, 100, 100)")
            if variants == 1:
                assert "reporting only; routing order unchanged" in log, log

        # Same filename, changed bytes: old per-board failures must not be reported as current.
        src.write_text(board_text().replace('(at 48 33)', '(at 47 33)'))
        _, _, log = route("edited", 1, directory / "v1.sqlite")
        assert "connections left unrouted before" not in log, log
        with sqlite3.connect(directory / "v1.sqlite") as database:
            assert database.execute("SELECT COUNT(DISTINCT board_hash) FROM runs").fetchone()[0] == 2
    print("kb_repeat: unchanged copper/work/portfolio across repeated runs; changed board isolated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
