# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from shapely.geometry import box
import quality
import quality_returns as returns
import quality_si as si


def pad(ref, num, net, x, y=0, layers=('F.Cu',)):
    return dict(ref=ref, num=num, net=net, x=round(x*1e6), y=round(y*1e6),
                layers=list(layers), w=200000, h=200000, angle=0, attr='smd', shape='rect')


def track(net, start, end, layer='F.Cu'):
    return dict(net=net, sx=round(start[0]*1e6), sy=round(start[1]*1e6),
                ex=round(end[0]*1e6), ey=round(end[1]*1e6), layer=layer, width=200000)


def board():
    return dict(nets=['S', 'AGND', 'DGND'], copper_layers=['F.Cu', 'In1.Cu', 'B.Cu'],
                tracks=[track('S', (0, 0), (10, 0))], arcs=[], vias=[],
                pads=[pad('U1', '1', 'S', 0), pad('U2', '1', 'S', 10),
                      pad('U1', '2', 'DGND', 0, 1, ('In1.Cu',)), pad('U2', '2', 'DGND', 10, 1, ('In1.Cu',)),
                      pad('U3', '2', 'AGND', 9, 0, ('In1.Cu',))])


def interface(net='S', references=('DGND',), isolated=(), kind='usb2'):
    return {'interfaces': [dict(id='i', interface=kind, confidence='confirmed', mode=None, series=[],
                               reference_nets=list(references), isolated_nets=list(isolated),
                               lanes=[dict(id='clk', role='single', p=net, n=None, endpoints=[])])]}


