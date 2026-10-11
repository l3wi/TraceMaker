# SPDX-License-Identifier: GPL-3.0-or-later
"""Reference-domain coverage proxies; not electromagnetic or isolation sign-off."""
import math

import intent_quality as iq
import quality_rules as rules
import quality_si as si
from shapely.geometry import LineString, Point
from shapely.ops import unary_union


def copper_buffer(geometry, radius):
    """Bound circular-edge chord error below 0.1 µm in millimetre geometry."""
    if radius <= 0:
        return geometry
    count = max(16, math.ceil(math.pi/(4*math.acos(1-min(.0001/radius, 1)))))
    return geometry.buffer(radius, quad_segs=count)


def canonical_data(d):
    """Keep NetGraph's geometry/tie handling independent of input item order."""
    return dict(d, **{k: sorted(d.get(k, []), key=lambda x: tuple(sorted((a, repr(b)) for a, b in x.items())))
                      for k in ('pads', 'tracks', 'arcs', 'vias')})


def islands(fills):
    """Physical fill identity is (net, layer, canonical polygon index)."""
    out = {}
    for (net, layer), union in sorted(fills.items()):
        pieces = list(union.geoms) if hasattr(union, 'geoms') else [union]
        for i, piece in enumerate(sorted(pieces, key=lambda p: (p.bounds, p.normalize().wkb))):
            if not piece.is_empty:
                out[net, layer, i] = piece
    return out


def contact(g, d, layer, island, node):
    if node[0] == 'v':
        v = g.vias[node[1]]
        ls = d['copper_layers']
        if v['top'] not in ls or v['bottom'] not in ls:
            return False
        lo, hi = sorted((ls.index(v['top']), ls.index(v['bottom'])))
        return layer in ls[lo:hi+1] and island.intersects(Point(v['x']/1e6, v['y']/1e6).buffer(v['size']/2e6))
    if node[0] == 'p':
        p = g.pads[node[1]]
        return layer in p['layers'] and island.covers(Point(p['x']/1e6, p['y']/1e6))
    return any(l == layer and island.covers(Point(x/1e6, y/1e6))
               for l, x, y in (g.pos[k] for k in g.members[node]))


def grounded_islands(d, fills):
    """A fill must contact a same-net pad, directly or by track/via copper.

    Contacts use the scorecard's centre/pad and via-disk graph model. A pad is
    an electrical anchor, not proof that its circuit is tied to earth.
    """
    d = canonical_data(d)
    out, graphs = {}, {}
    for key, island in islands(fills).items():
        net, layer, _ = key
        pads = [p for p in d['pads'] if p['net'] == net]
        direct = any(layer in p['layers'] and island.covers(Point(p['x']/1e6, p['y']/1e6)) for p in pads)
        if direct or not pads:
            out[key] = direct
            continue
        if net not in graphs:
            graphs[net] = iq.NetGraph(d, net)
        g = graphs[net]
        out[key] = any(g.shortest(('p', i), lambda n: contact(g, d, layer, island, n)) is not None
                       for i in range(len(g.pads)))
    return out


def inferred_references(d, nets):
    """Freeze from HUMAN geometry: nearest ground pin on each endpoint IC.

    Only U-designated endpoint footprints qualify as IC evidence. Tied nearest
    pins on different ground nets, or disagreement between ICs, are unknown.
    Connectors/passives alone cannot establish a signal's reference.
    """
    out = {}
    for net in sorted(nets):
        choices = []
        for p in sorted((p for p in d['pads'] if p['net'] == net and p['ref'].upper().startswith('U')),
                        key=lambda p: (p['ref'], p['x'], p['y'], p['num'])):
            candidates = [q for q in d['pads'] if q['ref'] == p['ref'] and si._GROUND.fullmatch(q['net'])]
            if not candidates:
                choices.append(set())
                continue
            distance = lambda q: (p['x']-q['x'])**2 + (p['y']-q['y'])**2
            best = min(map(distance, candidates))
            choices.append({q['net'] for q in candidates if distance(q) == best})
        out[net] = sorted(choices[0]) if choices and len(choices[0]) == 1 and all(c == choices[0] for c in choices) else None
    return out


