# SPDX-License-Identifier: GPL-3.0-or-later
"""Plane and via-placement metrics for one board, run with KiCad's Python (pcbnew); called by bench/quality.py.

  KICAD_PYTHON bench/plane_metrics_kicad.py BOARD.kicad_pcb [SMALL_PAD_MM]

Refills every zone with KiCad's own filler (in memory; the file is not written), then prints JSON:
  planes            per conductive zone covering >= 25 % of the board on a layer: net, layer, fill coverage
                    (filled area / outline area) and fill islands (filled polygons)
  unsupported_mm    signal track/arc length inside the board on a plane-adjacent layer with no filled reference
                    copper at the sample point (union of all planes on adjacent layers, any net), sampled at <= 0.1 mm
  plane_adjacent_mm sampled signal track/arc length inside the board on plane-adjacent layers (the denominator)
  vias, plane_vias  all vias, and foreign-net via antipads intersecting plane outlines, once per via/layer
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
outline = pcbnew.SHAPE_POLY_SET()
if not board.GetBoardPolygonOutlines(outline, False):
    raise RuntimeError("cannot measure reference support without a board outline")

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

def track_samples(track):
    """Midpoint quadrature along the actual straight or circular centreline."""
    a, b = track.GetStart(), track.GetEnd()
    length = track.GetLength()
    n = max(1, math.ceil(length / STEP))
    if track.Type() == pcbnew.PCB_ARC_T:
        c, m = track.GetCenter(), track.GetMid()
        start = math.atan2(a.y - c.y, a.x - c.x)
        mid = (math.atan2(m.y - c.y, m.x - c.x) - start) % math.tau
        end = (math.atan2(b.y - c.y, b.x - c.x) - start) % math.tau
        sweep = end if mid <= end else end - math.tau
        radius = track.GetRadius()
        for k in range(n):
            angle = start + sweep * (k + 0.5) / n
            yield pcbnew.VECTOR2I(round(c.x + radius * math.cos(angle)), round(c.y + radius * math.sin(angle))), pcbnew.ToMM(length) / n
    else:
        for k in range(n):
            yield pcbnew.VECTOR2I(round(a.x + (b.x - a.x) * (k + 0.5) / n),
                                 round(a.y + (b.y - a.y) * (k + 0.5) / n)), pcbnew.ToMM(length) / n


unsupported = adjacent = 0.0
for t in board.GetTracks():
    if t.Type() not in (pcbnew.PCB_TRACE_T, pcbnew.PCB_ARC_T) or t.GetNetCode() in plane_nets:
        continue
    layer = t.GetLayer()
    if layer not in stack:
        continue
    i = stack.index(layer)
    refs = [stack[j] for j in (i - 1, i + 1) if 0 <= j < len(stack) and stack[j] in plane_layers]
    reference_planes = [(z, pl) for z, pl in planes if pl in refs]
    if not reference_planes:
        continue
    for point, length_mm in track_samples(t):
        if not outline.Contains(point):
            continue
        adjacent += length_mm
        if not any(z.HitTestFilledArea(pl, point) for z, pl in reference_planes):
            unsupported += length_mm

vias = [t for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
plane_vias = sum(1 for v in vias for layer in sorted(plane_layers)
                 if v.IsOnLayer(layer) and any(pl == layer and v.GetNetCode() != z.GetNetCode()
                                              and z.Outline().Collide(v.GetEffectiveShape(layer), z.GetOwnClearance(layer))
                                              for z, pl in planes))

limit = pcbnew.FromMM(float(sys.argv[2]) if len(sys.argv) > 2 else 2.0)
small_pads = []
for p in board.GetPads():
    if p.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
        continue
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        if not p.IsOnLayer(layer):
            continue
        size = p.GetSize(layer)
        if size.x < limit and size.y < limit:
            small_pads.append((p, layer))
small_pad_vias = sum(1 for v in vias if any(v.IsOnLayer(layer) and p.GetEffectiveShape(layer).Collide(v.GetEffectiveShape(layer), 0)
                                            for p, layer in small_pads))
print(json.dumps({"planes": out_planes, "unsupported_mm": round(unsupported, 1), "plane_adjacent_mm": round(adjacent, 1),
                  "vias": len(vias), "plane_vias": plane_vias, "small_pad_vias": small_pad_vias}))
