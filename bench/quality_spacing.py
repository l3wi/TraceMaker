# SPDX-License-Identifier: GPL-3.0-or-later
"""Guide R36–39 spacing screens; nanometre topology, deterministic STRtrees.

Straight runs coalesce degree-two collinear equal-width pieces. A non-local
site is an unordered same-layer run pair without a shared joint/pad/via and
without an endpoint-to-endpoint centreline path of <=2*max(width). One run pair
is one site (not each file segment). This deliberately screens remote folds,
not the vanishing clearance at an ordinary connected corner. Circular arcs are
one geometric run; their tessellation vertices never become bend segments.
"""
import heapq
import math
from collections import defaultdict

from shapely.geometry import LineString, Point, box
from shapely.affinity import rotate
from shapely.strtree import STRtree

import quality_craft as craft
import quality_rules as rules
import quality_si as si
from intent_quality import in_pad


def signal(net):
    return bool(net) and not net.startswith('unconnected') and not craft.is_power(net) and not craft.is_ground(net)


def _runs(d, net):
    pieces = set()
    for t in d.get('tracks', []):
        if t['net'] == net:
            a, b = sorted(((t['sx'], t['sy']), (t['ex'], t['ey'])))
            if a != b:
                pieces.add((t['layer'], t['width'], a, b))
    protected = {(a['layer'], (a[k+'x'], a[k+'y'])) for a in d.get('arcs', []) if a['net'] == net
                 and all(key in a for key in ('layer', 'sx', 'sy', 'ex', 'ey')) for k in ('s', 'e')}
    # Coalescing depends only on geometric identities, never UUIDs/file order.
    while True:
        joints = defaultdict(list)
        for r in sorted(pieces):
            for p in r[2:]:
                joints[r[0], p].append(r)
        merged = False
        for (layer, p), touching in sorted(joints.items()):
            if len(touching) != 2 or (layer, p) in protected:
                continue
            a, b = touching
            u, v = next(q for q in a[2:] if q != p), next(q for q in b[2:] if q != p)
            if a[1] == b[1] and (u[0]-p[0])*(v[1]-p[1]) == (u[1]-p[1])*(v[0]-p[0]) and u != v:
                pieces.remove(a)
                pieces.remove(b)
                pieces.add((a[0], a[1], *sorted((u, v))))
                merged = True
                break
        if not merged:
            return sorted(pieces)


def _swallowed(d, net, layer, point):
    x, y = point
    for p in d.get('pads', []):
        if p['net'] == net and layer in p['layers'] and in_pad(p, x, y, 0):
            return True
    layers = d.get('copper_layers', [])
    for v in d.get('vias', []):
        if v['net'] != net:
            continue
        lo, hi = sorted((layers.index(v['top']), layers.index(v['bottom'])))
        if layer in layers[lo:hi+1] and math.dist(point, (v['x'], v['y'])) <= v['size']/2:
            return True
    return False


def _joint_copper(d, net, a, b):
    if set(a[2:]) & set(b[2:]):
        return True
    for p in d.get('pads', []):
        if p['net'] == net and a[0] in p['layers'] and any(in_pad(p, *q, 0) for q in a[2:]) and any(in_pad(p, *q, 0) for q in b[2:]):
            return True
    layers = d.get('copper_layers', [])
    for v in d.get('vias', []):
        if v['net'] != net:
            continue
        lo, hi = sorted((layers.index(v['top']), layers.index(v['bottom'])))
        if a[0] in layers[lo:hi+1] and all(any(math.dist(q, (v['x'], v['y'])) <= v['size']/2 for q in r[2:]) for r in (a, b)):
            return True
    return False


