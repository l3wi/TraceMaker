#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic inner-plane track bans (doc 05 §42, D103), CLI/shared args contract."""
import json
import pathlib
import subprocess
import sys

BOARD = '''(kicad_pcb (version 20240108) (generator pcbnew)
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "SIGNAL") (net 2 "WALL") (net 3 "GND") (net 4 "POWER")
 (footprint "T:A" (layer "F.Cu") (at 5 8) (property "Reference" "A")
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL")))
 (footprint "T:B" (layer "F.Cu") (at 25 8) (property "Reference" "B")
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL")))
 (segment (start 15 0) (end 15 20) (width 0.5) (locked yes) (layer "F.Cu") (net 2))
 (segment (start 15 0) (end 15 20) (width 0.5) (locked yes) (layer "B.Cu") (net 2))
 (zone (net 3) (net_name "GND") (layer "In1.Cu") (hatch edge 0.5)
  (connect_pads (clearance 0.2)) (min_thickness 0.25)
  (polygon (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20))))
 (zone (net 4) (net_name "POWER") (layer "In2.Cu") (hatch edge 0.5)
  (connect_pads (clearance 0.2)) (min_thickness 0.25)
  (polygon (pts (xy 0 14) (xy 30 14) (xy 30 20) (xy 0 20))))
 (gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
)'''


def main():
    tm, work = sys.argv[1], pathlib.Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    src = work / "planes.kicad_pcb"
    src.write_text(BOARD)
    results = {}
    for name, extra in (("off", []), ("on", ["--protect-planes"]),
                        ("union", ["--protect-planes", "--no-tracks-on", "In1.Cu"])):
        out = work / (name + ".kicad_pcb")
        js, items = out.with_suffix(".json"), out.with_suffix(".items.json")
        p = subprocess.run([tm, "route", str(src), "-o", str(out), "--work", "3000000",
                            "--time", "3600", "--variants", "1", "--no-kb", "--no-gpu",
                            "--soft-zones", "--json", str(js), "--emit-items", str(items), *extra],
                           capture_output=True, text=True)
        assert p.returncode in (0, 3), (p.returncode, p.stdout, p.stderr)
        results[name] = json.loads(js.read_text()), json.loads(items.read_text()), p.stdout
    assert "protected_planes" not in results["off"][0], "off must preserve the default JSON contract"
    for name in ("on", "union"):
        summary, items, log = results[name]
        planes = summary["protected_planes"]
        assert len(planes) == 1 and planes[0]["layer"] == "In1.Cu", planes
        assert planes[0]["net"] == "GND" and abs(planes[0]["coverage"] - 1.0) < 1e-9, planes
        assert summary["routed"] == summary["connections"] == 1, summary
        assert not any(t["layer"] == "In1.Cu" for t in items["tracks"]), items
        assert any(t["layer"] == "In2.Cu" for t in items["tracks"]), items
        assert "protected planes: In1.Cu (GND, 100.0%)" in log, log
        assert log.count("no new tracks on: In1.Cu") == 1, log
    assert any(t["layer"] == "In1.Cu" for t in results["off"][1]["tracks"]), "fixture must expose the missing ban"
    print("protect_planes: ok (In1.Cu protected, 30% In2.Cu still routable, explicit ban deduplicated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
