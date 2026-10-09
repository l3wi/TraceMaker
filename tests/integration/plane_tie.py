#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Plane ties (doc 05 §43, D98).

  plane_tie.py <tracemaker> <workdir>

An SMD GND pad 1.5 mm from a through GND pad that sits in the B.Cu GND plane. With --soft-zones the pad's plan is a
connection into the plane. With --plane-tie it must end on the through pad (about 1 mm of track, no via), connected in
TraceMaker's DRC and, when kicad-cli is available, in KiCad's after a refill; without it, the pad drops its own via.
"""
import json
import math
import pathlib
import shutil
import subprocess
import sys

BOARD = """(kicad_pcb (version 20240108) (generator pcbnew)
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "GND")
 (footprint "T:S" (layer "F.Cu") (at 8 6) (property "Reference" "S" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 1 "GND")))
 (footprint "T:T" (layer "F.Cu") (at 9.5 6) (property "Reference" "T" (at 0 -1) (layer "F.SilkS"))
  (pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6) (layers "*.Cu" "*.Mask") (net 1 "GND")))
 (zone (net 1) (net_name "GND") (layer "B.Cu") (name "plane") (hatch edge 0.5) (connect_pads yes (clearance 0.2))
  (min_thickness 0.25) (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
  (polygon (pts (xy 0.5 0.5) (xy 19.5 0.5) (xy 19.5 11.5) (xy 0.5 11.5)))
  (filled_polygon (layer "B.Cu") (pts (xy 0.5 0.5) (xy 19.5 0.5) (xy 19.5 11.5) (xy 0.5 11.5))))
 (gr_rect (start 0 0) (end 20 12) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""


def route(tm, src, out, *extra):
    js, items = out.with_suffix(".json"), out.with_suffix(".items.json")
    r = subprocess.run([tm, "route", str(src), "-o", str(out), "--work", "1000000", "--no-kb", "--no-gpu", "--soft-zones", "--variants", "1",
                        "--threads", "1", "--json", str(js), "--emit-items", str(items), *extra], capture_output=True, text=True)
    if r.returncode not in (0, 3):
        print(r.stdout, r.stderr)
        raise SystemExit(f"route failed with exit {r.returncode}")
    return json.loads(js.read_text()), json.loads(items.read_text())


def unconnected(tm, board, work):
    rep = work / (board.stem + ".drc.json")
    subprocess.run([tm, "drc", str(board), "--json", str(rep)], capture_output=True)
    n = {"TraceMaker": len(json.loads(rep.read_text())["unconnected_items"])}
    kicad = shutil.which("kicad-cli")
    if kicad:
        krep = work / (board.stem + ".kicad.json")
        krep.unlink(missing_ok=True)
        subprocess.run([kicad, "pcb", "drc", "--refill-zones", "--format", "json", "--severity-all", "-o", str(krep), str(board)], capture_output=True,
                       timeout=600)
        if krep.exists():
            n["KiCad"] = len(json.loads(krep.read_text())["unconnected_items"])
    return n


def main() -> int:
    tm, work = sys.argv[1], pathlib.Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    src = work / "tie.kicad_pcb"
    src.write_text(BOARD)
    ok = True
    for name, extra, want_vias in (("own_via", [], 1), ("tie", ["--plane-tie"], 0)):
        out = work / f"{name}.kicad_pcb"
        summary, items = route(tm, src, out, *extra)
        length = sum(math.dist(t["start"], t["end"]) for t in items["tracks"]) / 1e6
        if summary["routed"] != summary["connections"] or len(items["vias"]) != want_vias or (want_vias == 0 and not 0.5 < length < 1.5):
            print(f"FAIL {name}: routed {summary['routed']}/{summary['connections']}, {length:.2f} mm new track, {len(items['vias'])} vias "
                  f"(expected {want_vias} vias{', about 1 mm of track to the through pad' if want_vias == 0 else ''})")
            ok = False
        for judge, n in unconnected(tm, out, work).items():
            if n:
                print(f"FAIL {name}: {judge} DRC finds {n} unconnected items")
                ok = False
    print("plane_tie: " + ("ok" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