def rule(measure, limit, comparator='<=', kind='*'):
    return dict(id=measure, interface=kind, measure=measure, limit=limit, comparator=comparator,
                strength='should', source={'section': '6.8', 'page': 26}, treatment='rule')


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.d = board()
        self.fills = {('DGND', 'In1.Cu'): box(-1, -2, 5, 2), ('AGND', 'In1.Cu'): box(5, -2, 11, 2)}

    def domain(self, d=None, fills=None, **kwargs):
        return returns.domain_metrics(d or self.d, 'S', fills or self.fills, {'In1.Cu'}, ['DGND'], **kwargs)

    def test_split_full_combined_coverage(self):
        legacy = si.retmetric(self.d, ['S'], {'In1.Cu': box(-1, -2, 11, 2)}, {'In1.Cu'})
        self.assertEqual(legacy['gap_mm'], 0)
        self.assertEqual(self.domain()['wrong_reference_mm'], 5)
        self.assertEqual(self.domain()['reference_uncovered_mm'], 5)
        self.assertEqual(self.domain()['floating_reference_mm'], 0)

    def test_joined_ground_control(self):
        measured = self.domain(fills={('DGND', 'In1.Cu'): box(-1, -2, 11, 2)})
        self.assertEqual(measured['wrong_reference_mm'], 0)
        self.assertEqual(measured['reference_uncovered_mm'], 0)

    def test_floating_ground_is_not_reference(self):
        fills = {('DGND', 'In1.Cu'): box(-1, -2, 2, 2).union(box(3, -.5, 11, .5))}
        measured = self.domain(fills=fills)
        self.assertEqual(measured['floating_reference_mm'], 7)
        self.assertEqual(measured['reference_uncovered_mm'], 8)

    def test_track_to_pad_anchors_island(self):
        d = copy.deepcopy(self.d)
        d['tracks'].append(track('DGND', (10, 1), (10, 0), 'In1.Cu'))
        fills = {('DGND', 'In1.Cu'): box(3, -.5, 11, .5)}
        self.assertEqual(self.domain(d=d, fills=fills)['floating_reference_mm'], 0)

    def test_unknown_reference_is_na(self):
        result = returns.domain_metrics(self.d, 'S', self.fills, {'In1.Cu'}, None)
        self.assertIsNone(result['wrong_reference_mm'])
        self.assertIn('ambiguous', result['reference_reason'])

    def test_reference_inference_is_frozen_and_ambiguous_ties_rejected(self):
        self.assertEqual(returns.inferred_references(self.d, ['S'])['S'], ['DGND'])
        d = copy.deepcopy(self.d)
        d['pads'].append(pad('U1', '3', 'AGND', 0, -1, ('In1.Cu',)))
        self.assertIsNone(returns.inferred_references(d, ['S'])['S'])

    def test_isolated_domain_is_excluded(self):
        measured = self.domain(isolated=['S'])
        self.assertEqual(measured['reference_uncovered_mm'], 0)
        legacy = si.retmetric(self.d, ['S'], {}, set(), isolated=['S'])
        self.assertIsNone(legacy['gap_mm'])
        self.assertEqual(legacy['unknown_mm'], 0)

    def test_isolated_ground_via_cannot_earn_return_credit(self):
        d = dict(nets=['S','AGND'], copper_layers=['F.Cu','In1.Cu','In2.Cu','B.Cu'], pads=[], arcs=[],
                 tracks=[track('S',(-1,0),(0,0)),track('S',(0,0),(1,0),'B.Cu')],
                 vias=[dict(net='S',x=0,y=0,size=400000,top='F.Cu',bottom='B.Cu'),
                       dict(net='AGND',x=500000,y=0,size=400000,top='F.Cu',bottom='B.Cu')])
        fills={layer:box(-2,-2,2,2) for layer in ('In1.Cu','In2.Cu')}
        self.assertEqual(si.retmetric(d,['S'],fills,set(fills))['no_return_1mm'],0)
        self.assertEqual(si.retmetric(d,['S'],fills,set(fills),isolated=['AGND'])['no_return_1mm'],1)

    def test_isolated_reference_excludes_cable_side_signal_penalty(self):
        with patch.object(si,'all_fills',return_value={('ISO','In1.Cu'):box(-1,-2,11,2)}), \
             patch.object(si,'fills',return_value=({},set(),'hash')):
            cats,_,_=quality.measure(self.d,{},pathlib.Path('unused'),{},[],
                                    interfaces=interface('S',('ISO',),('ISO',),'ethernet_mdi'))
        r=cats['return_paths']['S']
        self.assertEqual(r['unknown_mm'],0)
        self.assertIsNone(r['gap_mm'])
        self.assertEqual(r['wrong_reference_mm'],0)

    def test_floating_island_cannot_shortcut_cap_ground_lead(self):
        d=dict(nets=['VCC','GND'],copper_layers=['F.Cu','In1.Cu','B.Cu'],arcs=[],vias=[],
               pads=[pad('C1','1','VCC',0,2),pad('U1','1','VCC',10,2),
                     pad('C1','2','GND',0),pad('U1','2','GND',10)],
               tracks=[track('VCC',(0,2),(10,2))])
        manifest={'connections':[{'from':'C1.1','to':'U1.1'}]}
        fills={('GND','In1.Cu'):box(-1,-1,11,1)}
        r=quality.effective_paths(d,manifest,fills)['C1.1>U1.1']
        self.assertIsNone(r['return_lead_mm'])
        self.assertEqual(r['effective_mm'],10)

    def test_single_manifest_lane_added_without_changing_legacy_fields(self):
        with patch.object(si, 'all_fills', return_value=self.fills), patch.object(si, 'fills', return_value=({'In1.Cu': box(-1, -2, 11, 2)}, {'In1.Cu'}, 'hash')):
            categories, _, _ = quality.measure(self.d, {}, pathlib.Path('unused'), {}, [], interfaces=interface())
            self.assertEqual(categories['return_paths']['S']['wrong_reference_mm'], 5)
            original, _, _ = quality.measure(self.d, {}, pathlib.Path('unused'), {}, [])
            self.assertNotIn('S', original['return_paths'])
        d = copy.deepcopy(self.d)
        d['nets'] = ['USB_DP', 'USB_DM', 'DGND', 'AGND']
        for p in d['pads']:
            if p['net'] == 'S': p['net'] = 'USB_DP'
        d['tracks'][0]['net'] = 'USB_DP'
        d['tracks'].append(track('USB_DM', (0, .3), (10, .3)))
        d['pads'] += [pad('U1', '4', 'USB_DM', 0, .3), pad('U2', '4', 'USB_DM', 10, .3)]
        union = {'In1.Cu': box(-1, -2, 11, 2)}
        with patch.object(si, 'all_fills', return_value=self.fills), patch.object(si, 'fills', return_value=(union, {'In1.Cu'}, 'hash')):
            cats, _, _ = quality.measure(d, {}, pathlib.Path('unused'), {}, [])
        old = si.retmetric(d, ['USB_DP'], union, {'In1.Cu'})
        self.assertEqual({k: cats['return_paths']['USB_DP'][k] for k in old}, old)

    def test_reference_rule_boundary_and_path_hook(self):
        profile = {'rules': [rule('reference_uncovered_mm', 5)]}
        ctx = dict(fills=self.fills, broad={'In1.Cu'}, complete={'S': True}, board_path='unused')
        for delta, outcome in [(-.002, 'pass'), (-.001, 'boundary'), (0, 'boundary'), (.001, 'boundary'), (.002, 'fail')]:
            d = copy.deepcopy(self.d)
            d['tracks'][0]['ex'] += round(delta*1e6)
            measured = returns.measure_rules(d, interface(), profile, ctx)
            self.assertEqual(measured[0]['outcome'], outcome)
        ctx['lane_segments'] = {'S': [dict(layer='F.Cu', sx=0, sy=0, ex=1, ey=0, width=.2)]}
        self.assertEqual(returns.measure_rules(self.d, interface(), profile, ctx)[0]['value'], 0)
        ctx['lane_segments'] = {}
        self.assertEqual(returns.measure_rules(self.d, interface(), profile, ctx)[0]['outcome'], 'na')

    def test_isolation_all_layer_edge_gap_and_boundary(self):
        d = dict(nets=['ISO', 'GND'], tracks=[], arcs=[], vias=[], pads=[], copper_layers=['F.Cu', 'B.Cu'])
        profile = {'rules': [rule('isolation_gap_mm', 2, '>=', 'ethernet_mdi')]}
        manifest = interface('ISO', ('ISO',), ('ISO',), 'ethernet_mdi')
        for delta, outcome in [(-.002, 'fail'), (-.001, 'boundary'), (0, 'boundary'), (.001, 'boundary'), (.002, 'pass')]:
            fills = {('ISO', 'F.Cu'): box(0, 0, 1, 1), ('GND', 'B.Cu'): box(3+delta, 0, 4, 1)}
            ctx = dict(fills=fills, broad=set(), complete={'ISO': True}, board_path='unused')
            measured = returns.measure_rules(d, manifest, profile, ctx)
            self.assertEqual(measured[0]['outcome'], outcome)
        self.assertEqual(returns.measure_rules(d, None, profile, ctx), [])

    def test_shuffled_items_identical(self):
        d = copy.deepcopy(self.d)
        d['pads'].reverse(); d['tracks'].reverse(); d['vias'].reverse()
        self.assertEqual(self.domain(), self.domain(d=d, fills=dict(reversed(list(self.fills.items())))))
        self.assertEqual(returns.inferred_references(d, ['S']), returns.inferred_references(self.d, ['S']))
        profile={'rules':[rule('reference_uncovered_mm',5)]}
        ctx=dict(fills=self.fills,broad={'In1.Cu'},complete={'S':True},board_path='unused')
        self.assertEqual(returns.measure_rules(d,interface(),profile,ctx),
                         returns.measure_rules(self.d,interface(),profile,ctx))

    def test_ground_via_return_lead_adjacent_vs_five_mm(self):
        manifest = {'connections': [{'from': 'C1.1', 'to': 'U1.1'}]}
        fills = {('GND', 'In1.Cu'): box(-1, -1, 11, 1)}
        values = []
        for distance in (5, .2):
            d = dict(nets=['VCC', 'GND'], copper_layers=['F.Cu', 'In1.Cu', 'B.Cu'], arcs=[],
                     pads=[pad('C1', '1', 'VCC', 0, 2), pad('U1', '1', 'VCC', 10, 2),
                           pad('C1', '2', 'GND', 0), pad('U1', '2', 'GND', 10, layers=('In1.Cu',))],
                     tracks=[track('VCC', (0, 2), (10, 2)), track('GND', (0, 0), (distance, 0))],
                     vias=[dict(net='GND', x=round(distance*1e6), y=0, size=400000, top='F.Cu', bottom='B.Cu')])
            result = quality.effective_paths(d, manifest, fills)['C1.1>U1.1']
            values.append(result['return_lead_mm'])
            self.assertEqual(result['effective_mm'], 10)
            d['pads'].reverse(); d['tracks'].reverse()
            self.assertEqual(quality.effective_paths(d, manifest, fills)['C1.1>U1.1']['return_lead_mm'], values[-1])
        self.assertEqual(values, [5, .2])

    def test_fill_identity_named_numeric_order(self):
        polygons = ['(filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 1 0) (xy 1 1) (xy 0 1)))',
                    '(filled_polygon (layer "In1.Cu") (pts (xy 2 0) (xy 3 0) (xy 3 1) (xy 2 1)))']
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td)/'board.kicad_pcb'
            p.write_text('(kicad_pcb (zone (net "GND") (layer "In1.Cu") '+''.join(polygons)+'))')
            first = returns.islands(si.all_fills(p))
            p.write_text('(kicad_pcb (net 1 "GND") (zone (net 1) (layer "In1.Cu") '+''.join(reversed(polygons))+'))')
            second = returns.islands(si.all_fills(p))
            self.assertEqual({k: v.wkb for k,v in first.items()}, {k: v.wkb for k,v in second.items()})


if __name__ == '__main__':
    unittest.main()
