# SPDX-License-Identifier: GPL-3.0-or-later
"""Uncapped KiCad connectivity and immutable saved-run rejudgment."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import drc_judge
import kicad_connectivity
import run as harness


def report(count, path='board'):
    return dict(unconnected=count, report={'unconnected_items': [{}] * count}, analysis_board=path)


def exact(count):
    return dict(unconnected=count, per_net={'GND': count} if count else {},
                method='pcbnew-connectivity', kicad_version='10.0.3')


class ConnectivityHarnessTests(unittest.TestCase):
    def test_capped_input_uncapped_output_exact_ratio(self):
        row = {'exit': 3}
        with patch.object(kicad_connectivity, 'count_unconnected', return_value={'in': exact(1616), 'out': exact(100)}) as call:
            harness.apply_judgment(row, report(499, 'in'), report(100, 'out'))
        self.assertAlmostEqual(row['completion'], 1 - 100 / 1616)
        self.assertEqual(row['unconnected_uncapped'], {'before': 1616, 'after': 100})
        self.assertEqual(row['unconnected_per_net']['before'], {'GND': 1616})
        self.assertFalse(row['censored'])
        self.assertTrue(row['judge_list_censored'])
        self.assertEqual(row['judge'], 'ok')
        call.assert_called_once_with([pathlib.Path('in'), pathlib.Path('out')])

    def test_uncapped_mismatch_invalidates_board_and_headline(self):
        row = {'exit': 0}
        with patch.object(kicad_connectivity, 'count_unconnected', return_value={'in': exact(6), 'out': exact(0)}):
            harness.apply_judgment(row, report(5, 'in'), report(0, 'out'))
        self.assertEqual(row['judge'], 'failed')
        self.assertFalse(row['clean'])
        self.assertIn('uncapped DRC list 5 != KiCad connectivity 6', row['error'])
        self.assertEqual(harness.headline([row], 1)['judge_failures'], 1)

    def test_unavailable_and_version_mismatch_preserve_unknown(self):
        for error in (kicad_connectivity.ConnectivityUnavailable('no python'),
                      kicad_connectivity.ConnectivityVersionMismatch('10.0.3 != 10.0.6')):
            with self.subTest(error=error), patch.object(kicad_connectivity, 'count_unconnected', side_effect=error):
                row = {'exit': 3}
                harness.apply_judgment(row, report(499, 'in'), report(0, 'out'))
                self.assertIsNone(row['completion'])
                self.assertTrue(row['censored'])
                self.assertEqual(row['connectivity_unavailable']['before'], str(error))
                self.assertEqual(row['error_gate'], 'pass')

    def test_cap_types_excluded_from_exact_delta_and_gate_unknown(self):
        for kind, count in (('clearance', 499), ('track_width', 199)):
            for severity in ('error', 'warning'):
                with self.subTest(kind=kind, severity=severity):
                    before = {'violations': [dict(type=kind, severity=severity, description='old')] * count}
                    after = {'violations': [dict(type=kind, severity='error', description='new')]}
                    result = drc_judge.added_errors(before, after)
                    self.assertEqual(result['error_gate'], 'unknown')
                    self.assertEqual(result['added_any'], {})
                    self.assertEqual(result['capped']['before'], {kind: count})
                    self.assertIsNone(drc_judge.judge(before, after)['drc_pass'])

    def test_library_placement_text_and_warning_only_caps_are_not_routing_gates(self):
        for kind in sorted(drc_judge.ROUTING_IMMUTABLE_TYPES):
            for severity in ('warning', 'error'):
                with self.subTest(kind=kind, severity=severity):
                    report = {'violations': [dict(type=kind, severity=severity)] * 199}
                    result = drc_judge.added_errors(report, report)
                    self.assertEqual(result['error_gate'], 'pass')
                    self.assertEqual(result['capped_evaluated_types'], [])
                    self.assertEqual(result['capped_not_evaluated_types'], [kind])
        warning_only = {'violations': [dict(type='clearance', severity='warning')] * 499}
        result = drc_judge.added_errors(warning_only, warning_only)
        self.assertEqual(result['error_gate'], 'pass')
        self.assertEqual(result['capped_not_evaluated_types'], ['clearance'])

    def test_rejudge_keeps_source_and_routing_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = root / 'fixtures/b/unrouted.kicad_pcb'
            fixture.parent.mkdir(parents=True)
            fixture.write_text('''(kicad_pcb
                (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
                (gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts"))
                (zone (net "GND") (layer "B.Cu")
                    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))))''')
            source = root / 'bench/results/old'
            (source / 'boards').mkdir(parents=True)
            raw = source / 'boards/b.kicad_pcb'
            raw.write_text(fixture.read_text()[:-1] + '(via (net "GND")))')
            old = dict(board='b', preparation={'input_sha256': harness.digest(fixture)}, exit=3,
                       stop='exhausted', seconds=20, cpu_s=17, work={'expansions': 99},
                       unconnected_before=499, unconnected_after=100, completion=None, clean=False,
                       plane_nets={}, plane_vias_added=0, plane_unconnected_after=0,
                       signal_unconnected_after=100)
            (source / 'boards.jsonl').write_text(json.dumps(old) + '\n')
            (source / 'summary.json').write_text(json.dumps({'fixtures': str(fixture.parent.parent), 'boards': 1, 'set': 'test'}))
            source_bytes = (source / 'boards.jsonl').read_bytes()
            def judge(path, *, saved, **kwargs):
                saved.write_text('filled')
                result = report(499 if path == fixture else 100, str(saved))
                return result
            def connectivity(boards):
                return {str(board): exact(1000 if '.input.' in board.name else 100) for board in boards}
            with patch.object(harness, 'ROOT', root), patch.object(harness, 'kicad_version', return_value='10.0.3'), \
                    patch.object(harness, 'drc', side_effect=judge), patch.object(harness, 'REFILL', False), \
                    patch.object(kicad_connectivity, 'count_unconnected', side_effect=connectivity), \
                    patch.object(harness, 'run_measured') as router, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(harness.rejudge_run(str(source)), 0)
                router.assert_not_called()
            target = root / 'bench/results/old-rejudged'
            row = json.loads((target / 'boards.jsonl').read_text())
            summary = json.loads((target / 'summary.json').read_text())
            manifest = json.loads((target / 'manifest.json').read_text())
            self.assertEqual(row['completion'], .9)
            for key in ('stop', 'seconds', 'cpu_s', 'work'):
                self.assertEqual(row[key], old[key])
            self.assertEqual((source / 'boards.jsonl').read_bytes(), source_bytes)
            self.assertEqual((target / 'boards/b.kicad_pcb').read_bytes(), raw.read_bytes())
            self.assertEqual(manifest['state'], 'final')
            self.assertEqual(manifest['provenance']['kicad_python_version'], ['10.0.3'])
            self.assertEqual(summary['pooled_completion'], .9)
            self.assertEqual(set(row['plane_nets']), {'GND'})
            self.assertEqual((row['plane_unconnected_after'], row['signal_unconnected_after']), (100, 0))
            self.assertEqual(row['plane_vias_added'], 1)
            self.assertEqual(summary['plane_vias_added'], 1)
            self.assertEqual(summary['plane_unconnected_after'], 100)


class FeatureConsumerTests(unittest.TestCase):
    def test_board_quality_uses_exact_completion_and_unknown_error_gate(self):
        import board_quality
        before, after = report(499, 'in'), report(0, 'out')
        before['report']['violations'] = [{'type': 'clearance', 'severity': 'error'}] * 499
        with patch.object(board_quality.bench_run, 'drc', side_effect=[before, after]), \
                patch.object(kicad_connectivity, 'count_unconnected', return_value={'in': exact(1000), 'out': exact(0)}), \
                patch.object(board_quality, 'board_json', return_value={}), \
                patch.object(board_quality, 'geometry', return_value={}), \
                patch.object(board_quality, 'coupling', return_value={}):
            result = board_quality.metrics(pathlib.Path('in'), pathlib.Path('out'))
        self.assertEqual(result['completion'], 1)
        self.assertEqual(result['unconnected'], 0)
        self.assertFalse(result['clean'])
        self.assertEqual(result['error_gate'], 'unknown')

    def test_passes_gate_cannot_pass_capped_warning_instances(self):
        import argparse
        import subprocess
        import passes_gate
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = root / 'fixtures/b/unrouted.kicad_pcb'
            fixture.parent.mkdir(parents=True)
            fixture.write_text('input')
            output = root / 'outputs'
            output.mkdir()
            before, after = report(0, 'in'), report(0, 'out')
            after['report']['violations'] = [{'type': 'track_width', 'severity': 'warning'}] * 199
            def route(cmd, timeout):
                pathlib.Path(cmd[cmd.index('-o') + 1]).write_text('raw')
                pathlib.Path(cmd[cmd.index('--json') + 1]).write_text(json.dumps(
                    dict(routed=5, connections=5, tracks=3, vias=0)))
                return subprocess.CompletedProcess(cmd, 0, '', '')
            args = argparse.Namespace(fixtures=str(fixture.parent.parent), pass_args='--widen-power',
                                      binary='tracemaker', work=10, route_args='', refill=True)
            with patch.object(passes_gate, 'run', side_effect=route), \
                    patch.object(passes_gate, 'judge', side_effect=[before, after]), \
                    patch.object(kicad_connectivity, 'count_unconnected', return_value={'in': exact(0), 'out': exact(0)}):
                result = passes_gate.one('b', args, output)
            self.assertFalse(result['pass'])
            self.assertEqual(result['added'], {})
            self.assertEqual(result['error_gate'], 'pass')  # This feature gate also evaluates warnings.
            self.assertEqual(result['capped_types'], ['track_width'])


if __name__ == '__main__':
    unittest.main()
