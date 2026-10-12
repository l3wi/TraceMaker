# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from plane_metrics import child, children, parse
from prepare_inputs import (expressions, fixture_transaction, prepare_board, refill_board,
                            repair_design_ids, strip_design_routing, terminal_signature)
from prepare_pcbench import transplant_design_copper


HEADER = '(kicad_pcb (layers (0 "F.Cu" signal) (2 "In1.Cu" power) (31 "B.Cu" signal))'


def via(x, y, layers='"F.Cu" "B.Cu"'):
    return f'(via (at {x} {y}) (size 0.6) (drill 0.3) (layers {layers}) (net 1))'


class PreparationTests(unittest.TestCase):
    def test_physical_contact_and_layer_span(self):
        track = '(segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu") (net 2))'
        # Copper contact, including endpoint and different-net contact, strips a via.
        text = HEADER + track + via(5, 0.4) + via(10.3, 0) + via(5, 0.5)
        # A buried via under an outer track is free: its span does not reach F.Cu.
        text += via(5, 0, '"In1.Cu" "B.Cu"') + ')'
        stripped, stats = strip_design_routing(text)
        self.assertEqual(stats['vias_removed'], 2)
        self.assertEqual(stats['fixed_vias'], 2)
        self.assertEqual(children(parse(stripped), 'segment'), [])
        self.assertEqual(len(children(parse(stripped), 'via')), 2)

    def test_arc_contact_is_not_chord_contact(self):
        arc = '(arc (start 1 0) (mid 0.7071067811865476 0.7071067811865476) (end 0 1) (width 0.1) (layer "F.Cu"))'
        text = HEADER + arc + via(0.7071067811865476, 0.7071067811865476) + via(0, 0) + ')'
        stripped, stats = strip_design_routing(text)
        self.assertEqual(stats['vias_removed'], 1)
        self.assertEqual(stats['fixed_vias'], 1)
        self.assertEqual(children(parse(stripped), 'arc'), [])

    def test_remove_fills_preserve_outlines_and_escaped_text(self):
        zone = '(zone (net 1) (layer "F.Cu") (polygon (pts (xy 0 0) (xy 2 0) (xy 2 2))) (filled_polygon (layer "F.Cu") (pts (xy 1 1))))'
        text = HEADER + r'(property "note" "escaped \" (via not copper)")' + zone + via(1, 1) + ')'
        stripped, stats = strip_design_routing(text)
        self.assertNotIn('(filled_polygon', stripped)
        self.assertIn('(polygon (pts (xy 0 0)', stripped)
        self.assertIn(r'escaped \" (via not copper)', stripped)
        self.assertEqual(stats['fixed_vias'], 1)

    def test_terminal_signature_across_kicad_versions(self):
        old = '(kicad_pcb (net 1 "GND") (module "Pkg" (fp_text reference "U1") (pad "1" smd rect (net 1 "GND"))))'
        new = '(kicad_pcb (footprint "Pkg" (property "Reference" "U1") (pad "1" smd rect (net "GND"))))'
        self.assertEqual(terminal_signature(old), terminal_signature(new))
        self.assertNotEqual(terminal_signature(old), terminal_signature(new.replace('"1" smd', '"2" smd')))

    def test_refill_private_copy_rules_and_atomic_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, target, report = root / 'raw.kicad_pcb', root / 'saved.kicad_pcb', root / 'drc.json'
            source.write_text(HEADER + ')')
            source.with_suffix('.kicad_dru').write_text('rules')
            def run(command, **kwargs):
                work = pathlib.Path(command[-1])
                self.assertEqual(work.with_suffix('.kicad_dru').read_text(), 'rules')
                self.assertIn('--save-board', command)
                work.write_text(HEADER + '(property "fill" "saved"))')
                pathlib.Path(command[command.index('-o') + 1]).write_text('{"violations": []}')
                return type('Result', (), {'returncode': 0})()
            with patch('prepare_inputs.subprocess.run', side_effect=run):
                refill_board(source, target, report)
            self.assertEqual(source.read_text(), HEADER + ')')
            self.assertEqual(target.read_text(), HEADER + '(property "fill" "saved"))')
            self.assertEqual(json.loads(report.read_text()), {'violations': []})
            self.assertEqual(target.with_suffix('.kicad_dru').read_text(), 'rules')
            with patch('prepare_inputs.subprocess.run', return_value=type('Result', (), {'returncode': 1, 'stderr': 'failed', 'stdout': ''})()):
                with self.assertRaises(RuntimeError):
                    refill_board(source, target, report)
            self.assertEqual(target.read_text(), HEADER + '(property "fill" "saved"))')
            self.assertEqual(json.loads(report.read_text()), {'violations': []})

    def test_pcbench_transplant_preserves_published_terminals(self):
        raw = HEADER + '(net 0 "") (net 1 "GND") (module "P" (fp_text reference "REF**") (pad "1" thru_hole circle (net 1 "GND")))'
        raw += '(zone (net 1) (net_name "GND") (layer "F.Cu") (polygon (pts (xy 0 0))))' + via(1, 1) + ')'
        published = HEADER + '(net 0 "") (net 7 "GND") (module "P" (fp_text reference "REF**_2") (pad "1" thru_hole circle (net 7 "GND"))))'
        result, stats = transplant_design_copper(raw, published)
        self.assertEqual(terminal_signature(result), terminal_signature(published))
        self.assertEqual(stats['fixed_vias'], 1)
        self.assertEqual(child(children(parse(result), 'via')[0], 'net'), ['net', '7'])
        with self.assertRaisesRegex(ValueError, 'no published net match'):
            transplant_design_copper(raw, published.replace('"GND"', '"POWER"'))
        incompatible = published[:-1] + '(gr_line (start 0 0) (end 1 0) (layer "Edge.Cuts")))'
        result, stats = transplant_design_copper(raw, incompatible)
        self.assertEqual(terminal_signature(result), terminal_signature(published))
        self.assertFalse(stats['design_copper_comparable'])
        self.assertEqual(stats['zone_preparation'], 'none')
        self.assertEqual(children(parse(result), 'zone'), [])
        missing_zone_raw = raw.replace(via(1, 1), '')
        result, stats = transplant_design_copper(missing_zone_raw, published.replace('"GND"', '"POWER"'))
        self.assertEqual(stats['dropped_zone_nets'], ['GND'])
        self.assertEqual(children(parse(result), 'zone'), [])

    def test_missing_zero_duplicate_design_ids_are_deterministic(self):
        zero = '00000000-0000-0000-0000-000000000000'
        valid = '00000000-0000-0000-0000-000000000001'
        text = HEADER + f'(zone (uuid "{zero}")) (zone (uuid "{zero}"))'
        text += f'(footprint "Pkg" (uuid "{valid}")) (via (uuid "{valid}")) (via (at 1 1)) )'
        fixed, repairs = repair_design_ids(text, 'board')
        self.assertEqual(repairs, {'zero': 2, 'duplicate': 1, 'missing_via': 1})
        self.assertEqual(fixed, repair_design_ids(text, 'board')[0])
        self.assertEqual(repair_design_ids(fixed, 'board'), (fixed, {}))
        identities = [uuid.UUID(parse(fixed[s:e])[1]) for s, e, _, head in expressions(fixed) if head == 'uuid']
        self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue(all(identity.int for identity in identities))
        self.assertIn(f'(uuid "{valid}")', fixed)

    def test_refill_converts_then_repairs_private_human_ids(self):
        zero = '00000000-0000-0000-0000-000000000000'
        original = HEADER + '(zone (net 1) (layer "F.Cu")) )'
        converted = HEADER + f'(zone (uuid "{zero}")) (dimension (uuid "{zero}")) )'
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, target, report = root / 'raw.kicad_pcb', root / 'human.kicad_pcb', root / 'report.json'
            source.write_text(original)
            calls = []

            def run(command, **kwargs):
                work = pathlib.Path(command[-1])
                calls.append(work.read_text())
                if len(calls) == 1:
                    work.write_text(converted)
                else:
                    self.assertEqual(repair_design_ids(work.read_text(), 'fixture')[1], {})
                    # Native dimension serialization reintroduces a display-text alias.
                    text = work.read_text()
                    dimension = children(parse(text), 'dimension')[0]
                    identity = child(dimension, 'uuid')[1]
                    work.write_text(text.replace(f'(dimension (uuid "{identity}"))',
                                                f'(dimension (uuid "{identity}") (gr_text (uuid "{identity}")))'))
                pathlib.Path(command[command.index('-o') + 1]).write_text(json.dumps({'pass': len(calls)}))
                return subprocess.CompletedProcess(command, 0)

            with patch('prepare_inputs.subprocess.run', side_effect=run):
                stats = refill_board(source, target, report, board_name='fixture')
            self.assertEqual(len(calls), 2)
            self.assertEqual(source.read_text(), original)
            self.assertEqual(stats['identity_repairs'], {'zero': 2, 'duplicate': 1})
            self.assertEqual(stats['post_save_identity_repairs'], {'duplicate': 1})
            self.assertEqual(repair_design_ids(target.read_text(), 'fixture')[1], {})
            self.assertEqual(json.loads(report.read_text()), {'pass': 2})

    def test_refill_repair_failure_does_not_publish_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, target, report = root / 'raw.kicad_pcb', root / 'saved.kicad_pcb', root / 'report.json'
            source.write_text(HEADER + ')')
            target.write_text('old saved')
            report.write_text('{"old": true}')
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if len(calls) == 2:
                    raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                pathlib.Path(command[-1]).write_text(HEADER + '(zone (uuid "00000000-0000-0000-0000-000000000000")))')
                pathlib.Path(command[command.index('-o') + 1]).write_text('{}')
                return subprocess.CompletedProcess(command, 0)

            with patch('prepare_inputs.subprocess.run', side_effect=run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    refill_board(source, target, report)
            self.assertEqual(source.read_text(), HEADER + ')')
            self.assertEqual(target.read_text(), 'old saved')
            self.assertEqual(json.loads(report.read_text()), {'old': True})

    @staticmethod
    def refill_success(command, **kwargs):
        pathlib.Path(command[command.index('-o') + 1]).write_text('{"violations": []}')
        return subprocess.CompletedProcess(command, 0)

    def test_preparation_manifest_covers_entire_published_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / 'source.kicad_pcb'
            source.write_text(HEADER + ')')
            source.with_suffix('.kicad_pro').write_text('project')
            source.with_suffix('.kicad_dru').write_text('rules')
            destination = root / 'fixture' / 'unrouted.kicad_pcb'
            destination.parent.mkdir()
            (destination.parent / 'stale.kicad_dru').write_text('stale')
            with patch('prepare_inputs.subprocess.run', side_effect=self.refill_success):
                stats = prepare_board(source, destination, metadata={'board': 'fixture', 'custom': 'kept'})
            manifest = json.loads(destination.with_name('preparation.json').read_text())
            self.assertEqual(stats, manifest)
            self.assertEqual(manifest['state'], 'valid')
            self.assertEqual(manifest['custom'], 'kept')
            self.assertEqual(set(manifest['files']), {
                'unrouted.kicad_pcb', 'routed.kicad_pcb', 'unrouted.kicad_pro', 'unrouted.kicad_dru',
                'routed.kicad_pro', 'routed.kicad_dru', 'preparation.drc.json'})
            for name, digest in manifest['files'].items():
                self.assertEqual(digest, hashlib.sha256((destination.parent / name).read_bytes()).hexdigest())
            self.assertEqual(destination.with_name('routed.kicad_pcb').read_bytes(), source.read_bytes())
            self.assertFalse(destination.with_name('stale.kicad_dru').exists())

    def test_preparation_timeout_or_terminal_mismatch_invalidates_old_fixture(self):
        for failure in ('timeout', 'terminals', 'bad_report'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                source = root / 'source.kicad_pcb'
                source.write_text(HEADER + '(footprint "P" (property "Reference" "U1") (pad "1" smd rect)))')
                source.with_suffix('.kicad_dru').write_text('new rules')
                destination = root / 'fixture' / 'unrouted.kicad_pcb'
                destination.parent.mkdir()
                destination.write_text('old input')
                human = destination.with_name('routed.kicad_pcb')
                human.write_text('old human')
                destination.with_suffix('.kicad_dru').write_text('old rules')
                destination.with_name('preparation.drc.json').write_text('{"old": true}')
                destination.with_name('preparation.json').write_text('{"state": "valid"}')

                def run(command, **kwargs):
                    self.assertEqual(destination.read_text(), 'old input')
                    self.assertEqual(human.read_text(), 'old human')
                    self.assertEqual(json.loads(destination.with_name('preparation.json').read_text())['state'], 'invalid')
                    if failure == 'timeout':
                        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                    if failure == 'terminals':
                        work = pathlib.Path(command[-1])
                        work.write_text(work.read_text().replace('"U1"', '"CHANGED"'))
                    report = pathlib.Path(command[command.index('-o') + 1])
                    report.write_text('broken JSON' if failure == 'bad_report' else '{}')
                    return subprocess.CompletedProcess(command, 0)

                with patch('prepare_inputs.subprocess.run', side_effect=run):
                    with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                        prepare_board(source, destination)
                self.assertEqual(json.loads(destination.with_name('preparation.json').read_text())['state'], 'invalid')
                self.assertEqual(destination.read_text(), 'old input')
                self.assertEqual(human.read_text(), 'old human')
                self.assertEqual(destination.with_suffix('.kicad_dru').read_text(), 'old rules')
                self.assertEqual(json.loads(destination.with_name('preparation.drc.json').read_text()), {'old': True})

    def test_failed_directory_publish_restores_invalid_old_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = pathlib.Path(directory) / 'fixture'
            destination.mkdir()
            (destination / 'unrouted.kicad_pcb').write_text('old')
            import prepare_inputs
            replace = prepare_inputs.os.replace

            def fail_publish(source, target):
                if pathlib.Path(source).name == 'fixture' and pathlib.Path(source).parent != destination.parent:
                    raise OSError('injected directory publish failure')
                return replace(source, target)

            with patch('prepare_inputs.os.replace', side_effect=fail_publish):
                with self.assertRaisesRegex(OSError, 'injected'):
                    with fixture_transaction(destination, {}) as stage:
                        (stage / 'unrouted.kicad_pcb').write_text('new')
            self.assertEqual((destination / 'unrouted.kicad_pcb').read_text(), 'old')
            self.assertEqual(json.loads((destination / 'preparation.json').read_text())['state'], 'invalid')


if __name__ == '__main__':
    unittest.main()
