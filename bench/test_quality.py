# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic measurement cases: holes/named nets, missing references, censoring."""
import hashlib
import json
import math
import pathlib
import tempfile
import unittest
from unittest.mock import patch

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
            with patch.object(io.connectivity, 'count_unconnected',
                              side_effect=io.connectivity.ConnectivityUnavailable('synthetic unavailable')):
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

    @staticmethod
    def saved_report(run, report, version='10.0.3'):
        board=run/'synthetic.kicad_pcb'
        board.write_text('(kicad_pcb)')
        source_hash=io.digest(board)
        key=hashlib.sha256((source_hash+version).encode()).hexdigest()
        cache=run/'quality_cache'/key
        cache.mkdir(parents=True,exist_ok=True)
        analysis=cache/board.name
        analysis.write_bytes(board.read_bytes())
        (cache/'drc.json').write_text(json.dumps(report))
        (cache/'provenance.json').write_text(json.dumps(dict(
            source=str(board),source_sha256=source_hash,analysis_board=str(analysis),
            identity_repair_code_sha256=io.digest(io.ROOT/'bench/prepare_inputs.py'))))
        return board,analysis

    def test_uncapped_connectivity_cached_after_saved_refill(self):
        with tempfile.TemporaryDirectory() as directory:
            run=pathlib.Path(directory)
            report=dict(unconnected_items=[{}]*499,violations=[])
            board,analysis=self.saved_report(run,report)
            counts=dict(unconnected=708,per_net={'listed':5,'hidden':703},
                        kicad_version='10.0.3',method='pcbnew-connectivity')
            with patch.object(io.connectivity,'count_unconnected',return_value={str(analysis):counts}) as counter:
                target,before,provenance=io.prepare(board,run,'synthetic','10.0.3')
                _,cached,_=io.prepare(board,run,'synthetic','10.0.3')
                counter.assert_called_once_with([target],kicad_cli=io.KICAD)
                self.assertEqual(cached['connectivity'],counts)
                self.assertEqual(provenance['connectivity']['unconnected'],708)
            after=dict(unconnected_items=[{}],violations=[],connectivity=dict(
                counts,unconnected=1,per_net={'hidden':1}))
            judged=io.judge(before,after)
            self.assertEqual(judged['common_denominator'],708)
            self.assertAlmostEqual(judged['completion'],1-1/708)
            self.assertEqual(judged['unconnected'],1)
            self.assertFalse(judged['output_unconnected_censored'])
            states=scorecard.net_complete(dict(nets=['listed','hidden','other']),['listed'],
                censored=True,per_net=judged['unconnected_per_net']['after'])
            self.assertEqual(states,dict(listed=True,hidden=False,other=True))
            human={'board':{'power':{'listed':dict(connected=True,median_width_mm=.2)}}}
            arms={'exact':{'board':{'power':{'listed':dict(connected=states['listed'],median_width_mm=.2)}}}}
            result=compare.summarize(human,arms)['power']['median_width_mm']['arms']['exact']['all_obligations']
            self.assertEqual(result['not_assessable'],0)
            self.assertEqual(result['tie'],1)
            capped=dict(unconnected_items=[{}]*499,violations=[],connectivity=dict(
                counts,unconnected=600,per_net={'hidden':600}))
            judged=io.judge(before,capped)
            self.assertAlmostEqual(judged['completion'],1-600/708)
            self.assertFalse(judged['electrical_pass'])
            self.assertFalse(judged['output_unconnected_censored'])
            self.assertEqual(scorecard.net_complete(dict(nets=['hidden','other']),[],
                censored=True,per_net=judged['unconnected_per_net']['after']),dict(hidden=False,other=True))

    def test_connectivity_cache_includes_judge_version(self):
        with tempfile.TemporaryDirectory() as directory:
            run=pathlib.Path(directory)
            report=dict(unconnected_items=[],violations=[])
            first,first_analysis=self.saved_report(run,report,'10.0.3')
            second,second_analysis=self.saved_report(run,report,'10.0.6')
            counts=dict(unconnected=0,per_net={},kicad_version='10.0.3',method='pcbnew-connectivity')
            with patch.object(io.connectivity,'count_unconnected',side_effect=[
                    {str(first_analysis):counts},{str(second_analysis):dict(counts,kicad_version='10.0.6')}]) as counter:
                io.prepare(first,run,'synthetic','10.0.3')
                io.prepare(second,run,'synthetic','10.0.6')
                self.assertEqual(counter.call_count,2)

    def test_connectivity_unavailable_keeps_censored_fallback(self):
        for error in (io.connectivity.ConnectivityUnavailable('not installed'),
                      io.connectivity.ConnectivityVersionMismatch('10.0.6 != 10.0.3')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                run=pathlib.Path(directory)
                board,_=self.saved_report(run,dict(unconnected_items=[{}]*499,violations=[]))
                with patch.object(io.connectivity,'count_unconnected',side_effect=error):
                    _,report,provenance=io.prepare(board,run,'synthetic','10.0.3')
                self.assertNotIn('connectivity',report)
                self.assertEqual(provenance['connectivity_unavailable'],str(error))
                judged=io.judge(report,report)
                self.assertIsNone(judged['completion'])
                self.assertTrue(judged['output_unconnected_censored'])
                self.assertTrue(judged['connectivity_unavailable'])
                self.assertIsNone(scorecard.net_complete(dict(nets=['hidden']),[],
                    judged['output_unconnected_censored'])['hidden'])

    def test_uncapped_connectivity_mismatch_invalidates_judgment(self):
        valid=dict(unconnected_items=[],violations=[],connectivity=dict(
            unconnected=0,per_net={},kicad_version='10.0.3',method='pcbnew-connectivity'))
        wrong=dict(valid,connectivity=dict(valid['connectivity'],unconnected=1,per_net={'GND':1}))
        for before,after in ((valid,wrong),(wrong,valid),(None,wrong)):
            with self.subTest(before=before,after=after), self.assertRaises(ValueError):
                io.judge(before,after)

    def test_connectivity_mismatch_records_arm_evaluation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            reference=pathlib.Path(directory)
            (reference/'boards').mkdir()
            source=reference/'boards/synthetic.kicad_pcb'
            source.write_text('(kicad_pcb)')
            data=dict(pads=[],nets=[])
            report=dict(unconnected_items=[],violations=[],connectivity=dict(
                unconnected=1,per_net={'GND':1},kicad_version='10.0.3',method='pcbnew-connectivity'))
            with patch.object(io,'fixture',return_value=None), \
                 patch.object(io,'inspect',return_value=data), \
                 patch.object(io,'prepare',return_value=(source,report,{})), \
                 patch.object(scorecard.craft,'metadata',return_value={}), \
                 patch.object(scorecard.identity,'canonical_human',return_value=(data,{})), \
                 patch.object(scorecard.returns,'inferred_references',return_value={}), \
                 patch.object(scorecard.qpaths,'infer_endpoints',return_value={}):
                result=scorecard.board_job(('synthetic',[reference/'router'],reference,None,'10.0.3'))
            self.assertIn(str(reference),result['errors'])
            self.assertNotIn(str(reference),result['runs'])

    def test_saturated_error_types_never_pass_new_drc_gate(self):
        for kind,limit in (('clearance',499),('shorting_items',199)):
            report=dict(unconnected_items=[],violations=[
                dict(type=kind,severity='error',description='synthetic')]*limit)
            empty=dict(unconnected_items=[],violations=[])
            for before,after in ((report,report),(report,empty),(empty,report)):
                with self.subTest(kind=kind,before=bool(before['violations']),after=bool(after['violations'])):
                    judged=io.judge(before,after)
                    self.assertIsNone(judged['drc_pass'])
                    self.assertEqual(judged['error_gate'],'unknown')
                    self.assertIn(kind,judged['capped_types'])
                    self.assertNotIn(kind,judged['added_errors'])

    def test_exact_per_net_data_removes_censored_ground_na(self):
        data=dict(nets=['GND'],pads=[],tracks=[],arcs=[],vias=[],copper_layers=[])
        cap=dict(net='GND',connected=True,return_net='GND',return_connected=True,return_lead_mm=1)
        with patch.object(scorecard,'effective_paths',return_value={'cap':cap}), \
             patch.object(si,'all_fills',return_value={}), \
             patch.object(si,'fills',return_value=({},set(),None)), \
             patch.object(scorecard.craft,'measure',return_value={'per_pad_entry':{}}), \
             patch.object(scorecard.craft,'power',return_value={}), \
             patch.object(scorecard.craft,'thermal',return_value={}), \
             patch.object(scorecard.returns,'grounded_islands',return_value={}):
            categories,_,_=scorecard.measure(data,{},pathlib.Path('synthetic'),{},[],
                censored=True,per_net={})
        self.assertTrue(categories['decoupling']['cap']['return_connected'])
        self.assertNotIn('return_reason',categories['decoupling']['cap'])

    @staticmethod
    def arc(net, radius, angles, centre=(0, 0)):
        points = [(round((centre[0] + radius * math.cos(math.radians(a))) * 1e6),
                   round((centre[1] + radius * math.sin(math.radians(a))) * 1e6)) for a in angles]
        return dict(net=net, layer="F.Cu", width=200000, sx=points[0][0], sy=points[0][1],
                    mx=points[1][0], my=points[1][1], ex=points[2][0], ey=points[2][1])

    def test_concentric_arc_coupling_uses_exact_circle_intervals(self):
        for angles in ((0, 45, 90), (90, 45, 0), (315, 360, 405)):
            d = dict(tracks=[], arcs=[self.arc("USB_DP", 5, angles), self.arc("USB_DM", 5.4, angles)])
            a, b = si.segs(d, "USB_DP"), si.segs(d, "USB_DM")
            self.assertAlmostEqual(si.target_gap(a, b), .2)
            good, total = si.coupled(a, b, .2)
            self.assertAlmostEqual(total, (5 + 5.4) * math.pi / 2, places=5)
            self.assertAlmostEqual(good / total, 1, places=6)  # integer-nm three-point rounding
            self.assertEqual(si.coupled(a, b, .1)[0], 0)

    def test_partial_concentric_arcs_union_and_crossing_controls(self):
        d = dict(tracks=[], arcs=[self.arc("USB_DP", 5, (0, 45, 90)),
                                  self.arc("USB_DM", 5.4, (45, 90, 135))])
        a, b = si.segs(d, "USB_DP"), si.segs(d, "USB_DM")
        good, total = si.coupled(a, b, .2)
        self.assertAlmostEqual(good / total, .5, places=6)
        # Duplicate partner geometry is unioned, not counted twice.
        self.assertAlmostEqual(si.coupled(a, b + b, .2)[0], good + (good * 5.4 / 10.4), places=5)
        crossing = dict(tracks=[], arcs=[d["arcs"][0], self.arc("USB_DM", 5.4, (45, 90, 135), centre=(1, 0))])
        self.assertIsNone(si.target_gap(si.segs(crossing, "USB_DP"), si.segs(crossing, "USB_DM")))
        self.assertEqual(si.coupled(si.segs(crossing, "USB_DP"), si.segs(crossing, "USB_DM"), .2)[0], 0)
        tracks = dict(tracks=[
            dict(net="USB_DP", layer="F.Cu", sx=0, sy=0, ex=1000000, ey=0, width=200000),
            dict(net="USB_DM", layer="F.Cu", sx=500000, sy=-500000, ex=500000, ey=500000, width=200000)])
        self.assertEqual(si.coupled(si.segs(tracks, "USB_DP"), si.segs(tracks, "USB_DM"), .2)[0], 0)


if __name__ == '__main__':
    unittest.main()
