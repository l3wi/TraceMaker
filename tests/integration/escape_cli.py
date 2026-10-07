#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strategy A: CLI policy parity, exact-result schema, connectivity and budget verdicts."""
import json
import pathlib
import subprocess
import sys


def board_text(connected=False):
    pads = '\n'.join(f'(pad "{i + 1}" smd circle (at {i % 4} {i // 4}) '
                     '(size 0.3 0.3) (layers "F.Cu") (net 1 "signal"))' for i in range(8))
    tracks = ''
    if connected:
        points = [(4 + i % 4, 4 + i // 4) for i in range(8)] + [(18, 4)]
        tracks = '\n'.join(f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) '
                           '(width 0.15) (layer "F.Cu") (net 1))' for a, b in zip(points, points[1:]))
    return f'''(kicad_pcb (version 20240108) (generator "pcbnew")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (44 "Edge.Cuts" user))
 (net 0 "") (net 1 "signal")
 (footprint "dense" (layer "F.Cu") (at 4 4)
  (property "Reference" "U1" (at 0 -1) (layer "F.SilkS")) {pads})
 (footprint "terminal" (layer "F.Cu") (at 18 4)
  (property "Reference" "J1" (at 0 -1) (layer "F.SilkS"))
  (pad "1" smd circle (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 1 "signal")))
 {tracks}
 (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (stroke (width 0.1) (type solid))))
'''


PROJECT = {'net_settings': {'classes': [dict(name='Default', track_width=0.15, clearance=0.1,
                                            via_diameter=0.5, via_drill=0.3)]},
           'board': {'design_settings': {'rules': dict(min_track_width=0.1, min_clearance=0.1,
                                                      min_via_diameter=0.4, min_through_hole_diameter=0.2,
                                                      min_via_annular_width=0.1)}}}


def main():
    tm, work = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    board = work / 'input.kicad_pcb'
    board.write_text(board_text())
    board.with_suffix('.kicad_pro').write_text(json.dumps(PROJECT))
    board.with_suffix('.kicad_dru').unlink(missing_ok=True)
    original = board.read_bytes(), board.with_suffix('.kicad_pro').read_bytes()

    def escape(name, flags=(), path=board):
        report = work / f'{name}.json'
        cmd = [str(tm), 'escape', str(path), '--pitch-um', '100', '--json', str(report), *flags]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, (cmd, p.stdout, p.stderr)
        return json.loads(report.read_text())

    base = escape('base')
    assert base['pins'] == 8 and base['dead'] == 0, base
    assert base['statuses']['witness'] == 8 and base['statuses']['unknown'] == 0, base
    for part in base['parts']:
        assert part['pins'] == len(part['results'])
        for pin in part['results']:
            assert pin['status'] == 'witness' and pin['domain']
            witness = pin['witness']
            assert witness['units'] == 'nm' and witness['steps']
            for step in witness['steps']:
                assert all(isinstance(c, int) for c in step['a'] + step['b'])
                assert step['width'] > 0 and step['layer'] in (0, 1)
    reference = escape('reference', ['--reference'])
    assert base['parts'] == reference['parts'], (base, reference)
    assert base['statuses'] == reference['statuses']
    unknown = escape('budget', ['--work', '1'])
    assert unknown['statuses']['unknown'] > 0 and unknown['dead'] == unknown['statuses']['exhausted'], unknown
    assert all(p['status'] != 'unknown' or p['reason'] for part in unknown['parts'] for p in part['results'])
    blocked = work / 'blocked.kicad_pcb'
    blocked.write_text(board_text())
    blocked.with_suffix('.kicad_pro').write_text(json.dumps(PROJECT))
    blocked.with_suffix('.kicad_dru').write_text(
        '(version 1)\n(rule "No signal tracks" (constraint disallow track) '
        '(condition "A.NetName == \'signal\'"))\n')
    exhausted = escape('exhausted', path=blocked)
    assert exhausted['dead'] == exhausted['statuses']['exhausted'] == 8, exhausted
    assert exhausted['statuses']['unknown'] == 0, exhausted
    assert all(pin['domain'] and pin['status'] == 'exhausted'
               for part in exhausted['parts'] for pin in part['results'])

    default = escape('pad_default', ['--keep-vias-off-pads'])
    explicit = escape('pad_explicit', ['--keep-vias-off-pads', '2'])
    equals = escape('pad_equals', ['--keep-vias-off-pads=2'])
    assert default == explicit == equals
    configured = escape('configured', ['--soft-zones', '--blind-vias', '--keep-vias-off-pads', '0.4', '--component-rules', 'on'])
    assert configured['configuration']['soft_zones'] and configured['configuration']['blind_vias']
    assert configured['configuration']['component_rules'] == 'on'
    assert configured['configuration']['keep_vias_off_pads_mm'] == 0.4
    assert configured['parts'][0]['results'][0]['domain'] != base['parts'][0]['results'][0]['domain']
    for value in ('0', '-1', 'nan', 'inf'):
        p = subprocess.run([str(tm), 'escape', str(board), '--keep-vias-off-pads=' + value], capture_output=True, timeout=120)
        assert p.returncode != 0, value
    bad_override = work / 'invalid-overrides.json'
    bad_override.write_text('{')
    p = subprocess.run([str(tm), 'escape', str(board), '--rules-override', str(bad_override)], capture_output=True, timeout=120)
    assert p.returncode != 0, 'escape did not validate overrides with component rules off'

    connected = work / 'connected.kicad_pcb'
    connected.write_text(board_text(connected=True))
    connected.with_suffix('.kicad_pro').write_text(json.dumps(PROJECT))
    done = escape('satisfied', path=connected)
    assert done['statuses']['satisfied'] == 8 and done['dead'] == 0, done
    assert done['statuses']['witness'] == done['statuses']['unknown'] == 0, done
    assert original == (board.read_bytes(), board.with_suffix('.kicad_pro').read_bytes())
    assert not list(work.glob('*.tracemaker.kicad_dru')), 'read-only escape wrote a sidecar'
    print('escape CLI statuses, witnesses, reference parity, shared policy and read-only input checks passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
