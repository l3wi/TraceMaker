#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural coverage, grading and route diagnostics share the engine's rule policy."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ENGINE = Path(sys.argv[1]).resolve()
PREFLIGHT = ROOT / 'skills/tracemaker-board-prep/scripts/preflight.py'
BOARD = '''(kicad_pcb (version 20240108) (generator pcbnew)
(layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user))
(net 0 "") (net 1 SIG)
(gr_rect (start 0 0) (end 20 10) (layer Edge.Cuts) (stroke (width 0.1) (type solid)))
(footprint R (layer F.Cu) (at 4 5) (property Reference R1 (at 0 0) (layer F.SilkS))
(pad 1 smd rect (at 0 0) (size 1 1) (layers F.Cu) (net 1 SIG)))
(footprint R (layer F.Cu) (at 16 5) (property Reference R2 (at 0 0) (layer F.SilkS))
(pad 1 smd rect (at 0 0) (size 1 1) (layers F.Cu) (net 1 SIG)))
(zone (net 0) (net_name "") (layers F.Cu B.Cu) (name box)
(hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25)
(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
(polygon (pts (xy 8 3) (xy 12 3) (xy 12 7) (xy 8 7)))))'''
PROJECT = {'board': {'design_settings': {'rules': {'min_track_width': 0.15}}},
           'net_settings': {'classes': [{'name': 'Default', 'clearance': 0.1,
                                         'track_width': 0.25, 'via_diameter': 0.6, 'via_drill': 0.3}]}}

with tempfile.TemporaryDirectory(prefix='tracemaker-rule-grading-') as temporary:
    directory = Path(temporary)
    board = directory / 'input.kicad_pcb'
    board.write_text(BOARD)
    board.with_suffix('.kicad_pro').write_text(json.dumps(PROJECT))
    report_path = directory / 'preflight.json'
    cases = [
        ("A.insideArea('box')", 'track', False, True),
        ("A.NetClass == 'Default'", 'via', False, False),
        ("false && A.Missing_Property == 1", 'track', True, True),
        ("true || A.memberOfGroup('g')", 'track', True, True),
        ("A.isPlated('bad')", 'track', True, True),
        ("NetName == 'SIG'", 'track', True, True),
        ("A.Type == 'Track'", 'text', True, False),
    ]
    for condition, item_type, blocked, exact in cases:
        board.with_suffix('.kicad_dru').write_text(
            f'(version 1)\n(rule "coverage" (condition "{condition}") (constraint disallow {item_type}))\n')
        result = subprocess.run([sys.executable, str(PREFLIGHT), str(board), '--tracemaker', str(ENGINE),
                                 '--json', str(report_path)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (condition, result.stderr)
        report = json.loads(report_path.read_text())
        custom = [finding for finding in report['findings'] if finding['section'] == 'Custom rules']
        assert any(f['severity'] == 'block' for f in custom) == blocked, (condition, custom)
        assert not any(f['severity'] == 'quality' for f in custom), (condition, custom)
        assert report['caches_disabled'] == exact, (condition, report['caches_disabled'])
        if not blocked:
            assert any('enforced on actual candidate geometry' in f['message'] for f in custom), custom
        if 'Missing_Property' in condition:
            assert any('Missing_Property' in f['message'] and f['severity'] == 'block' for f in custom), custom
            route_path = directory / 'route.json'
            result = subprocess.run([str(ENGINE), 'route', str(board), '-o', str(directory / 'output.kicad_pcb'),
                                     '--work', '1000', '--threads', '1', '--variants', '1', '--json', str(route_path)],
                                    capture_output=True, text=True, timeout=60)
            assert result.returncode == 3, result.stderr
            summary = json.loads(route_path.read_text())
            assert summary['routed'] == 0 and summary['tracks'] == 0 and summary['vias'] == 0, summary
            assert any('Missing_Property' in warning for warning in summary['rule_warnings']), summary
            assert all(warning in result.stderr for warning in summary['rule_warnings']), result.stderr
    board.with_suffix('.kicad_dru').write_text('(version 1) (rule "broken" (condition "true")')
    output = directory / 'unreadable-output.kicad_pcb'
    result = subprocess.run([str(ENGINE), 'route', str(board), '-o', str(output), '--threads', '1', '--variants', '1'],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode not in (0, 3) and not output.exists(), (result.returncode, result.stderr)
    assert 'custom rules could not be read' in result.stderr, result.stderr
print('8 preflight/route coverage cases passed')
