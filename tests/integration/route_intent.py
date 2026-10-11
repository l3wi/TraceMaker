#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Routing intent manifest (doc 05 §44, D103).

  route_intent.py <tracemaker> <workdir>

1. Decoupling: capacitor C1 and IC pin U1.1 on VCC, 8 mm apart, over a B.Cu VCC plane. Without a manifest each pad
   drops its own via and the two meet only through the plane. With the pair C1.1 -> U1.1 in the manifest a track
   must join them, and with C1.1 as a plane pad C1.1 must still have its own via within 1.5 mm of track.
2. No-branch: pad C beside the middle of an existing A-B track of its net. --tree-attach alone branches off the
   track; with the net in no_branch_nets the new route must meet the net's copper at pads only (no T-joint).
3. A manifest naming a pad the board does not have is an error that names the entry.
"""
import json
import math
import pathlib
import subprocess
import sys

DECAP = """(kicad_pcb (version 20240108) (generator pcbnew)
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "VCC")
 (footprint "T:U" (layer "F.Cu") (at 4 6) (property "Reference" "U1" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 1 "VCC")))
 (footprint "T:C" (layer "F.Cu") (at 12 6) (property "Reference" "C1" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 1 "VCC")))
 (zone (net 1) (net_name "VCC") (layer "B.Cu") (name "plane") (hatch edge 0.5) (connect_pads yes (clearance 0.2))
  (min_thickness 0.25) (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
  (polygon (pts (xy 0.5 0.5) (xy 19.5 0.5) (xy 19.5 11.5) (xy 0.5 11.5)))
  (filled_polygon (layer "B.Cu") (pts (xy 0.5 0.5) (xy 19.5 0.5) (xy 19.5 11.5) (xy 0.5 11.5))))
 (gr_rect (start 0 0) (end 20 12) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""

BRANCH = """(kicad_pcb (version 20240108) (generator pcbnew)
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "XTAL")
 (footprint "T:A" (layer "F.Cu") (at 2 5) (property "Reference" "A" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "XTAL")))
 (footprint "T:B" (layer "F.Cu") (at 18 5) (property "Reference" "B" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "XTAL")))
 (footprint "T:C" (layer "F.Cu") (at 10 8) (property "Reference" "C" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "XTAL")))
 (segment (start 2 5) (end 18 5) (width 0.25) (layer "F.Cu") (net 1))
 (gr_rect (start 0 0) (end 20 12) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""
MM = 1e6


def route(tm, src, out, *extra):
    js, items = out.with_suffix(".json"), out.with_suffix(".items.json")
    r = subprocess.run([tm, "route", str(src), "-o", str(out), "--work", "1000000", "--time", "3600", "--no-kb", "--no-gpu", "--variants", "1",
                        "--threads", "1", "--json", str(js), "--emit-items", str(items), *extra], capture_output=True, text=True)
    if r.returncode not in (0, 3):
        print(r.stdout, r.stderr)
        raise SystemExit(f"route failed with exit {r.returncode}")
    return json.loads(js.read_text()), json.loads(items.read_text())


def manifest(work, name, body):
    p = work / f"{name}.intent.json"
    p.write_text(json.dumps({"version": 1, **body}))
    return p


def track_path(items, a, b, tol=0.6 * MM):
    """Is there a chain of new tracks from point a to point b (ends within `tol` of each point / of each other)?"""
    segs = [(tuple(t["start"]), tuple(t["end"])) for t in items["tracks"]]
    near = lambda p, q, d=tol: math.dist(p, q) <= d
    seen, frontier = set(), [i for i, (s, e) in enumerate(segs) if near(s, a) or near(e, a)]
    while frontier:
        i = frontier.pop()
        if i in seen:
            continue
        seen.add(i)
        s, e = segs[i]
        if near(s, b) or near(e, b):
            return True
        frontier += [j for j, (s2, e2) in enumerate(segs) if j not in seen and any(near(p, q, 1000) for p in (s, e) for q in (s2, e2))]
    return False


def seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy)), t


def t_joints(items, fixed, pads, pad_r=0.5 * MM):
    """New track ends outside pads that touch the interior of another segment (fixed or new): branches."""
    segs = [(tuple(t["start"]), tuple(t["end"])) for t in items["tracks"]]
    allsegs = segs + fixed
    joints = []
    for i, (s, e) in enumerate(segs):
        for p in (s, e):
            if any(math.dist(p, c) <= pad_r for c in pads):
                continue
            for j, (a, b) in enumerate(allsegs):
                if j == i:
                    continue
                d, t = seg_dist(p, a, b)
                if d <= 0.15 * MM and 0.001 < t < 0.999:
                    joints.append(p)
    return joints


def main() -> int:
    tm, work = sys.argv[1], pathlib.Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    ok = True
    U1, C1 = (4 * MM, 6 * MM), (12 * MM, 6 * MM)

    src = work / "decap.kicad_pcb"
    src.write_text(DECAP)
    _, items = route(tm, src, work / "decap_plain.kicad_pcb", "--soft-zones")
    if track_path(items, C1, U1):
        print("FAIL decap: precondition: without a manifest C1.1 and U1.1 should meet only through the plane")
        ok = False
    man = manifest(work, "decap", {"connections": [{"from": "C1.1", "to": "U1.1"}], "plane_pads": [{"pad": "C1.1"}]})
    summary, items = route(tm, src, work / "decap_intent.kicad_pcb", "--soft-zones", "--tree-attach", "--intent", str(man))
    if summary.get("intent", {}).get("connections") != 1:
        print(f"FAIL decap: summary intent block {summary.get('intent')}")
        ok = False
    if not track_path(items, C1, U1):
        print("FAIL decap: with the manifest a track must join C1.1 and U1.1")
        ok = False
    own = [v for v in items["vias"] if math.dist(tuple(v["position"]), C1) <= 1.5 * MM]
    if not own:
        print(f"FAIL decap: C1.1 is a plane pad but has no via within 1.5 mm ({items['vias']})")
        ok = False

    src = work / "branch.kicad_pcb"
    src.write_text(BRANCH)
    fixed = [((2 * MM, 5 * MM), (18 * MM, 5 * MM))]
    pads = [(2 * MM, 5 * MM), (18 * MM, 5 * MM), (10 * MM, 8 * MM)]
    _, items = route(tm, src, work / "branch_tree.kicad_pcb", "--tree-attach")
    if not t_joints(items, fixed, pads):
        print("FAIL branch: precondition: --tree-attach alone should branch off the A-B track")
        ok = False
    man = manifest(work, "branch", {"no_branch_nets": [{"net": "XTAL"}]})
    summary, items = route(tm, src, work / "branch_intent.kicad_pcb", "--tree-attach", "--intent", str(man))
    joints = t_joints(items, fixed, pads)
    if summary["routed"] != summary["connections"] or joints:
        print(f"FAIL branch: routed {summary['routed']}/{summary['connections']}, T-joints {joints}")
        ok = False

    man = manifest(work, "bad", {"connections": [{"from": "C1.9", "to": "U1.1"}]})
    r = subprocess.run([tm, "route", str(work / "decap.kicad_pcb"), "-o", str(work / "bad.kicad_pcb"), "--work", "1000", "--no-kb", "--intent", str(man)],
                       capture_output=True, text=True)
    if r.returncode in (0, 3) or "connections[0].from: C1 has no pad 9" not in (r.stdout + r.stderr):
        print(f"FAIL bad manifest: exit {r.returncode}: {(r.stdout + r.stderr)[-300:]}")
        ok = False

    print("route_intent: " + ("ok" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
