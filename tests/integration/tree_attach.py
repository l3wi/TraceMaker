#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pad-to-tree routing (doc 05 §41, D99).

  tree_attach.py <tracemaker> <workdir>

1. A pad 3 mm beside the middle of an existing 16 mm track of its net must branch off that track (about 3 mm of
   new copper), not run back to one of the track's end pads (about 9 mm).
2. A GND island (two pads tied by an existing track that runs on out of a no-via area) must drop its via into the
   B.Cu plane straight off the track where vias are allowed, not lay new track over the old one from a pad.
3. The same trunk and terminal placed in one package must use the planned pad, not tree copper among the
   package's other pins. Inter-package branching (case 1) and fixed-island plane starts (case 2) remain available.
4. PCBench mini-whip with --tree-attach and the clean-up: connections branch off routed copper and the clean-up
   re-routes connections, so the router's bookkeeping of what each route physically joins must hold. Every
   connection the router reports routed must be connected in TraceMaker's DRC and, when kicad-cli is available,
   in KiCad's. The run is repeated with two threads and must be byte-identical.
Exit 77 when the PCBench fixture is missing (TM_FIXTURES overrides the PCBench directory).
"""
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = pathlib.Path(os.environ.get("TM_FIXTURES", ROOT / "bench/data/freerouting/scripts/benchmark/fixtures/PCBench"))
WHIP = FIX / "mini-whip_mini-whip-power-feed/unrouted.kicad_pcb"

BRANCH = """(kicad_pcb (version 20240108) (generator pcbnew)
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "BRANCH")
 (footprint "T:A" (layer "F.Cu") (at 2 5) (property "Reference" "A" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "BRANCH")))
 (footprint "T:B" (layer "F.Cu") (at 18 5) (property "Reference" "B" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "BRANCH")))
 (footprint "T:C" (layer "F.Cu") (at 10 8) (property "Reference" "C" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "BRANCH")))
 (segment (start 2 5) (end 18 5) (width 0.25) (layer "F.Cu") (net 1))
 (gr_rect (start 0 0) (end 20 12) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""

PACKAGE = """(kicad_pcb (version 20240108) (generator pcbnew)
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "BRANCH")
 (footprint "T:PACKAGE" (layer "F.Cu") (at 0 0) (property "Reference" "U1" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 2 5) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "BRANCH"))
  (pad "2" smd circle (at 18 5) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "BRANCH"))
  (pad "3" smd circle (at 10 8) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "BRANCH")))
 (segment (start 2 5) (end 18 5) (width 0.25) (layer "F.Cu") (net 1))
 (gr_rect (start 0 0) (end 20 12) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""

ISLAND = """(kicad_pcb (version 20240108) (generator pcbnew)
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "GND")
 (footprint "T:A" (layer "F.Cu") (at 2 5) (property "Reference" "A" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "GND")))
 (footprint "T:B" (layer "F.Cu") (at 6 5) (property "Reference" "B" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "GND")))
 (segment (start 2 5) (end 6 5) (width 0.25) (layer "F.Cu") (net 1))
 (segment (start 6 5) (end 18 5) (width 0.25) (layer "F.Cu") (net 1))
 (zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (name "novia") (hatch edge 0.5) (connect_pads (clearance 0))
  (min_thickness 0.25) (keepout (tracks allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed))
  (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) (polygon (pts (xy 0 0) (xy 12 0) (xy 12 12) (xy 0 12))))
 (zone (net 1) (net_name "GND") (layer "B.Cu") (name "plane") (hatch edge 0.5) (connect_pads (clearance 0.2))
  (min_thickness 0.25) (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
  (polygon (pts (xy 0.5 0.5) (xy 19.5 0.5) (xy 19.5 11.5) (xy 0.5 11.5)))
  (filled_polygon (layer "B.Cu") (pts (xy 0.5 0.5) (xy 19.5 0.5) (xy 19.5 11.5) (xy 0.5 11.5))))
 (gr_rect (start 0 0) (end 20 12) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""


def route(tm, src, out, *extra):
    js = out.with_suffix(".json")
    items = out.with_suffix(".items.json")
    r = subprocess.run([tm, "route", str(src), "-o", str(out), "--work", "1000000", "--time", "3600", "--no-kb", "--no-gpu", "--tree-attach",
                        "--json", str(js), "--emit-items", str(items), *extra], capture_output=True, text=True)
    if r.returncode not in (0, 3):
        print(r.stdout, r.stderr)
        raise SystemExit(f"route failed with exit {r.returncode}")
    return json.loads(js.read_text()), json.loads(items.read_text())


def unconnected_tm(tm, board, work):
    rep = work / (board.stem + ".drc.json")
    subprocess.run([tm, "drc", str(board), "--json", str(rep)], capture_output=True)
    return len(json.loads(rep.read_text())["unconnected_items"])


def unconnected_kicad(board, work):
    kicad = shutil.which("kicad-cli")
    if not kicad:
        return None
    rep = work / (board.stem + ".kicad.json")
    rep.unlink(missing_ok=True)
    subprocess.run([kicad, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(rep), str(board)], capture_output=True, timeout=600)
    return len(json.loads(rep.read_text())["unconnected_items"]) if rep.exists() else None


def main() -> int:
    tm, work = sys.argv[1], pathlib.Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    ok = True

    src = work / "branch.kicad_pcb"
    src.write_text(BRANCH)
    out = work / "branch_routed.kicad_pcb"
    summary, items = route(tm, src, out, "--variants", "1", "--threads", "1")
    length = sum(math.dist(t["start"], t["end"]) for t in items["tracks"]) / 1e6
    if summary["routed"] != summary["connections"] or length > 4.0 or items["vias"]:
        print(f"FAIL branch: routed {summary['routed']}/{summary['connections']}, {length:.2f} mm new copper, {len(items['vias'])} vias "
              "(expected one branch of about 3 mm off the track)")
        ok = False
    for name, n in (("TraceMaker", unconnected_tm(tm, out, work)), ("KiCad", unconnected_kicad(out, work))):
        if n:
            print(f"FAIL branch: {name} DRC finds {n} unconnected items")
            ok = False

    src = work / "island.kicad_pcb"
    src.write_text(ISLAND)
    out = work / "island_routed.kicad_pcb"
    summary, items = route(tm, src, out, "--variants", "1", "--threads", "1", "--soft-zones")
    length = sum(math.dist(t["start"], t["end"]) for t in items["tracks"]) / 1e6
    if summary["routed"] != summary["connections"] or length > 1.0 or len(items["vias"]) != 1:
        print(f"FAIL island: routed {summary['routed']}/{summary['connections']}, {length:.2f} mm new copper, {len(items['vias'])} vias "
              "(expected one via dropped straight off the existing track)")
        ok = False
    if unconnected_tm(tm, out, work):
        print("FAIL island: TraceMaker DRC finds unconnected items")
        ok = False

    src = work / "package.kicad_pcb"
    src.write_text(PACKAGE)
    out = work / "package_routed.kicad_pcb"
    summary, items = route(tm, src, out, "--variants", "1", "--threads", "1")
    length = sum(math.dist(t["start"], t["end"]) for t in items["tracks"]) / 1e6
    if summary["routed"] != summary["connections"] or not 7.0 < length < 12.0 or items["vias"]:
        print(f"FAIL package: routed {summary['routed']}/{summary['connections']}, {length:.2f} mm new copper, {len(items['vias'])} vias "
              "(expected a planned-pad route, not the 3 mm intra-package tree shortcut)")
        ok = False
    for name, n in (("TraceMaker", unconnected_tm(tm, out, work)), ("KiCad", unconnected_kicad(out, work))):
        if n:
            print(f"FAIL package: {name} DRC finds {n} unconnected items")
            ok = False

    if not WHIP.exists():
        print("PCBench fixture missing: fourth part skipped")
        return 0 if ok else 1
    outs = []
    for threads in ("1", "2"):
        o = work / f"whip_t{threads}.kicad_pcb"
        summary, _ = route(tm, WHIP, o, "--variants", "2", "--threads", threads)
        outs.append((o, summary))
    if outs[0][0].read_bytes() != outs[1][0].read_bytes():
        print("FAIL whip: outputs differ between 1 and 2 threads")
        ok = False
    o, summary = outs[0]
    # Unrouted connections leave unconnected items; every routed one must be connected.
    missing = summary["connections"] - summary["routed"]
    for name, n in (("TraceMaker", unconnected_tm(tm, o, work)), ("KiCad", unconnected_kicad(o, work))):
        if n is not None and n > missing:
            print(f"FAIL whip: {name} DRC finds {n} unconnected items, router reports {missing} unrouted")
            ok = False
    print("tree_attach: " + ("ok" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