def reference_bindings(interfaces):
    bindings, isolated, conflicts = {}, set(), set()
    for iface in (interfaces or {}).get('interfaces', []):
        isolated.update(iface.get('isolated_nets', []))
        for lane in iface['lanes']:
            nets = {lane.get('p'), lane.get('n')}
            # Declared series sections are part of the same reference obligation.
            changed = True
            while changed:
                changed = False
                for series in iface.get('series', []):
                    if series['a'] in nets or series['b'] in nets:
                        before = len(nets)
                        nets.update((series['a'], series['b']))
                        changed |= len(nets) != before
            for net in sorted(n for n in nets if n):
                value = sorted(set(iface.get('reference_nets', []))) or None
                if iface.get('confidence') != 'confirmed':
                    value = None
                if net in bindings and bindings[net] != value:
                    conflicts.add(net)
                bindings[net] = None if net in conflicts else value
    return bindings, isolated


def domain_metrics(d, net, fills, broad, references, *, grounded=None, segments=None, isolated=()):
    out = dict(wrong_reference_mm=None, floating_reference_mm=None, reference_uncovered_mm=None,
               reference_reason=None, intended_reference_nets=references)
    if net in isolated or references and set(references) <= set(isolated):
        out.update(wrong_reference_mm=0.0, floating_reference_mm=0.0, reference_uncovered_mm=0.0,
                   reference_reason='intentional isolated domain: missing-ground checks excluded')
        return out
    if not references:
        out['reference_reason'] = 'intended reference unknown or ambiguous'
        return out
    identities = islands(fills)
    grounded = grounded_islands(d, fills) if grounded is None else grounded
    layers = d['copper_layers']
    lines = si.segs(d, net) if segments is None else segments
    totals = [[], [], []]
    coverage = {}
    reference_layers = broad | {k[1] for k in identities if k[0] in references}
    for s in sorted(lines, key=lambda s: (s['layer'], s['sx'], s['sy'], s['ex'], s['ey'], s['width'])):
        if s['layer'] not in layers:
            out['reference_reason'] = 'unknown signal layer'
            return out
        if s['layer'] not in coverage:
            i = layers.index(s['layer'])
            adjacent = {layers[j] for j in (i-1, i+1) if 0 <= j < len(layers) and layers[j] in reference_layers}
            if not adjacent:
                out['reference_reason'] = 'no adjacent reference layer'
                return out
            eligible = [(k, p) for k, p in identities.items() if k[1] in adjacent]
            coverage[s['layer']] = (
                unary_union([p for k, p in eligible if k[0] in references and grounded[k]]),
                unary_union([p for k, p in eligible if k[0] not in references and si._GROUND.fullmatch(k[0])]),
                unary_union([p for k, p in eligible if k[0] in references and not grounded[k]]))
        intended, wrong, floating = coverage[s['layer']]
        line = LineString([(s['sx'], s['sy']), (s['ex'], s['ey'])])
        totals[0].append(line.intersection(wrong).difference(intended).length)
        totals[1].append(line.intersection(floating).difference(intended).length)
        totals[2].append(line.difference(intended).length)
    if not lines:
        out['reference_reason'] = 'no signal copper'
        return out
    out.update(zip(('wrong_reference_mm', 'floating_reference_mm', 'reference_uncovered_mm'), map(math.fsum, totals)))
    return out


