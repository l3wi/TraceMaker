#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preflight escape verdict severity, domain reporting and forwarding of route policy."""
import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('preflight', ROOT / 'skills/tracemaker-board-prep/scripts/preflight.py')
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)

ESCAPE = {'dead': 1, 'statuses': dict(satisfied=1, witness=1, exhausted=1, unknown=1),
          'parts': [{'ref': 'U1', 'results': [
              dict(pad=0, pin='1', status='satisfied', reason='connected', domain='domain-a'),
              dict(pad=1, pin='2', status='witness', reason='exterior reached', domain='domain-a'),
              dict(pad=2, pin='3', status='exhausted', reason='finite graph exhausted', domain='domain-b'),
              dict(pad=3, pin='4', status='unknown', reason='work budget exhausted', domain='domain-c')]}]}
BOARD = '''(kicad_pcb (version 20240108)
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (net 0 "") (net 1 "GND")
 (footprint "test" (layer "F.Cu") (at 5 5)
  (property "Reference" "R1")
  (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND")))
 (zone (net 1) (net_name "GND") (layer "B.Cu")
  (polygon (pts (xy 1 1) (xy 9 1) (xy 9 9) (xy 1 9)))
  (filled_polygon (layer "B.Cu") (pts (xy 1 1) (xy 9 1) (xy 9 9) (xy 1 9))))
 (gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts")))
'''


class EscapePreflightTests(unittest.TestCase):
    def test_status_severity_and_domains(self):
        findings = preflight.escape_findings(ESCAPE)
        self.assertEqual([s for s, _ in findings], ['info', 'quality', 'quality'])
        self.assertIn('1 already satisfied', findings[0][1])
        self.assertIn('1 exact witnessed', findings[0][1])
        self.assertIn('domain-b', findings[1][1])
        self.assertIn('not proof of physical impossibility', findings[1][1])
        self.assertIn('domain-c', findings[2][1])
        self.assertIn('work budget exhausted', findings[2][1])
        self.assertNotIn('strip', ' '.join(text for _, text in findings))

    def test_empty_current_report_and_legacy_report(self):
        current = preflight.escape_findings({'parts': [], 'statuses': dict(satisfied=0, witness=0, exhausted=0, unknown=0)})
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0][0], 'info')
        legacy = preflight.escape_findings({'parts': [], 'dead': 2})
        self.assertEqual(legacy[0][0], 'quality')
        self.assertIn('search domain unavailable', legacy[0][1])

    def run_preflight(self, options):
        with tempfile.TemporaryDirectory() as tmp:
            board = pathlib.Path(tmp) / 'input.kicad_pcb'
            board.write_text(BOARD)
            board.with_suffix('.kicad_pro').write_text(json.dumps({
                'net_settings': {'classes': [dict(name='Default', track_width=0.25, clearance=0.2, via_diameter=0.6, via_drill=0.3)]},
                'board': {'design_settings': {'rules': {'min_track_width': 0.1}}}}))
            report = pathlib.Path(tmp) / 'preflight.json'
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[1] == 'drc':
                    return subprocess.CompletedProcess(command, 0, stdout='unconnected_items 1\n', stderr='')
                pathlib.Path(command[command.index('--json') + 1]).write_text(json.dumps(ESCAPE))
                return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

            with patch.object(sys, 'argv', ['preflight.py', str(board), '--json', str(report), *options]), \
                    patch.object(preflight.shutil, 'which', return_value='/test/tracemaker'), \
                    patch.object(preflight.subprocess, 'run', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight.main(), 0)
            return json.loads(report.read_text()), calls

    def test_recommended_policy_matches_escape(self):
        report, calls = self.run_preflight(['--small-pad-mm', '1.2', '--blind-vias', '--component-rules', 'on', '--rules-override', 'override.json'])
        expected = ['--soft-zones', '--keep-vias-off-pads', '1.2', '--blind-vias', '--component-rules', 'on', '--rules-override', 'override.json']
        self.assertEqual(report['routing_args'], expected)
        escape = next(c for c in calls if c[1] == 'escape')
        self.assertEqual(escape[3:-2], expected)
        for token in expected:
            self.assertIn(token, report['recommended_command'])
        self.assertEqual(report['escape'], ESCAPE)
        self.assertTrue(any(f['severity'] == 'quality' and 'domain-b' in f['message'] for f in report['findings']))

    def test_hard_zone_policy_and_explicit_pad_preference(self):
        report, calls = self.run_preflight(['--no-soft-zones', '--keep-vias-off-pads', '0.7'])
        self.assertEqual(report['routing_args'], ['--keep-vias-off-pads', '0.7'])
        escape = next(c for c in calls if c[1] == 'escape')
        self.assertNotIn('--soft-zones', escape)
        self.assertNotIn('--soft-zones', report['recommended_command'])


if __name__ == '__main__':
    unittest.main()
