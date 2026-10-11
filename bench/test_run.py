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


if __name__ == '__main__':
    unittest.main()
