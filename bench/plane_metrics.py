# SPDX-License-Identifier: GPL-3.0-or-later
"""Read plane ownership and routed copper without modifying a KiCad board.

Plane coverage is the union of a net's zone boundaries on one copper layer,
clipped to Edge.Cuts, divided by outline area; stale fills are never measured.
"""
import math
import pathlib
import re


def parse(text):
    # A balanced parser: escaped quotes and parentheses inside strings are data.
    tokens = re.finditer(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+', text)
    stack, current = [], []
    for match in tokens:
        token = match.group()
        if token == '(':
            stack.append(current)
            current = []
        elif token == ')':
            if not stack:
                raise ValueError('unbalanced board expression')
            node, current = current, stack.pop()
            current.append(node)
        else:
            if token.startswith('"'):
                token = re.sub(r'\\(.)', r'\1', token[1:-1])
            current.append(token)
    if stack or len(current) != 1:
        raise ValueError('unbalanced board expression')
    return current[0]


def children(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def child(node, key, default=None):
    return next(iter(children(node, key)), default)


def point(node, key):
    value = child(node, key)
    return float(value[1]), float(value[2])


def cross(a, b):
    return a[0] * b[1] - b[0] * a[1]


def arc_geometry(node, tessellate=False):
    start, end = point(node, 'start'), point(node, 'end')
    mid = child(node, 'mid')
    if mid is None:  # Legacy KiCad: start is the centre, end is the first point.
        centre, first = start, end
        sweep = math.radians(float(child(node, 'angle')[1]))
    else:
        first, middle = start, (float(mid[1]), float(mid[2]))
        ax, ay = first
        bx, by = middle
        cx, cy = end
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-12:
            raise ValueError('collinear arc')
        a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
        centre = ((a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d,
                  (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d)
        angles = [math.atan2(p[1] - centre[1], p[0] - centre[0]) for p in (first, middle, end)]
        sweep = (angles[2] - angles[0]) % math.tau
        if (angles[1] - angles[0]) % math.tau > sweep:
            sweep -= math.tau
    radius = math.dist(first, centre)
    angle = math.atan2(first[1] - centre[1], first[0] - centre[0])
    last = (centre[0] + radius * math.cos(angle + sweep), centre[1] + radius * math.sin(angle + sweep))
    if tessellate:
        # <= 1 micrometre sagitta, independent of the board's stale fill settings.
        step = 2 * math.acos(max(-1, 1 - 0.001 / radius)) if radius else math.pi
        count = max(1, math.ceil(abs(sweep) / step))
        return [(centre[0] + radius * math.cos(angle + sweep * i / count),
                 centre[1] + radius * math.sin(angle + sweep * i / count)) for i in range(count + 1)]
    # Exact Green's-theorem contribution, including the circular segment.
    integral = cross(centre, (last[0] - first[0], last[1] - first[1])) + radius * radius * sweep
    return first, last, integral, radius * abs(sweep)


def outline_loops(board):
    edges, loops = [], []
    for item in board[1:]:
        if not isinstance(item, list) or child(item, 'layer', ['', ''])[1] != 'Edge.Cuts':
            continue
        kind = item[0]
        if kind == 'gr_rect':
            a, b = point(item, 'start'), point(item, 'end')
            loops.append([a, (b[0], a[1]), b, (a[0], b[1])])
        elif kind == 'gr_circle':
            centre, end = point(item, 'center'), point(item, 'end')
            radius = math.dist(centre, end)
            count = max(12, math.ceil(math.pi / math.acos(max(-1, 1 - 0.001 / radius))))
            loops.append([(centre[0] + radius * math.cos(math.tau * i / count),
                           centre[1] + radius * math.sin(math.tau * i / count)) for i in range(count)])
        elif kind == 'gr_poly':
            loops.append([(float(p[1]), float(p[2])) for p in children(child(item, 'pts'), 'xy')])
        elif kind == 'gr_line':
            edges.append([point(item, 'start'), point(item, 'end')])
        elif kind == 'gr_arc':
            edges.append(arc_geometry(item, tessellate=True))
    while edges:
        points = edges.pop(0)
        while math.dist(points[0], points[-1]) > 0.01:
            for index, edge in enumerate(edges):
                if math.dist(edge[0], points[-1]) <= 0.01:
                    points.extend(edge[1:])
                    edges.pop(index)
                    break
                if math.dist(edge[-1], points[-1]) <= 0.01:
                    points.extend(edge[-2::-1])
                    edges.pop(index)
                    break
            else:
                raise ValueError('Edge.Cuts is not a closed outline')
        loops.append(points)
    if not loops:
        raise ValueError('no closed Edge.Cuts outline')
    return loops


def clipped_area(groups, outline):
    """Union area of polygon groups, clipped to an even-odd board outline.

    Vertex and edge-intersection heights divide the integral into linear slabs;
    midpoint integration is exact for straight edges (arcs are tessellated above).
    """
    def edges_of(loops):
        return [(a, b) for p in loops for a, b in zip(p, p[1:] + p[:1]) if a[1] != b[1]]

    grouped_edges, boundary = [edges_of(polygons) for polygons in groups], edges_of(outline)
    all_edges = [edge for edges in grouped_edges for edge in edges] + boundary
    ys = {p[1] for polygons in groups + [outline] for loop in polygons for p in loop}
    for i, (a, b) in enumerate(all_edges):
        for c, d in all_edges[i + 1:]:
            if max(min(a[1], b[1]), min(c[1], d[1])) >= min(max(a[1], b[1]), max(c[1], d[1])):
                continue
            ab, cd = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1])
            den = cross(ab, cd)
            if abs(den) < 1e-12:
                continue
            ac = (c[0] - a[0], c[1] - a[1])
            t, u = cross(ac, cd) / den, cross(ac, ab) / den
            if 0 < t < 1 and 0 < u < 1:
                ys.add(a[1] + t * ab[1])

    def intervals(items, y):
        xs = sorted(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                    for a, b in items if min(a[1], b[1]) < y < max(a[1], b[1]))
        return list(zip(xs[::2], xs[1::2]))

    heights, area = sorted(ys), 0.0
    for low, high in zip(heights, heights[1:]):
        y = (low + high) / 2
        merged = []
        for a, b in sorted(interval for edges in grouped_edges for interval in intervals(edges, y)):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        width = sum(max(0, min(b, d) - max(a, c)) for a, b in merged
                    for c, d in intervals(boundary, y))
        area += width * (high - low)
    return area


def plane_nets(path):
    board = parse(pathlib.Path(path).read_text())
    nets = {n[1]: n[2] for n in children(board, 'net')}
    def zone_name(zone):
        return child(zone, 'net_name', ['', nets.get(child(zone, 'net', ['', '0'])[1], '')])[1]
    zones = [z for z in children(board, 'zone') if child(z, 'keepout') is None
             and zone_name(z) and not any(child(attr, 'teardrop') for attr in children(z, 'attr'))]
    if not zones:
        return {}
    outline = outline_loops(board)
    area = clipped_area([outline], outline)
    if area <= 0:
        raise ValueError('nonpositive board outline area')
    planes = {}
    copper_names = {layer[1] for layer in child(board, 'layers', [])[1:]
                    if isinstance(layer, list) and layer[0].isdigit() and int(layer[0]) < 32}
    grouped_zones = {}
    for zone in zones:
        layer = child(zone, 'layer')
        layers = child(zone, 'layers')
        copper = [layer[1]] if layer else (layers[1:] if layers else [])
        copper = sorted(copper_names) if '*.Cu' in copper else [name for name in copper if name in copper_names]
        polygons = [[(float(p[1]), float(p[2])) for p in children(child(poly, 'pts'), 'xy')]
                    for poly in children(zone, 'polygon')]
        name = zone_name(zone)
        for copper_layer in copper:
            grouped_zones.setdefault((name, copper_layer), []).append(polygons)
    for (name, copper_layer), groups in sorted(grouped_zones.items()):
        coverage = clipped_area(groups, outline) / area
        if coverage >= 0.6:
            previous = planes.get(name, {'layers': [], 'coverage': 0, 'layer_coverage': {}})
            planes[name] = {'layers': sorted(previous['layers'] + [copper_layer]),
                            'coverage': round(max(previous['coverage'], coverage), 6),
                            'layer_coverage': {**previous['layer_coverage'], copper_layer: round(coverage, 6)}}
    return planes


def unconnected_split(items, planes):
    plane = 0
    for violation in items:
        # KiCad descriptions include the net in brackets on pads, vias and tracks.
        descriptions = [item.get('description', '') for item in violation.get('items', [])]
        plane += any(f'[{name}]' in description for name in planes for description in descriptions)
    return {'plane_unconnected': plane, 'signal_unconnected': len(items) - plane}


def copper_totals(path, planes):
    board = parse(pathlib.Path(path).read_text())
    names = {n[1]: n[2] for n in children(board, 'net')}
    totals = {'plane_vias': 0, 'signal_vias': 0, 'plane_track_mm': 0.0, 'signal_track_mm': 0.0}
    for item in board[1:]:
        if not isinstance(item, list) or item[0] not in ('segment', 'arc', 'via'):
            continue
        net = child(item, 'net', ['', '0'])[1]
        group = 'plane' if names.get(net) in planes else 'signal'
        if item[0] == 'via':
            totals[group + '_vias'] += 1
        else:
            length = arc_geometry(item)[3] if item[0] == 'arc' else math.dist(point(item, 'start'), point(item, 'end'))
            totals[group + '_track_mm'] += length
    return totals
