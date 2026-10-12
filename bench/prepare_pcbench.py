#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare quick/mid with PCBench's published terminals and raw design copper.

  python3 bench/prepare_pcbench.py --set quick --set mid

The published unrouted board's references/pads/nets are authoritative (PCBench
renames duplicate references). Transplant only raw zone definitions and fixed
no-track/pad-array vias, mapping nets by name after checking outline/layer compatibility. Incompatible
boards retain the published input and an explicit coverage warning; human stays raw.
"""
import argparse
import json
import pathlib
import subprocess
import sys

from plane_metrics import child, children, parse
from prepare_inputs import (expressions, invalidate_fixture, prepare_board, set_boards,
                            strip_design_routing, terminal_signature, without_spans)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'bench/data/freerouting/scripts/benchmark/fixtures/PCBench'


def copper_layers(board):
    return [n for n in child(board, 'layers', []) if isinstance(n, list) and len(n) > 2
            and n[2] in ('signal', 'power', 'mixed', 'jumper')]


def board_outline(board):
    # Timestamps/UUIDs and stroke style are not board geometry. Legacy graphics
    # use width instead of stroke, but their geometric expressions are identical.
    geometry = []
    for node in board:
        if not isinstance(node, list) or child(node, 'layer', ['', ''])[1] != 'Edge.Cuts':
            continue
        geometry.append([node[0]] + [n for n in node if isinstance(n, list)
                                    and n[0] in ('start', 'end', 'mid', 'angle', 'center', 'pts')])
    return sorted(geometry, key=lambda n: json.dumps(n))


def transplant_design_copper(raw_text, published_text):
    raw, published = parse(raw_text), parse(published_text)
    a, b = copper_layers(raw), copper_layers(published)
    layer_mismatch = [(n[0], n[2]) for n in a] != [(n[0], n[2]) for n in b]
    outline_mismatch = board_outline(raw) != board_outline(published)
    if layer_mismatch or outline_mismatch:
        stripped, stats = strip_design_routing(published_text)
        stats.update({'zone_preparation': 'none', 'design_copper_comparable': False,
                      'reason': 'raw versus published ' + '/'.join(label for label, differs in
                                 (('copper layer stack', layer_mismatch), ('board outline', outline_mismatch)) if differs)
                                 + ' mismatch; retained published input without transplantation'})
        return stripped, stats
    layer_names = {x[1]: y[1] for x, y in zip(a, b)}
    raw_nets = {n[1]: n[2] for n in children(raw, 'net')}
    published_nets = {n[2]: n[1] for n in children(published, 'net')}
    stripped, stats = strip_design_routing(raw_text)
    nodes = []
    dropped_zones = []
    for start, stop, depth, head in expressions(stripped):
        if depth != 1 or head not in ('zone', 'via'):
            continue
        text = stripped[start:stop]
        replacements = []
        skip = False
        for s, e, d, key in expressions(text):
            if key not in ('net', 'net_name', 'layer', 'layers'):
                continue
            node = parse(text[s:e])
            if key == 'net':
                name = node[2] if len(node) > 2 else raw_nets.get(node[1], node[1] if node[1] != '0' else '')
                if name not in published_nets:
                    if head != 'zone':
                        raise ValueError(f'fixed {head} net {name!r} has no published net match')
                    dropped_zones.append(name)
                    skip = True
                    break
                replacement = f'(net {published_nets[name]})'
            elif key == 'net_name':
                replacement = '(net_name ' + json.dumps(node[1]) + ')'
            else:
                replacement = '(' + key + ' ' + ' '.join(json.dumps(layer_names.get(v, v)) for v in node[1:]) + ')'
            replacements.append((s, e, replacement))
        if not skip:
            for s, e, replacement in sorted(replacements, reverse=True):
                text = text[:s] + replacement + text[e:]
            nodes.append(text)
    base = without_spans(published_text, [(s, e) for s, e, depth, head in expressions(published_text)
                                         if depth == 1 and head in ('zone', 'via', 'segment', 'arc')])
    closing = base.rfind(')')
    stats.update({'zones': sum(parse(n)[0] == 'zone' for n in nodes),
                  'fixed_vias': sum(parse(n)[0] == 'via' for n in nodes),
                  'zone_preparation': 'transplanted' if any(parse(n)[0] == 'zone' for n in nodes) else 'none',
                  'dropped_zone_nets': sorted(dropped_zones), 'design_copper_comparable': not dropped_zones,
                  'reason': 'dropped unmappable raw zones: ' + ', '.join(sorted(dropped_zones)) if dropped_zones else None})
    return base[:closing] + '\n' + '\n'.join(nodes) + '\n' + base[closing:], stats


def prepare(name, source, out):
    invalidate_fixture(out / name, 'preparation in progress')
    raw, published = source / name / 'raw.kicad_pcb', source / name / 'unrouted.kicad_pcb'
    text = raw.read_text()
    board = parse(text)
    tracks = len(children(board, 'segment')) + len(children(board, 'arc'))
    truth_path = raw.with_name('ground_truth.json')
    truth = json.loads(truth_path.read_text())
    if pathlib.PureWindowsPath(truth.get('source_pcb', '')).name != 'raw.kicad_pcb':
        raise ValueError('ground_truth.json does not identify raw.kicad_pcb as the human reference')
    if tracks == 0 or truth.get('segment_count') != len(children(board, 'segment')):
        raise ValueError('raw human routing is missing or disagrees with ground_truth segment_count')
    published_text = published.read_text()
    expected = terminal_signature(published_text)
    original = terminal_signature(text)
    labels = ('net names', 'footprint references', 'pad/net multiplicities')
    differences = [label for label, a, b in zip(labels, original, expected) if a != b]
    transplanted, raw_stats = transplant_design_copper(text, published_text)
    destination = out / name / 'unrouted.kicad_pcb'
    raw_stats.update({'board': name, 'identity_matches_pcbench': True, 'human_confirmed': True,
                      'human_tracks': tracks, 'ground_truth_source': truth['source_pcb'],
                      'raw_published_differences': differences, 'preparation': 'published terminals + raw design copper'})
    stats = prepare_board(raw, destination, input_text=transplanted, rules_source=published,
                          metadata=raw_stats, expected_terminals=expected)
    if stats.get('reason'):
        print(f'COVERAGE {name}: {stats["reason"]}', file=sys.stderr, flush=True)
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', type=pathlib.Path, default=SOURCE)
    ap.add_argument('--out', type=pathlib.Path, default=ROOT / 'build/pcbench')
    ap.add_argument('--set', action='append', choices=['quick', 'mid'])
    ap.add_argument('names', nargs='*')
    args = ap.parse_args()
    names = list(args.names)
    for name in args.set or []:
        names.extend(set_boards(ROOT / 'bench/sets' / f'{name}.txt'))
    if not names:
        ap.error('give --set quick/--set mid or board names')
    args.out.mkdir(parents=True, exist_ok=True)
    records, excluded = [], []
    for name in sorted(set(names)):
        try:
            stats = prepare(name, args.source, args.out)
            records.append(stats)
            print(f'{name}: zones={stats["zones"]}, fixed_vias={stats["fixed_vias"]}, identity OK', flush=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            record = {'board': name, 'state': 'invalid', 'excluded': True, 'reason': str(error)}
            records.append(record)
            excluded.append(record)
            invalidate_fixture(args.out / name, error)
            print(f'EXCLUDED {name}: {error}', file=sys.stderr, flush=True)
    # Retain exclusions from other sets previously prepared into this directory.
    old = args.out / 'exclusions.json'
    previous = json.loads(old.read_text()) if old.exists() else []
    exclusions = [r for r in previous if r['board'] not in names] + excluded
    old.write_text(json.dumps(sorted(exclusions, key=lambda r: r['board']), indent=2) + '\n')
    manifest = args.out / 'preparation.json'
    previous = json.loads(manifest.read_text()) if manifest.exists() else []
    records = [r for r in previous if r['board'] not in names] + records
    manifest.write_text(json.dumps(sorted(records, key=lambda r: r['board']), indent=2) + '\n')
    print(args.out)
    return int(bool(excluded))


if __name__ == '__main__':
    raise SystemExit(main())