def net_spacing(d, net, *, indexed=True):
    """Counts and minimum ratios; indexed=False is the linear reference path."""
    unknown = None
    straight = _runs(d, net)
    entries = [(r, LineString(r[2:]), math.dist(r[2], r[3]), {}) for r in straight]
    for arc in d.get('arcs', []):
        if arc['net'] != net:
            continue
        geometry = _arc_line(arc)
        if geometry is None:
            unknown = 'invalid circular arc geometry'
            continue
        first, last = sorted(((arc['sx'], arc['sy']), (arc['ex'], arc['ey'])))
        entries.append(((arc['layer'], arc['width'], first, last), *geometry))
    physical = {(r, line.wkb_hex): (r, line, length, tangent) for r, line, length, tangent in entries}
    entries = [physical[key] for key in sorted(physical)]
    runs = [entry[0] for entry in entries]
    d = dict(d, pads=[p for p in d.get('pads', []) if p['net'] == net],
             vias=[v for v in d.get('vias', []) if v['net'] == net])
    if any(r[1] <= 0 for r in runs):
        unknown = 'nonpositive track width'
    if unknown:
        return dict(self_spacing_violations=None, short_bend_segments=None, self_spacing_ratio=None,
                    bend_segment_ratio=None, reason=unknown)
    if not runs:
        return dict(self_spacing_violations=None, short_bend_segments=None, self_spacing_ratio=None,
                    bend_segment_ratio=None, reason='no represented signal tracks')
    lines = [entry[1] for entry in entries]
    graph = defaultdict(list)
    joints = defaultdict(list)
    for i, r in enumerate(runs):
        a, b = (r[0], r[2]), (r[0], r[3])
        length = entries[i][2]
        graph[a].append((b, length))
        graph[b].append((a, length))
        joints[a].append(i)
        joints[b].append(i)
    near = {}
    max_width = max(r[1] for r in runs)
    for node in sorted(graph):
        dist, queue = {node: 0}, [(0, node)]
        while queue:
            cost, p = heapq.heappop(queue)
            if cost != dist[p]:
                continue
            for q, length in sorted(graph[p]):
                value = cost+length
                if value <= 2*max_width and value < dist.get(q, math.inf):
                    dist[q] = value
                    heapq.heappush(queue, (value, q))
        near[node] = dist
    def eligible(i, j):
        a, b = runs[i], runs[j]
        return a[0] == b[0] and lines[i].distance(lines[j]) > (a[1]+b[1])/2 and not _joint_copper(d, net, a, b) and not any(
            near[a[0], p].get((b[0], q), math.inf) <= 2*max(a[1], b[1]) for p in a[2:] for q in b[2:])
    tree = STRtree(lines) if indexed else None
    # Radius sufficient for every <4W site; expand only to determine the minimum.
    bounds = (min(line.bounds[0] for line in lines), min(line.bounds[1] for line in lines),
              max(line.bounds[2] for line in lines), max(line.bounds[3] for line in lines))
    extent = math.hypot(bounds[2]-bounds[0], bounds[3]-bounds[1])+max_width
    ratios, violations = [], 0
    for i, (r, line) in enumerate(zip(runs, lines)):
        radius = 5*max_width
        checked, best = set(), None
        while True:
            candidates = sorted(int(j) for j in tree.query(box(line.bounds[0]-radius, line.bounds[1]-radius,
                line.bounds[2]+radius, line.bounds[3]+radius))) if tree is not None else range(i+1, len(runs))
            for j in candidates:
                if j <= i or j in checked:
                    continue
                checked.add(j)
                if not eligible(i, j):
                    continue
                width = max(r[1], runs[j][1])
                gap = max(0, line.distance(lines[j])-(r[1]+runs[j][1])/2)
                ratio = gap/width
                best = ratio if best is None else min(best, ratio)
                violations += ratio < 4
            if tree is None or radius >= extent or (best is not None and radius >= (best+1)*max_width):
                break
            radius = min(extent, radius*2)
        if best is not None:
            ratios.append(best)
    bends = []
    def direction_change(i, p):
        joined = joints[runs[i][0], p]
        if len(joined) != 2:
            return False
        vectors = []
        for j in joined:
            if entries[j][3]:
                vectors.append(entries[j][3][p])
            else:
                q = next(q for q in runs[j][2:] if q != p)
                vectors.append((q[0]-p[0], q[1]-p[1]))
        a, b = vectors
        cross = abs(a[0]*b[1]-a[1]*b[0])
        tolerance = 1e-6 if any(entries[j][3] for j in joined) else 0
        return cross > tolerance*math.hypot(*a)*math.hypot(*b) or a[0]*b[0]+a[1]*b[1] >= 0
    for i, r in enumerate(runs):
        if not entries[i][3] and all(direction_change(i, p) and not _swallowed(d, net, r[0], p) for p in r[2:]):
            bends.append(math.dist(r[2], r[3])/r[1])
    return dict(self_spacing_violations=violations, short_bend_segments=sum(v < 1.5 for v in bends),
                self_spacing_ratio=min(ratios, default=None), bend_segment_ratio=min(bends, default=None),
                ratio_band=1000/min(r[1] for r in runs),
                nonlocal_runs_observed=len(ratios), straight_runs=len(straight), arc_runs=sum(bool(e[3]) for e in entries), reason=None)


