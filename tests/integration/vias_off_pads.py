#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D62: optional CLI value, route-only rules, dog-bones, and KiCad's equivalent rule."""
import json
import pathlib
import shutil
import subprocess
import sys

BOARD = '''(kicad_pcb (version 20240108) (generator "pcbnew")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (44 "Edge.Cuts" user))
 (net 0 "") (net 1 "GND")
 (footprint "0402" (layer "F.Cu") (at 4 3)
  (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 0.5 0.6) (layers "F.Cu") (net 1 "GND")))
 (footprint "plane-anchor" (layer "B.Cu") (at 16 3)
  (property "Reference" "J1" (at 0 0) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 3 3) (layers "B.Cu") (net 1 "GND")))
 (zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) (connect_pads (clearance 0.2))
  (min_thickness 0.25) (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
  (polygon (pts (xy 1 1) (xy 19 1) (xy 19 9) (xy 1 9)))
  (filled_polygon (layer "B.Cu") (pts (xy 1 1) (xy 19 1) (xy 19 9) (xy 1 9))))
 (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (stroke (width 0.1) (type solid))))
'''
RULE = '''(version 1)
(rule "Keep vias off small SMD pads"
 (constraint physical_hole_clearance (min 0.35mm))
 (condition "A.Type == 'Via' && B.Type == 'Pad' && B.Pad_Type == 'SMD' && B.Size_X < 2mm && B.Size_Y < 2mm"))
'''


def main():
    tm, work = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    board = work / 'input.kicad_pcb'
    board.write_text(BOARD)
    board.with_suffix('.kicad_dru').unlink(missing_ok=True)
    common = ['--work', '1000000', '--seed', '7', '--variants', '1', '--threads', '1', '--no-gpu', '--no-kb', '--pitch-um', '50']

    def route(name, args):
        out, items = work / f'{name}.kicad_pcb', work / f'{name}.items.json'
        out.with_suffix('.kicad_dru').unlink(missing_ok=True)
        cmd = [str(tm), 'route', *args, '-o', str(out), '--emit-items', str(items), *common]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, (cmd, p.stdout, p.stderr)
        return out, json.loads(items.read_text()), p.stdout

    baseline, before, _ = route('off', [str(board)])
    on, after, log = route('on', ['--keep-vias-off-pads', str(board)])
    explicit, _, _ = route('explicit', [str(board), '--keep-vias-off-pads', '2'])
    next_flag, _, _ = route('next_flag', [str(board), '--keep-vias-off-pads', '--no-optimize'])
    equal, _, _ = route('equal', [str(board), '--keep-vias-off-pads=2'])
    exempt, _, _ = route('exempt', ['--keep-vias-off-pads', '0.4', str(board)])
    assert on.read_bytes() == explicit.read_bytes() == equal.read_bytes()
    assert baseline.read_bytes() == exempt.read_bytes()
    assert 'route only' in log
    assert len(before['vias']) == len(after['vias']) == 1
    assert before['vias'][0]['position'] != after['vias'][0]['position']
    for v in after['vias']:
        x, y = v['position']
        dx, dy = max(abs(x - 4000000) - 250000, 0), max(abs(y - 3000000) - 300000, 0)
        assert dx * dx + dy * dy >= (v['diameter'] // 2 + 200000) ** 2
    assert not list(work.glob('*.tracemaker.kicad_dru'))
    report = work / 'tm.drc.json'
    p = subprocess.run([str(tm), 'drc', str(on), '--json', str(report)], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert not json.loads(report.read_text())['violations']
    for value in ['0', '-1', 'nan', 'inf']:
        p = subprocess.run([str(tm), 'route', str(board), '--keep-vias-off-pads=' + value, '-o', str(work / 'invalid.kicad_pcb'), *common], capture_output=True, timeout=120)
        assert p.returncode not in (0, 3), value

    kicad = shutil.which('kicad-cli')
    if not kicad:
        print('CLI / dog-bone / route-only checks passed; kicad-cli missing: skip KiCad equivalence')
        return 77

    def hole_count(path):
        report = path.with_suffix('.kicad.drc.json')
        p = subprocess.run([kicad, 'pcb', 'drc', '--format', 'json', '-o', str(report), str(path)], capture_output=True, text=True, timeout=120)
        assert report.exists(), (p.stdout, p.stderr)
        return sum(v['type'] == 'hole_clearance' for v in json.loads(report.read_text())['violations'])

    no_rule = hole_count(baseline)
    baseline.with_suffix('.kicad_dru').write_text(RULE)
    assert hole_count(baseline) > no_rule, 'KiCad did not enforce Size_X/Size_Y rule on via-in-pad'
    on.with_suffix('.kicad_dru').write_text(RULE)
    assert hole_count(on) == 0, 'dog-bone violates equivalent KiCad rule'
    print('CLI optional values, dog-bone, DRC isolation and KiCad equivalent rule passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
