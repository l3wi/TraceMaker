#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Net-tie pad entry and intentional mask apertures, judged by KiCad as well as TraceMaker."""
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
board = '''(kicad_pcb (version 20240108) (generator "pcbnew")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
 (net 0 "") (net 1 "A") (net 2 "B") (net 3 "UNTIED")
 (footprint "Jumper" (layer "F.Cu") (at 10 5)
  (property "Reference" "JP1" (at 0 -2) (layer "F.SilkS"))
  (attr allow_soldermask_bridges) (net_tie_pad_groups "1,2")
  (fp_poly (pts (xy -0.9 -0.3) (xy -0.4 -0.3) (xy -0.4 0.3) (xy -0.9 0.3))
   (stroke (width 0) (type solid)) (fill yes) (layer "F.Cu"))
  (fp_rect (start -0.8 -0.75) (end 0.8 0.75) (stroke (width 0) (type solid)) (fill yes) (layer "F.Mask"))
  (pad "1" smd custom (at -1.3 0) (size 1 0.5) (layers "F.Cu" "F.Mask") (net 2 "B")
   (options (clearance outline) (anchor rect)) (primitives
    (gr_circle (center 0 0.25) (end 0.5 0.25) (width 0) (fill yes))
    (gr_circle (center 0 -0.25) (end 0.5 -0.25) (width 0) (fill yes))
    (gr_poly (pts (xy 0.55 0.75) (xy 0 0.75) (xy 0 -0.75) (xy 0.55 -0.75)) (width 0) (fill yes))))
  (pad "2" smd rect (at 0 0) (size 1 1.5) (layers "F.Cu" "F.Mask") (net 1 "A"))
  (pad "3" smd rect (at 1.3 0) (size 1.1 1.5) (layers "F.Cu" "F.Mask") (net 3 "UNTIED")))
 (footprint "Target" (layer "F.Cu") (at 10 2)
  (property "Reference" "J1" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "A")))
 (footprint "Target" (layer "F.Cu") (at 8.7 8)
  (property "Reference" "J2" (at 0 1) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "B")))
 (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)))
)
'''


def run(*args, allowed=(0,)):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode not in allowed:
        raise RuntimeError(f"{args}: {result.stdout}\n{result.stderr}")
    return result


def violations(path, judge):
    report = work / f"{path.stem}-{judge}.json"
    if judge == "kicad":
        run("kicad-cli", "pcb", "drc", str(path), "--format", "json", "-o", str(report))
    else:
        run(binary, "drc", str(path), "--json", str(report), allowed=(0, 5))
    return json.loads(report.read_text())


src = work / "input.kicad_pcb"
src.write_text(board)
output, summary = work / "routed.kicad_pcb", work / "route.json"
run(binary, "route", str(src), "-o", str(output), "--work", "2000000", "--variants", "1", "--no-kb", "--json", str(summary))
result = json.loads(summary.read_text())
assert result["routed"] == result["connections"] == 2, result
for judge in ("kicad", "tracemaker"):
    report = violations(output, judge)
    errors = [v for v in report["violations"] if v["type"] in {"clearance", "shorting_items", "hole_clearance", "solder_mask_bridge"}]
    assert not errors, (judge, errors)
    assert not report["unconnected_items"], (judge, report["unconnected_items"])
# Explicit entries touch the bridge inside each net's pad; a foreign net crossing the gap cannot use the exemption.
entries = work / "entries.kicad_pcb"
entries.write_text(board.rsplit(")", 1)[0] + '''
 (segment (start 9.2 5) (end 9.2 3.5) (width 0.15) (layer "F.Cu") (net 2))
 (segment (start 9.55 5) (end 9.55 3.5) (width 0.15) (layer "F.Cu") (net 1))
 (via (at 9.75 5) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
''')
foreign = work / "foreign.kicad_pcb"
foreign.write_text(board.rsplit(")", 1)[0] + '''
 (segment (start 9.35 4) (end 9.35 6) (width 0.15) (layer "F.Cu") (net 3))
)
''')
for judge in ("kicad", "tracemaker"):
    assert not [v for v in violations(entries, judge)["violations"] if v["type"] in {"clearance", "shorting_items", "hole_clearance"}], judge
    assert [v for v in violations(foreign, judge)["violations"] if v["type"] in {"clearance", "shorting_items"}], judge
print("net-tie entries legal, foreign crossing rejected; both distinct nets routed and KiCad-clean")
