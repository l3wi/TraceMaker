# SPDX-License-Identifier: GPL-3.0-or-later
import json
import pathlib
import tempfile
import unittest
import uuid
from unittest.mock import patch

from plane_metrics import child, children, parse
from prepare_inputs import expressions, refill_board, repair_design_ids, strip_design_routing, terminal_signature
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
            source.write_text('raw')
            source.with_suffix('.kicad_dru').write_text('rules')
            def run(command, **kwargs):
                work = pathlib.Path(command[-1])
                self.assertEqual(work.with_suffix('.kicad_dru').read_text(), 'rules')
                self.assertIn('--save-board', command)
                work.write_text('refilled')
                pathlib.Path(command[command.index('-o') + 1]).write_text('{"violations": []}')
                return type('Result', (), {'returncode': 0})()
            with patch('prepare_inputs.subprocess.run', side_effect=run):
                refill_board(source, target, report)
            self.assertEqual(source.read_text(), 'raw')
            self.assertEqual(target.read_text(), 'refilled')
            self.assertEqual(json.loads(report.read_text()), {'violations': []})
            self.assertEqual(target.with_suffix('.kicad_dru').read_text(), 'rules')
            with patch('prepare_inputs.subprocess.run', return_value=type('Result', (), {'returncode': 1, 'stderr': 'failed', 'stdout': ''})()):
                with self.assertRaises(RuntimeError):
                    refill_board(source, target, report)
            self.assertEqual(target.read_text(), 'refilled')
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


if __name__ == '__main__':
    unittest.main()