def measure(d, complete):
    """New parity fields only for known-connected non-supply, non-ground nets."""
    return {n: net_spacing(d, n) for n in sorted(complete) if complete[n] is True and signal(n)}


def _buffer(geometry, radius):
    # <=100 nm sagitta per round primitive, below the 1 um verdict band.
    count = max(128, math.ceil(math.pi/(4*math.acos(max(-1, 1-100/radius)))))
    return geometry.buffer(radius, quad_segs=count)


def _pad_shape(p, meta):
    shape = meta.get(p['ref']+'.'+p['num'], {}).get('shape', p.get('shape'))
    w, h = p['w'], p['h']
    if w <= 0 or h <= 0:
        return None
    if shape == 'circle':
        return _buffer(Point(p['x'], p['y']), w/2) if w == h else None
    if shape == 'oval':
        a, b = (w-h)/2, (h-w)/2
        g = _buffer(LineString([(-max(a, 0), -max(b, 0)), (max(a, 0), max(b, 0))]), min(w, h)/2)
    elif shape == 'rect':
        g = box(-w/2, -h/2, w/2, h/2)
    elif shape == 'roundrect':
        ratio = meta.get(p['ref']+'.'+p['num'], {}).get('rr', p.get('rr'))
        if ratio is None or not 0 <= ratio <= 0.5:
            return None
        radius = min(w, h)*ratio
        g = _buffer(box(-w/2+radius, -h/2+radius, w/2-radius, h/2-radius), radius) if radius else box(-w/2, -h/2, w/2, h/2)
    else:
        return None
    from shapely.affinity import translate
    return translate(rotate(g, -p.get('angle', 0), origin=(0, 0)), p['x'], p['y'])


def _arc_line(arc):
    """Three-point circle, as plane_metrics.arc_geometry; <=100 nm sagitta.

    Work relative to the canonical first endpoint to avoid cancellation from
    board-origin offsets. Tessellation is copper geometry, never bend vertices.
    """
    if not all(key in arc for key in ('sx', 'sy', 'mx', 'my', 'ex', 'ey', 'width', 'layer')):
        return None
    first, last = sorted(((arc['sx'], arc['sy']), (arc['ex'], arc['ey'])))
    if arc['width'] <= 0:
        return None
    canonical = dict(arc, sx=first[0], sy=first[1], ex=last[0], ey=last[1])
    try:
        circle = si._arc_parameters(canonical)
    except ValueError:
        return None
    radius = circle['radius']*1e6
    centre = circle['cx']*1e6-first[0], circle['cy']*1e6-first[1]
    end = last[0]-first[0], last[1]-first[1]
    sweep, start = circle['sweep'], circle['start']
    count = max(1, math.ceil(abs(sweep)/(2*math.acos(max(-1, 1-100/radius)))))
    points = [(first[0]+centre[0]+radius*math.cos(start+sweep*i/count),
               first[1]+centre[1]+radius*math.sin(start+sweep*i/count)) for i in range(count+1)]
    points[0], points[-1] = first, last
    sign = 1 if sweep > 0 else -1
    tangents = {first: (centre[1]*sign, -centre[0]*sign),
                last: ((end[1]-centre[1])*sign, -(end[0]-centre[0])*sign)}
    return LineString(points), radius*abs(sweep), tangents


