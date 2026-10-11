# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure geometric signal-integrity kernels for benchmark scorecards.

Ported from the SignalIntegrityReview reviewer measurement prototype (2026-10).
Inspect coordinates are nanometres; returned lengths and Shapely geometry are
millimetres. Coupling is a parallel-centreline proxy, not an impedance or field
solver. Return coverage uses adjacent broad ground layers, not stackup dielectric
properties. Board readers never refill zones: the caller must supply a refilled
board. They use actual filled polygons, never zone outlines as copper.
"""

import collections
import hashlib
import math
import re
from pathlib import Path

import plane_metrics as pm
from intent_quality import NetGraph
from shapely.geometry import GeometryCollection, LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

_GROUND = re.compile(r"(?:/?GND|GNDD|DGND|AGND)", re.I)
_COPPER = re.compile(r"(?:F|B|In\d+)\.Cu")


def pairs(d):
    """Return prototype name-inferred differential pairs in stable name order.

    This deliberately excludes low-speed analog P/N names; it is not schematic
    intent detection and cannot establish that a pair requires controlled impedance.
    """
    names = set(d["nets"])
    out = []
    for n in sorted(names):
        if n.startswith("unconnected"):
            continue
        mate = None
        if n.endswith("+"):
            mate = n[:-1] + "-"
        elif n.endswith("_P"):
            mate = n[:-1] + "N"
        elif n.endswith("DP"):
            mate = n[:-1] + "M"
        elif n.endswith("D_P"):
            mate = n[:-1] + "N"
        if mate in names and re.search(
            r"USB|PCIE|CSI|DSI|LVDS|GMSL|HDMI|ETH|DDR|SATA|SGMII|DP_|D[+-]$|PAIR_", n, re.I
        ):
            out.append((n, mate))
    return out


def pdata(d, n):
    """Return two-terminal connected-net data, or None when inapplicable.

    Prototype semantics: NetGraph checks connectivity (arcs approximated as two
    chords, no zone copper); length sums all net tracks and exact circular arcs,
    not the shortest pad path. Duplicate pad instances share a logical terminal.
    """
    pads = [p for p in d["pads"] if p["net"] == n]
    unique = {(p["ref"], p["num"]): p for p in pads}
    if len(unique) != 2:
        return None
    keys = sorted(unique)
    graph = NetGraph(d, n)
    first, last = graph.pad_index(*keys[0]), graph.pad_index(*keys[1])
    if graph.shortest(("p", first), lambda node: node == ("p", last)) is None:
        return None
    return {"length": exact_copper_length(d, n), "vias": len(graph.vias), "pads": keys}


def _arc_node(a):
    return [
        "arc",
        ["start", str(a["sx"] / 1e6), str(a["sy"] / 1e6)],
        ["mid", str(a["mx"] / 1e6), str(a["my"] / 1e6)],
        ["end", str(a["ex"] / 1e6), str(a["ey"] / 1e6)],
    ]


def exact_copper_length(d, n):
    """Sum prototype planar track/arc lengths in mm, excluding via barrels."""
    length = sum(
        math.hypot(t["ex"] - t["sx"], t["ey"] - t["sy"]) / 1e6
        for t in d["tracks"] if t["net"] == n
    )
    for arc in d.get("arcs", []):
        if arc["net"] == n:
            length += pm.arc_geometry(_arc_node(arc))[3]
    return length


def segs(d, n):
    """Return prototype mm segments, tessellating arcs via plane_metrics.

    The shared tessellator uses <=0.001 mm sagitta (the prototype called this
    same helper); lengths here are chord lengths, unlike exact_copper_length.
    """
    out = []
    for track in d["tracks"]:
        if track["net"] != n:
            continue
        segment = dict(track)
        segment["len"] = math.hypot(track["ex"] - track["sx"], track["ey"] - track["sy"]) / 1e6
        for key in ("sx", "sy", "ex", "ey", "width"):
            segment[key] /= 1e6
        if segment["len"] > 1e-7:
            out.append(segment)
    for arc in d.get("arcs", []):
        if arc["net"] != n:
            continue
        points = pm.arc_geometry(_arc_node(arc), True)
        for p, q in zip(points, points[1:]):
            out.append({
                "sx": p[0], "sy": p[1], "ex": q[0], "ey": q[1],
                "width": arc["width"] / 1e6, "layer": arc["layer"], "len": math.dist(p, q),
            })
    return out


def _overlaps(s, t):
    if s["layer"] != t["layer"]:
        return None
    ux, uy = (s["ex"] - s["sx"]) / s["len"], (s["ey"] - s["sy"]) / s["len"]
    vx, vy = (t["ex"] - t["sx"]) / t["len"], (t["ey"] - t["sy"]) / t["len"]
    if abs(ux * vy - uy * vx) > math.sin(math.radians(5)):
        return None
    # Preserve the prototype's stricter parallel test: crossings do not couple.
    if abs(ux * vy - uy * vx) > 1e-4:
        return None
    dx, dy = t["sx"] - s["sx"], t["sy"] - s["sy"]
    gap = abs(dx * uy - dy * ux) - (s["width"] + t["width"]) / 2
    a = dx * ux + dy * uy
    b = (t["ex"] - s["sx"]) * ux + (t["ey"] - s["sy"]) * uy
    lo, hi = max(0, min(a, b)), min(s["len"], max(a, b))
    return (lo, hi, gap) if hi > lo and gap >= -0.005 else None


def target_gap(sa, sb):
    """Prototype length-weighted 0.025 mm gap mode, or None if unobserved.

    Only edge gaps in [0, 0.5] mm count; ties select the smallest gap bin.
    """
    contributions = collections.defaultdict(list)
    order = lambda s: (s["layer"], s["sx"], s["sy"], s["ex"], s["ey"], s["width"], s["len"])
    for s in sorted(sa, key=order):
        for t in sorted(sb, key=order):
            overlap = _overlaps(s, t)
            if overlap and 0 <= overlap[2] <= 0.5:
                contributions[round(overlap[2] / 0.025) * 0.025].append(overlap[1] - overlap[0])
    histogram = {gap: math.fsum(lengths) for gap, lengths in contributions.items()}
    return min(histogram, key=lambda gap: (-histogram[gap], gap)) if histogram else None


def coupled(sa, sb, gap):
    """Return (coupled mm, total mm) with prototype interval-union semantics.

    Tolerance is max(0.025 mm, 20% of gap); both legs contribute to the total.
    An unknown target gives unknown coupled length, not zero coupling.
    """
    total = sum(s["len"] for s in sa) + sum(s["len"] for s in sb)
    if gap is None:
        return None, total
    good = 0
    for aa, bb in ((sa, sb), (sb, sa)):
        for s in aa:
            intervals = []
            for t in bb:
                overlap = _overlaps(s, t)
                if overlap and abs(overlap[2] - gap) <= max(0.025, 0.2 * gap):
                    intervals.append(overlap[:2])
            end = -1
            for lo, hi in sorted(intervals):
                good += max(0, hi - max(lo, end))
                end = max(end, hi)
    return good, total


def _canonical_layers(root):
    aliases = {}
    for entry in pm.child(root, "layers", [])[1:]:
        if not isinstance(entry, list) or len(entry) < 2 or not entry[0].isdigit():
            continue
        ordinal, name = int(entry[0]), entry[1]
        if _COPPER.fullmatch(name):
            aliases[name] = name
        elif ordinal == 0:
            aliases[name] = "F.Cu"
        elif ordinal == 31:
            aliases[name] = "B.Cu"
        elif 0 < ordinal < 31:
            aliases[name] = f"In{ordinal}.Cu"
    return aliases


def _zone_name(zone, names):
    name = pm.child(zone, "net_name")
    if name is not None:
        return name[1]
    net = pm.child(zone, "net")
    if net is None or len(net) < 2:
        return ""
    if len(net) > 2:
        return net[2]
    return names.get(net[1], "") if net[1].isdigit() else net[1]


def _points(polygon):
    return [(float(p[1]), float(p[2])) for p in pm.children(pm.child(polygon, "pts", []), "xy")]


def _even_odd(loops):
    """Build even-odd copper, including holes bridged into one KiCad contour.

    buffer(0) alone can discard self-intersecting contour lobes. Noding and
    polygonizing boundaries, then selecting odd-parity faces, preserves them.
    """
    loops = [p for p in loops if len(p) >= 3]
    if not loops:
        return GeometryCollection()
    if len(loops) == 1:
        polygon = Polygon(loops[0])
        if polygon.is_valid:
            return polygon
    boundaries = unary_union([LineString(p + p[:1]) for p in loops])
    faces = []
    for face in polygonize(boundaries):
        point = face.representative_point()
        x, y = point.x, point.y
        odd = False
        for loop in loops:
            for (x0, y0), (x1, y1) in zip(loop, loop[1:] + loop[:1]):
                if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                    odd = not odd
        if odd:
            faces.append(face)
    return unary_union(faces)


def _read_fills(board, ground_only=False):
    root = pm.parse(Path(board).read_text())
    aliases = _canonical_layers(root)
    names = {n[1]: n[2] for n in pm.children(root, "net") if len(n) > 2}
    groups = collections.defaultdict(list)
    outlines = collections.defaultdict(list)
    raw = []
    for zone in pm.children(root, "zone"):
        if pm.child(zone, "keepout") is not None:
            continue
        name = _zone_name(zone, names)
        if not name or (ground_only and not _GROUND.fullmatch(name)):
            continue
        layer, layers = pm.child(zone, "layer"), pm.child(zone, "layers")
        zone_layers = [layer[1]] if layer else (layers[1:] if layers else [])
        if "*.Cu" in zone_layers:
            zone_layers = sorted(set(aliases.values()))
        else:
            zone_layers = [aliases.get(l, l) for l in zone_layers if l in aliases or _COPPER.fullmatch(l)]
        if ground_only:
            boundary = _even_odd([_points(p) for p in pm.children(zone, "polygon")])
            for l in zone_layers:
                outlines[l].append(boundary)
        filled = collections.defaultdict(list)
        for polygon in pm.children(zone, "filled_polygon"):
            polygon_layer = pm.child(polygon, "layer")
            # Legacy single-layer fills omit layer; a multilayer fill without
            # ownership cannot safely be assigned to every layer.
            l = polygon_layer[1] if polygon_layer else (zone_layers[0] if len(zone_layers) == 1 else "")
            l = aliases.get(l, l)
            if not _COPPER.fullmatch(l):
                continue
            points = _points(polygon)
            if len(points) >= 3:
                filled[l].append(points)
                raw.append((name, l, points))
        for l, loops in filled.items():
            groups[(name, l)].append(_even_odd(loops))
    unions = {key: unary_union(groups[key]) for key in sorted(groups)}
    return root, unions, outlines, raw


def all_fills(board):
    """Return {(net, canonical layer): filled Shapely copper in mm}.

    Supports legacy numeric nets/layer aliases and KiCad 10 named nets. Holes
    follow even-odd contour parity per zone/layer; distinct zones are unioned.
    Missing/unfilled zones have no entry, rather than an outline-based fallback.
    Caller owns refill freshness and may cache this read-only result.
    """
    return _read_fills(board)[1]


def fills(board):
    """Return (ground fills by layer, broad reference layers, fill hash).

    Prototype ground-name whitelist and >=60% outline-area threshold are kept.
    The threshold uses zone boundaries, but measured copper uses only fills.
    Invalid/missing Edge.Cuts raises ValueError rather than inventing coverage.
    """
    root, net_fills, outlines, raw = _read_fills(board, ground_only=True)
    grouped = collections.defaultdict(list)
    for (_, layer), geometry in net_fills.items():
        grouped[layer].append(geometry)
    unions = {layer: unary_union(grouped[layer]) for layer in sorted(grouped)}
    outline_loops = pm.outline_loops(root)
    area = pm.clipped_area([outline_loops], outline_loops)
    broad = {l for l, polygons in outlines.items() if unary_union(polygons).area >= 0.6 * area} if area > 0 else set(outlines)
    return unions, broad, hashlib.sha256(repr(raw).encode()).hexdigest()


def retmetric(d, nets, uf, broad, *, isolated=()):
    """Measure prototype adjacent-plane return gaps and stitching distances.

    Segment centres, not track edges, determine gaps. A layer transition counts
    only when touched signal layers have disjoint adjacent broad ground layers.
    Stitching requires a ground via spanning and touching every needed fill;
    the 1 mm and 5.08 mm thresholds are geometric proxies, not SI sign-off.

    Gap values describe eligible copper only. No eligible reference gives None
    and valid=False; partial coverage reports unknown_mm and validity='partial'.
    Transitions with missing reference/fill data are unknown, not failures.
    Counts describe known events; transitions_valid exposes their completeness.
    """
    total = eligible = gap = unknown = 0
    events = bad1 = bad5 = unknown_transitions = 0
    max_gap = 0
    layers = d["copper_layers"]
    indices = {l: i for i, l in enumerate(layers)}
    reference_layers = {}
    reference_unions = {}
    for l, i in indices.items():
        refs = [layers[j] for j in (i - 1, i + 1) if 0 <= j < len(layers) and layers[j] in broad]
        reference_layers[l] = refs
        if refs and all(r in uf for r in refs):
            reference_unions[l] = unary_union([uf[r] for r in refs])
    ground_vias = []
    for via in d["vias"]:
        if not _GROUND.fullmatch(via["net"]) or via["net"] in isolated:
            continue
        top, bottom = indices.get(via["top"]), indices.get(via["bottom"])
        if top is None or bottom is None:
            continue
        span = set(layers[min(top, bottom):max(top, bottom) + 1])
        disk = Point(via["x"] / 1e6, via["y"] / 1e6).buffer(via["size"] / 2e6 + 0.001)
        touching = {l for l in span if l in uf and uf[l].intersects(disk)}
        ground_vias.append((via, touching))
    for n in sorted(nets):
        if n in isolated:
            continue
        segments = segs(d, n)
        lines = [LineString([(s["sx"], s["sy"]), (s["ex"], s["ey"])]) for s in segments]
        for s, line in zip(segments, lines):
            total += s["len"]
            reference = reference_unions.get(s["layer"])
            if reference is None:
                unknown += s["len"]
                continue
            eligible += s["len"]
            uncovered = line.difference(reference)
            gap += uncovered.length
            pieces = list(uncovered.geoms) if hasattr(uncovered, "geoms") else [uncovered]
            max_gap = max([max_gap] + [piece.length for piece in pieces])
        for via in (v for v in d["vias"] if v["net"] == n):
            point = Point(via["x"] / 1e6, via["y"] / 1e6)
            touched = {s["layer"] for s, line in zip(segments, lines)
                       if line.distance(point) <= (via["size"] / 1e6 + s["width"]) / 2 + 0.001}
            if len(touched) < 2:
                continue
            refs = [set(reference_layers.get(l, [])) for l in sorted(touched)]
            if any(not r for r in refs):
                unknown_transitions += 1
                continue
            if set.intersection(*refs):
                continue
            needed = set.union(*refs)
            if any(l not in uf for l in needed):
                unknown_transitions += 1
                continue
            events += 1
            distances = [math.hypot(via["x"] - gv["x"], via["y"] - gv["y"]) / 1e6
                         for gv, touching in ground_vias if needed <= touching]
            nearest = min(distances, default=math.inf)
            bad1 += nearest > 1
            bad5 += nearest > 5.08
    valid = eligible > 0
    validity = "no_signal_copper" if total == 0 else "no_reference" if not valid else "partial" if unknown > 0 else "complete"
    return {
        "total_mm": total, "eligible_mm": eligible,
        "gap_mm": gap if valid else None, "unknown_mm": unknown,
        "max_gap_mm": max_gap if valid else None,
        "transitions": events if valid else None,
        "no_return_1mm": bad1 if valid else None,
        "no_return_5_08mm": bad5 if valid else None,
        "valid": valid, "validity": validity,
        "transitions_valid": valid and unknown_transitions == 0,
        "unknown_transitions": unknown_transitions,
    }
