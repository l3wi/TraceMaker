# SPDX-License-Identifier: GPL-3.0-or-later
import math
import random
import unittest
from unittest.mock import patch

from shapely.geometry import Point
import intent_quality
import quality_paths

import quality_spacing as spacing


def track(a, b, net='S', width=100000, layer='F.Cu'):
    return dict(net=net, width=width, layer=layer, sx=a[0], sy=a[1], ex=b[0], ey=b[1])


def board(tracks):
    return dict(tracks=tracks, pads=[], vias=[], arcs=[], copper_layers=['F.Cu', 'B.Cu'], nets=sorted({t['net'] for t in tracks}))


def meander(gap):
    h = gap+100000
    return board([track((0, 0), (3000000, 0)), track((3000000, 0), (3000000, h)), track((3000000, h), (0, h))])


def manifest():
    return dict(schema=1, status='reviewed', interfaces=[dict(id='bus', interface='pcie', confidence='confirmed', lanes=[
        dict(id='a', role='tx', p='AP', n='AN', endpoints=[]), dict(id='b', role='rx', p='BP', n='BN', endpoints=[])])])


def pairs(gap=500000, layer='F.Cu'):
    return board([track((0, y), (3000000, y), net=n, layer=l) for y, n, l in [
        (0, 'AP', 'F.Cu'), (200000, 'AN', 'F.Cu'), (300000+gap, 'BP', layer), (500000+gap, 'BN', layer)]])


def ctx(d):
    return dict(fills={}, complete={n: True for n in d['nets']})


