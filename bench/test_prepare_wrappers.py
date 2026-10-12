# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixture publication fault injection; no KiCad, router or network is required."""
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import prepare_dac2020
import prepare_demos
import prepare_fanout31
import prepare_modern
import prepare_pcbench
import prepare_srj
from plane_metrics import children, parse


BOARD = '(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))'


class PreparationWrapperTests(unittest.TestCase):
    def assert_invalid(self, destination):
        manifest = json.loads((destination / 'preparation.json').read_text())
        self.assertEqual(manifest['state'], 'invalid')
        self.assertNotIn('files', manifest)

    def assert_valid_hashes(self, destination):
        manifest = json.loads((destination / 'preparation.json').read_text())
        self.assertEqual(manifest['state'], 'valid')
        for name, digest in manifest['files'].items():
            self.assertEqual(digest, hashlib.sha256((destination / name).read_bytes()).hexdigest())
        return manifest

    @staticmethod
    def import_success(command, **kwargs):
        board = pathlib.Path(command[command.index('-o') + 1])
        board.write_text(BOARD)
        board.with_suffix('.kicad_pro').write_text('{}')
        board.with_suffix('.kicad_dru').write_text('(version 1)')
        pathlib.Path(command[command.index('--json') + 1]).write_text(
            json.dumps({'srj_pairs': 2, 'board_pairs': 2, 'warnings': []}))
        return subprocess.CompletedProcess(command, 0, '', '')

    def test_native_cli_wrappers_invalidate_on_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            timeout = subprocess.TimeoutExpired('injected', 600)
            destination = root / 'fixture'
            destination.mkdir()
            (destination / 'unrouted.kicad_pcb').write_text('old input')
            sources = root / 'sources.json'
            sources.write_text(json.dumps([{'name': 'fixture', 'pcb': 'fixture.kicad_pcb'}]))
            with patch.object(prepare_modern, 'SOURCES', sources), \
                    patch.object(prepare_modern, 'set_boards', return_value=['fixture']), \
                    patch.object(prepare_modern, 'fetch', side_effect=timeout), \
                    patch('sys.argv', ['prepare_modern', '--out', str(root)]):
                self.assertEqual(prepare_modern.main(), 1)
            self.assert_invalid(destination)
            with patch.object(prepare_demos, 'prepare', side_effect=timeout), \
                    patch('sys.argv', ['prepare_demos', '--out', str(root), 'fixture']):
                self.assertEqual(prepare_demos.main(), 1)
            self.assert_invalid(destination)
            with patch.object(prepare_pcbench, 'prepare', side_effect=timeout), \
                    patch('sys.argv', ['prepare_pcbench', '--out', str(root), 'fixture']):
                self.assertEqual(prepare_pcbench.main(), 1)
            self.assert_invalid(destination)
            self.assertEqual((destination / 'unrouted.kicad_pcb').read_text(), 'old input')

    def test_pcbench_validates_published_terminals_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / 'source' / 'fixture'
            source.mkdir(parents=True)
            terminals = '(net 1 "GND") (footprint "P" (property "Reference" "REF") (pad "1" smd rect (net 1 "GND")))'
            raw_text = BOARD[:-1] + terminals + '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1)))'
            (source / 'raw.kicad_pcb').write_text(raw_text)
            (source / 'unrouted.kicad_pcb').write_text(BOARD[:-1] + terminals.replace('"REF"', '"REF_2"') + ')')
            (source / 'ground_truth.json').write_text('{"source_pcb": "raw.kicad_pcb", "segment_count": 1}')
            out = root / 'out'
            destination = out / 'fixture'

            def refill(command, **kwargs):
                pathlib.Path(command[command.index('-o') + 1]).write_text('{}')
                return subprocess.CompletedProcess(command, 0, '', '')

            with patch('prepare_inputs.subprocess.run', side_effect=refill):
                stats = prepare_pcbench.prepare('fixture', root / 'source', out)
            self.assertTrue(stats['identity_matches_pcbench'])
            self.assertTrue(stats['human_confirmed'])
            self.assert_valid_hashes(destination)
            self.assertEqual((destination / 'routed.kicad_pcb').read_text(), raw_text)
            self.assertIn('"REF_2"', (destination / 'unrouted.kicad_pcb').read_text())
            previous = (destination / 'unrouted.kicad_pcb').read_bytes()

            def change_terminal(command, **kwargs):
                work = pathlib.Path(command[-1])
                work.write_text(work.read_text().replace('"REF_2"', '"WRONG"'))
                return refill(command, **kwargs)

            with patch('prepare_inputs.subprocess.run', side_effect=change_terminal):
                with self.assertRaises(ValueError):
                    prepare_pcbench.prepare('fixture', root / 'source', out)
            self.assert_invalid(destination)
            self.assertEqual((destination / 'unrouted.kicad_pcb').read_bytes(), previous)
            self.assertEqual((destination / 'routed.kicad_pcb').read_text(), raw_text)

    def test_srj_success_freezes_input_rules_problem_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            problem = root / 'source.json'
            problem.write_text('{"connections": []}')
            destination = root / 'fixture'
            with patch.object(prepare_srj.subprocess, 'run', side_effect=self.import_success):
                result = prepare_srj.convert((problem, destination, '0.1', '0.1', False))
            self.assertNotIn('error', result)
            manifest = self.assert_valid_hashes(destination)
            self.assertFalse(manifest['human_available'])
            self.assertEqual(set(manifest['files']), {'unrouted.kicad_pcb', 'unrouted.kicad_pro',
                                                     'unrouted.kicad_dru', 'problem.json', 'import.json'})
            self.assertFalse((destination / 'problem.json').is_symlink())
            problem.write_text('changed source')
            self.assertEqual((destination / 'problem.json').read_text(), '{"connections": []}')
            self.assert_valid_hashes(destination)

    def test_srj_timeout_and_count_mismatch_never_publish(self):
        for fault in ('timeout', 'import_count', 'route_count', 'route_exit', 'missing_route_report'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                problem = root / 'source.json'
                problem.write_text('{}')
                destination = root / 'fixture'
                destination.mkdir()
                (destination / 'unrouted.kicad_pcb').write_text('old input')
                (destination / 'preparation.json').write_text('{"state": "valid"}')

                def run(command, **kwargs):
                    if fault == 'timeout':
                        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                    if command[1] == 'srj-import':
                        result = self.import_success(command, **kwargs)
                        if fault == 'import_count':
                            pathlib.Path(command[command.index('--json') + 1]).write_text(
                                '{"srj_pairs": 2, "board_pairs": 1, "warnings": []}')
                        return result
                    report = pathlib.Path(command[command.index('--json') + 1])
                    if fault != 'missing_route_report':
                        report.write_text(json.dumps({'connections': 1 if fault == 'route_count' else 2}))
                    return subprocess.CompletedProcess(command, 1 if fault == 'route_exit' else 3, '', 'injected')

                with patch.object(prepare_srj.subprocess, 'run', side_effect=run):
                    result = prepare_srj.convert((problem, destination, '0.1', '0.1', True))
                self.assertIn('error', result)
                self.assert_invalid(destination)
                self.assertEqual((destination / 'unrouted.kicad_pcb').read_text(), 'old input')
                self.assertFalse((destination / 'import.json').exists())

    def test_srj_verified_success_discards_counting_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            problem = root / 'problem.json'
            problem.write_text('{}')
            destination = root / 'fixture'

            def run(command, **kwargs):
                if command[1] == 'srj-import':
                    return self.import_success(command, **kwargs)
                board = pathlib.Path(command[command.index('-o') + 1])
                board.write_text(BOARD)
                board.with_suffix('.kicad_pro').write_text('{}')
                pathlib.Path(command[command.index('--json') + 1]).write_text('{"connections": 2}')
                return subprocess.CompletedProcess(command, 3, '', '')

            with patch.object(prepare_srj.subprocess, 'run', side_effect=run):
                result = prepare_srj.convert((problem, destination, '0.1', '0.1', True))
            self.assertNotIn('error', result)
            self.assertEqual(result['route_connections'], 2)
            manifest = self.assert_valid_hashes(destination)
            self.assertTrue(manifest['connections_verified'])
            self.assertFalse(any(name.startswith('count.') for name in manifest['files']))

    def test_fanout_success_and_timeout_invalidates_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / 'source.json'
            source.write_text(json.dumps({'simpleRouteJson': {}, 'solverOptions': {'escapeLayers': ['F.Cu']}}))
            destination = root / 'fixture'
            with patch.object(prepare_fanout31.subprocess, 'run', side_effect=self.import_success):
                result = prepare_fanout31.convert((source, destination))
            self.assertIsNone(result['error'])
            manifest = self.assert_valid_hashes(destination)
            self.assertIn('source.json', manifest['files'])
            self.assertEqual(json.loads((destination / 'problem.json').read_text())['tracemakerTrackLayers'], ['F.Cu'])
            previous = (destination / 'unrouted.kicad_pcb').read_bytes()
            with patch.object(prepare_fanout31.subprocess, 'run', side_effect=subprocess.TimeoutExpired('injected', 600)):
                result = prepare_fanout31.convert((source, destination))
            self.assertIsNotNone(result['error'])
            self.assert_invalid(destination)
            self.assertEqual((destination / 'unrouted.kicad_pcb').read_bytes(), previous)

    def test_fanout_count_mismatch_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / 'source.json'
            source.write_text(json.dumps({'simpleRouteJson': {}, 'solverOptions': {'escapeLayers': ['F.Cu']}}))
            destination = root / 'fixture'

            def run(command, **kwargs):
                result = self.import_success(command, **kwargs)
                pathlib.Path(command[command.index('--json') + 1]).write_text('{"srj_pairs": 2, "board_pairs": 1}')
                return result

            with patch.object(prepare_fanout31.subprocess, 'run', side_effect=run):
                result = prepare_fanout31.convert((source, destination))
            self.assertIsNotNone(result['error'])
            self.assert_invalid(destination)
            self.assertFalse((destination / 'unrouted.kicad_pcb').exists())

    def test_dac_preserves_legacy_stripping_and_publishes_human_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / 'source.kicad_pcb'
            text = BOARD[:-1] + '(segment (start 0 0) (end 1 1)) (via (at 1 1)))'
            source.write_text(text)
            source.with_suffix('.kicad_dru').write_text('legacy rules')
            dsn = root / 'input.dsn'
            dsn.write_text('(pcb fixture)')
            destination = root / 'fixture'
            prepare_dac2020.prepare(source, dsn, destination)
            manifest = self.assert_valid_hashes(destination)
            self.assertFalse(manifest['refilled'])
            self.assertEqual((destination / 'raw.kicad_pcb').read_text(), text)
            self.assertEqual((destination / 'routed.kicad_pcb').read_text(), text)
            board = parse((destination / 'unrouted.kicad_pcb').read_text())
            self.assertEqual(children(board, 'via'), [])
            self.assertEqual(children(board, 'segment'), [])
            self.assertIn('preparation.report.json', manifest['files'])
            with patch.object(prepare_dac2020, 'strip_routing', return_value=BOARD[:-1] + '(footprint "P" (property "Reference" "NEW")))'):
                with self.assertRaises(ValueError):
                    prepare_dac2020.prepare(source, dsn, destination)
            self.assert_invalid(destination)
            self.assertEqual((destination / 'raw.kicad_pcb').read_text(), text)


if __name__ == '__main__':
    unittest.main()
