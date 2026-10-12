# SPDX-License-Identifier: GPL-3.0-or-later
"""Three-state rule checks, profile/manifest validation and rule summaries."""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import quality

import quality_rules as rules

SKEW = dict(id='pcie.intra_skew', interface='pcie', measure='intra_skew_mm', comparator='<', limit=0.15,
            strength='must', source='p33 table 5', treatment='rule')


class RuleTests(unittest.TestCase):
    def test_band_edges(self):
        # Inside the 1 µm band every comparator reports a boundary, not a verdict.
        for comparator in rules.COMPARATORS:
            self.assertEqual(rules.check(0.1500, 0.15, comparator), 'boundary')
            self.assertEqual(rules.check(0.1509, 0.15, comparator), 'boundary')
        self.assertEqual(rules.check(0.1489, 0.15, '<'), 'pass')
        self.assertEqual(rules.check(0.1511, 0.15, '<'), 'fail')
        # A value exactly 1 µm from the limit is on the band edge despite float error.
        self.assertEqual(rules.check(0.499, 0.5, '>'), 'boundary')
        self.assertEqual(rules.check(0.4989, 0.5, '>'), 'fail')
        self.assertEqual(rules.check(0.1511, 0.15, '>'), 'pass')
        # Counts are exact: no band, inclusive comparator decides equality.
        self.assertEqual(rules.check(2, 2, '<=', band=0), 'pass')
        self.assertEqual(rules.check(3, 2, '<=', band=0), 'fail')
        self.assertEqual(rules.check(2, 2, '<', band=0), 'fail')
        self.assertEqual(rules.check(None, 2, '<'), 'na')

    def test_result_never_passes_unknown(self):
        r = rules.result(SKEW, 'pcie0:RX0', None)
        self.assertEqual((r['outcome'], r['reason']), ('na', 'value not measurable'))

    def test_profile_validation_and_modes(self):
        slot = dict(SKEW, id='pcie.vias.slot', measure='vias_per_leg', comparator='<=', limit=2, mode='slot')
        down = dict(slot, id='pcie.vias.down', limit=4, mode='device_down')
        anyif = dict(SKEW, id='any.self_spacing', interface='*', measure='self_spacing_ratio', comparator='>=', limit=4)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)/'p.json'
            path.write_text(json.dumps(dict(id='p', rules=[SKEW, slot, down, anyif])))
            profile = rules.load_profile(path)
            self.assertEqual([r['id'] for r in rules.rules_for(profile, 'pcie', 'vias_per_leg', 'slot')], ['pcie.vias.slot'])
            # Unknown mode never guesses a mode-specific limit.
            self.assertEqual(rules.rules_for(profile, 'pcie', 'vias_per_leg'), [])
            self.assertEqual([r['id'] for r in rules.rules_for(profile, 'usb2', 'self_spacing_ratio')], ['any.self_spacing'])
            path.write_text(json.dumps(dict(id='p', rules=[SKEW, SKEW])))
            with self.assertRaises(ValueError):
                rules.load_profile(path)

    def test_manifest_requires_differential_mate(self):
        bad = dict(schema=1, board='b', status='draft', interfaces=[dict(id='u', interface='usb2', mode=None, confidence='candidate',
                   lanes=[dict(id='D', role='data', p='USB_P', n=None, endpoints=[])], series=[], reference_nets=[], isolated_nets=[])])
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d)/'b.interfaces.json').write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                rules.load_interfaces(d, 'b')
            self.assertIsNone(rules.load_interfaces(d, 'other'))

    def test_profile_without_manifest_board_job_and_markdown(self):
        d = dict(nets=[], pads=[], tracks=[], arcs=[], vias=[], copper_layers=['F.Cu', 'B.Cu'])
        provenance = dict(rules={})
        profile = dict(id='test', rules=[SKEW])
        with tempfile.TemporaryDirectory() as directory:
            reference = pathlib.Path(directory)/'human'
            (reference/'boards').mkdir(parents=True)
            board = reference/'boards'/'b.kicad_pcb'
            board.write_text('(kicad_pcb)')
            router = pathlib.Path(directory)/'router'
            (router/'boards').mkdir(parents=True)
            (router/'boards'/'b.kicad_pcb').write_text('(kicad_pcb)')
            for interface_dir in (None, pathlib.Path(directory)/'missing'):
                with self.subTest(interface_dir=interface_dir), \
                     patch.object(quality.io, 'fixture', return_value=None), \
                     patch.object(quality.io, 'inspect', return_value=d), \
                     patch.object(quality.io, 'prepare', return_value=(board, {}, provenance)), \
                     patch.object(quality.craft, 'metadata', return_value={}), \
                     patch.object(quality.identity, 'canonical_human', return_value=(d, {})), \
                     patch.object(quality.si, 'all_fills', return_value={}), \
                     patch.object(quality.si, 'fills', return_value=({}, set(), 'hash')):
                    result = quality.board_job(('b', [router], reference, None, '10', interface_dir, profile))
                self.assertEqual(result['errors'], {})
                self.assertEqual(result['runs'][str(reference)]['coverage']['interfaces']['status'], 'missing')
                report = dict(reference='human', kicad_version='10', seconds=0, gates={}, categories={},
                              noise_floor={}, limits=[], errors={}, rules={}, boards=[result],
                              common_finished=dict(boards=[], categories={}, comparisons=[]))
                self.assertIn('| b | human | missing | N/A | 0 | N/A |', quality.markdown(report))
                # Old cached arms may predate coverage publication.
                result['runs'][str(reference)]['coverage'].pop('interfaces')
                self.assertIn('| b | human | missing | N/A | 0 | N/A |', quality.markdown(report))

    def test_summary_keeps_human_failures_and_missing_items(self):
        human = {'b': {}}
        router = {'b': {}}
        for item, h, r in [('x:1', 0.3, 0.1), ('x:2', 0.1, 0.3), ('x:3', 0.3, 0.3), ('x:4', 0.1, None)]:
            hr = rules.result(SKEW, item, h)
            human['b'][rules.key(hr)] = hr
            if r is not None:
                rr = rules.result(SKEW, item, r)
                router['b'][rules.key(rr)] = rr
        row = rules.summarize(human, {'T': router})['pcie.intra_skew']
        self.assertEqual(row['human'], dict(**{'pass': 2, 'fail': 2, 'boundary': 0, 'na': 0, 'not_applicable': 0}))
        self.assertEqual(row['arms']['T'], {'pass': 1, 'fail': 2, 'boundary': 0, 'na': 1, 'not_applicable': 0, 'fixed': 1, 'broke': 1, 'both_fail': 1})
        self.assertIn('| pcie.intra_skew | < 0.15 | 2/2/0/0/0 | 1/2/0/1/0 (1, 1) |', rules.markdown(rules.summarize(human, {'T': router}), ['T']))


if __name__ == '__main__':
    unittest.main()