class SpacingTests(unittest.TestCase):
    def test_pad_rotation_matches_kicad_inverse(self):
        p = dict(ref='U1', num='1', net='S', x=0, y=0, w=2000000, h=100000, angle=45)
        meta = {'U1.1': dict(shape='rect')}
        core, radius, known = quality_paths._pad_region(p, meta['U1.1'])
        shape = spacing._pad_shape(p, meta)
        self.assertTrue(known)
        for x, y, inside in ((500000, -500000, True), (500000, 500000, False)):
            self.assertEqual(core.distance(Point(x/1e6, y/1e6)) <= radius, inside)
            self.assertEqual(shape.covers(Point(x, y)), inside)
        rng = random.Random(114)
        for angle in (-135, -45, 0, 30, 45, 90, 165):
            p['angle'] = angle
            core, radius, _ = quality_paths._pad_region(p, meta['U1.1'])
            shape = spacing._pad_shape(p, meta)
            for _ in range(200):
                x, y = rng.randrange(-1100000, 1100000), rng.randrange(-1100000, 1100000)
                expected = intent_quality.in_pad(p, x, y, margin=-intent_quality.TOL)
                self.assertEqual(core.distance(Point(x/1e6, y/1e6)) <= radius, expected)
                self.assertEqual(shape.covers(Point(x, y)), expected)

    def test_pair_spacing_uses_frozen_renamed_pad_metadata(self):
        d = pairs()
        d['pads'] = [dict(ref='U1@0,0', num='1', net='BP', x=1000000, y=400000,
                          w=100000, h=100000, angle=0, layers=['F.Cu'])]
        context = dict(ctx(d), board_path='router.kicad_pcb', pad_metadata={'U1@0,0.1': dict(shape='rect')})
        with patch.object(spacing.craft, 'metadata', side_effect=AssertionError('must use frozen metadata')):
            measured = spacing.pair_spacing(d, manifest(), context)
            self.assertAlmostEqual(measured['bus:a']['pair_gap_mm'], .1)
            self.assertEqual(measured, spacing.pair_spacing(d, manifest(), context, indexed=False))
        with patch.object(spacing.craft, 'metadata', return_value=context['pad_metadata']) as reader:
            fallback = dict(context)
            fallback.pop('pad_metadata')
            self.assertEqual(measured, spacing.pair_spacing(d, manifest(), fallback))
            reader.assert_called_once_with('router.kicad_pcb')

    def test_meander_boundary_and_linear_reference(self):
        for gap, count in [(399000, 1), (400000, 0), (401000, 0)]:
            d = meander(gap)
            value = spacing.net_spacing(d, 'S')
            self.assertAlmostEqual(value['self_spacing_ratio'], gap/100000)
            self.assertEqual(value['self_spacing_violations'], count)
            self.assertEqual(value, spacing.net_spacing(d, 'S', indexed=False))

    def test_collinear_split_invariant(self):
        d = meander(399000)
        expected = spacing.net_spacing(d, 'S')
        first = d['tracks'].pop(0)
        d['tracks'] += [track((i*300000, 0), ((i+1)*300000, 0)) for i in range(10)]
        self.assertEqual(expected, spacing.net_spacing(d, 'S'))

    def test_corner_and_ordinary_45_degree_bend(self):
        for tracks in [[track((0, 0), (1000000, 0)), track((1000000, 0), (1000000, 1000000))],
                       [track((0, 0), (1000000, 0)), track((1000000, 0), (1500000, 500000)), track((1500000, 500000), (2500000, 500000))]]:
            value = spacing.net_spacing(board(tracks), 'S')
            self.assertEqual(value['self_spacing_violations'], 0)
            self.assertEqual(value['short_bend_segments'], 0)

    def test_short_bend_boundary(self):
        for length, count in [(149000, 1), (150000, 0), (151000, 0)]:
            d = board([track((0, 0), (1000000, 0)), track((1000000, 0), (1000000, length)), track((1000000, length), (2000000, length))])
            self.assertAlmostEqual(spacing.net_spacing(d, 'S')['bend_segment_ratio'], length/100000)
            self.assertEqual(spacing.net_spacing(d, 'S')['short_bend_segments'], count)

    def test_pair_gap_boundary_and_ratio(self):
        profile = dict(rules=[dict(id='pair', interface='pcie', measure='pair_gap_mm', comparator='>', limit=.5)])
        for gap in [499000, 500000, 501000]:
            d = pairs(gap)
            result = spacing.pair_spacing(d, manifest(), ctx(d))
            self.assertAlmostEqual(result['bus:a']['pair_gap_mm'], gap/1e6)
            self.assertAlmostEqual(result['bus:a']['pair_gap_ratio'], gap/100000)
            self.assertEqual(result, spacing.pair_spacing(d, manifest(), ctx(d), indexed=False))
            for entry in spacing.measure_rules(d, manifest(), profile, ctx(d)):
                self.assertEqual(entry['outcome'], 'boundary')

    def test_other_layer_not_neighbour(self):
        d = pairs(layer='B.Cu')
        self.assertIsNone(spacing.pair_spacing(d, manifest(), ctx(d))['bus:a']['pair_gap_mm'])

    def test_shuffle_is_identical(self):
        d = pairs()
        d['tracks'] += meander(399000)['tracks']
        d['nets'].append('S')
        a, b = spacing.measure(d, ctx(d)['complete']), spacing.pair_spacing(d, manifest(), ctx(d))
        random.Random(42).shuffle(d['tracks'])
        m = manifest()
        m['interfaces'][0]['lanes'].reverse()
        self.assertEqual(a, spacing.measure(d, ctx(d)['complete']))
        self.assertEqual(b, spacing.pair_spacing(d, m, ctx(d)))

    def test_unknowns_and_signal_classification(self):
        d = meander(399000)
        for n in ['GND', '+3V3', '/power/VDD']:
            d['tracks'] += [dict(t, net=n) for t in list(d['tracks']) if t['net'] == 'S']
            d['nets'].append(n)
        self.assertEqual(set(spacing.measure(d, ctx(d)['complete'])), {'S'})
        self.assertEqual(spacing.measure(d, {'S': None}), {})
        d['arcs'] = [dict(net='S')]
        self.assertIsNone(spacing.net_spacing(d, 'S')['self_spacing_violations'])
        self.assertEqual(spacing.measure_rules(d, None, {}, ctx(d)), [])

    def test_wildcard_rule_and_unconfirmed_intent(self):
        d = meander(399000)
        m = dict(status='reviewed', interfaces=[dict(id='s', interface='spi', confidence='confirmed', lanes=[dict(id='s', role='single', p='S', n=None)])])
        p = dict(rules=[dict(id='self', interface='*', measure='self_spacing_ratio', comparator='>=', limit=4, band=.01)])
        self.assertEqual(spacing.measure_rules(d, m, p, ctx(d))[0]['outcome'], 'boundary')
        m['status'] = 'draft'
        self.assertEqual(spacing.measure_rules(d, m, p, ctx(d))[0]['outcome'], 'na')

    def test_other_signal_not_ground(self):
        d = pairs()
        d['tracks'] += [track((0, 300000), (3000000, 300000), net='GND'), track((0, -200000), (3000000, -200000), net='CLK')]
        d['nets'] += ['GND', 'CLK']
        self.assertAlmostEqual(spacing.pair_spacing(d, manifest(), ctx(d))['bus:a']['other_gap_mm'], .1)

    def test_no_neighbour_is_na(self):
        d = board([track((0, 0), (1000000, 0))])
        m = dict(status='reviewed', interfaces=[dict(id='s', interface='spi', confidence='confirmed', lanes=[dict(id='s', role='single', p='S', n=None)])])
        p = dict(rules=[dict(id='self', interface='*', measure='self_spacing_ratio', comparator='>=', limit=4)])
        self.assertEqual(spacing.measure_rules(d, m, p, ctx(d))[0]['outcome'], 'na')
    def test_width_change_is_not_a_bend(self):
        d = board([track((0, 0), (1000000, 0)), track((1000000, 0), (1100000, 0), width=200000),
                   track((1100000, 0), (2100000, 0))])
        self.assertEqual(spacing.net_spacing(d, 'S')['short_bend_segments'], 0)

    def test_declared_connector_exclusion_is_pad_only(self):
        d = pairs()
        for i, (net, y) in enumerate([('AP', 0), ('AN', 200000), ('BP', 800000), ('BN', 1000000)]):
            d['pads'].append(dict(ref='J1', num=str(i), net=net, x=0, y=y, w=100000, h=100000,
                                  angle=0, layers=['F.Cu'], shape='rect'))
        m = manifest()
        m['interfaces'][0]['breakout_radius_nm'] = 2000000
        for lane in m['interfaces'][0]['lanes']:
            lane['endpoints'] = [dict(x_nm=p['x'], y_nm=p['y'], net=p['net'], layers=p['layers'])
                                 for p in d['pads'] if p['net'] in (lane['p'], lane['n'])]
        a = spacing.pair_spacing(d, m, ctx(d))
        self.assertEqual(a['bus:a']['excluded_pad_comparisons'], 4)
        self.assertAlmostEqual(a['bus:a']['pair_gap_mm'], .5)
        self.assertEqual(a, spacing.pair_spacing(d, m, ctx(d), indexed=False))
        random.Random(9).shuffle(d['pads'])
        self.assertEqual(a, spacing.pair_spacing(d, m, ctx(d)))

    def test_default_ratio_band_converts_one_micron(self):
        d = meander(399000)
        m = dict(status='reviewed', interfaces=[dict(id='s', interface='spi', confidence='confirmed',
                 lanes=[dict(id='s', role='single', p='S', n=None)])])
        p = dict(rules=[dict(id='self', interface='*', measure='self_spacing_ratio', comparator='>=', limit=4)])
        entry = spacing.measure_rules(d, m, p, ctx(d))[0]
        self.assertEqual(entry['outcome'], 'boundary')
        self.assertAlmostEqual(entry['band'], .01)

    def test_interior_copper_joint_is_not_self_spacing(self):
        d = board([track((0, 0), (2000000, 0)), track((1000000, 0), (1000000, 2000000))])
        self.assertEqual(spacing.net_spacing(d, 'S')['self_spacing_violations'], 0)

    def test_other_declared_single_lane_counts(self):
        d = pairs()
        d['tracks'].append(track((0, -200000), (3000000, -200000), net='CLK'))
        d['nets'].append('CLK')
        m = manifest()
        m['interfaces'].append(dict(id='camera', interface='camera', confidence='confirmed',
                                   lanes=[dict(id='clk', role='single', p='CLK', n=None)]))
        self.assertAlmostEqual(spacing.pair_spacing(d, m, ctx(d))['bus:a']['pair_gap_mm'], .1)

    def test_arc_copper_spacing_and_reversed_direction(self):
        d = board([])
        d['nets'] = ['AP', 'AN', 'BP', 'BN']
        for net, radius in zip(d['nets'], [1000000, 1200000, 1800000, 2000000]):
            middle = round(radius/math.sqrt(2))
            d['arcs'].append(dict(net=net, layer='F.Cu', width=100000, sx=radius, sy=0,
                                  mx=middle, my=middle, ex=0, ey=radius))
        expected = spacing.pair_spacing(d, manifest(), ctx(d))
        self.assertAlmostEqual(expected['bus:a']['pair_gap_mm'], .5, delta=.0002)
        self.assertEqual(expected, spacing.pair_spacing(d, manifest(), ctx(d), indexed=False))
        for arc in d['arcs']:
            arc['sx'], arc['ex'] = arc['ex'], arc['sx']
            arc['sy'], arc['ey'] = arc['ey'], arc['sy']
        d['arcs'].reverse()
        self.assertEqual(expected, spacing.pair_spacing(d, manifest(), ctx(d)))

    def test_self_spacing_circular_runs(self):
        for gap, count in [(399000, 1), (401000, 0)]:
            radii = [1000000, 1100000+gap]
            d = board([track((0, radii[0]), (0, radii[1]))])
            for radius in radii:
                middle = round(radius/math.sqrt(2))
                d['arcs'].append(dict(net='S', layer='F.Cu', width=100000, sx=radius, sy=0,
                                      mx=middle, my=middle, ex=0, ey=radius))
            value = spacing.net_spacing(d, 'S')
            self.assertEqual(value['self_spacing_violations'], count)
            self.assertAlmostEqual(value['self_spacing_ratio'], gap/100000, delta=.002)
            self.assertEqual(value, spacing.net_spacing(d, 'S', indexed=False))
            d['arcs'].reverse()
            self.assertEqual(value, spacing.net_spacing(d, 'S'))

    def test_short_straight_between_smooth_arcs_is_not_bend(self):
        d = board([track((1000000, 0), (1000000, -100000))])
        d['arcs'] = [
            dict(net='S', layer='F.Cu', width=100000, sx=0, sy=1000000, mx=707107, my=707107, ex=1000000, ey=0),
            dict(net='S', layer='F.Cu', width=100000, sx=1000000, sy=-100000, mx=1292893, my=-807107, ex=2000000, ey=-1100000)]
        self.assertEqual(spacing.net_spacing(d, 'S')['short_bend_segments'], 0)



if __name__ == '__main__':
    unittest.main()
