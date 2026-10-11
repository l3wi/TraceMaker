# SPDX-License-Identifier: GPL-3.0-or-later
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import run as harness


class HarnessTests(unittest.TestCase):
    def test_saved_refill_reuses_atomic_drc_cache(self):
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
            before = {'errors': {}, 'routed_errors': {}, 'unconnected': 5}
            after = {'errors': {}, 'routed_errors': {}, 'unconnected': 0}
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
            before = {'errors': {}, 'routed_errors': {}, 'unconnected': 5}
            after = {'errors': {}, 'routed_errors': {}, 'unconnected': 0}
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


if __name__ == '__main__':
    unittest.main()