def isolation_gap(d, fills, isolated):
    """Minimum projected copper edge gap, across ALL layers (not creepage)."""
    if not isolated:
        return None, 'isolated domain not declared'
    copper = {True: [], False: []}
    for (net, _), fill in sorted(fills.items()):
        copper[net in isolated].append(fill)
    for net in sorted(d['nets']):
        for s in si.segs(d, net):
            copper[net in isolated].append(copper_buffer(LineString([(s['sx'], s['sy']), (s['ex'], s['ey'])]), s['width']/2))
    for v in d['vias']:
        copper[v['net'] in isolated].append(copper_buffer(Point(v['x']/1e6, v['y']/1e6), v['size']/2e6))
    # A shape-sensitive isolation check cannot substitute bounding rectangles.
    from shapely.affinity import rotate, translate
    from shapely.geometry import box
    for p in d['pads']:
        shape = p.get('shape')
        if shape is None:
            return None, 'pad copper shape missing; supply ctx pad metadata'
        w, h = p['w']/1e6, p['h']/1e6
        if shape == 'rect':
            geometry = box(-w/2, -h/2, w/2, h/2)
        elif shape == 'circle':
            geometry = copper_buffer(Point(0, 0), w/2)
        elif shape in ('oval', 'roundrect'):
            if shape == 'roundrect' and p.get('rr') is None:
                return None, 'roundrect radius missing'
            r = min(w, h)/2 if shape == 'oval' else min(w, h)*p['rr']
            geometry = copper_buffer(box(-w/2+r, -h/2+r, w/2-r, h/2-r), r)
        else:
            return None, 'unsupported pad copper shape'
        copper[p['net'] in isolated].append(translate(rotate(geometry, -p['angle']), p['x']/1e6, p['y']/1e6))
    if not all(copper.values()):
        return None, 'domain copper missing'
    return unary_union(copper[True]).distance(unary_union(copper[False])), None


def measure_rules(d, interfaces, profile, ctx):
    if interfaces is None:
        return []
    fills = ctx['fills']
    grounded = grounded_islands(d, fills)
    broad = ctx.get('broad')
    if broad is None:
        try:
            _, broad, _ = si.fills(ctx['board_path'])
        except (ValueError, OSError):
            broad = set()
    out = []
    for iface in sorted(interfaces['interfaces'], key=lambda i: i['id']):
        isolated = iface.get('isolated_nets', [])
        if iface['interface'] == 'ethernet_mdi':
            shaped = dict(d, pads=[dict(p, **ctx.get('pad_metadata', {}).get(p['ref']+'.'+p['num'], {})) for p in d['pads']])
            value, reason = isolation_gap(shaped, fills, isolated)
            for rule in rules.rules_for(profile, iface['interface'], 'isolation_gap_mm', iface.get('mode')):
                out.append(rules.result(rule, iface['id'], value if iface.get('confidence') == 'confirmed' else None,
                                        reason=reason if iface.get('confidence') == 'confirmed' else 'interface not confirmed'))
        for lane in sorted(iface['lanes'], key=lambda l: l['id']):
            for net in sorted(n for n in (lane.get('p'), lane.get('n')) if n):
                for rule in rules.rules_for(profile, iface['interface'], 'reference_uncovered_mm', iface.get('mode')):
                    if rule.get('role') not in (None, lane['role']):
                        continue
                    reason = None
                    value = None
                    if iface.get('confidence') != 'confirmed':
                        reason = 'interface not confirmed'
                    elif ctx['complete'].get(net) is not True:
                        reason = 'signal connectivity incomplete or unknown'
                    else:
                        segments = ctx.get('lane_segments', {}).get(net) if 'lane_segments' in ctx else None
                        if 'lane_segments' in ctx and segments is None:
                            reason = 'endpoint path unavailable'
                        else:
                            r = domain_metrics(d, net, fills, broad, iface.get('reference_nets'), grounded=grounded,
                                               segments=segments, isolated=isolated)
                            value, reason = r['reference_uncovered_mm'], r['reference_reason']
                    out.append(rules.result(rule, iface['id']+':'+lane['id']+':'+net, value, reason=reason))
    return out
