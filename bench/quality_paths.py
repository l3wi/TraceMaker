# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen endpoint, planar copper paths (Toradex 2015 §§6.6–6.7, 7).

Lengths are exact straight/circular centreline lengths, not net copper sums.
Pads are equipotential contacts; declared series parts have zero planar length.
No plane transport, via barrel, package or external cable delay is inferred.
"""
import math
from collections import defaultdict, deque

import quality_rules as rules
import quality_si as si
from shapely.geometry import LineString, Point, Polygon

NM = 1e6


def _identity(item):
    return tuple((k, tuple(v) if isinstance(v, list) else v) for k, v in sorted(item.items())
                 if k not in ('uuid', 'physical_matched', 'ref', 'num'))


def _unique(items):
    unique = {}
    for item in sorted(items, key=lambda i: (_identity(i), repr(sorted(i.items())))):
        unique.setdefault(_identity(item), item)
    return list(unique.values())


class _Curve:
    def __init__(self, item, arc=False):
        self.item = item
        self.a = (item['sx'], item['sy'])
        self.b = (item['ex'], item['ey'])
        if self.a > self.b:
            self.a, self.b = self.b, self.a
        self.arc = arc
        if arc:
            ax, ay = self.a
            bx, by = item['mx'], item['my']
            cx, cy = self.b
            det = 2 * (ax*(by-cy) + bx*(cy-ay) + cx*(ay-by))
            if det == 0:
                raise ValueError('collinear arc')
            aa, bb, cc = ax*ax+ay*ay, bx*bx+by*by, cx*cx+cy*cy
            self.c = ((aa*(by-cy)+bb*(cy-ay)+cc*(ay-by))/det,
                      (aa*(cx-bx)+bb*(ax-cx)+cc*(bx-ax))/det)
            self.r = math.dist(self.a, self.c)
            self.angle = math.atan2(ay-self.c[1], ax-self.c[0])
            self.sweep = (math.atan2(cy-self.c[1], cx-self.c[0])-self.angle) % math.tau
            if (math.atan2(by-self.c[1], bx-self.c[0])-self.angle) % math.tau > self.sweep:
                self.sweep -= math.tau
            self.length = self.r * abs(self.sweep)
        else:
            self.length = math.dist(self.a, self.b)
        self.cuts = {0.0, 1.0}

    def point(self, t):
        if t == 0:
            return self.a
        if t == 1:
            return self.b
        if self.arc:
            angle = self.angle+self.sweep*t
            return self.c[0]+self.r*math.cos(angle), self.c[1]+self.r*math.sin(angle)
        return tuple(a+(b-a)*t for a, b in zip(self.a, self.b))

    def project(self, p):
        if self.arc:
            angle = math.atan2(p[1]-self.c[1], p[0]-self.c[0])
            distance = ((angle-self.angle) if self.sweep > 0 else (self.angle-angle)) % math.tau
            t = distance/abs(self.sweep)
            if t > 1:
                t = min((0, 1), key=lambda u: (math.dist(p, self.point(u)), u))
        else:
            dx, dy = self.b[0]-self.a[0], self.b[1]-self.a[1]
            t = max(0, min(1, ((p[0]-self.a[0])*dx+(p[1]-self.a[1])*dy)/(dx*dx+dy*dy)))
        if t*self.length <= 1:
            t = 0.
        elif (1-t)*self.length <= 1:
            t = 1.
        return t, math.dist(p, self.point(t))


def _circle_line(c, r, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    ox, oy = a[0]-c[0], a[1]-c[1]
    aa = dx*dx+dy*dy
    bb = 2*(ox*dx+oy*dy)
    cc = ox*ox+oy*oy-r*r
    disc = bb*bb-4*aa*cc
    if disc < 0 or not aa:
        return []
    return [(a[0]+t*dx, a[1]+t*dy) for t in
            ((-bb-math.sqrt(disc))/(2*aa), (-bb+math.sqrt(disc))/(2*aa)) if 0 <= t <= 1]


def _crossings(a, b):
    if not a.arc and not b.arc:
        ax, ay = a.b[0]-a.a[0], a.b[1]-a.a[1]
        bx, by = b.b[0]-b.a[0], b.b[1]-b.a[1]
        det = ax*by-ay*bx
        if not det:
            return []  # collinear overlap is split by endpoint projections below
        dx, dy = b.a[0]-a.a[0], b.a[1]-a.a[1]
        t, u = (dx*by-dy*bx)/det, (dx*ay-dy*ax)/det
        return [a.point(t)] if 0 <= t <= 1 and 0 <= u <= 1 else []
    if a.arc and b.arc:
        d = math.dist(a.c, b.c)
        if not d or d > a.r+b.r or d < abs(a.r-b.r):
            return []
        x = (a.r*a.r-b.r*b.r+d*d)/(2*d)
        h = math.sqrt(max(0, a.r*a.r-x*x))
        ux, uy = (b.c[0]-a.c[0])/d, (b.c[1]-a.c[1])/d
        points = [(a.c[0]+x*ux+s*h*uy, a.c[1]+x*uy-s*h*ux) for s in (-1, 1)]
    else:
        circle, line = (a, b) if a.arc else (b, a)
        points = _circle_line(circle.c, circle.r, line.a, line.b)
    return [p for p in points if a.project(p)[1] <= 1 and b.project(p)[1] <= 1]


def _pad_region(p, info=None):
    """Exact supported pad as a convex linear core plus a circular dilation."""
    shape = (info or {}).get('shape')
    w, h = p['w']/NM, p['h']/NM
    radius = 0.
    known = shape in ('rect', 'roundrect', 'circle', 'oval')
    if shape == 'circle':
        return Point(p['x']/NM, p['y']/NM), min(w, h)/2, True
    if shape == 'oval':
        radius = min(w, h)/2
    elif shape == 'roundrect':
        radius = min(w, h)*info.get('rr', 0)
    elif not known:
        return Point(p['x']/NM, p['y']/NM), min(w, h)/2, False
    dx, dy = max(0, w/2-radius), max(0, h/2-radius)
    angle = -math.radians(p.get('angle', 0))
    def xy(x, y):
        return (p['x']/NM+x*math.cos(angle)-y*math.sin(angle),
                p['y']/NM+x*math.sin(angle)+y*math.cos(angle))
    if dx == 0 and dy == 0:
        core = Point(xy(0, 0))
    elif dx == 0 or dy == 0:
        core = LineString([xy(-dx, -dy), xy(dx, dy)])
    else:
        core = Polygon([xy(-dx, -dy), xy(dx, -dy), xy(dx, dy), xy(-dx, dy)])
    return core, radius, known


def _pad_contact(p, xy, margin=0, info=None):
    core, radius, _ = _pad_region(p, info)
    return core.distance(Point(xy[0]/NM, xy[1]/NM)) <= radius+margin/NM


def _closure(net, series):
    nets = {net}
    while True:
        more = {s[k] for s in series if s['a'] in nets or s['b'] in nets for k in ('a', 'b')}
        if more <= nets:
            return nets
        nets |= more


def _binding(p, e):
    return (p['net'] == e['net'] and abs(p['x']-e['x_nm']) <= 1000
            and abs(p['y']-e['y_nm']) <= 1000 and set(p['layers']) == set(e['layers']))


def endpoint_path(d, net, endpoints, series=(), *, pad_metadata=None):
    """Unique endpoint path, exact planar mm, ordered transitions and peeled stubs.

    Endpoints are physical schema-1 bindings. Extra shunt pads need not be
    terminals. A connected loop (including one on a branch) is conservatively
    ambiguous. Unknown geometry and unrepresented plane paths are N/A.
    """
    def na(reason):
        return dict(length_mm=None, stub_mm=None, transitions=None, vias=None, reason=reason)
    nets = _closure(net, series)
    series_ends = [e for s in series for e in s.get('endpoints', [])]
    ends = [e for e in endpoints if e.get('net') in nets
            and not any(e == inner for inner in series_ends)]
    if len(ends) != 2:
        return na('need exactly two frozen endpoints per leg')
    layers = d.get('copper_layers', [])
    pads = _unique(p for p in d.get('pads', []) if p['net'] in nets)
    vias = _unique(v for v in d.get('vias', []) if v['net'] in nets)
    try:
        curves = [_Curve(t, arc) for kind, arc in (('tracks', False), ('arcs', True))
                  for t in _unique(t for t in d.get(kind, []) if t['net'] in nets)
                  if (t['sx'], t['sy']) != (t['ex'], t['ey'])]
        curves.sort(key=lambda c: (c.item['net'], c.item['layer'], c.a, c.b, c.arc, _identity(c.item)))
        if any(c.item['layer'] not in layers for c in curves):
            return na('unknown copper layer')
        for p in pads:
            if not {'x', 'y', 'w', 'h', 'layers'} <= p.keys():
                return na('unknown pad geometry')
        spans = []
        for v in vias:
            lo, hi = sorted((layers.index(v['top']), layers.index(v['bottom'])))
            spans.append(tuple(layers[lo:hi+1]))
    except (KeyError, ValueError, ZeroDivisionError):
        return na('unknown or degenerate copper geometry')

    # Contacts are represented on exact centrelines. Projection contacts handle
    # endpoint-on-copper T junctions; analytic intersections handle crossing arcs.
    joins = []
    for i, a in enumerate(curves):
        for j in range(i):
            b = curves[j]
            if (a.item['net'], a.item['layer']) != (b.item['net'], b.item['layer']):
                continue
            for xy in _crossings(a, b):
                ta, tb = a.project(xy)[0], b.project(xy)[0]
                a.cuts.add(ta); b.cuts.add(tb)
                joins.append(((i, ta), (j, tb)))
            for src, dst, ii, jj in ((a, b, i, j), (b, a, j, i)):
                for t in (0., 1.):
                    u, gap = dst.project(src.point(t))
                    if gap <= (src.item['width']+dst.item['width'])/2:
                        dst.cuts.add(u)
                        joins.append(((ii, t), (jj, u)))
    contacts = defaultdict(list)
    for kind, items in (('pad', pads), ('via', vias)):
        for index, item in enumerate(items):
            ls = item['layers'] if kind == 'pad' else spans[index]
            info = (pad_metadata or {}).get(item.get('ref', '')+'.'+item.get('num', '')) if kind == 'pad' else None
            for i, c in enumerate(curves):
                if c.item['net'] != item['net'] or c.item['layer'] not in ls:
                    continue
                t, gap = c.project((item['x'], item['y']))
                radius = min(item['w'], item['h'])/2 if kind == 'pad' else item['size']/2
                touching = _pad_contact(item, c.point(t), c.item['width']/2, info) if kind == 'pad' else gap <= radius+c.item['width']/2
                if touching:
                    c.cuts.add(t)
                    contacts[kind, index, c.item['layer']].append((i, t))
                for u in (0., 1.):
                    if (kind == 'pad' and _pad_contact(item, c.point(u), c.item['width']/2, info)
                            or kind == 'via' and math.dist((item['x'], item['y']), c.point(u)) <= radius+c.item['width']/2):
                        contacts[kind, index, c.item['layer']].append((i, u))

    parent = {}
    def root(v):
        parent.setdefault(v, v)
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v
    def union(a, b):
        a, b = root(a), root(b)
        parent[max(a, b)] = min(a, b)
    def node(i, t):
        c = curves[i]
        x, y = c.point(t)
        return ('copper', c.item['net'], c.item['layer'], round(x), round(y))
    for i, c in enumerate(curves):
        for t in sorted(c.cuts):
            root(node(i, t))
    for a, b in joins:
        union(node(*a), node(*b))
    for (kind, index, layer), points in sorted(contacts.items()):
        hub = (kind, index, layer)
        root(hub)
        for i, t in points:
            union(hub, node(i, t))
    # Even an isolated pad is a real endpoint; it must not disappear from binding.
    for i, p in enumerate(pads):
        for layer in sorted(p['layers']):
            root(('pad', i, layer))

    edges = []
    def edge(a, b, length, event=None, curve=None):
        a, b = root(a), root(b)
        if a != b:
            edges.append((a, b, length, event, curve))
    for i, c in enumerate(curves):
        cuts = sorted(c.cuts)
        for a, b in zip(cuts, cuts[1:]):
            edge(node(i, a), node(i, b), (b-a)*c.length/NM, curve=(i, a, b))
    for kind, items in (('pad', pads), ('via', vias)):
        for i, item in enumerate(items):
            ls = sorted(set(item['layers'])) if kind == 'pad' else spans[i]
            used = [l for l in ls if (kind, i, l) in parent]
            if len(used) > 1:
                hub = ('transition', kind, i)
                for layer in used:
                    edge(hub, (kind, i, layer), 0., (kind, i, tuple(ls), layer))
    for index, s in enumerate(sorted(series, key=lambda s: (s['a'], s['b'], s['ref']))):
        if s['a'] not in nets:
            continue
        if s.get('endpoints'):
            sides = [[(i, p) for i, p in enumerate(pads) if p['net'] == n
                      and any(_binding(p, e) for e in s['endpoints'] if e['net'] == n)]
                     for n in (s['a'], s['b'])]
        else:
            sides = [[(i, p) for i, p in enumerate(pads) if p.get('ref') == s['ref'] and p['net'] == n]
                     for n in (s['a'], s['b'])]
        if any(len(side) != 1 for side in sides):
            return na('ambiguous declared series pad binding')
        (ia, pa), (ib, pb) = sides[0][0], sides[1][0]
        edge(('pad', ia, sorted(pa['layers'])[0]), ('pad', ib, sorted(pb['layers'])[0]), 0.,
             ('series', index, s['ref'], None))
    terminals = []
    for e in ends:
        matches = [i for i, p in enumerate(pads) if _binding(p, e)]
        if len(matches) != 1:
            return na('unmatched or ambiguous frozen endpoint')
        terminals.append(root(('pad', matches[0], sorted(e['layers'])[0])))
    if terminals[0] == terminals[1]:
        return na('endpoints share an equipotential contact')
    # Deduplicate split overlapping copper, but retain distinct geometrical routes.
    dedup = {}
    for a, b, length, event, curve in edges:
        geometry = None
        if curve:
            c = curves[curve[0]]
            geometry = (c.arc, tuple(round(x, 3) for x in c.point((curve[1]+curve[2])/2)))
        k = (min(a, b), max(a, b), event, geometry)
        dedup[k] = (a, b, length, event, curve)
    edges = [v for _, v in sorted(dedup.items(), key=lambda kv: repr(kv[0]))]
    adj = defaultdict(set)
    for i, (a, b, *_) in enumerate(edges):
        adj[a].add(i); adj[b].add(i)
    reached, queue = {terminals[0]}, deque([terminals[0]])
    while queue:
        v = queue.popleft()
        for i in sorted(adj[v]):
            a, b = edges[i][:2]
            w = b if a == v else a
            if w not in reached:
                reached.add(w); queue.append(w)
    if terminals[1] not in reached:
        return na('disconnected endpoints or path outside copper model')
    active = {i for v in reached for i in adj[v]}
    removed = set()
    leaves = deque(sorted(v for v in reached if len(adj[v]) == 1 and v not in terminals))
    while leaves:
        v = leaves.popleft()
        incident = adj[v] & active
        if len(incident) != 1 or v in terminals:
            continue
        i = min(incident)
        active.remove(i); removed.add(i)
        a, b = edges[i][:2]
        w = b if v == a else a
        if w not in terminals and len(adj[w] & active) == 1:
            leaves.append(w)
    v, path = terminals[0], []
    seen = {v}
    while v != terminals[1]:
        next_edges = sorted((adj[v] & active)-{i for i, _ in path})
        if len(next_edges) != 1:
            return na('ambiguous copper loop or multiple endpoint paths')
        i = next_edges[0]
        a, b = edges[i][:2]
        w = b if v == a else a
        if w in seen:
            return na('ambiguous copper loop or multiple endpoint paths')
        seen.add(w); path.append((i, v)); v = w
    if len(path) != len(active):
        return na('ambiguous copper loop on a branch')
    total, transitions, traversed_vias, previous = 0., [], set(), None
    portions, pieces = [], []
    node_distances = {terminals[0]: 0.}
    for i, start in path:
        a, b, length, event, curve = edges[i]
        pieces.append(length)
        total = math.fsum(pieces)
        node_distances[b if start == a else a] = total
        if curve:
            c = curves[curve[0]]
            portions.append((c, curve[1], curve[2]))
        if event:
            kind, index, span, layer = event
            physical = (kind, index)
            if kind == 'series':
                transitions.append(dict(kind=kind, span=(), at_mm=total))
            elif previous and previous[0] == physical:
                entry_layer = previous[1]
                transitions.append(dict(kind=kind, span=span, layers=(entry_layer, layer), at_mm=total))
                if kind == 'via':
                    traversed_vias.add(index)
                previous = None
            else:
                previous = physical, layer
    for index, span in enumerate(spans):
        if index in traversed_vias:
            continue
        contacts_on_path = [(node_distances[root(('via', index, layer))], layer)
                            for layer in span if ('via', index, layer) in parent
                            and root(('via', index, layer)) in node_distances]
        if contacts_on_path:
            at, layer = min(contacts_on_path)
            traversed_vias.add(index)
            transitions.append(dict(kind='via', span=span, layers=(layer, layer), at_mm=at))
    transitions.sort(key=lambda t: (t['at_mm'], t['kind'], t['span'], t.get('layers', ())))
    return dict(length_mm=math.fsum(pieces), stub_mm=math.fsum(edges[i][2] for i in sorted(removed)),
                transitions=transitions, vias=len(traversed_vias), reason=None, portions=portions,
                nets=sorted(nets))


def pair_metrics(p, n):
    if p['length_mm'] is None or n['length_mm'] is None:
        return dict(segment_skew_mm=None, via_layer_mismatch=None,
                    reason=p.get('reason') or n.get('reason'))
    pt, nt = p['transitions'], n['transitions']
    signature = lambda t: (t['kind'], tuple(t['span']), tuple(t.get('layers', ())))
    pv, nv = ([t for t in ts if t['kind'] == 'via'] for ts in (pt, nt))
    mismatch = sum(signature(a) != signature(b) for a, b in zip(pv, nv)) + abs(len(pv)-len(nv))
    if [signature(t) for t in pt] != [signature(t) for t in nt]:
        return dict(segment_skew_mm=None, via_layer_mismatch=mismatch, reason='unmatched transition sequences')
    pp = [0.]+[t['at_mm'] for t in pt]+[p['length_mm']]
    nn = [0.]+[t['at_mm'] for t in nt]+[n['length_mm']]
    return dict(segment_skew_mm=max(abs((b-a)-(d-c)) for a, b, c, d in zip(pp, pp[1:], nn, nn[1:])),
                via_layer_mismatch=0, reason=None)


def _orient_pair(d, p_ends, n_ends):
    """Match frozen terminals jointly, preferring footprints then proximity.

    Geometry comes only from endpoint bindings, never route copper. Equal
    correspondence evidence is ambiguous; pad dimensions cannot break that tie.
    """
    if len(p_ends) != 2 or len(n_ends) != 2:
        return None
    order = lambda e: (e['x_nm'], e['y_nm'], e['net'], tuple(sorted(e['layers'])))
    pp, nn = sorted(p_ends, key=order), sorted(n_ends, key=order)
    def ref(e):
        matches = {p['ref'] for p in d.get('pads', []) if _binding(p, e)}
        return next(iter(matches)) if len(matches) == 1 else None
    p_refs, n_refs = [ref(e) for e in pp], [ref(e) for e in nn]
    def score(indices):
        footprint_matches = sum(p_refs[i] is not None and p_refs[i] == n_refs[j] for i, j in enumerate(indices))
        distance = sum((pp[i]['x_nm']-nn[j]['x_nm'])**2+(pp[i]['y_nm']-nn[j]['y_nm'])**2
                       for i, j in enumerate(indices))
        return -footprint_matches, distance
    forward, reverse = score((0, 1)), score((1, 0))
    if forward == reverse:
        return None
    return pp, nn if forward < reverse else list(reversed(nn))


def _lane_endpoints(d, lane, series):
    endpoints = lane.get('endpoints', [])
    if not lane.get('n'):
        return endpoints
    inner = [e for s in series for e in s.get('endpoints', [])]
    legs = [[e for e in endpoints if e.get('net') in _closure(lane[leg], series) and e not in inner]
            for leg in ('p', 'n')]
    oriented = _orient_pair(d, *legs)
    return oriented[0]+oriented[1]+[e for e in endpoints if e in inner] if oriented else []


def infer_endpoints(d):
    """Freeze physical two-pad endpoints for legacy name-inferred candidates.

    Multi-pad topology needs a manifest: never select endpoints from routing.
    Keys/values may be reused unchanged for every comparison arm.
    """
    out = {}
    for p, n in si.pairs(d):
        pads = [_unique(pad for pad in d['pads'] if pad['net'] == net) for net in (p, n)]
        endpoints = [[dict(x_nm=pad['x'], y_nm=pad['y'], net=net, layers=sorted(pad['layers']))
                      for pad in leg] for net, leg in zip((p, n), pads)]
        oriented = _orient_pair(d, *endpoints)
        if oriented:
            out[p], out[n] = oriented
    return out


def parity_metrics(d, p, n, frozen_endpoints, *, pad_metadata=None):
    """New fields only; caller freezes ``infer_endpoints(human)`` once."""
    frozen_endpoints = frozen_endpoints or {}
    pp = endpoint_path(d, p, frozen_endpoints.get(p, []), pad_metadata=pad_metadata)
    nn = endpoint_path(d, n, frozen_endpoints.get(n, []), pad_metadata=pad_metadata)
    metrics = pair_metrics(pp, nn)
    return dict(segment_skew_mm=metrics['segment_skew_mm'],
                via_layer_mismatch=metrics['via_layer_mismatch'], path_reason=metrics['reason'],
                p_stub_mm=pp['stub_mm'], n_stub_mm=nn['stub_mm'])


def _arc_polygon_distance(c, poly):
    """Exact circle-arc/linear-polygon minimum centreline distance, in mm."""
    boundary = [tuple(x*NM for x in xy) for xy in poly.exterior.coords]
    candidates = [c.a, c.b]
    for a, b in zip(boundary, boundary[1:]):
        if any(c.project(xy)[1] <= 1 for xy in _circle_line(c.c, c.r, a, b)):
            return 0.
        for xy in (a, b):
            candidates.append(c.point(c.project(xy)[0]))
        dx, dy = b[0]-a[0], b[1]-a[1]
        t = max(0, min(1, ((c.c[0]-a[0])*dx+(c.c[1]-a[1])*dy)/(dx*dx+dy*dy)))
        q = a[0]+t*dx, a[1]+t*dy
        for sign in (-1, 1):
            scale = sign*c.r/math.hypot(dx, dy)
            xy = c.c[0]-dy*scale, c.c[1]+dx*scale
            if c.project(xy)[1] <= 1:
                candidates.append(xy)
        candidates.append(c.point(c.project(q)[0]))
    return min(poly.distance(Point(x/NM, y/NM)) for x, y in candidates)


def _linear_overlap(a, b):
    if a['layer'] != b['layer']:
        return None
    ux, uy = (a['ex']-a['sx'])/a['len'], (a['ey']-a['sy'])/a['len']
    vx, vy = (b['ex']-b['sx'])/b['len'], (b['ey']-b['sy'])/b['len']
    if abs(ux*vy-uy*vx) > 1e-4:
        return None
    dx, dy = b['sx']-a['sx'], b['sy']-a['sy']
    gap = abs(dx*uy-dy*ux)-(a['width']+b['width'])/2
    lo = max(0, min(dx*ux+dy*uy, (b['ex']-a['sx'])*ux+(b['ey']-a['sy'])*uy))
    hi = min(a['len'], max(dx*ux+dy*uy, (b['ex']-a['sx'])*ux+(b['ey']-a['sy'])*uy))
    return (lo, hi, gap) if hi > lo and gap >= 0 else None


def _path_segments(path):
    # Corridor uses only required path copper, never peeled stubs. Curved coupled
    # sections cannot be certified from chords and are explicitly not assessable.
    if any(c.arc for c, _, _ in path['portions']):
        return None
    return [dict(sx=c.point(a)[0]/NM, sy=c.point(a)[1]/NM, ex=c.point(b)[0]/NM, ey=c.point(b)[1]/NM,
                 len=(b-a)*c.length/NM, width=c.item['width']/NM, layer=c.item['layer'])
            for c, a, b in path['portions']]


def between_legs(d, p, n, series, fills=None, pad_metadata=None):
    sa, sb = _path_segments(p), _path_segments(n)
    if sa is None or sb is None:
        return None, 'curved between-leg corridor not represented'
    corridors = []
    for a in sa:
        for b in sb:
            ov = _linear_overlap(a, b)
            if not ov or ov[2] <= 0:
                continue
            lo, hi, gap = ov
            ux, uy = (a['ex']-a['sx'])/a['len'], (a['ey']-a['sy'])/a['len']
            vx, vy = -uy, ux
            offset = (b['sx']-a['sx'])*vx+(b['sy']-a['sy'])*vy
            sign = 1 if offset > 0 else -1
            near, far = sign*a['width']/2, offset-sign*b['width']/2
            pts = [(a['sx']+u*ux+v*vx, a['sy']+u*uy+v*vy)
                   for u, v in ((lo, near), (hi, near), (hi, far), (lo, far))]
            corridors.append((a['layer'], Polygon(pts)))
    if not corridors:
        return None, 'no assessable coupled sections'
    own = set(p['nets']) | set(n['nets'])
    excluded = {s['ref'] for s in series}
    found, unknown = set(), False
    layers = d['copper_layers']
    for kind in ('tracks', 'arcs', 'vias', 'pads'):
        for item in _unique(d.get(kind, [])):
            if item['net'] in own or kind == 'pads' and item.get('ref') in excluded:
                continue
            if kind in ('tracks', 'arcs'):
                ls = [item['layer']]
                if kind == 'arcs':
                    c = _Curve(item, True)
                    distance = lambda poly: _arc_polygon_distance(c, poly)
                else:
                    geometry = LineString([(item['sx']/NM, item['sy']/NM), (item['ex']/NM, item['ey']/NM)])
                    distance = geometry.distance
                radius = item['width']/2/NM
            elif kind == 'vias':
                geometry = Point(item['x']/NM, item['y']/NM)
                radius = item['size']/2/NM
                lo, hi = sorted((layers.index(item['top']), layers.index(item['bottom'])))
                ls = layers[lo:hi+1]
                distance = geometry.distance
            else:
                info = (pad_metadata or {}).get(item.get('ref', '')+'.'+item.get('num', ''))
                geometry, radius, known = _pad_region(item, info)
                ls = item['layers']
                distance = geometry.distance
            relevant = [poly for layer, poly in corridors if layer in ls]
            if any(distance(poly) < radius or radius == 0 and geometry.intersection(poly).area > 0 for poly in relevant):
                found.add((kind, _identity(item)))
            elif kind == 'pads' and not known:
                outer = math.hypot(item['w'], item['h'])/2/NM
                unknown |= any(distance(poly) < outer for poly in relevant)
    for (net, layer), copper in sorted((fills or {}).items()):
        if net in own:
            continue
        islands = list(copper.geoms) if hasattr(copper, 'geoms') else [copper]
        for island in sorted(islands, key=lambda g: g.wkb_hex):
            if any(layer == cl and island.intersection(poly).area > 0 for cl, poly in corridors):
                found.add(('fill', net, layer, island.wkb_hex))
    if unknown:
        return None, 'unknown pad shape at between-leg corridor boundary'
    return len(found), None


def measure_rules(d, interfaces, profile, ctx):
    if interfaces is None:
        return []
    out = []
    pad_metadata = ctx.get('pad_metadata')
    if pad_metadata is None and ctx.get('board_path'):
        import quality_craft
        from pathlib import Path
        pad_metadata = quality_craft.metadata(Path(ctx['board_path']))
    measures = ('intra_skew_mm', 'segment_skew_mm', 'group_skew_mm', 'max_length_mm',
                'vias_per_leg', 'between_legs_items')
    for iface in sorted(interfaces['interfaces'], key=lambda i: i['id']):
        lanes = sorted(iface['lanes'], key=lambda l: l['id'])
        measured = {}
        for lane in lanes:
            endpoints = _lane_endpoints(d, lane, iface.get('series', []))
            measured[lane['id']] = {leg: endpoint_path(d, lane[leg], endpoints, iface.get('series', []),
                                                      pad_metadata=pad_metadata)
                                     for leg in ('p', 'n') if lane.get(leg)}
        for measure in measures:
            for rule in rules.rules_for(profile, iface['interface'], measure, iface.get('mode')):
                selected = [l for l in lanes if not rule.get('role') or l.get('role') in (rule['role'], None, 'unknown')]
                if measure == 'group_skew_mm':
                    group = rule.get('group')
                    relevant = [l for l in lanes if l.get('role') in ('data', 'single', 'tx', 'rx', 'clock')]
                    reason = 'unknown group lane role' if any(l.get('role') not in ('data', 'tx', 'rx', 'clock', 'single') for l in lanes) else None
                    means = {}
                    for lane in relevant:
                        paths = measured[lane['id']]
                        if any(p['length_mm'] is None for p in paths.values()):
                            reason = 'unassessable group lane path'
                        else:
                            means[lane['id']] = math.fsum(p['length_mm'] for p in paths.values())/len(paths)
                    data = [l for l in relevant if l.get('role') != 'clock']
                    clocks = [l for l in relevant if l.get('role') == 'clock']
                    value = None
                    if not reason and group == 'clock' and len(clocks) == 1 and data:
                        value = max(abs(means[l['id']]-means[clocks[0]['id']]) for l in data)
                    elif not reason and group == 'spread' and len(data) >= 2:
                        value = max(means[l['id']] for l in data)-min(means[l['id']] for l in data)
                    else:
                        reason = reason or 'unknown group or missing clock/data roles'
                    if iface.get('confidence') != 'confirmed':
                        value, reason = None, 'unconfirmed interface identity'
                    out.append(rules.result(rule, iface['id'], value, reason=reason))
                    continue
                for lane in selected:
                    paths = measured[lane['id']]
                    reason = next((p['reason'] for p in paths.values() if p['length_mm'] is None), None)
                    if lane.get('role') not in ('data', 'clock', 'tx', 'rx', 'single'):
                        reason = 'unknown lane role'
                    if iface.get('confidence') != 'confirmed':
                        reason = 'unconfirmed interface identity'
                    item = iface['id']+':'+lane['id']
                    if measure == 'vias_per_leg':
                        for leg, path in paths.items():
                            out.append(rules.result(rule, item+':'+leg, None if reason else path['vias'], reason=reason, band=0))
                        continue
                    value = None
                    if not reason:
                        if measure == 'max_length_mm':
                            value = max(p['length_mm'] for p in paths.values())
                        elif len(paths) != 2:
                            reason = 'differential leg binding missing'
                        elif measure == 'intra_skew_mm':
                            value = abs(paths['p']['length_mm']-paths['n']['length_mm'])
                        elif measure == 'segment_skew_mm':
                            metrics = pair_metrics(paths['p'], paths['n'])
                            value, reason = metrics['segment_skew_mm'], metrics['reason']
                        else:
                            value, reason = between_legs(d, paths['p'], paths['n'], iface.get('series', []), ctx.get('fills'), pad_metadata)
                    out.append(rules.result(rule, item, value, reason=reason,
                                            band=0 if measure == 'between_legs_items' else None))
    return out
