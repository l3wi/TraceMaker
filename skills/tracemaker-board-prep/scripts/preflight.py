#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only KiCad 10 routing preflight; all dimensions in the report are mm."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


def sexprs(text):
    """Small iterative reader: quoted strings and comments cannot close a list."""
    root, stack = [], []
    current = root
    i = 0
    while i < len(text):
        c = text[i]
        if c.isspace():
            i += 1
        elif c in '#;':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
        elif c == '(':
            node = []
            current.append(node)
            stack.append(current)
            current = node
            i += 1
        elif c == ')':
            if not stack:
                raise ValueError('Unexpected closing parenthesis')
            current = stack.pop()
            i += 1
        elif c == '"':
            i += 1
            value = []
            while i < len(text) and text[i] != '"':
                if text[i] == '\\':
                    i += 1
                    if i >= len(text):
                        raise ValueError('Unterminated string escape')
                    value.append({'n': '\n', 'r': '\r', 't': '\t'}.get(text[i], text[i]))
                else:
                    value.append(text[i])
                i += 1
            if i == len(text):
                raise ValueError('Unterminated string')
            current.append(''.join(value))
            i += 1
        else:
            end = i
            while end < len(text) and not text[end].isspace() and text[end] not in '()':
                end += 1
            current.append(text[i:end])
            i = end
    if stack:
        raise ValueError('Unclosed parenthesis')
    return root


def children(node, head):
    return [n for n in node if isinstance(n, list) and n and n[0] == head]


def child(node, head):
    return next(iter(children(node, head)), [])


def val(node, head, default=None):
    n = child(node, head)
    return n[1] if len(n) > 1 and not isinstance(n[1], list) else default


def number(node, head, default=0):
    return float(val(node, head, default))


def locked(node):
    return 'locked' in node or bool(child(node, 'locked')) and val(node, 'locked') not in ('no', 'false')


def xy(node, head):
    n = child(node, head)
    return tuple(map(float, n[1:3])) if len(n) >= 3 else (0.0, 0.0)


def points(node):
    return [tuple(map(float, p[1:3])) for p in children(child(node, 'pts'), 'xy')]


def arc_bounds(a, mid, b):
    ax, ay = a
    mx, my = mid
    bx, by = b
    determinant = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(determinant) < 1e-12:
        return [a, mid, b]
    aa, mm, bb = ax * ax + ay * ay, mx * mx + my * my, bx * bx + by * by
    cx = (aa * (my - by) + mm * (by - ay) + bb * (ay - my)) / determinant
    cy = (aa * (bx - mx) + mm * (ax - bx) + bb * (mx - ax)) / determinant
    start, middle, end = [math.atan2(p[1] - cy, p[0] - cx) for p in (a, mid, b)]
    span = (end - start) % math.tau
    ccw = (middle - start) % math.tau <= span
    radius = math.hypot(ax - cx, ay - cy)
    result = [a, mid, b]
    for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        inside = ((angle - start) % math.tau <= span if ccw
                  else (start - angle) % math.tau <= (start - end) % math.tau)
        if inside:
            result.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return result


def curve_bounds(control):
    if len(control) != 4:
        return control
    times = {0.0, 1.0}
    for axis in (0, 1):
        p, q, r, s = [v[axis] for v in control]
        a, b, c = -p + 3 * q - 3 * r + s, 2 * (p - 2 * q + r), q - p
        if abs(a) < 1e-12:
            roots = [-c / b] if abs(b) >= 1e-12 else []
        elif b * b >= 4 * a * c:
            d = math.sqrt(b * b - 4 * a * c)
            roots = [(-b + d) / (2 * a), (-b - d) / (2 * a)]
        else:
            roots = []
        times.update(t for t in roots if 0 < t < 1)
    return [tuple((1 - t) ** 3 * control[0][axis] + 3 * (1 - t) ** 2 * t * control[1][axis]
                  + 3 * (1 - t) * t * t * control[2][axis] + t ** 3 * control[3][axis]
                  for axis in (0, 1)) for t in sorted(times)]


def pad_bounds(pad):
    width, height = xy(pad, 'size')
    bounds = [(-width / 2, -height / 2), (width / 2, height / 2)]
    for n in child(pad, 'primitives')[1:]:
        if not isinstance(n, list) or not n:
            continue
        kind = n[0]
        if kind == 'gr_circle':
            c, e = xy(n, 'center'), xy(n, 'end')
            radius = math.dist(c, e)
            vertices = [(c[0] - radius, c[1] - radius), (c[0] + radius, c[1] + radius)]
        elif kind == 'gr_arc':
            vertices = arc_bounds(xy(n, 'start'), xy(n, 'mid'), xy(n, 'end'))
        elif kind in ('gr_poly', 'gr_curve'):
            vertices = points(n)
            if kind == 'gr_curve':
                vertices = curve_bounds(vertices)
        elif kind in ('gr_line', 'gr_rect'):
            vertices = [xy(n, 'start'), xy(n, 'end')]
        else:
            raise ValueError('Unsupported custom pad primitive: ' + kind)
        stroke = number(n, 'width', number(child(n, 'stroke'), 'width')) / 2
        for x, y in vertices:
            bounds.extend(((x - stroke, y - stroke), (x + stroke, y + stroke)))
    return (min(p[0] for p in bounds), min(p[1] for p in bounds),
            max(p[0] for p in bounds), max(p[1] for p in bounds))