def _arc_shape(arc):
    geometry = _arc_line(arc)
    return _buffer(geometry[0], arc['width']/2) if geometry is not None else None


def _copper(d, fills, meta):
    """Stable (net, layer, physical identity, geometry) records in nm."""
    rows, unknown = [], set()
    for t in d.get('tracks', []):
        a, b = sorted(((t['sx'], t['sy']), (t['ex'], t['ey'])))
        if t['width'] <= 0:
            unknown.add(t['net'])
            continue
        g = _buffer(LineString([a, b]), t['width']/2)
        rows.append((t['net'], t['layer'], ('track', a, b, t['width']), g))
    for a in d.get('arcs', []):
        g = _arc_shape(a)
        if g is None:
            unknown.add(a['net'])
            continue
        first, last = sorted(((a['sx'], a['sy']), (a['ex'], a['ey'])))
        rows.append((a['net'], a['layer'], ('arc', first, last, (a['mx'], a['my']), a['width']), g))
    layers = d.get('copper_layers', [])
    for v in d.get('vias', []):
        if v['size'] <= 0:
            unknown.add(v['net'])
            continue
        lo, hi = sorted((layers.index(v['top']), layers.index(v['bottom'])))
        for layer in layers[lo:hi+1]:
            rows.append((v['net'], layer, ('via', v['x'], v['y'], v['size']), _buffer(Point(v['x'], v['y']), v['size']/2)))
    for p in d.get('pads', []):
        g = _pad_shape(p, meta)
        if g is None:
            unknown.add(p['net'])
            continue
        for layer in p['layers']:
            rows.append((p['net'], layer, ('pad', p['x'], p['y'], p['w'], p['h'], p.get('angle', 0), g.wkb_hex, p['ref']), g))
    from shapely.affinity import scale
    for (net, layer), g in sorted(fills.items()):
        rows.append((net, layer, ('fill', g.wkb_hex), scale(g, 1e6, 1e6, origin=(0, 0))))
    return sorted(rows, key=lambda r: r[:3]), unknown


def _lane_nets(iface, lane):
    return set().union(*(rules.series_nets(net, iface.get('series', []))
                         for net in (lane.get('p'), lane.get('n')) if net))


