#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""All-SMD plane routing, judged after KiCad refill; generated text, no pcbnew dependency."""
import collections
import json
import pathlib
import shutil
import subprocess
import sys
import uuid


def fixture():
    serial = 0

    def uid():
        nonlocal serial
        serial += 1
        return str(uuid.UUID(int=serial))

    nets = {1: "GND", 2: "3V3", 3: "SIG"}

    def pad(number, x, y, sx, sy, net):
        return (f'(pad "{number}" smd rect (at {x} {y}) (size {sx} {sy}) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{nets[net]}") (uuid "{uid()}"))')

    def footprint(ref, x, y, pads):
        return (f'(footprint "Generated:{ref}" (layer "F.Cu") (uuid "{uid()}") (at {x} {y}) '
                f'(property "Reference" "{ref}" (at 0 -2) (layer "F.SilkS") '
                f'(effects (font (size 1 1) (thickness 0.15)))) (attr smd) {pads})')

    def plane(layer, net):
        pts = '(xy 1 1) (xy 15 1) (xy 15 11) (xy 1 11)'
        return (f'(zone (net {net}) (net_name "{nets[net]}") (layer "{layer}") (uuid "{uid()}") '
                '(hatch edge 0.5) (connect_pads yes (clearance 0.2)) (min_thickness 0.2) '
                '(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3)) '
                f'(polygon (pts {pts})) (filled_polygon (layer "{layer}") (pts {pts})))')

    def area(layer):
        return (f'(zone (net 0) (net_name "") (layer "{layer}") (uuid "{uid()}") '
                '(hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.2) '
                '(keepout (tracks not_allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed)) '
                '(polygon (pts (xy 0.5 0.5) (xy 15.5 0.5) (xy 15.5 11.5) (xy 0.5 11.5))))')

    # Four coarse-pitch leads on each side of a QFN-like package and its exposed GND pad.
    qfn = ''.join(pad(i + 1, x, y, 0.6, 0.35, n) for i, (x, y, n) in enumerate([
        (-2, -1.2, 1), (-2, -0.4, 2), (-2, 0.4, 3), (-2, 1.2, 1),
        (2, -1.2, 2), (2, -0.4, 1), (2, 0.4, 3), (2, 1.2, 2)]))
    qfn += pad(9, 0, 0, 2, 2, 1)
    resistors = ''.join(footprint(f'R{i + 1}', x, y, pad(1, -0.5, 0, 0.5, 0.6, 1) + pad(2, 0.5, 0, 0.5, 0.6, 2))
                        for i, (x, y) in enumerate([(4, 3), (12, 3), (4, 9), (12, 9)]))
    return ('(kicad_pcb (version 20240108) (generator "pcbnew") (general (thickness 1.6)) (paper "A4") '
            '(layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal) '
            '(35 "F.Paste" user) (37 "F.SilkS" user) (39 "F.Mask" user) (44 "Edge.Cuts" user)) '
            '(setup (pad_to_mask_clearance 0) (tenting front back)) '
            '(net 0 "") (net 1 "GND") (net 2 "3V3") (net 3 "SIG") '
            f'{footprint("U1", 8, 6, qfn)} {resistors} '
            f'(gr_rect (start 0 0) (end 16 12) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") (uuid "{uid()}")) '
            f'{plane("In1.Cu", 1)} {plane("In2.Cu", 2)} {area("In1.Cu")} {area("In2.Cu")} )\n')


def drc(cli, board):
    report = board.with_suffix('.drc.json')
    result = subprocess.run([cli, 'pcb', 'drc', '--refill-zones', '--format', 'json', '--severity-all',
                             '--all-track-errors', '--exit-code-violations', '-o', str(report), str(board)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode not in (0, 5) or not report.exists():
        raise RuntimeError(result.stdout + result.stderr)
    return json.loads(report.read_text())


def errors(report):
    # Existing integration judges compare violation type and location, not generated UUIDs or item wording.
    return collections.Counter((v['type'], tuple(sorted((round(i['pos']['x'], 3), round(i['pos']['y'], 3))
                                                        for i in v.get('items', []) if 'pos' in i)))
                               for v in report.get('violations', []) if v.get('severity') == 'error')


def main():
    cli = shutil.which('kicad-cli')
    if not cli:
        print('kicad-cli absent: skip')
        return 77
    tm, work = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    board, output = work / 'plane_smd.kicad_pcb', work / 'routed.kicad_pcb'
    board.write_text(fixture())
    baseline = drc(cli, board)
    summary = work / 'route.json'
    subprocess.run([str(tm), 'route', str(board), '-o', str(output), '--soft-zones', '--work', '1000000',
                    '--seed', '7', '--no-kb', '--variants', '1', '--threads', '1', '--no-gpu', '--json', str(summary)],
                   check=True, timeout=180)
    routed = json.loads(summary.read_text())
    assert routed['plane_connections'] > 0, routed
    assert routed['routed'] == routed['connections'], routed
    # Refill/sign-off a copy, leaving the original routed output available for byte-level inspection.
    judge = work / 'refilled.kicad_pcb'
    shutil.copyfile(output, judge)
    report = drc(cli, judge)
    added = errors(report) - errors(baseline)
    unconnected = len(report.get('unconnected_items', []))
    print(f"soft zones: {routed['plane_connections']} plane connections, {routed['zones_needing_refill']} zones need refill; "
          f'KiCad: {unconnected} unconnected, {sum(added.values())} added errors')
    if unconnected or added:
        print(json.dumps({'added_errors': list(added.items()), 'unconnected': report.get('unconnected_items', [])}, indent=2))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