def area(poly):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2


def nm(mm):
    return int((Decimal(str(mm)) * 1000000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def pitch(width, clearance):
    # Integer division matches the engine before its 5 µm quantisation.
    return min(100000, max(25000, ((nm(width) + nm(clearance)) // 6 // 5000) * 5000)) / 1000000


# Keep this structural registry aligned with drc::Condition: validate every leaf,
# including branches that a sample evaluation would short-circuit.
CONDITION_PROPERTIES = frozenset({
    'NetClass', 'NetName', 'Type', 'Layer', 'Reference', 'Parent.Reference',
    'Pad_Type', 'Size_X', 'Size_Y', 'Width', 'Position_X', 'Position_Y',
})
COURTYARD_FUNCTIONS = frozenset({
    'intersectsCourtyard', 'intersectsFrontCourtyard', 'intersectsBackCourtyard',
    'insideCourtyard', 'insideFrontCourtyard', 'insideBackCourtyard',
})
CONDITION_FUNCTIONS = {
    'isPlated': 0, 'existsOnLayer': 1, 'insideArea': 1, 'intersectsArea': 1,
    'enclosedByArea': 1, 'inDiffPair': 1, 'memberOfFootprint': 1,
    **{name: 1 for name in COURTYARD_FUNCTIONS},
}
RESIDUAL_TERMS = frozenset({
    'insideArea', 'intersectsArea', 'enclosedByArea', 'memberOfFootprint',
    'Reference', 'Parent.Reference', 'Pad_Type', 'Width', 'Size_X', 'Size_Y',
    'Position_X', 'Position_Y', 'Layer', 'existsOnLayer',
}) | COURTYARD_FUNCTIONS


class Condition:
    """Parse the engine's small condition grammar, not KiCad's full evaluator."""
    def __init__(self, text):
        self.text, self.i, self.refs = text, 0, set()
        self.unsupported = set()

    def skip(self):
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def eat(self, token):
        self.skip()
        if self.text.startswith(token, self.i):
            self.i += len(token)
            return True
        return False

    def string(self):
        start = self.i + 1
        quote = self.text[self.i]
        self.i += 1
        end = self.text.find(quote, self.i)
        if end < 0:
            raise ValueError('unterminated condition string')
        self.i = end + 1
        return self.text[start:end]

    def term(self):
        self.skip()
        if self.i == len(self.text):
            raise ValueError('unexpected end of condition')
        if self.eat('('):
            self.expression()
            if not self.eat(')'):
                raise ValueError("missing ')' in condition")
        elif self.text[self.i] in "'\"":
            self.string()
        elif self.text[self.i] in '+-.0123456789':
            m = re.match(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', self.text[self.i:])
            if not m:
                raise ValueError('invalid condition number')
            number = float(m[0])
            self.i += len(m[0])
            self.skip()
            unit = re.match(r'[A-Za-z]*', self.text[self.i:])[0]
            if unit not in ('', 'mm', 'mil', 'in', 'ps', 'deg', 'fs'):
                raise ValueError('unsupported condition unit ' + unit)
            self.i += len(unit)
            scale = {'mm': 1000000, 'mil': 25400, 'in': 25400000, 'ps': 1000}.get(unit, 1)
            if not math.isfinite(number * scale):
                raise ValueError('invalid condition number')
        else:
            m = re.match(r'[A-Za-z_][A-Za-z_0-9.]*', self.text[self.i:])
            if not m:
                raise ValueError('unsupported condition syntax')
            name = m[0]
            self.i += len(name)
            if name not in ('true', 'false'):
                bound = name.startswith(('A.', 'B.'))
                symbol = name[2:] if name.startswith(('A.', 'B.')) else name
                self.refs.add(symbol)
                if self.eat('('):
                    arguments = []
                    while not self.eat(')'):
                        self.skip()
                        if self.i == len(self.text) or self.text[self.i] not in "'\"":
                            raise ValueError('function arguments must be strings')
                        arguments.append(self.string())
                        self.eat(',')
                    if not bound or symbol not in CONDITION_FUNCTIONS:
                        self.unsupported.add(symbol)
                    elif len(arguments) != CONDITION_FUNCTIONS[symbol]:
                        self.unsupported.add(symbol + ' (wrong argument count)')
                    if arguments:
                        if any('${Class:' in argument for argument in arguments):
                            self.unsupported.add(symbol + ' (${Class:...} metadata)')
                    if len(arguments) == 1:
                        selector = arguments[0]
                        if symbol in COURTYARD_FUNCTIONS:
                            if not selector or selector in ('A', 'B') or selector.startswith('$'):
                                self.unsupported.add(symbol + ' (unavailable footprint selector)')
                        elif symbol in {'insideArea', 'intersectsArea', 'enclosedByArea'}:
                            if not selector or selector == 'B':
                                self.unsupported.add(symbol + ' (unavailable area selector)')
                elif name != 'L' and (not bound or symbol not in CONDITION_PROPERTIES):
                    self.unsupported.add(symbol)

    def unary(self):
        self.skip()
        if self.text.startswith('!', self.i) and not self.text.startswith('!=', self.i):
            self.i += 1
            self.unary()
        else:
            self.term()
            for token in ('==', '!=', '<=', '>=', '<', '>'):
                if self.eat(token):
                    self.term()
                    break

    def conjunction(self):
        self.unary()
        while self.eat('&&'):
            self.unary()

    def expression(self):
        self.conjunction()
        while self.eat('||'):
            self.conjunction()

    def parse(self):
        if self.text:
            self.expression()
            self.skip()
            if self.i != len(self.text):
                raise ValueError('unsupported condition syntax at ' + self.text[self.i:self.i + 10])
        return self.refs


def outline(board):
    shapes = [n for n in board if isinstance(n, list) and n and n[0].startswith('gr_')
              and val(n, 'layer') == 'Edge.Cuts']
    edges, polygons, bounds = [], [], []
    curved = False
    for n in shapes:
        kind = n[0]
        if kind in ('gr_line', 'gr_arc'):
            a, b = xy(n, 'start'), xy(n, 'end')
            edges.append((a, b))
            bounds.extend((a, b))
            if kind == 'gr_arc':
                curved = True
                bounds.extend(arc_bounds(a, xy(n, 'mid'), b))
        elif kind == 'gr_rect':
            a, b = xy(n, 'start'), xy(n, 'end')
            p = [a, (b[0], a[1]), b, (a[0], b[1])]
            polygons.append(p)
            bounds.extend(p)
        elif kind == 'gr_poly':
            p = points(n)
            if len(p) >= 3:
                polygons.append(p)
                bounds.extend(p)
        elif kind == 'gr_circle':
            a, b = xy(n, 'center'), xy(n, 'end')
            r = math.dist(a, b)
            bounds.extend(((a[0] - r, a[1] - r), (a[0] + r, a[1] + r)))
            polygons.append([])
            curved = True
    # Endpoints quantised to 1 µm: sufficient for a preflight, not a geometric DRC.
    key = lambda p: (round(p[0], 3), round(p[1], 3))
    graph = defaultdict(list)
    for index, (a, b) in enumerate(edges):
        graph[key(a)].append((key(b), index))
        graph[key(b)].append((key(a), index))
    seen, loops = set(), []
    for start in sorted(graph):
        if start in seen:
            continue
        component, pending = set(), [start]
        while pending:
            p = pending.pop()
            if p in component:
                continue
            component.add(p)
            pending.extend(q for q, _ in graph[p] if q not in component)
        seen.update(component)
        if len(component) >= 2 and all(len(graph[p]) == 2 for p in component):
            loop, p, previous_edge = [], start, -1
            while True:
                loop.append(p)
                q, edge = next((q, e) for q, e in graph[p] if e != previous_edge)
                p, previous_edge = q, edge
                if p == start:
                    break
            loops.append(loop)
    bbox = (min(p[0] for p in bounds), min(p[1] for p in bounds),
            max(p[0] for p in bounds), max(p[1] for p in bounds)) if bounds else None
    simple = not curved and len(polygons) + len(loops) == 1
    polygon_area = area((polygons + loops)[0]) if simple else 0
    return {'present': bool(shapes), 'closed': bool(polygons or loops), 'bbox': bbox,
            'area_mm2': polygon_area or ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) if bbox else 0),
            'area_method': 'simple outline polygon' if polygon_area else 'outline bbox approximation'}


def class_for(net, classes, settings):
    by_name = {c['name']: c for c in classes}
    names = (settings.get('netclass_assignments') or {}).get(net, []) or []
    if isinstance(names, str):
        names = [names]
    candidates = [by_name[n] for n in names if n in by_name]
    if candidates:
        return candidates[0]
    for p in settings.get('netclass_patterns', []):
        pattern = p.get('pattern', '')
        wildcard = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
        match = re.fullmatch(wildcard, net, re.S) is not None
        if not match:
            try:
                match = re.fullmatch(pattern, net) is not None
            except re.error:
                pass
        if match and p.get('netclass') in by_name:
            return by_name[p['netclass']]
    return classes[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board', type=Path)
    parser.add_argument('--tracemaker')
    parser.add_argument('--json', type=Path)
    parser.add_argument('--small-pad-mm', type=float, default=2)
    args = parser.parse_args()
    if not math.isfinite(args.small_pad_mm) or args.small_pad_mm <= 0:
        parser.error('--small-pad-mm must be positive and finite')
    report = {'board': str(args.board), 'findings': []}
    sections = defaultdict(list)

    def finding(section, severity, text):
        sections[section].append(f'- **{severity}**: {text}')
        report['findings'].append({'section': section, 'severity': severity, 'message': text})

    try:
        expressions = sexprs(args.board.read_text())
        if len(expressions) != 1 or expressions[0][0] != 'kicad_pcb':
            raise ValueError('Not a KiCad PCB s-expression')
        board = expressions[0]
        project_path = args.board.with_suffix('.kicad_pro')
        project = json.loads(project_path.read_text()) if project_path.exists() else {}
        finding('Project and lattice', 'info' if project else 'block',
                f'Project: {project_path}' if project else 'Missing sibling .kicad_pro: restore the matching project before routing; fallback rules are not design intent.')
        settings = project.get('net_settings', {})
        minimums = project.get('board', {}).get('design_settings', {}).get('rules', {})
        if not project:
            minimums = dict(min_via_diameter=0.4, min_through_hole_diameter=0.3,
                            min_via_annular_width=0.1, min_hole_clearance=0.25,
                            min_hole_to_hole=0.25, min_copper_edge_clearance=0.5,
                            min_microvia_diameter=0.2, min_microvia_drill=0.1)
        elif not minimums:
            finding('Project and lattice', 'block', 'Project has no board design rules; TraceMaker uses zero minimums.')
        for key in sorted(minimums):
            if key.startswith('min_'):
                finding('Project and lattice', 'info', f'{key} = {minimums[key]}' + (' spokes' if key == 'min_resolved_spokes' else ' mm'))
        defaults = dict(name='Default', priority=2147483647, track_width=0.25, clearance=0.2, via_diameter=0.6, via_drill=0.3)
        source_classes = settings.get('classes', [])
        defaults.update({k: v for c in source_classes if c.get('name') == 'Default' for k, v in c.items() if v is not None})
        classes = [defaults]
        classes.extend(dict(defaults, **dict({k: v for k, v in c.items() if v is not None},
                                             priority=c.get('priority') if c.get('priority') is not None else 0))
                       for c in source_classes if c.get('name') not in (None, '', 'Default'))
        nets = {n[1]: n[2] for n in children(board, 'net') if len(n) >= 3}
        net_names = sorted(n for n in nets.values() if n)
        used = Counter(class_for(n, classes, settings)['name'] for n in net_names)
        for c in classes:
            c['effective_track'] = max(c['track_width'], minimums.get('min_track_width', 0))
            c['effective_clearance'] = max(c['clearance'], minimums.get('min_clearance', 0))
            c['pitch_mm'] = pitch(c['effective_track'], c['effective_clearance'])
        report['minimums'], report['net_classes'] = minimums, classes
        sections['Project and lattice'].extend(['', '| Class | Nets | Effective track | Effective clearance | Implied pitch |',
                                                 '|---|---:|---:|---:|---:|'])
        for c in classes:
            sections['Project and lattice'].append(f"| {c['name']} | {used[c['name']]} | {c['effective_track']:g} | {c['effective_clearance']:g} | {c['pitch_mm']:g} |")
        finest = min(c['pitch_mm'] for c in classes)
        setters = [c['name'] for c in classes if c['pitch_mm'] == finest]
        report['pitch_mm'] = finest
        finding('Project and lattice', 'info', f"Router pitch: {finest:g} mm, set by {', '.join(setters)}. ALL defined classes count, even unused ones.")
        for c in classes:
            others = [k['pitch_mm'] for k in classes if k is not c]
            if others and c['pitch_mm'] < 0.75 * min(others):
                finding('Project and lattice', 'slow', f"Class {c['name']} has pitch {c['pitch_mm']:g} mm vs next finest {min(others):g} mm; only keep if used ({used[c['name']]} nets currently resolve to it).")
        finding('Project and lattice', 'info', f"netclass_patterns: {len(settings.get('netclass_patterns', []))}; resolved once per net, not in the routing inner loop.")

        rules_path = args.board.with_suffix('.kicad_dru')
        rules = []
        if rules_path.exists():
            try:
                rules = children(sexprs(rules_path.read_text()), 'rule')
            except ValueError as exc:
                finding('Custom rules', 'block', f'Cannot parse {rules_path}: {exc}; routing must stop rather than drop custom constraints.')
        if not rules:
            finding('Custom rules', 'info', 'No parsed custom rules.')
        allowed = {'track', 'via', 'through_via', 'micro_via', 'buried_via', 'blind_via', 'pad', 'zone', 'graphic'}
        # Unknown conditions match conservatively at runtime, but cannot prove
        # KiCad parity. Preflight blocks them rather than calling that fallback support.
        caches_disabled = False
        for rule in rules:
            name, condition = rule[1], val(rule, 'condition', '')
            constraints = children(rule, 'constraint')
            kinds = [c[1] for c in constraints]
            reasons, refs = [], set()
            parsed = Condition(condition)
            condition_valid = True
            try:
                refs = parsed.parse()
            except ValueError as exc:
                condition_valid = False
                reasons.append('unparseable condition: ' + str(exc))
                finding('Custom rules', 'block', f"Rule '{name}': {exc}; the engine conservatively matches unreadable conditions, and ignored rules cannot waive known prior constraints. Fix the condition before routing.")
            missing = sorted(parsed.unsupported)
            if missing:
                reasons.append('unsupported condition terms: ' + ', '.join(missing))
                finding('Custom rules', 'block', f"Rule '{name}': unsupported condition terms: {', '.join(missing)}. Conditions are checked structurally, including short-circuited branches; the runtime fallback conservatively matches rather than silently skipping. memberOfGroup has no existing-item coverage; component-class selector metadata is unavailable.")
            residual = 'disallow' in kinds and bool(refs & RESIDUAL_TERMS)
            if residual:
                reasons.append('disallow has residual geometry/item predicates')
            if any(c[1] == 'disallow' and any(w in ('through_via', 'blind_via', 'buried_via', 'micro_via', 'hole') for w in c[2:])
                   for c in constraints):
                reasons.append('disallow requires actual via subtype/span checks')
            if 'disallow' in kinds and refs & {'NetName', 'inDiffPair'} and not residual:
                finding('Custom rules', 'info', f"Rule '{name}': static net predicates retain caches only when uniform within each class; engine metadata below reports the actual decision.")
            for kind in kinds:
                if kind not in ('disallow', 'physical_hole_clearance'):
                    reasons.append('constraint ' + kind)
                elif kind == 'physical_hole_clearance' and refs & {'NetName', 'NetClass', 'inDiffPair'}:
                    reasons.append('net-dependent physical_hole_clearance')
            caches_disabled |= bool(reasons)
            finding('Custom rules', 'slow' if reasons else 'info',
                    f"Rule '{name}': {', '.join(kinds) or 'no constraints'}; condition `{condition or 'true'}`; "
                    + ('bypasses per-class obstacle caches and cost-to-go fields (' + '; '.join(reasons) + ')' if reasons else 'cache-ok'))
            if 'disallow' in kinds and condition_valid and not missing:
                finding('Custom rules', 'info', f"Rule '{name}': track/via disallow is enforced on actual candidate geometry, including final segments and via type/span; "
                        + ('position/footprint/dimension predicates use the residual exact evaluator, not class-wide cached permission.' if residual else 'static net/type/layer predicates can be pre-evaluated, with exact candidate checks authoritative.'))
            for c in constraints:
                if c[1] == 'disallow':
                    unsupported = [w for w in c[2:] if isinstance(w, str) and w not in allowed]
                    if unsupported:
                        finding('Custom rules', 'block', f"Rule '{name}': disallow {', '.join(unsupported)} lacks exact item coverage; KiCad DRC is still required. Matching hole predicates conservatively reject new vias. Rewrite only if equivalent design intent can be preserved.")
        report['caches_disabled'] = caches_disabled

        for c in classes:
            drill = max(c['via_drill'], minimums.get('min_through_hole_diameter', 0))
            diameter = max(c['via_diameter'], minimums.get('min_via_diameter', 0), drill + 2 * minimums.get('min_via_annular_width', 0))
            changed = nm(diameter) > nm(c['via_diameter']) or nm(drill) > nm(c['via_drill'])
            finding('Vias', 'quality' if changed else 'info',
                    f"{c['name']}: class diameter/drill {c['via_diameter']:g}/{c['via_drill']:g} → effective {diameter:g}/{drill:g} mm"
                    + ('; board minimum overrides class (larger vias cost routing space).' if changed else '.'))
        finding('Vias', 'info', f"Blind/buried permitted: {minimums.get('allow_blind_buried_vias', False)}; microvias permitted: {minimums.get('allow_microvias', False)}.")

        out = outline(board)
        report['outline'] = out
        finding('Outline and placement', 'info' if out['closed'] else 'block',
                f"Edge.Cuts present: {out['present']}; closed loop detected: {out['closed']}; area {out['area_mm2']:g} mm² ({out['area_method']}). Closure/bounds are approximate; KiCad DRC is authoritative.")
        copper_layers = [n[1] for n in child(board, 'layers')[1:] if isinstance(n, list) and len(n) > 1 and n[1].endswith('.Cu')]
        planes, zone_details = [], []
        for index, z in enumerate(children(board, 'zone'), 1):
            keepout = child(z, 'keepout')
            if keepout:
                flags = ', '.join(f'{n[0]}={n[1]}' for n in keepout[1:] if isinstance(n, list) and len(n) > 1)
                finding('Zones', 'info', f'Rule area {index}: {flags}; layers {val(z, "layer", child(z, "layers")[1:])}.')
                continue
            layers = child(z, 'layers')[1:] or [val(z, 'layer', '')]
            layers = copper_layers if '*.Cu' in layers else [l for l in layers if l.endswith('.Cu')]
            if not layers:
                continue
            net = val(z, 'net_name', nets.get(val(z, 'net'), ''))
            fill = child(z, 'fill')
            bridge, gap = number(fill, 'thermal_bridge_width'), number(fill, 'thermal_gap')
            mode = val(z, 'connect_pads', 'thermal (default)')
            polygons = [points(p) for p in children(z, 'polygon')]
            zone_area = sum(area(p) for p in polygons)
            fraction = zone_area / out['area_mm2'] if out['area_mm2'] else 0
            filled_layers = {val(p, 'layer', layers[0]) for p in children(z, 'filled_polygon')}
            filled = bool(filled_layers) and all(l in filled_layers for l in layers)
            teardrop = bool(child(child(z, 'attr'), 'teardrop'))
            plane = bool(net) and fraction >= 0.25 and not teardrop
            detail = dict(index=index, net=net, layers=layers, priority=number(z, 'priority'), filled=filled,
                          connect_pads=mode, thermal_gap=gap, thermal_bridge_width=bridge,
                          island_removal_mode=val(fill, 'island_removal_mode', 'default'),
                          island_area_min=val(fill, 'island_area_min', 'default'),
                          area_fraction=fraction, plane=plane, teardrop=teardrop)
            zone_details.append(detail)
            if plane:
                planes.append(detail)
            finding('Zones', 'info', f"Zone {index}: net {net or '(none)'}, layers {', '.join(layers)}, priority {detail['priority']:g}, filled={filled}, connect_pads={mode}, thermal gap/bridge={gap:g}/{bridge:g}, island removal={detail['island_removal_mode']}, island area min={detail['island_area_min']}; outline-area ratio {fraction:.1%} per layer" + ('; PLANE.' if plane else '.'))
            if not filled:
                finding('Zones', 'block', f'Zone {index} unfilled on one or more layers: refill in KiCad before routing so connectivity is current.')
            if teardrop:
                finding('Zones', 'block', f'Zone {index} is a teardrop: Edit → Remove Teardrops before stripping tracks/routing; regenerate after routing.')
            if not net:
                finding('Zones', 'info', f'Zone {index} has no net; not a conductive routing plane.')
            width = class_for(net, classes, settings)['track_width']
            spokes = minimums.get('min_resolved_spokes', 0)
            narrow = bridge < width or spokes >= 2 and bridge <= width
            if narrow:
                finding('Zones', 'quality', f'Zone {index}: starved-thermal risk if thermal relief is used (bridge {bridge:g}, class track {width:g}, min_resolved_spokes {spokes}, connect_pads={mode}; pads can override the zone mode). Narrow means bridge ≤ class track width for the spoke-count heuristic. Router does not model thermal spoke starvation. Refill and judge in KiCad.')
        report['zones'], report['planes'] = zone_details, len(planes)
        if not zone_details:
            finding('Zones', 'info', 'No conductive zones.')
        finding('Zones', 'info', 'Plane classification uses zone outline areas, not clipped/refilled copper; overlaps, cutouts and clearance subtraction can change actual coverage.')

        routed = 0
        for kind in ('segment', 'arc', 'via'):
            items = children(board, kind)
            count = sum(locked(n) for n in items)
            routed += len(items)
            finding('Existing copper', 'info', f'{kind}: {len(items)} total, {count} locked, {len(items) - count} unlocked.')
        footprints = children(board, 'footprint')
        refs_locked = []
        small, pads_by_net = 0, defaultdict(set)
        for fp in footprints:
            ref = next((p[2] for p in children(fp, 'property') if p[1] == 'Reference'), val(fp, 'reference', '?'))
            if locked(fp):
                refs_locked.append(ref)
            at = child(fp, 'at')
            origin = xy(fp, 'at')
            angle = math.radians(float(at[3]) if len(at) > 3 else 0)
            pad_boxes = []
            for pad in children(fp, 'pad'):
                layers = child(pad, 'layers')[1:]
                if not any(l.endswith('.Cu') for l in layers):
                    continue
                local_bounds = pad_bounds(pad)
                size = (local_bounds[2] - local_bounds[0], local_bounds[3] - local_bounds[1])
                if pad[2] == 'smd' and size[0] < args.small_pad_mm and size[1] < args.small_pad_mm:
                    small += 1
                net = val(pad, 'net', '0')
                if net != '0':
                    pads_by_net[net].add((ref, pad[1]))
                p = xy(pad, 'at')
                x = origin[0] + p[0] * math.cos(angle) + p[1] * math.sin(angle)
                y = origin[1] - p[0] * math.sin(angle) + p[1] * math.cos(angle)
                # Pad angles in the board are absolute, unlike their positions.
                pa = child(pad, 'at')
                rotation = math.radians(float(pa[3]) if len(pa) > 3 else 0)
                corners = [(lx, ly) for lx in (local_bounds[0], local_bounds[2])
                           for ly in (local_bounds[1], local_bounds[3])]
                world = [(x + lx * math.cos(rotation) + ly * math.sin(rotation),
                          y - lx * math.sin(rotation) + ly * math.cos(rotation)) for lx, ly in corners]
                pad_boxes.append((min(p[0] for p in world), min(p[1] for p in world),
                                  max(p[0] for p in world), max(p[1] for p in world)))
            bbox = out['bbox']
            if bbox and pad_boxes and all(b[2] < bbox[0] or b[0] > bbox[2] or b[3] < bbox[1] or b[1] > bbox[3] for b in pad_boxes):
                finding('Outline and placement', 'info', f'{ref}: all copper pads entirely outside outline bbox; treated as unplaced (conservative rotated pad bounds).')
        finding('Existing copper', 'info', f"Locked footprints: {len(refs_locked)}" + (': ' + ', '.join(sorted(refs_locked)) if refs_locked else '.'))
        finding('Existing copper', 'info', 'Locked items are never moved/ripped. Existing unlocked routing is kept and routed around; rip-up may move it.')
        finding('Pads', 'quality' if small and planes else 'info', f'{small} SMD copper pads below {args.small_pad_mm:g} mm in BOTH dimensions (paste-only ignored).'
                + (' Use --keep-vias-off-pads with the plane strategy.' if small and planes else ' --keep-vias-off-pads matters when using a plane strategy.'))
        report['small_smd_pads'] = small

        names = set(net_names)
        pairs = sorted((n, n[:-1] + partner) for n in net_names for suffix, partner in (('P', 'N'), ('+', '-'))
                       if n.endswith(suffix) and len(n) >= 2 and n[:-1] + partner in names)
        report['diff_pairs'] = pairs
        finding('Differential pairs', 'info', f'{len(pairs)} final P/N or +/- pairs: ' + ('; '.join(f'{a} ↔ {b}' for a, b in pairs) or 'none'))
        misses = set()
        for n in net_names:
            for ending, replacement in (('DP', 'DM'), ('_P', '_M'), ('D+', 'DP'), ('D-', 'DM'), ('DP', 'D-'), ('DM', 'D+')):
                if n.endswith(ending):
                    other = n[:-len(ending)] + replacement
                    if other in names and (n, other) not in pairs and (other, n) not in pairs:
                        misses.add(tuple(sorted((n, other))))
            if n[-1:] in ('P', 'N', 'p', 'n', '+', '-'):
                partner = {'P': 'N', 'N': 'P', 'p': 'n', 'n': 'p', '+': '-', '-': '+'}[n[-1]]
                desired = n[:-1] + partner
                for other in net_names:
                    if other != n and other.casefold() == desired.casefold() and not any(n in p and other in p for p in pairs):
                        misses.add(tuple(sorted((n, other))))
        for a, b in sorted(misses):
            finding('Differential pairs', 'quality', f'Possible near-miss {a} / {b}: not paired. If these are a differential pair, rename to identical case-sensitive stems with final uppercase P/N or +/- (e.g. USB_P/USB_N or USB_D+/USB_D-).')

        estimate = sum(max(0, len(p) - 1) for p in pads_by_net.values())
        connections, connection_source = estimate, 'pad-count estimate, before existing copper/planes'
        binary = shutil.which(args.tracemaker or os.environ.get('TRACEMAKER', 'tracemaker'))
        if not binary:
            finding('Escape and engine warnings', 'info', 'TraceMaker not found: skipping engine DRC warnings and escape checks; set --tracemaker or TRACEMAKER. This is not routing clearance sign-off.')
        else:
            try:
                with tempfile.TemporaryDirectory(prefix='tracemaker-preflight-drc-') as drc_tmp:
                    drc_path = Path(drc_tmp) / 'drc.json'
                    p = subprocess.run([binary, 'drc', str(args.board), '--json', str(drc_path)],
                                       capture_output=True, text=True, timeout=55)
                    if drc_path.exists():
                        engine_drc = json.loads(drc_path.read_text())
                        if 'needs_exact_routing' in engine_drc:
                            report['caches_disabled'] = engine_drc['needs_exact_routing']
                            finding('Custom rules', 'slow' if report['caches_disabled'] else 'info',
                                    'Engine capability metadata: class caches/fields '
                                    + ('bypassed for item residuals or differing class predicates.' if report['caches_disabled'] else 'remain enabled.'))
                text = p.stdout + '\n' + p.stderr
                warnings = [line for line in text.splitlines() if 'warning:' in line]
                report['drc_warnings'] = warnings
                # Rule warnings repeat the custom-rule findings above; keep them verbatim for the user.
                for line in warnings:
                    coverage_gap = any(term in line for term in (
                        'unsupported symbol', 'unsupported disallow item', 'cannot parse condition',
                        'custom rules could not be read',
                    ))
                    finding('Escape and engine warnings', 'block' if coverage_gap else 'info', line)
                if not warnings:
                    finding('Escape and engine warnings', 'info', 'TraceMaker DRC emitted no rule warnings.')
                if p.returncode not in (0, 5):
                    finding('Escape and engine warnings', 'block', f'TraceMaker DRC failed (exit {p.returncode}): {text.strip()}')
                m = re.search(r'^unconnected_items\s+(\d+)\s*$', text, re.M)
                if p.returncode in (0, 5):
                    connections = int(m[1]) if m else 0
                    connection_source = 'TraceMaker DRC unconnected_items'
                with tempfile.TemporaryDirectory(prefix='tracemaker-preflight-') as tmp:
                    escape_path = Path(tmp) / 'escape.json'
                    p = subprocess.run([binary, 'escape', str(args.board), '--json', str(escape_path)],
                                       capture_output=True, text=True, timeout=55)
                    if p.returncode != 0 or not escape_path.exists():
                        raise RuntimeError(f'TraceMaker escape failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()}')
                    escape = json.loads(escape_path.read_text())
                report['escape'] = escape
                dead = escape['dead']
                descriptions = [f"{part['ref']}: {', '.join(group['pins'][:12])}"
                                + (f" (+{len(group['pins']) - 12} more)" if len(group['pins']) > 12 else '')
                                + f" ({group['reason']})"
                                for part in escape.get('parts', []) for group in part.get('dead', [])]
                # Lattice-based: "dead" means no escape on the router's lattice, not proof that none exists. The
                # analysis keeps existing tracks/vias and zone fills as fixed obstacles and ignores connectivity.
                caveats = []
                if dead and routed:
                    caveats.append(f'the board already has {routed} tracks/arcs/vias: they count as obstacles and connected pins are not skipped, so strip unlocked routing for a true count')
                if dead and any(z.get('filled') for z in zone_details):
                    caveats.append('zone fills count as fixed copper here; with --soft-zones some of these pins may escape through refillable planes')
                finding('Escape and engine warnings', 'quality' if dead else 'info',
                        f'Dead escape pins: {dead}; ' + ('; '.join(descriptions[:10]) if dead else 'dense-package pins checked can escape.')
                        + ('; more in JSON report' if len(descriptions) > 10 else '')
                        + (' Caveats: ' + '; '.join(caveats) + '.' if caveats else ''))
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                finding('Escape and engine warnings', 'info', f'Engine checks unavailable: {exc}')
        work = 1000000 if connections < 100 else 10000000 if connections < 500 else 50000000
        command = [binary or 'tracemaker', 'route', str(args.board), '-o',
                   str(args.board.with_name(args.board.stem + '-routed.kicad_pcb')), '--json', 'route.json']
        if planes:
            command.extend(['--soft-zones', '--keep-vias-off-pads'])
            if args.small_pad_mm != 2:
                command.append(f'{args.small_pad_mm:g}')
        if pairs:
            command.append('--diff-pairs')
        command.extend(['--work', str(work)])
        report.update(connections=connections, connection_source=connection_source, recommended_command=shlex.join(command))
        finding('Recommended routing', 'info', f'{connections} connections ({connection_source}); --work {work} is a starting point, not a completion guarantee. Budget tiers: <100: 1M; <500: 10M; otherwise 50M.')
        finding('Recommended routing', 'info', 'Resolve block findings first. Review quality/slow findings; then sign off both boards with refilled KiCad DRC.')
        print(f'# TraceMaker board preflight: {args.board.name}\n\nUnits: mm. Severity: block / slow / quality / info. Read-only; not an exact geometric DRC.')
        for title, lines in sections.items():
            print(f'\n## {title}\n')
            print('\n'.join(lines))
        print('\n```sh\n' + report['recommended_command'] + '\n```')
        report['severity_counts'] = dict(sorted(Counter(f['severity'] for f in report['findings']).items()))
        if args.json:
            args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
        return 0
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        print(f'Preflight tool error: {exc}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