def pair_spacing(d, interfaces, ctx, *, indexed=True):
    """Pair-to-pair/other-net minima, exact polygon distances after broad phase.

    Optional interface breakout_radius_nm freezes endpoint disks. Only pads on
    shared physical connectors (matching ref among both lanes' endpoint pads)
    inside BOTH lanes' disks are excluded. Tracks and vias are never excluded.
    Round copper uses >=128 chords/quadrant with <=100nm sagitta, below the
    1um verdict band. Returns the excluded pad-comparison count, not hidden zeros.
    """
    meta = ctx.get('pad_metadata')
    if meta is None:
        meta = craft.metadata(ctx['board_path']) if ctx.get('board_path') else {}
    rows, unknown = _copper(d, ctx.get('fills', {}), meta)
    all_lanes = [(f, l) for f in interfaces.get('interfaces', []) for l in f.get('lanes', [])]
    all_lanes.sort(key=lambda x: (x[0]['id'], x[1]['id']))
    lanes = all_lanes
    declared = {n for f, l in all_lanes for n in _lane_nets(f, l)}
    def connector_refs(l):
        return {p['ref'] for p in d.get('pads', []) if any(p['net'] == e['net'] and (p['x'], p['y']) == (e['x_nm'], e['y_nm']) for e in l.get('endpoints', []))}
    trees = {layer: STRtree([r[3] for r in rows if r[1] == layer]) for layer in sorted({r[1] for r in rows})}
    layer_rows = {layer: [r for r in rows if r[1] == layer] for layer in trees}
    out = {}
    extents = {}
    for layer, pool in layer_rows.items():
        bounds = [r[3].bounds for r in pool]
        extents[layer] = math.hypot(max(b[2] for b in bounds)-min(b[0] for b in bounds),
                                   max(b[3] for b in bounds)-min(b[1] for b in bounds))+1
    for iface, lane in lanes:
        nets = _lane_nets(iface, lane)
        own = [r for r in rows if r[0] in nets]
        others = declared-nets
        signal_others = {n for n in d.get('nets', []) if signal(n)}-nets
        gaps, excluded, pair_site = {}, 0, None
        def excluded_pad(r, s):
            if r[1] != s[1] or r[2][0] != 'pad' or s[2][0] != 'pad' or r[2][-1] != s[2][-1]:
                return False
            for f, l in all_lanes:
                if s[0] not in _lane_nets(f, l) or r[2][-1] not in connector_refs(l) & connector_refs(lane):
                    continue
                ra, rb = iface.get('breakout_radius_nm', 0), f.get('breakout_radius_nm', 0)
                if ra > 0 and rb > 0 and any(math.dist(r[2][1:3], (e['x_nm'], e['y_nm'])) <= ra for e in lane.get('endpoints', [])) and any(math.dist(s[2][1:3], (e['x_nm'], e['y_nm'])) <= rb for e in l.get('endpoints', [])):
                    return True
            return False
        excluded = sum(excluded_pad(r, s) for r in own if r[2][0] == 'pad'
                       for s in rows if s[0] in others and s[2][0] == 'pad')
        classes = iface.get('aggressor_classes', interfaces.get('aggressor_classes', {}))
        targets = [('pair_gap_mm', others), ('other_gap_mm', signal_others)]
        targets += [('other_gap_class:'+name, set(nets_in_class)-nets) for name, nets_in_class in sorted(classes.items())]
        for measure, target in targets:
            best = None
            for r in own:
                pool = layer_rows[r[1]]
                # Nearest query with predicate filtering: expand deterministically
                # until the current best bounds every possible closer candidate.
                radius = 1_000_000
                extent = extents[r[1]]
                seen = set()
                while True:
                    b = r[3].bounds
                    candidates = sorted(int(j) for j in trees[r[1]].query(box(b[0]-radius, b[1]-radius, b[2]+radius, b[3]+radius))) if indexed else range(len(pool))
                    for j in candidates:
                        if j in seen:
                            continue
                        seen.add(j)
                        s = pool[j]
                        if s[0] not in target:
                            continue
                        if measure == 'pair_gap_mm' and excluded_pad(r, s):
                            continue
                        gap = r[3].distance(s[3])
                        if best is None or gap < best:
                            best = gap
                            if measure == 'pair_gap_mm':
                                pair_site = (r, s)
                    if not indexed or radius >= extent or (best is not None and radius >= best):
                        break
                    radius = min(extent, radius*2)
            gaps[measure] = best/1e6 if best is not None else None
        # Use the own-leg spacing at the actual closest foreign-copper site.
        # A closest site on a pad/via/fill has no controlled local pair gap.
        best_ratio = None
        if pair_site is not None and pair_site[0][2][0] == 'track':
            from shapely.ops import nearest_points
            r, s = pair_site
            point = nearest_points(r[3], s[3])[0]
            line = LineString(r[2][1:3])
            centre = line.interpolate(line.project(point))
            mate = lane['n'] if r[0] == lane['p'] else lane['p'] if r[0] == lane['n'] else None
            mates = [t for t in own if t[0] == mate and t[1] == r[1] and t[2][0] == 'track']
            if mates:
                mate_lines = [LineString(t[2][1:3]) for t in mates]
                indices = STRtree(mate_lines).query_nearest(centre) if indexed else range(len(mates))
                j = min((int(j) for j in indices), key=lambda j: (centre.distance(mate_lines[j]), mates[j][:3]))
                intra = centre.distance(mate_lines[j])-(r[2][3]+mates[j][2][3])/2
                if intra > 0:
                    best_ratio = gaps['pair_gap_mm']*1e6/intra
                    gaps['pair_ratio_band'] = 1000/intra
        gaps['pair_gap_ratio'] = best_ratio
        state = ctx.get('complete', {})
        reason = ('unknown/incomplete lane connectivity' if any(state.get(n) is not True for n in nets) else
                  'unsupported or missing lane copper geometry' if nets & unknown else
                  'no represented lane copper' if not own else None)
        reasons = {measure: reason or ('unsupported neighbour copper geometry' if target & unknown else
                                      'declared aggressor net missing from board' if target-set(d['nets']) else None)
                   for measure, target in targets+[('pair_gap_ratio', others)]}
        out[iface['id']+':'+lane['id']] = dict(gaps, reason=reason, reasons=reasons,
                                             excluded_pad_comparisons=excluded)
    return out


