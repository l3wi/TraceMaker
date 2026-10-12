# SPDX-License-Identifier: GPL-3.0-or-later
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import run as harness


class HarnessTests(unittest.TestCase):
    def test_saved_refill_reuses_atomic_drc_cache(self):
        self.addCleanup(harness.kicad_version.cache_clear)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, saved = root / 'raw.kicad_pcb', root / 'raw.refilled.kicad_pcb'
            source.write_text('raw')
            source.with_suffix('.kicad_dru').write_text('rules')
            def refill(src, target, report, timeout):
                self.assertEqual(src, source)
                target.write_text('fresh fills')
                report.write_text(json.dumps({'violations': [], 'unconnected_items': []}))
            with patch.object(harness, 'REFILL', True), patch.object(harness, 'DRC_CACHE', root / 'cache'), \
                    patch.object(harness, 'kicad_version', return_value='10.test'), \
                    patch.object(harness, 'refill_board', side_effect=refill) as call:
                self.assertEqual(harness.drc(source, saved=saved)['unconnected'], 0)
                saved.unlink()
                self.assertEqual(harness.drc(source, saved=saved)['unconnected'], 0)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(source.read_text(), 'raw')
                self.assertEqual(saved.read_text(), 'fresh fills')
                self.assertEqual(saved.with_suffix('.kicad_dru').read_text(), 'rules')
                # Rules changes invalidate the DRC cache even when raw board is unchanged.
                source.with_suffix('.kicad_dru').write_text('new rules')
                harness.drc(source, saved=saved)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(list((root / 'cache').glob('*.part.json')), [])

    def test_human_uses_normal_judge_without_router(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixtures = root / 'fixtures'
            fixture = fixtures / 'board'
            fixture.mkdir(parents=True)
            (fixture / 'unrouted.kicad_pcb').write_text('input')
            (fixture / 'routed.kicad_pcb').write_text('original human')
            (fixture / 'routed.kicad_dru').write_text('human rules')
            before = {'errors': {}, 'routed_errors': {}, 'unconnected': 5, 'report': {}}
            after = {'errors': {}, 'routed_errors': {}, 'unconnected': 0, 'report': {}}
            with patch.object(harness, 'SRC_FIX', fixtures), patch.object(harness, 'HUMAN', True), \
                    patch.object(harness, 'REFILL', False), patch.object(harness, 'PLACE_MODE', None), \
                    patch.object(harness, 'plane_nets', return_value=None), \
                    patch.object(harness, 'drc', side_effect=[before, after]) as judge, \
                    patch.object(harness, 'dead_pins') as escape, patch.object(harness.subprocess, 'run') as router:
                row = harness.run_board('board', root / 'run', 1)
            self.assertTrue(row['human'])
            self.assertTrue(row['clean'])
            self.assertEqual(row['completion'], 1)
            self.assertNotIn('routed', row)
            self.assertEqual(judge.call_count, 2)
            escape.assert_not_called()
            router.assert_not_called()
            self.assertEqual((root / 'run/boards/board.kicad_pcb').read_text(), 'original human')
            self.assertEqual((root / 'run/boards/board.kicad_dru').read_text(), 'human rules')

    def test_routing_keeps_measurements_intent_and_saved_judge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixtures = root / 'fixtures'
            fixture = fixtures / 'board'
            fixture.mkdir(parents=True)
            (fixture / 'unrouted.kicad_pcb').write_text('input')
            (fixture / 'routed.kicad_pcb').write_text('human')
            intents = root / 'intents'
            intents.mkdir()
            manifest = intents / 'board.intent.json'
            manifest.write_text('{}')
            report = {
                'routed': 5, 'connections': 5, 'tracks': 3, 'vias': 1, 'seconds': 9,
                'variant_seconds': 2, 'variants': [{'variant': 0, 'seconds': 2}],
                'expansions': 100, 'work': {'setup': {'expansions': 10}},
                'work_all_variants': {'setup': {'expansions': 20}}, 'stop': 'complete',
                'style': {'connections': 5}, 'widen': {'widened': 1}, 'stitch': {'added': 1},
            }
            before = {'errors': {}, 'routed_errors': {}, 'unconnected': 5, 'report': {}}
            after = {'errors': {}, 'routed_errors': {}, 'unconnected': 0, 'report': {}}
            def route(cmd, timeout):
                self.assertEqual(cmd[cmd.index('--intent') + 1], str(manifest))
                self.assertEqual(cmd[cmd.index('--work') + 1], '100')
                self.assertIn('--no-kb', cmd)
                output = pathlib.Path(cmd[cmd.index('-o') + 1])
                output.write_text('routed')
                pathlib.Path(cmd[cmd.index('--json') + 1]).write_text(json.dumps(report))
                return 0, '', '', 12.5
            def judge(path, **kwargs):
                if kwargs.get('saved') is not None:
                    kwargs['saved'].write_text('refilled')
                return before if path.name == 'unrouted.kicad_pcb' else after
            with patch.object(harness, 'SRC_FIX', fixtures), patch.object(harness, 'HUMAN', False), \
                    patch.object(harness, 'REFILL', True), patch.object(harness, 'PLACE_MODE', None), \
                    patch.object(harness, 'INTENTS', intents), patch.object(harness, 'WORK', 100), \
                    patch.object(harness, 'plane_nets', return_value=None), \
                    patch.object(harness, 'dead_pins', return_value=0), \
                    patch.object(harness, 'run_measured', side_effect=route) as measured, \
                    patch.object(harness, 'style_of', return_value={'connections': 5}) as reference, \
                    patch.object(harness, 'drc', side_effect=judge):
                row = harness.run_board('board', root / 'run', 1)
            self.assertTrue(row['clean'])
            self.assertEqual(row['cpu_s'], 12.5)
            self.assertEqual(row['intent'], str(manifest))
            self.assertEqual(row['refilled_board'], 'boards/board.refilled.kicad_pcb')
            for key, value in report.items():
                self.assertEqual(row[key], value)
            measured.assert_called_once()
            self.assertEqual(reference.call_args.args[0], fixture / 'routed.kicad_pcb')

    def test_failure_remains_in_headline_denominator_and_cost(self):
        rows = [dict(board='ok', judge='ok', exit=0, clean=True, completion=1, seconds=10, cpu_s=7),
                dict(board='failed', judge='failed', exit=-1, seconds=20, cpu_s=11)]
        summary = harness.headline(rows, 2)
        self.assertEqual((summary['scheduled'], summary['attempted'], summary['judged']), (2, 2, 1))
        self.assertEqual(summary['clean_pass'], 0.5)
        self.assertEqual(summary['judge_failures'], 1)
        self.assertEqual(summary['infrastructure_failures'], 1)
        self.assertEqual(summary['seconds'], 30)
        self.assertEqual(summary['cpu_seconds'], 18)

    def test_abnormal_router_exit_never_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = root / 'fixtures/b'
            fixture.mkdir(parents=True)
            (fixture / 'unrouted.kicad_pcb').write_text('input')
            def failed_route(cmd, timeout):
                pathlib.Path(cmd[cmd.index('-o') + 1]).write_text('output')
                return 9, '', 'crashed', 3
            report = dict(unconnected=0, report={})
            with patch.object(harness, 'SRC_FIX', fixture.parent), patch.object(harness, 'HUMAN', False), \
                    patch.object(harness, 'PLACE_MODE', None), patch.object(harness, 'INTENTS', None), \
                    patch.object(harness, 'REFILL', False), patch.object(harness, 'plane_nets', return_value=None), \
                    patch.object(harness, 'dead_pins', return_value=0), \
                    patch.object(harness, 'drc', return_value=report), patch.object(harness, 'run_measured', side_effect=failed_route):
                row = harness.run_board('b', root / 'run', 1)
            self.assertFalse(row['clean'])
            self.assertEqual(harness.headline([row], 1)['infrastructure_failures'], 1)

    def test_placement_copies_rules_before_judgment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            board = root / 'b.kicad_pcb'
            board.write_text('input')
            board.with_suffix('.kicad_dru').write_text('custom')
            outdir = root / 'run'
            (outdir / 'boards').mkdir(parents=True)
            def place(cmd, **kwargs):
                pathlib.Path(cmd[cmd.index('-o') + 1]).write_text('placed')
                import subprocess
                return subprocess.CompletedProcess(cmd, 0)
            def judge(path):
                self.assertEqual(path.with_suffix('.kicad_dru').read_text(), 'custom')
                return dict(cross_report={})
            with patch.object(harness, 'FIX', root / 'missing'), \
                    patch.object(harness.subprocess, 'run', side_effect=place), patch.object(harness, 'drc', side_effect=judge):
                placed = harness.place_board('b', board, outdir, {})
            self.assertEqual(placed.with_suffix('.kicad_dru').read_text(), 'custom')

    def test_run_directory_reuse_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = root / 'fixtures/b'
            fixture.mkdir(parents=True)
            (fixture / 'unrouted.kicad_pcb').write_text('input')
            (root / 'bench/results/reused').mkdir(parents=True)
            (root / 'bench/results/reused/summary.json').write_text('{}')
            with patch.object(harness, 'ROOT', root), patch.object(harness, 'fr_baseline', return_value={}), \
                    patch('sys.argv', ['run.py', '--fixtures', str(fixture.parent), '--boards', 'b', '--name', 'reused']):
                with self.assertRaises(SystemExit) as caught:
                    harness.main()
            self.assertEqual(caught.exception.code, 2)

    def test_failed_judge_finalizes_honestly_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = root / 'fixtures/b'
            fixture.mkdir(parents=True)
            (fixture / 'unrouted.kicad_pcb').write_text('input')
            (fixture / 'routed.kicad_pcb').write_text('human')
            row = dict(board='b', judge='failed', exit=0, wall_s=2)
            import contextlib
            import io
            with patch.object(harness, 'ROOT', root), patch.object(harness, 'SRC_FIX', fixture.parent), \
                    patch.object(harness, 'HUMAN', True), patch.object(harness, 'fr_baseline', return_value={}), \
                    patch.object(harness, 'fixture_provenance', return_value={'board_sha256': 'input', 'rules': {}, 'manifest_sha256': 'manifest'}), \
                    patch.object(harness, 'kicad_version', return_value='10.test'), \
                    patch.object(harness, 'attempt_board', return_value=row), \
                    patch('sys.argv', ['run.py', '--human', '--fixtures', str(fixture.parent), '--boards', 'b', '--name', 'new']), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(harness.main(), 1)
            result = root / 'bench/results/new'
            summary = json.loads((result / 'summary.json').read_text())
            manifest = json.loads((result / 'manifest.json').read_text())
            self.assertEqual((summary['boards'], summary['attempted'], summary['judged']), (1, 1, 0))
            self.assertEqual(summary['clean_pass'], 0)
            self.assertEqual(summary['judge_failures'], 1)
            self.assertIsNone(summary['completion'])
            self.assertEqual(summary['seconds'], 2)
            self.assertEqual(summary['state'], manifest['state'])
            self.assertEqual(manifest['state'], 'final')
            self.assertEqual(manifest['judge_failures'], 1)

    def test_censored_board_excluded_from_completion_mean(self):
        rows = [dict(judge='ok', exit=0, clean=False, completion=None, censored=True),
                dict(judge='ok', exit=0, clean=True, completion=1)]
        summary = harness.headline(rows, 2)
        self.assertEqual(summary['completion'], 1)
        self.assertEqual(summary['completion_boards'], 1)
        self.assertEqual(summary['censored_boards'], 1)

    def test_cache_content_and_judge_version_not_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            board = root / 'a.kicad_pcb'
            board.write_text('aaaa')
            def refill(src, target, report, timeout):
                target.write_text('analysis')
                report.write_text('{"violations":[],"unconnected_items":[]}')
            with patch.object(harness, 'REFILL', True), patch.object(harness, 'DRC_CACHE', root / 'cache'), \
                    patch.object(harness, 'kicad_version', return_value='10.a') as version, \
                    patch.object(harness, 'refill_board', side_effect=refill) as call:
                first = harness.drc(board)['cache_key']
                import os
                st = board.stat()
                board.write_text('bbbb')
                os.utime(board, ns=(st.st_atime_ns, st.st_mtime_ns))
                second = harness.drc(board)['cache_key']
                self.assertNotEqual(first, second)
                version.return_value = '10.b'
                self.assertNotEqual(second, harness.drc(board)['cache_key'])
                self.assertEqual(call.call_count, 3)

    def test_preflight_requires_valid_preparation_and_verified_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = root / 'b'
            fixture.mkdir()
            board = fixture / 'unrouted.kicad_pcb'
            board.write_text('input')
            manifest = fixture / 'preparation.json'
            manifest.write_text(json.dumps({'state': 'invalid'}))
            with patch.object(harness, 'SRC_FIX', root), patch.object(harness, 'HUMAN', False):
                with self.assertRaisesRegex(ValueError, 'not validated'):
                    harness.fixture_provenance('b')
                manifest.write_text(json.dumps({'state': 'valid', 'files': {'unrouted.kicad_pcb': harness.digest(board)}}))
                self.assertEqual(harness.fixture_provenance('b')['board_sha256'], harness.digest(board))
                board.with_suffix('.kicad_dru').write_text('new unhashed rules')
                with self.assertRaisesRegex(ValueError, 'omits board/rule hashes'):
                    harness.fixture_provenance('b')
                board.with_suffix('.kicad_dru').unlink()
                board.write_text('changed')
                with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                    harness.fixture_provenance('b')


if __name__ == '__main__':
    unittest.main()
