# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic measurement cases: holes/named nets, missing references, censoring."""
import hashlib
import json
import pathlib
import tempfile
import unittest

import quality_compare as compare
import quality_io as io
import quality as scorecard
import quality_si as si
import quality_identity as identity
from shapely.geometry import Point


class QualityTests(unittest.TestCase):
    def test_named_and_numeric_fill_holes(self):
        polygon = '(filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))'
        hole = '(filled_polygon (layer "In1.Cu") (pts (xy 4 4) (xy 6 4) (xy 6 6) (xy 4 6)))'
        for nets, zone in [('(net 1 "GND")', '(net 1)'), ('', '(net "GND")')]:
            board = '(kicad_pcb (layers (0 "F.Cu" signal) (1 "In1.Cu" power) (31 "B.Cu" signal)) '+nets+' (zone '+zone+' (layer "In1.Cu") '+polygon+hole+'))'
            with tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory)/'test.kicad_pcb'
                path.write_text(board)
                filled = si.all_fills(path)['GND', 'In1.Cu']
                self.assertAlmostEqual(filled.area, 96)
                self.assertFalse(filled.covers(Point(5, 5)))
                self.assertTrue(filled.covers(Point(1, 1)))

    def test_missing_reference_is_not_zero_gap(self):
        data = dict(copper_layers=['F.Cu', 'B.Cu'], vias=[], arcs=[], tracks=[dict(net='USB_DP', layer='F.Cu', sx=0, sy=0, ex=1000000, ey=0, width=200000)])
        measured = si.retmetric(data, ['USB_DP'], {}, set())
        self.assertIsNone(measured['gap_mm'])
        self.assertEqual(measured['unknown_mm'], 1)
        self.assertFalse(measured['valid'])

    def test_capped_common_denominator_is_unknown(self):
        judged = io.judge(dict(unconnected_items=[{}]*499), dict(unconnected_items=[],violations=[]))
        self.assertTrue(judged['electrical_pass'])
        self.assertTrue(judged['censored'])
        self.assertIsNone(judged['completion'])

    def test_error_instances_ignore_renames_but_do_not_cancel_locations(self):
        def report(reference,uuid,x):
            return dict(unconnected_items=[],violations=[dict(type='clearance',severity='error',items=[
                dict(uuid=uuid,description='Pad 1 [GND] of '+reference+' on F.Cu',pos=dict(x=x,y=2))])])
        original=report('CB1','legacy-uuid',1)
        renamed=report('CB1_2','converted-uuid',1)
        self.assertTrue(io.judge(original,renamed)['drc_pass'])
        moved=report('CB1_2','converted-uuid',3)
        self.assertEqual(io.judge(original,moved)['added_errors'],dict(clearance=1))

    def test_missing_obligation_loses_but_not_in_intersection(self):
        human = {'board': {'decoupling': {'C1.1>U1.1': dict(effective_mm=2,connected=True)}}}
        arms = {'P': {'board': {'decoupling': {'C1.1>U1.1': dict(effective_mm=1,connected=True)}}},
                'R': {'board': {'decoupling': {'C1.1>U1.1': dict(effective_mm=None,connected=False)}}}}
        result = compare.summarize(human, arms)['decoupling']['effective_mm']['arms']
        self.assertEqual(result['R']['all_obligations']['loss'], 1)
        self.assertEqual(result['R']['all_obligations']['missing'], 1)
        self.assertEqual(result['P']['common_connected']['intersection'], 0)
        self.assertIsNone(result['P']['common_connected']['pooled_parity_pct'])

    def test_published_reference_rename_matches_physical_terminal(self):
        pad=dict(ref='CB1',num='1',net='+3V3',layers=['F.Cu'],x=1000000,y=2000000)
        human=dict(pads=[pad])
        published=dict(pads=[dict(pad,ref='CB1_2')])
        matched,labels,coverage=identity.match(published,human)
        self.assertEqual(matched['pads'][0]['ref'],'CB1')
        self.assertEqual(labels['CB1_2.1'],'CB1.1')
        self.assertEqual(coverage['unmatched'],0)
        moved=dict(pads=[dict(pad,ref='CB1_2',x=2000000)])
        _,labels,coverage=identity.match(moved,human)
        self.assertEqual(coverage['unmatched'],1)
        self.assertNotIn('CB1_2.1',labels)

    def test_identical_sources_reuse_saved_analysis_with_different_names(self):
        with tempfile.TemporaryDirectory() as directory:
            run=pathlib.Path(directory)
            original=run/'unrouted.kicad_pcb'
            human=run/'routed.kicad_pcb'
            original.write_text('(kicad_pcb)')
            human.write_bytes(original.read_bytes())
            source_hash=io.digest(original)
            key=hashlib.sha256((source_hash+'unit-version').encode()).hexdigest()
            cache=run/'quality_cache'/key
            cache.mkdir(parents=True)
            analysis=cache/original.name
            analysis.write_bytes(original.read_bytes())
            (cache/'drc.json').write_text(json.dumps(dict(unconnected_items=[],violations=[])))
            (cache/'provenance.json').write_text(json.dumps(dict(
                source=str(original),source_sha256=source_hash,analysis_board=str(analysis),
                identity_repair_code_sha256=io.digest(io.ROOT/'bench/prepare_inputs.py'))))
            target,_,provenance=io.prepare(human,run,'quality-synthetic','unit-version')
            self.assertEqual(target,analysis)
            self.assertTrue(target.exists())
            self.assertEqual(provenance['source'],str(human))

    def test_capped_unlisted_net_is_coverage_not_route_loss(self):
        states=scorecard.net_complete(dict(nets=['listed','unlisted']),['listed'],censored=True)
        self.assertFalse(states['listed'])
        self.assertIsNone(states['unlisted'])
        reference={'board':{'power':{'unlisted':dict(connected=True,median_width_mm=.2)}}}
        arms={'unknown':{'board':{'power':{'unlisted':dict(connected=None,median_width_mm=.2)}}},
              'broken':{'board':{'power':{'unlisted':dict(connected=False,median_width_mm=None)}}}}
        rows=compare.summarize(reference,arms)['power']['median_width_mm']['arms']
        self.assertEqual(rows['unknown']['all_obligations']['not_assessable'],1)
        self.assertEqual(rows['unknown']['all_obligations']['missing'],0)
        self.assertEqual(rows['unknown']['all_obligations']['eligible'],0)
        self.assertEqual(rows['broken']['all_obligations']['loss'],1)


if __name__ == '__main__':
    unittest.main()