def measure_rules(d, interfaces, profile, ctx):
    if interfaces is None:
        return []
    paired = pair_spacing(d, interfaces, ctx)
    output = []
    for iface in sorted(interfaces.get('interfaces', []), key=lambda f: f['id']):
        for lane in sorted(iface.get('lanes', []), key=lambda l: l['id']):
            item = iface['id']+':'+lane['id']
            nets = _lane_nets(iface, lane)
            for measure in ('self_spacing_ratio', 'bend_segment_ratio', 'pair_gap_mm', 'pair_gap_ratio', 'other_gap_mm'):
                applicable = rules.rules_for(profile, iface['interface'], measure, iface.get('mode'))
                for rule in applicable:
                    if rule.get('role') not in (None, lane.get('role')):
                        continue
                    if measure in ('self_spacing_ratio', 'bend_segment_ratio'):
                        values = [net_spacing(d, n) for n in sorted(nets)]
                        reason = next((v['reason'] for v in values if v['reason']), None)
                        if any(ctx.get('complete', {}).get(n) is not True for n in nets):
                            reason = 'unknown/incomplete lane connectivity'
                        value = min((v[measure] for v in values if v[measure] is not None), default=None)
                        band = rule.get('band', max((v.get('ratio_band', 0.001) for v in values), default=0.001))
                        detail = {}
                    else:
                        detail = paired.get(item, {})
                        value = detail.get(measure)
                        reason = detail.get('reasons', {}).get(measure, detail.get('reason', 'not a differential lane'))
                        band = rule.get('band', detail.get('pair_ratio_band', 0.001)) if measure == 'pair_gap_ratio' else rule.get('band', 0.001)
                        if measure in rules.PAIR_MEASURES and not lane.get('n'):
                            entry = rules.result(rule, item, None, reason='single-ended lane: pair-only rule')
                            entry['applicability'] = 'not applicable'
                            entry['outcome'] = 'not_applicable'
                            output.append(entry)
                            continue
                        if measure == 'other_gap_mm':
                            classes = iface.get('aggressor_classes', interfaces.get('aggressor_classes', {}))
                            name = rule.get('aggressor_class')
                            if name:
                                key = 'other_gap_class:'+name
                                value = detail.get(key)
                                reason = ('frozen aggressor-class net set missing: '+name if name not in classes else
                                          detail.get('reasons', {}).get(key))
                    if iface.get('confidence') != 'confirmed' or interfaces.get('status') != 'reviewed':
                        reason = 'interface intent not confirmed/reviewed'
                    entry = rules.result(rule, item, None if reason else value, band=band,
                                         reason=reason or ('no eligible geometry/neighbour' if value is None else None))
                    if measure == 'other_gap_mm':
                        entry['unclassified_nearest_gap_mm'] = detail.get('other_gap_mm')
                        entry['aggressor_class'] = rule.get('aggressor_class')
                    if detail:
                        entry['excluded_pad_comparisons'] = detail['excluded_pad_comparisons']
                    output.append(entry)
    return sorted(output, key=rules.key)
