# SPDX-License-Identifier: GPL-3.0-or-later
"""Plane and via-placement metrics for one board, run with KiCad's Python (pcbnew); called by bench/quality.py.

  KICAD_PYTHON bench/plane_metrics_kicad.py BOARD.kicad_pcb [SMALL_PAD_MM]

Refills every zone with KiCad's own filler (in memory; the file is not written), then prints JSON:
  planes            per conductive zone covering >= 25 % of the board on a layer: net, layer, fill coverage
                    (filled area / outline area) and fill islands (filled polygons)
  over_gap_mm       signal track length on a layer adjacent (in the copper stack) to a plane layer, sampled every
                    0.1 mm, where the point lies inside that plane's outline but not in its fill: a slot or void in
                    the reference plane under the track
  plane_adjacent_mm signal track length on layers adjacent to a plane layer (the denominator for over_gap_mm)
  vias, plane_vias  all vias, and crossings of a plane layer by a via of another net (each one an antipad in the
                    plane; a via of the plane's own net stitches it instead)
  small_pad_vias    vias touching an SMD pad smaller than SMALL_PAD_MM (default 2) in both dimensions: an untented
                    via there wicks solder away from the joint (IPC-4761 would fill and cap it)
"""
import json
import math
import sys

import pcbnew

STEP = pcbnew.FromMM(0.1)

board = pcbnew.LoadBoard(sys.argv[1])
pcbnew.ZONE_FILLER(board).Fill(board.Zones())
edge = board.GetBoardEdgesBoundingBox()
board_area = float(edge.GetWidth()) * float(edge.GetHeight())
stack = list(board.GetEnabledLayers().CuStack())

planes = []  # (zone, layer)
for z in board.Zones():
    if z.GetIsRuleArea() or z.GetNetCode() <= 0 or z.IsTeardropArea():
        continue
    for layer in z.GetLayerSet().CuStack():
        if z.Outline().Area() >= 0.25 * board_area:
            planes.append((z, layer))
plane_layers = {layer for _, layer in planes}
plane_nets = {z.GetNetCode() for z, _ in planes}

out_planes = []
for z, layer in planes:
    fill = z.GetFilledPolysList(layer)
    out_planes.append({"net": z.GetNetname(), "layer": board.GetLayerName(layer),
                       "coverage": round(fill.Area() / z.Outline().Area(), 4) if z.Outline().Area() else None,
                       "islands": fill.OutlineCount()})

over_gap = adjacent = 0.0
for t in board.GetTracks():
    if t.Type() != pcbnew.PCB_TRACE_T or t.GetNetCode() in plane_nets:
        continue
    layer = t.GetLayer()
    if layer not in stack:
        continue
    i = stack.index(layer)
    refs = [stack[j] for j in (i - 1, i + 1) if 0 <= j < len(stack) and stack[j] in plane_layers]
    if not refs:
        continue
    a, b = t.GetStart(), t.GetEnd()
    length = math.hypot(b.x - a.x, b.y - a.y)
    n = max(1, int(length // STEP))
    adjacent += pcbnew.ToMM(length)
    for k in range(n):
        p = pcbnew.VECTOR2I(int(a.x + (b.x - a.x) * (k + 0.5) / n), int(a.y + (b.y - a.y) * (k + 0.5) / n))
        for z, pl in planes:
            if pl in refs and z.Outline().Contains(p) and not z.HitTestFilledArea(pl, p):
                over_gap += pcbnew.ToMM(length) / n
                break

vias = [t for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
plane_vias = sum(1 for v in vias for z, layer in planes if v.IsOnLayer(layer) and v.GetNetCode() != z.GetNetCode())

limit = pcbnew.FromMM(float(sys.argv[2]) if len(sys.argv) > 2 else 2.0)
small_pads = []
for p in board.GetPads():
    if p.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
        continue
    layer = pcbnew.F_Cu if p.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu
    size = p.GetSize(layer)
    if size.x < limit and size.y < limit:
        small_pads.append((p, layer))
small_pad_vias = sum(1 for v in vias if any(v.IsOnLayer(layer) and p.GetEffectiveShape(layer).Collide(v.GetEffectiveShape(layer), 0)
                                            for p, layer in small_pads))
print(json.dumps({"planes": out_planes, "over_gap_mm": round(over_gap, 1), "plane_adjacent_mm": round(adjacent, 1),
                  "vias": len(vias), "plane_vias": plane_vias, "small_pad_vias": small_pad_vias}))
