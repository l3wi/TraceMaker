# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks the Python module `tracemaker` against the CLI (no pytest needed).

usage: test_bindings.py MODULE_DIR [TRACEMAKER_BINARY]
Exit code 77 (ctest: skipped) when the module was not built or the fixture board is missing.
"""
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, "bench/data/freerouting/scripts/benchmark/fixtures/PCBench/C-BISCUIT_buck-reg-5v/unrouted.kicad_pcb")
# Deterministic run: a work budget instead of wall time, no knowledge base, CPU fields (the GPU may be busy).
ROUTE = dict(work=2_000_000, threads=8, seed=1, kb=False, gpu=False)
CLI = ["--work", "2000000", "--threads", "8", "--seed", "1", "--no-kb", "--no-gpu"]


def selective_reroute(tracemaker, tmp, exe):
    board = os.path.join(tmp, "selective.kicad_pcb")
    original = []
    removable = []
    for net in (1, 2):
        for locked in (False, True):
            x = 2 + net * 5 + int(locked)
            lock = " (locked)" if locked else ""
            original.extend([
                f'(segment (start {x} 2) (end {x} 3) (width 0.25) (layer "F.Cu"){lock} (net {net}))',
                f'(arc (start {x} 5) (mid {x + 0.5} 5.5) (end {x + 1} 5) (width 0.25) (layer "F.Cu"){lock} (net {net}))',
                f'(via (at {x} 8) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu"){lock} (net {net}))'])
            if net == 1 and not locked:
                removable.extend(original[-3:])
    text = ('(kicad_pcb (version 20240108) (generator "pcbnew") '
            '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user)) '
            '(net 0 "") (net 1 "SIG") (net 2 "GND") '
            '(gr_rect (start 0 0) (end 20 12) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)))\n'
            + "\n".join(original) + "\n)")
    with open(board, "w") as f:
        f.write(text)
    out = os.path.join(tmp, "selective-out.kicad_pcb")
    options = ["--reroute-nets", "SIG", "--variants", "1", "--work", "1", "--no-kb", "--no-gpu"]
    result = tracemaker.route(board, out, args=options)
    assert result["exit_code"] == 0
    with open(out) as f:
        after = f.read()
    for node in original:
        assert (node in after) == (node not in removable), node
    counts = lambda content: Counter(re.findall(r'\((segment|arc|via)\b.*?\(net (\d+)\)', content, flags=re.S))
    assert counts(text) == Counter({(kind, net): 2 for kind in ("segment", "arc", "via") for net in ("1", "2")})
    assert counts(after) == Counter({(kind, net): 1 if net == "1" else 2 for kind in ("segment", "arc", "via") for net in ("1", "2")})
    try:
        tracemaker.route(board, args=options + ["--reroute-nets", "MISSING"])
        raise AssertionError("unknown net accepted")
    except ValueError as e:
        assert "unknown net" in str(e)
    if exe:
        cli = os.path.join(tmp, "selective-cli.kicad_pcb")
        p = subprocess.run([exe, "route", board, "-o", cli] + options, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
        with open(cli) as f:
            assert f.read() == after


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sys.path.insert(0, sys.argv[1])
    try:
        import tracemaker
    except ImportError as e:
        print(f"SKIP: tracemaker module not importable from {sys.argv[1]}: {e}")
        return 77
    if not os.path.exists(BOARD):
        print(f"SKIP: fixture missing: {BOARD} (scripts/fetch_fixtures.sh freerouting)")
        return 77
    exe = sys.argv[2] if len(sys.argv) > 2 else None

    b = tracemaker.read_board(BOARD)
    print(b)
    assert b.copper_layers == ["F.Cu", "B.Cu"], b.copper_layers
    assert b.nets > 0 and b.footprints > 0 and b.pads > 0 and b.tracks == 0 and b.vias == 0
    assert b.outline_mm is not None and b.outline_mm[0] > 0 and b.outline_mm[1] > 0
    assert len(b.net_names) == b.nets

    with tempfile.TemporaryDirectory(prefix="tm-bindings-") as tmp:
        out = os.path.join(tmp, "py.kicad_pcb")
        items_path = os.path.join(tmp, "py_items.json")
        r = tracemaker.route(BOARD, out, items_out=items_path, **ROUTE)
        print({k: r[k] for k in ("routed", "connections", "tracks", "vias", "seconds", "exit_code")})
        assert r["connections"] > 0 and r["routed"] == r["connections"], r
        assert r["exit_code"] == 0 and r["unrouted"] == [] and r["failures"] == []
        assert r["tracks"] > 0 and os.path.exists(out)
        assert any(line.startswith("routed ") for line in r["log"]), r["log"]

        items = tracemaker.emit_items(BOARD, **ROUTE)
        assert len(items["tracks"]) == r["tracks"] and len(items["vias"]) == r["vias"]
        with open(items_path) as f:
            assert json.load(f) == items, "emit_items() differs from route(items_out=...)"

        violations = tracemaker.drc(out)
        errors = [v for v in violations if v["severity"] == "error"]
        print(f"drc: {len(violations)} items, {len(errors)} errors")
        assert not any(v["type"] == "unconnected_items" for v in violations), "routed board still has unconnected items"
        for v in violations:
            assert {"type", "severity", "description", "items", "actual_mm", "required_mm", "layer"} <= v.keys()

        if exe:  # the module and the CLI share run_route_job: identical boards and items
            cli_out = os.path.join(tmp, "cli.kicad_pcb")
            cli_items = os.path.join(tmp, "cli_items.json")
            p = subprocess.run([exe, "route", BOARD, "-o", cli_out, "--emit-items", cli_items] + CLI, capture_output=True, text=True)
            assert p.returncode == 0, p.stdout + p.stderr
            with open(cli_out, "rb") as f1, open(out, "rb") as f2:
                assert f1.read() == f2.read(), "CLI and bindings wrote different boards"
            with open(cli_items) as f:
                assert json.load(f) == items, "CLI and bindings emitted different items"
            cli_drc = os.path.join(tmp, "cli_drc.json")
            subprocess.run([exe, "drc", out, "--json", cli_drc], capture_output=True, text=True)
            with open(cli_drc) as f:
                d = json.load(f)
            assert len(d["violations"]) + len(d["unconnected_items"]) == len(violations), "drc() differs from `tracemaker drc`"
            print("CLI parity: identical board, items and DRC count")

            # Non-default options exercise the shared parser, not just run_route_job.
            args = CLI + ["--variants", "1", "--soft-zones", "--no-tracks-on", "B.Cu", "--layer-cost", "F.Cu=2",
                          "--first-nets", b.net_names[0], "--escape-plan", "--diff-pairs", "--no-pair-twists",
                          "--component-rules", "report", "--no-optimize"]
            py_args_out = os.path.join(tmp, "args-py.kicad_pcb")
            cli_args_out = os.path.join(tmp, "args-cli.kicad_pcb")
            args_json = os.path.join(tmp, "args-cli.json")
            py_result = tracemaker.route(BOARD, py_args_out, args=args)
            p = subprocess.run([exe, "route", BOARD, "-o", cli_args_out, "--json", args_json] + args, capture_output=True, text=True)
            assert p.returncode == py_result["exit_code"], p.stdout + p.stderr
            with open(py_args_out, "rb") as f1, open(cli_args_out, "rb") as f2:
                assert f1.read() == f2.read(), "non-default CLI/Python options differ"
            with open(args_json) as f:
                cli_result = json.load(f)
            for key in ("routed", "connections", "failures", "unrouted", "rules"):
                assert cli_result[key] == py_result[key], key
            args_items = tracemaker.emit_items(BOARD, args=args)
            assert len(args_items["tracks"]) == py_result["tracks"]

        sparse = tracemaker.route(BOARD, work=2_000_000, args=["--work", "1", "--variants", "1", "--no-kb", "--no-gpu"])
        assert sparse["exit_code"] == 3 and sparse["unrouted"]
        for u in sparse["unrouted"]:
            assert {"net", "a", "b", "reason", "detail", "a_mm", "b_mm", "a_layers", "b_layers"} <= u.keys()
            assert u["reason"] and u["detail"] and len(u["a_mm"]) == 2
        try:
            tracemaker.route(BOARD, args=["--unsupported-route-option"])
            raise AssertionError("unsupported option accepted")
        except ValueError:
            pass
        selective_reroute(tracemaker, tmp, exe)

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
