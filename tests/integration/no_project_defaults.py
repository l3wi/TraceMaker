#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""KiCad's project-less minimums and missing-key fallbacks, including routing below a class width."""
import json
import pathlib
import shutil
import subprocess
import sys

binary, work = sys.argv[1], pathlib.Path(sys.argv[2])
if not shutil.which("kicad-cli"):
    print("SKIP: kicad-cli unavailable")
    sys.exit(77)
work.mkdir(parents=True, exist_ok=True)


def run(*args, allowed=(0,)):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode not in allowed:
        raise RuntimeError(f"{args}: {result.stdout}\n{result.stderr}")
    return result


def board(extra="", legacy=""):
    return '''(kicad_pcb (version 20211014) (generator pcbnew)
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (44 "Edge.Cuts" user))
 (net 0 "") (net 1 "SIG") (net 2 "OTHER")
 (footprint "Test" (layer "F.Cu") (at 3 5)
  (property "Reference" "J1" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG")))
 (footprint "Test" (layer "F.Cu") (at 17 5)
  (property "Reference" "J2" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG")))
 (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)))
''' + legacy + extra + ")\n"


def violations(path, judge):
    report = work / f"{path.stem}-{judge}.json"
    if judge == "kicad":
        run("kicad-cli", "pcb", "drc", str(path), "--format", "json", "--severity-all", "--all-track-errors", "-o", str(report))
    else:
        run(binary, "drc", str(path), "--json", str(report), allowed=(0, 5))
    return json.loads(report.read_text())["violations"]


# Spaced probes expose the actual judge's numbers, not an assumption copied from our reader.
probe = work / "probe.kicad_pcb"
probe.write_text(board('''
 (segment (start 3 5) (end 17 5) (width 0.15) (layer "F.Cu") (net 1))
 (via (at 4 2) (size 0.4) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
 (via (at 7 2) (size 0.5) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1))
 (via (at 10 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
 (via (at 10.5 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
 (segment (start 13 2.2) (end 14 2.2) (width 0.2) (layer "F.Cu") (net 2))
 (via (at 13.5 1.8) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
 (segment (start 0.2 7) (end 0.2 8) (width 0.2) (layer "F.Cu") (net 1))
'''))
expected = {"track_width": "0.2000", "via_diameter": "0.5000", "annular_width": "0.1000",
            "drill_out_of_range": "0.3000", "hole_to_hole": "0.2495", "hole_clearance": "0.2500",
            "copper_edge_clearance": "0.5000", "clearance": "0.2000"}
kicad = violations(probe, "kicad")
for kind, minimum in expected.items():
    matching = [v for v in kicad if v["type"] == kind]
    assert matching and any(minimum in v["description"] for v in matching), (kind, minimum, matching)
    print(kind, "; ".join(v["description"] for v in matching))
assert [v for v in violations(probe, "tracemaker") if v["type"] == "track_width"]

# A project with a rules object but no dimensional minimums must retain the same defaults.
partial_probe = work / "partial-probe.kicad_pcb"
partial_probe.write_text(probe.read_text())
partial_probe.with_suffix(".kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rules": {"min_clearance": 0}}}}))
partial_report = violations(partial_probe, "kicad")
for kind, minimum in expected.items():
    assert any(v["type"] == kind and minimum in v["description"] for v in partial_report), (kind, partial_report)
assert [v for v in violations(partial_probe, "tracemaker") if v["type"] == "track_width"]

# A legacy class narrower than the built-in minimum used to produce illegal tracks without a project.
legacy = '(net_class "Default" "" (clearance 0.2) (trace_width 0.15) (via_dia 0.6) (via_drill 0.3) (add_net "SIG"))\n'
for name, project in (("no-project", None), ("missing-keys", {"board": {"design_settings": {"rules": {"min_clearance": 0}}},
                                                         "net_settings": {"classes": [{"name": "Default", "track_width": 0.15}]}})):
    src = work / f"{name}.kicad_pcb"
    src.write_text(board(legacy=legacy))
    if project is not None:
        src.with_suffix(".kicad_pro").write_text(json.dumps(project))
    output, summary = work / f"{name}-routed.kicad_pcb", work / f"{name}-route.json"
    if project is not None:
        output.with_suffix(".kicad_pro").write_text(json.dumps(project))
    run(binary, "route", str(src), "-o", str(output), "--work", "1000000", "--variants", "1", "--no-kb", "--json", str(summary))
    result = json.loads(summary.read_text())
    assert result["routed"] == result["connections"] == 1, result
    for judge in ("kicad", "tracemaker"):
        assert not [v for v in violations(output, judge) if v["type"] == "track_width"], (name, judge)
print("project-less and missing-key routing obey KiCad minimum widths")
