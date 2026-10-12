# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared benchmark preparation: remove routing, preserve design copper, refill.

Via classification uses physical copper contact with any original track on a
layer in its span, not net names or proximity to a pad. No-track vias are fixed
thermal/stitching design copper; their intent is not inferred from a package.
"""
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager

from plane_metrics import child, children, parse, point

SLOW = '#slow '


def set_boards(path, full=True):
    """Board names of a bench/sets/*.txt file. Lines starting `#slow ` are boards left out of everyday runs (doc 10
    §4); they are prepared always and routed only with `full` (run.py --full)."""
    names = []
    for line in pathlib.Path(path).read_text().splitlines():
        s = line.strip()
        if s.startswith(SLOW):
            if full:
                names.append(s[len(SLOW):].strip())
        elif s and not s.startswith('#'):
            names.append(s)
    return names



def expressions(text):
    """Yield balanced expression spans (start, end, depth, head)."""
    stack = []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+', text):
        token = match.group()
        if token == '(':
            stack.append([match.start(), len(stack), None])
        elif token == ')':
            if not stack:
                raise ValueError('unbalanced expression')
            start, depth, head = stack.pop()
            yield start, match.end(), depth, head
        elif stack and stack[-1][2] is None:
            stack[-1][2] = token
    if stack:
        raise ValueError('unbalanced expression')


def without_spans(text, spans):
    result, end = [], 0
    for start, stop in sorted(spans):
        if start < end:  # A removed parent already covers this expression.
            continue
        result.append(text[end:start])
        end = stop
    result.append(text[end:])
    return ''.join(result)


def arc_circle(node):
    start, end = point(node, 'start'), point(node, 'end')
    if child(node, 'mid') is None:
        centre, first = start, end
        sweep = math.radians(float(child(node, 'angle')[1]))
    else:
        first, mid = start, point(node, 'mid')
        ax, ay = first
        bx, by = mid
        cx, cy = end
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-12:
            raise ValueError('collinear track arc')
        a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
        centre = ((a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d,
                  (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d)
        angles = [math.atan2(p[1] - centre[1], p[0] - centre[0]) for p in (first, mid, end)]
        sweep = (angles[2] - angles[0]) % math.tau
        if (angles[1] - angles[0]) % math.tau > sweep:
            sweep -= math.tau
    return centre, math.dist(first, centre), math.atan2(first[1] - centre[1], first[0] - centre[0]), sweep


def track_distance(p, node):
    if node[0] == 'segment':
        a, b = point(node, 'start'), point(node, 'end')
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2)) if length2 else 0
        return math.dist(p, (a[0] + t * dx, a[1] + t * dy))
    centre, radius, angle, sweep = arc_circle(node)
    theta = math.atan2(p[1] - centre[1], p[0] - centre[0])
    delta = (theta - angle) % math.tau if sweep >= 0 else (angle - theta) % math.tau
    if delta <= abs(sweep) + 1e-12:
        return abs(math.dist(p, centre) - radius)
    ends = [(centre[0] + radius * math.cos(a), centre[1] + radius * math.sin(a)) for a in (angle, angle + sweep)]
    return min(math.dist(p, a) for a in ends)


def strip_design_routing(text):
    board = parse(text)
    tracks = children(board, 'segment') + children(board, 'arc')
    layers = [n[1] for n in child(board, 'layers', []) if isinstance(n, list) and len(n) > 2
              and n[2] in ('signal', 'power', 'mixed', 'jumper')]
    # Sparse cells avoid scanning every track for every via on dense boards.
    cells = defaultdict(list)
    pitch = 2.0
    for track in tracks:
        half = float(child(track, 'width')[1]) / 2
        if track[0] == 'arc':
            centre, radius, _, _ = arc_circle(track)
            lo = (centre[0] - radius - half, centre[1] - radius - half)
            hi = (centre[0] + radius + half, centre[1] + radius + half)
        else:
            a, b = point(track, 'start'), point(track, 'end')
            lo, hi = (min(a[0], b[0]) - half, min(a[1], b[1]) - half), (max(a[0], b[0]) + half, max(a[1], b[1]) + half)
        for x in range(math.floor(lo[0] / pitch), math.floor(hi[0] / pitch) + 1):
            for y in range(math.floor(lo[1] / pitch), math.floor(hi[1] / pitch) + 1):
                cells[child(track, 'layer')[1], x, y].append(track)
    via_spans = sorted((s, e) for s, e, depth, head in expressions(text) if depth == 1 and head == 'via')
    removed, kept = [], 0
    for start, stop in via_spans:
        via = parse(text[start:stop])
        p, radius = point(via, 'at'), float(child(via, 'size')[1]) / 2
        span = child(via, 'layers')[1:]
        if '*.Cu' in span:
            via_layers = layers
        else:
            indexes = [layers.index(layer) for layer in span]
            via_layers = layers[min(indexes):max(indexes) + 1]
        touches = False
        for layer in via_layers:
            for x in range(math.floor((p[0] - radius) / pitch), math.floor((p[0] + radius) / pitch) + 1):
                for y in range(math.floor((p[1] - radius) / pitch), math.floor((p[1] + radius) / pitch) + 1):
                    if any(track_distance(p, t) <= radius + float(child(t, 'width')[1]) / 2 + 1e-7
                           for t in cells.get((layer, x, y), ())):
                        touches = True
                        break
                if touches:
                    break
            if touches:
                break
        if touches:
            removed.append((start, stop))
        else:
            kept += 1
    zones = [(s, e) for s, e, depth, head in expressions(text) if depth == 1 and head == 'zone']
    for start, stop in zones:
        removed.extend((start + s, start + e) for s, e, depth, head in expressions(text[start:stop])
                       if depth == 1 and head in ('filled_polygon', 'fill_segments'))
    removed.extend((s, e) for s, e, depth, head in expressions(text) if depth == 1 and head in ('segment', 'arc'))
    return without_spans(text, removed), {'tracks_removed': len(tracks), 'vias_removed': len(via_spans) - kept,
                                        'fixed_vias': kept, 'zones': len(zones)}


def copy_rules(source, target, fallback=None):
    for ext in ('.kicad_pro', '.kicad_dru'):
        dst = target.with_suffix(ext)
        src = source.with_suffix(ext)
        if not src.exists() and fallback is not None:
            src = fallback.with_suffix(ext)
        if src.exists():
            if src.resolve() != dst.resolve():
                shutil.copyfile(src, dst)
        elif dst.exists():
            dst.unlink()  # Do not reuse rules from an earlier fixture revision.


def refill_board(source, target, report, timeout=600, board_name=None):
    """Convert, repair identities and refill a private copy, leaving the source untouched."""
    target.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    board_name = board_name or source.parent.name
    with tempfile.TemporaryDirectory(prefix='.refill-', dir=target.parent) as tmp:
        work = pathlib.Path(tmp) / target.name
        private_report = pathlib.Path(tmp) / 'drc.json'
        shutil.copyfile(source, work)
        copy_rules(source, work)

        def refill():
            private_report.unlink(missing_ok=True)
            result = subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all', '--all-track-errors',
                                     '--refill-zones', '--save-board', '-o', str(private_report), str(work)],
                                    capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0 or not private_report.exists():
                raise RuntimeError(f'KiCad refill failed for {source}: {result.stderr or result.stdout}')
            json.loads(private_report.read_text())

        refill()  # Legacy conversion must precede native UUID repair.
        corrected, repairs = repair_design_ids(work.read_text(), board_name)
        if repairs:
            work.write_text(corrected)
            refill()
        # KiCad aliases dimension display-text UUIDs to their parent on save.
        corrected, post_save_repairs = repair_design_ids(work.read_text(), board_name)
        if post_save_repairs:
            work.write_text(corrected)
        copy_rules(source, target)
        os.replace(work, target)
        os.replace(private_report, report)
        return {'identity_repairs': dict(Counter(repairs) + Counter(post_save_repairs)),
                'post_save_identity_repairs': post_save_repairs}

def canonical_net_name(name):
    # KiCad upgrades legacy overbar syntax without changing electrical identity.
    if '~{' not in name:
        return re.sub(r'~([^~/\s]+)~?', r'~{\1}', name)
    return name



def terminal_signature(text):
    """Net names, footprint references and pad/net multiplicities across KiCad versions."""
    board = parse(text)
    nets = {n[1]: canonical_net_name(n[2]) for n in children(board, 'net') if len(n) > 2}
    def net_name(node):
        n = child(node, 'net', ['', '0'])
        return canonical_net_name(n[2] if len(n) > 2 else nets.get(n[1], n[1] if n[1] != '0' else ''))
    refs, pads = Counter(), Counter()
    for fp in children(board, 'footprint') + children(board, 'module'):
        ref = next((p[2] for p in children(fp, 'property') if p[1] == 'Reference'), None)
        if ref is None:
            ref = next((p[2] for p in children(fp, 'fp_text') if p[1] == 'reference'), '')
        refs[ref] += 1
        for pad in children(fp, 'pad'):
            # KiCad clears meaningless numbers on nonplated mechanical holes.
            number = '' if pad[2] == 'np_thru_hole' else pad[1]
            pads[ref, number, net_name(pad)] += 1
    # Unused standalone declarations are pruned by KiCad's loader. Electrical
    # identity is the terminal-bearing net set; copper net mapping is checked
    # separately against the full published declaration table before transplant.
    net_names = {key[2] for key in pads}
    net_names.discard('')
    return net_names, refs, pads
def repair_design_ids(text, board_name):
    """Make native saved identities unique before the authoritative refill.

    KiCad's legacy loader can emit all-zero IDs for zones; shared rule-area IDs
    then corrupt keepout checking. Keep every valid unique ID, repair every
    duplicate/zero ID, and supply missing zone/via IDs deterministically.
    """
    spans = list(expressions(text))
    used = {str(uuid.UUID(parse(text[s:e])[1])) for s, e, _, head in spans if head == 'uuid'}
    seen, changes, repairs = set(), [], Counter()
    for index, (start, stop, depth, head) in enumerate(sorted(spans)):
        if head != 'uuid':
            continue
        value = str(uuid.UUID(parse(text[start:stop])[1]))
        if uuid.UUID(value).int and value not in seen:
            seen.add(value)
            continue
        attempt = 0
        while True:
            replacement = str(uuid.uuid5(uuid.NAMESPACE_URL, f'tracemaker-prepared:{board_name}:identity:{index}:{attempt}'))
            if replacement not in used:
                break
            attempt += 1
        used.add(replacement)
        seen.add(replacement)
        changes.append((start, stop, f'(uuid "{replacement}")'))
        repairs['zero' if not uuid.UUID(value).int else 'duplicate'] += 1
    for index, (start, stop, depth, head) in enumerate(sorted(spans)):
        if depth != 1 or head not in ('zone', 'via') or child(parse(text[start:stop]), 'uuid') is not None:
            continue
        replacement = str(uuid.uuid5(uuid.NAMESPACE_URL, f'tracemaker-prepared:{board_name}:{head}:{index}'))
        attempt = 0
        while replacement in used:
            attempt += 1
            replacement = str(uuid.uuid5(uuid.NAMESPACE_URL, f'tracemaker-prepared:{board_name}:{head}:{index}:{attempt}'))
        used.add(replacement)
        changes.append((stop - 1, stop - 1, f'\n(uuid "{replacement}")'))
        repairs[f'missing_{head}'] += 1
    for start, stop, replacement in sorted(changes, reverse=True):
        text = text[:start] + replacement + text[stop:]
    return text, dict(repairs)




def invalidate_fixture(destination, error):
    """Make old fixture bytes ineligible without rewriting the human reference."""
    destination.mkdir(parents=True, exist_ok=True)
    record = {'state': 'invalid', 'board': destination.name, 'reason': str(error)}
    with tempfile.NamedTemporaryFile(mode='w', prefix='.preparation-', dir=destination, delete=False) as handle:
        temporary = pathlib.Path(handle.name)
        json.dump(record, handle, indent=2)
        handle.write('\n')
    os.replace(temporary, destination / 'preparation.json')


@contextmanager
def fixture_transaction(destination, metadata):
    """Publish a whole validated fixture; failures never leave a successful manifest."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    invalidate_fixture(destination, 'preparation in progress')
    try:
        with tempfile.TemporaryDirectory(prefix=f'.prepare-{destination.name}-', dir=destination.parent) as tmp:
            stage = pathlib.Path(tmp) / 'fixture'
            stage.mkdir()
            yield stage
            files = {path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(stage.rglob('*')) if path.is_file() and path.name != 'preparation.json'}
            if 'unrouted.kicad_pcb' not in files:
                raise ValueError('preparation did not produce unrouted.kicad_pcb')
            metadata.update(state='valid', files=files,
                            input_sha256=files['unrouted.kicad_pcb'])
            (stage / 'preparation.json').write_text(json.dumps(metadata, indent=2) + '\n')
            previous = pathlib.Path(tmp) / 'previous'
            os.replace(destination, previous)
            try:
                os.replace(stage, destination)
            except BaseException:
                os.replace(previous, destination)
                raise
    except BaseException as error:
        invalidate_fixture(destination, error)
        raise


def prepare_board(source, destination, input_text=None, rules_source=None, metadata=None, expected_terminals=None):
    stats = {'board': destination.parent.name, **(metadata or {})}
    with fixture_transaction(destination.parent, stats) as stage:
        text = source.read_text() if input_text is None else input_text
        rules_source = rules_source or source
        stripped, stripping = strip_design_routing(text)
        # Transplant statistics describe the raw design, not just the already-stripped input.
        stats.update({**stripping, **stats})
        human = stage / 'routed.kicad_pcb'
        shutil.copyfile(source, human)
        copy_rules(rules_source, human, fallback=source)
        with tempfile.TemporaryDirectory(prefix='.strip-', dir=stage.parent) as tmp:
            scratch = pathlib.Path(tmp) / destination.name
            scratch.write_text(stripped)
            copy_rules(rules_source, scratch, fallback=source)
            stats.update(refill_board(scratch, stage / destination.name, stage / 'preparation.drc.json',
                                      board_name=destination.parent.name))
        expected = terminal_signature(text) if expected_terminals is None else expected_terminals
        if terminal_signature((stage / destination.name).read_text()) != expected:
            raise ValueError(f'{source}: refill changed nets, footprint references or pads')
        stats.update(source=str(source), source_sha256=hashlib.sha256(human.read_bytes()).hexdigest(),
                     refilled=True, human_available=True, report='preparation.drc.json')
    return stats
