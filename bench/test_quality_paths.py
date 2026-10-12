# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import math
import random
import unittest

import quality_compare
import quality_paths as paths
import quality_si as si


LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']


def nm(v):
    return round(v*1e6)


def pad(net, x, y, ref='U1', num='1', layer='F.Cu'):
    return dict(net=net, x=nm(x), y=nm(y), w=100000, h=100000, angle=0,
                layers=[layer], ref=ref, num=num, attr='smd')


def track(net, a, b, layer='F.Cu', width=.1):
    return dict(net=net, sx=nm(a[0]), sy=nm(a[1]), ex=nm(b[0]), ey=nm(b[1]),
                layer=layer, width=nm(width))


def endpoint(p):
    return dict(net=p['net'], x_nm=p['x'], y_nm=p['y'], layers=p['layers'])


def board():
    return dict(pads=[], tracks=[], arcs=[], vias=[], copper_layers=LAYERS, nets=[])


def leg(d, net, length=10, y=0, via=None, bottom='B.Cu'):
    a, b = pad(net, 0, y), pad(net, length, y, 'J1', layer=bottom if via is not None else 'F.Cu')
    d['pads'] += [a, b]
    d['nets'].append(net)
    if via is None:
        d['tracks'].append(track(net, (0, y), (length, y)))
    else:
        d['tracks'] += [track(net, (0, y), (via, y)), track(net, (via, y), (length, y), bottom)]
        d['vias'].append(dict(net=net, x=nm(via), y=nm(y), top='F.Cu', bottom=bottom, size=200000, drill=100000))
    return [endpoint(a), endpoint(b)]


def pair(d, p=10, n=10, y=0, name='D', role='data', pv=None, nv=None):
    return dict(id=name, role=role, p=name+'_P', n=name+'_N',
                endpoints=leg(d, name+'_P', p, y, pv)+leg(d, name+'_N', n, y+.4, nv))


def manifest(lanes, series=(), confidence='confirmed', mode='slot'):
    return dict(schema=1, interfaces=[dict(id='if0', interface='pcie', mode=mode,
               confidence=confidence, lanes=lanes, series=list(series))])


def profile(measure, limit, **extra):
    return dict(rules=[dict(id='check', interface='pcie', measure=measure, comparator='<',
                           limit=limit, strength='must', source=dict(section='6.7', page=23), treatment='rule', **extra)])


def results(d, m, measure, limit, **extra):
    return paths.measure_rules(d, m, profile(measure, limit, **extra), {})


class PathTests(unittest.TestCase):
    def test_esd_branch_is_stub_not_endpoint_length(self):
        d = board()
        ep = leg(d, 'USB_P')
        d['pads'].append(pad('USB_P', 5, 3, 'D1'))
        d['tracks'].append(track('USB_P', (5, 0), (5, 3)))
        p = paths.endpoint_path(d, 'USB_P', ep)
        self.assertEqual(p['length_mm'], 10)
        self.assertEqual(p['stub_mm'], 3)
        self.assertIsNone(p['reason'])

    def test_exact_arc_and_midarc_branch(self):
        d = board()
        a, b = pad('P', -1, 0), pad('P', 1, 0, 'J1')
        d['pads'] = [a, b, pad('P', 0, 2, 'D1')]
        d['arcs'] = [dict(net='P', sx=-1000000, sy=0, mx=0, my=1000000, ex=1000000, ey=0, layer='F.Cu', width=100000)]
        d['tracks'] = [track('P', (0, 1), (0, 2))]
        p = paths.endpoint_path(d, 'P', [endpoint(a), endpoint(b)])
        self.assertAlmostEqual(p['length_mm'], math.pi, places=12)
        self.assertAlmostEqual(p['stub_mm'], 1, places=12)

    def test_series_ac_cap_joins_halves_with_physical_binding(self):
        d = board()
        a, ca, cb, b = pad('P', 0, 0), pad('P', 4, 0, 'C1'), pad('P_AC', 5, 0, 'C1', '2'), pad('P_AC', 10, 0, 'J1')
        d['pads'] = [a, ca, cb, b]
        d['tracks'] = [track('P', (0, 0), (4, 0)), track('P_AC', (5, 0), (10, 0))]
        series = [dict(ref='original-C1', a='P', b='P_AC', endpoints=[endpoint(ca), endpoint(cb)])]
        p = paths.endpoint_path(d, 'P', [endpoint(a), endpoint(ca), endpoint(cb), endpoint(b)], series)
        self.assertEqual(p['length_mm'], 9)
        self.assertEqual(p['transitions'], [dict(kind='series', span=(), at_mm=4)])

    def test_ambiguous_loop_and_disconnected_are_na(self):
        d = board()
        ep = leg(d, 'P')
        d['tracks'] += [track('P', (0, 0), (5, 2)), track('P', (5, 2), (10, 0))]
        self.assertIsNone(paths.endpoint_path(d, 'P', ep)['length_mm'])
        d['tracks'] = []
        self.assertIn('disconnected', paths.endpoint_path(d, 'P', ep)['reason'])

    def test_attached_loop_is_not_a_peeled_stub(self):
        d = board()
        ep = leg(d, 'P')
        d['tracks'] += [track('P', (5, 0), (4, 2)), track('P', (4, 2), (6, 2)), track('P', (6, 2), (5, 0))]
        self.assertIsNone(paths.endpoint_path(d, 'P', ep)['length_mm'])

    def test_total_cancellation_is_local_one_mm_skew(self):
        d = board()
        lane = pair(d, pv=6, nv=5)
        pp = paths.endpoint_path(d, lane['p'], lane['endpoints'])
        nn = paths.endpoint_path(d, lane['n'], lane['endpoints'])
        self.assertEqual(pp['length_mm'], nn['length_mm'])
        self.assertEqual(paths.pair_metrics(pp, nn)['segment_skew_mm'], 1)
        self.assertEqual(pp['vias'], 1)
        self.assertEqual(pp['transitions'][0]['span'], tuple(LAYERS))

    def test_unmatched_via_layer_sequences(self):
        d = board()
        lane = pair(d, pv=5, nv=5)
        d['vias'][1]['bottom'] = 'In1.Cu'
        d['tracks'][-1]['layer'] = 'In1.Cu'
        d['pads'][-1]['layers'] = ['In1.Cu']
        lane['endpoints'][-1]['layers'] = ['In1.Cu']
        pp = paths.endpoint_path(d, lane['p'], lane['endpoints'])
        nn = paths.endpoint_path(d, lane['n'], lane['endpoints'])
        metrics = paths.pair_metrics(pp, nn)
        self.assertIsNone(metrics['segment_skew_mm'])
        self.assertEqual(metrics['via_layer_mismatch'], 1)

    def test_unmatched_via_count(self):
        d = board()
        lane = pair(d, pv=5)
        pp = paths.endpoint_path(d, lane['p'], lane['endpoints'])
        nn = paths.endpoint_path(d, lane['n'], lane['endpoints'])
        self.assertEqual(paths.pair_metrics(pp, nn)['via_layer_mismatch'], 1)

    def test_all_mm_rules_at_true_one_micrometre_band(self):
        for measure in ('intra_skew_mm', 'segment_skew_mm', 'max_length_mm', 'group_skew_mm'):
            for delta, outcome in ((-.002, 'pass'), (-.001, 'boundary'), (0, 'boundary'), (.001, 'boundary'), (.002, 'fail')):
                with self.subTest(measure=measure, delta=delta):
                    d = board()
                    limit, extra = 1., {}
                    if measure == 'max_length_mm':
                        lane = pair(d, 10+delta, 10+delta)
                        limit = 10.
                    elif measure == 'group_skew_mm':
                        lane = pair(d, 11+delta, 11+delta)
                        clock = pair(d, 10, 10, y=2, name='CLK', role='clock')
                        extra = dict(group='clock')
                    elif measure == 'segment_skew_mm':
                        lane = pair(d, pv=6+delta, nv=5)
                    else:
                        lane = pair(d, 11+delta, 10)
                    lanes = [lane, clock] if measure == 'group_skew_mm' else [lane]
                    self.assertEqual(results(d, manifest(lanes), measure, limit, **extra)[0]['outcome'], outcome)

    def test_group_spread_and_missing_clock(self):
        d = board()
        a, b = pair(d), pair(d, 12, 12, y=2, name='D1')
        self.assertEqual(results(d, manifest([a, b]), 'group_skew_mm', 1, group='spread')[0]['value'], 2)
        self.assertEqual(results(d, manifest([a, b]), 'group_skew_mm', 1, group='clock')[0]['outcome'], 'na')

    def test_single_ended_group_clock(self):
        d = board()
        a = dict(id='D', role='single', p='SD_D0', n=None, endpoints=leg(d, 'SD_D0', 10))
        b = dict(id='CLK', role='clock', p='SD_CLK', n=None, endpoints=leg(d, 'SD_CLK', 12, 2))
        self.assertEqual(results(d, manifest([a, b]), 'group_skew_mm', 1, group='clock')[0]['value'], 2)

    def test_single_ended_group_spread_producer_roles(self):
        d = board()
        lanes = [dict(id=f'D{i}', role='single', p=f'RGB_D{i}', n=None,
                      endpoints=leg(d, f'RGB_D{i}', length, i*2))
                 for i, length in enumerate((10, 12, 11))]
        lanes.append(dict(id='CLK', role='clock', p='RGB_CLK', n=None,
                          endpoints=leg(d, 'RGB_CLK', 20, 6)))
        self.assertEqual(results(d, manifest(lanes), 'group_skew_mm', 1, group='spread')[0]['value'], 2)

    def test_inferred_pair_orientation_ignores_endpoint_pad_attributes(self):
        d = board()
        lane = pair(d, name='USB')
        # Inline same-layer vias expose ordered path sections without changing lengths.
        d['vias'] = [dict(net=net, x=nm(2), y=nm(y), top='F.Cu', bottom='F.Cu', size=200000, drill=100000)
                     for net, y in ((lane['p'], 0), (lane['n'], .4))]
        next(p for p in d['pads'] if p['net'] == lane['n'] and p['x'] == nm(10))['h'] = 90000
        frozen = paths.infer_endpoints(d)
        self.assertEqual(paths.parity_metrics(d, lane['p'], lane['n'], frozen)['segment_skew_mm'], 0)
        # A manifest's independently ordered N endpoints must be aligned as well.
        lane['endpoints'] = lane['endpoints'][:2]+list(reversed(lane['endpoints'][2:]))
        self.assertEqual(results(d, manifest([lane]), 'segment_skew_mm', 1)[0]['value'], 0)
        d['pads'].reverse()
        self.assertEqual(frozen, paths.infer_endpoints(d))

    def test_pair_orientation_uses_same_footprint_before_distance(self):
        d = board()
        lane = pair(d, name='USB')
        # Physical mates share refs, even when geometry alone would reverse them.
        for p in d['pads']:
            if p['net'] == lane['n']:
                p['ref'] = 'J1' if p['x'] == 0 else 'U1'
        frozen = paths.infer_endpoints(d)
        self.assertEqual(frozen[lane['n']][0]['x_nm'], nm(10))

    def test_pair_orientation_ambiguous_is_not_assessable(self):
        d = board()
        p = [pad('USB_P', -1, 0, 'P1'), pad('USB_P', 1, 0, 'P2')]
        n = [pad('USB_N', 0, -1, 'N1'), pad('USB_N', 0, 1, 'N2')]
        d['pads'], d['nets'] = p+n, ['USB_P', 'USB_N']
        lane = dict(id='D', role='data', p='USB_P', n='USB_N', endpoints=[endpoint(x) for x in p+n])
        self.assertEqual(paths.infer_endpoints(d), {})
        self.assertEqual(results(d, manifest([lane]), 'segment_skew_mm', 1)[0]['outcome'], 'na')

    def test_integer_via_limit_role_and_mode(self):
        d = board()
        lane = pair(d, pv=5, nv=5, role='tx')
        r = profile('vias_per_leg', 1, role='tx', mode='slot')
        r['rules'][0]['comparator'] = '<='
        values = paths.measure_rules(d, manifest([lane]), r, {})
        self.assertEqual([v['outcome'] for v in values], ['pass', 'pass'])
        self.assertEqual([v['band'] for v in values], [0, 0])
        r['rules'][0]['limit'] = 0
        self.assertEqual([v['outcome'] for v in paths.measure_rules(d, manifest([lane]), r, {})], ['fail', 'fail'])
        lane['role'] = 'rx'
        self.assertEqual(paths.measure_rules(d, manifest([lane]), r, {}), [])
        lane['role'] = 'tx'
        self.assertEqual(paths.measure_rules(d, manifest([lane], mode='device_down'), r, {}), [])

    def test_between_leg_intruders_and_controls(self):
        d = board()
        lane = pair(d)
        m = manifest([lane])
        self.assertEqual(results(d, m, 'between_legs_items', 1)[0]['value'], 0)
        d['vias'] += [dict(net='GND', x=nm(5), y=nm(.2), top='F.Cu', bottom='B.Cu', size=100000, drill=50000)]
        d['tracks'] += [track('OTHER', (6, .1), (6, .3))]
        d['pads'] += [pad('OTHER', 7, .2, 'R1')]
        self.assertEqual(results(d, m, 'between_legs_items', 1)[0]['value'], 3)
        # Other-layer copper, own-net branch and outside copper must not trigger.
        d['tracks'] += [track('LAYER', (8, .1), (8, .3), 'In1.Cu'), track(lane['p'], (3, 0), (3, -.4))]
        d['pads'] += [pad('OUTSIDE', 9, 2, 'U2')]
        self.assertEqual(results(d, m, 'between_legs_items', 1)[0]['value'], 3)

    def test_no_coupling_is_na_not_pass(self):
        d = board()
        lane = pair(d)
        d['tracks'][1]['ey'] = nm(1)
        d['pads'][3]['y'] = nm(1)
        lane['endpoints'][3]['y_nm'] = nm(1)
        self.assertEqual(results(d, manifest([lane]), 'between_legs_items', 1)[0]['outcome'], 'na')

    def test_unknown_identity_and_role_are_na(self):
        d = board()
        lane = pair(d)
        self.assertEqual(results(d, manifest([lane], confidence='candidate'), 'intra_skew_mm', 1)[0]['outcome'], 'na')
        lane['role'] = 'unknown'
        self.assertEqual(results(d, manifest([lane]), 'max_length_mm', 20)[0]['outcome'], 'na')
        self.assertEqual(paths.measure_rules(d, None, profile('max_length_mm', 1), {}), [])

    def test_shuffled_item_order_does_not_change_rules(self):
        d = board()
        a = pair(d, pv=6, nv=5)
        b = pair(d, 11, 11, y=2, name='CLK', role='clock')
        d['tracks'] += [track('D_P', (3, 0), (3, -.5))]
        d['vias'] += [dict(net='GND', x=nm(2), y=nm(.2), top='F.Cu', bottom='B.Cu', size=100000, drill=50000)]
        m = manifest([a, b])
        p = dict(rules=[profile(measure, 1, **({'group':'clock'} if measure == 'group_skew_mm' else {}))['rules'][0]
                        for measure in ('intra_skew_mm', 'segment_skew_mm', 'max_length_mm', 'vias_per_leg', 'group_skew_mm', 'between_legs_items')])
        expected = paths.measure_rules(d, m, p, {})
        shuffled = copy.deepcopy(d)
        rng = random.Random(107)
        for key in ('pads', 'tracks', 'arcs', 'vias', 'nets'):
            rng.shuffle(shuffled[key])
        self.assertEqual(paths.measure_rules(shuffled, m, p, {}), expected)

    def test_duplicate_vias_and_reversed_duplicate_tracks(self):
        d = board()
        lane = pair(d, pv=5, nv=5)
        d['vias'] += copy.deepcopy(d['vias'])
        t = d['tracks'][0]
        d['tracks'].append(dict(t, sx=t['ex'], sy=t['ey'], ex=t['sx'], ey=t['sy']))
        p = paths.endpoint_path(d, lane['p'], lane['endpoints'])
        self.assertEqual(p['length_mm'], 10)
        self.assertEqual(p['vias'], 1)

    def test_gap_mode_tie_is_smallest_bin_in_both_orders(self):
        sa = [dict(sx=0, sy=0, ex=1, ey=0, width=.1, len=1, layer='F.Cu'),
              dict(sx=2, sy=0, ex=3, ey=0, width=.1, len=1, layer='F.Cu')]
        sb = [dict(sx=0, sy=.3, ex=1, ey=.3, width=.1, len=1, layer='F.Cu'),
              dict(sx=2, sy=.2, ex=3, ey=.2, width=.1, len=1, layer='F.Cu')]
        self.assertAlmostEqual(si.target_gap(sa, sb), .1)
        self.assertEqual(si.target_gap(sa, sb), si.target_gap(sa[::-1], sb[::-1]))

    def test_legacy_parity_metric_contract(self):
        d = board()
        lane = pair(d, name='USB')
        endpoints = paths.infer_endpoints(d)
        self.assertEqual(set(endpoints), {lane['p'], lane['n']})
        self.assertEqual(quality_compare.METRICS['differential_pairs']['segment_skew_mm'], (False, .01))
        self.assertEqual(quality_compare.METRICS['differential_pairs']['via_layer_mismatch'], (False, 0))

    def test_between_leg_via_contact_at_one_micrometre_offsets(self):
        for y, expected in ((.399, 1), (.401, 0)):
            d = board()
            lane = pair(d)
            d['vias'].append(dict(net='GND', x=nm(5), y=nm(y), top='F.Cu', bottom='B.Cu', size=100000, drill=50000))
            rule = profile('between_legs_items', 0)
            rule['rules'][0]['comparator'] = '<='
            row = paths.measure_rules(d, manifest([lane]), rule, {})[0]
            self.assertEqual(row['value'], expected)
            self.assertEqual(row['outcome'], 'fail' if expected else 'pass')
            self.assertEqual(row['band'], 0)

    def test_foreign_arc_width_not_only_centreline(self):
        d = board()
        lane = pair(d)
        d['arcs'] = [dict(net='OTHER', sx=nm(4.99), sy=nm(.37), mx=nm(5), my=nm(.36),
                          ex=nm(5.01), ey=nm(.37), layer='F.Cu', width=nm(.04))]
        self.assertEqual(results(d, manifest([lane]), 'between_legs_items', 1)[0]['value'], 1)
        d['arcs'][0]['layer'] = 'In1.Cu'
        self.assertEqual(results(d, manifest([lane]), 'between_legs_items', 1)[0]['value'], 0)

    def test_pad_shape_coverage_and_exact_rectangle_control(self):
        d = board()
        lane = pair(d)
        p = pad('OTHER', 5, .39, 'R1')
        p.update(w=nm(.5), h=nm(.05))
        d['pads'].append(p)
        rule = profile('between_legs_items', 0)
        rule['rules'][0]['comparator'] = '<='
        self.assertEqual(paths.measure_rules(d, manifest([lane]), rule, {})[0]['outcome'], 'na')
        row = paths.measure_rules(d, manifest([lane]), rule, dict(pad_metadata={'R1.1':dict(shape='rect')}))[0]
        self.assertEqual(row['outcome'], 'pass')

    def test_same_layer_foreign_fill_counts_but_reference_layer_does_not(self):
        from shapely.geometry import box
        d = board()
        lane = pair(d)
        rule = profile('between_legs_items', 1)
        ctx = dict(fills={('GND','F.Cu'):box(4,.1,6,.3), ('GND','In1.Cu'):box(0,0,10,1)})
        row = paths.measure_rules(d, manifest([lane]), rule, ctx)[0]
        self.assertEqual(row['value'], 1)

    def test_stub_via_does_not_consume_endpoint_path_budget(self):
        d = board()
        ep = leg(d, 'USB_P')
        d['tracks'].append(track('USB_P', (5,0), (5,2)))
        d['vias'].append(dict(net='USB_P', x=nm(5), y=nm(2), top='F.Cu', bottom='B.Cu', size=200000, drill=100000))
        p = paths.endpoint_path(d, 'USB_P', ep)
        self.assertEqual(p['vias'], 0)
        self.assertEqual(p['stub_mm'], 2)

    def test_arc_and_branch_order_is_deterministic(self):
        d = board()
        a, b = pad('P', -1, 0), pad('P', 1, 0, 'J1')
        d['pads'] = [a, b, pad('P', 0, 2, 'D1')]
        d['arcs'] = [dict(net='P', sx=-1000000, sy=0, mx=0, my=1000000, ex=1000000, ey=0, layer='F.Cu', width=100000)]
        d['tracks'] = [track('P', (0, 1), (0, 2)), track('OTHER', (0, 4), (1, 4))]
        ep = [endpoint(a), endpoint(b)]
        p = paths.endpoint_path(d, 'P', ep)
        shuffled = copy.deepcopy(d)
        for key in ('tracks', 'pads', 'arcs'):
            shuffled[key].reverse()
        q = paths.endpoint_path(shuffled, 'P', ep)
        self.assertEqual({k:v for k,v in p.items() if k != 'portions'},
                         {k:v for k,v in q.items() if k != 'portions'})

    def test_parity_uses_frozen_endpoints_not_router_terminal_roster(self):
        d = board()
        lane = pair(d, name='USB')
        frozen = paths.infer_endpoints(d)
        d['pads'].append(pad('USB_P', 5, 3, 'D1'))
        d['tracks'].append(track('USB_P', (5,0), (5,3)))
        measured = paths.parity_metrics(d, lane['p'], lane['n'], frozen)
        self.assertEqual(measured['segment_skew_mm'], 0)
        self.assertEqual(measured['p_stub_mm'], 3)
        self.assertIsNone(paths.parity_metrics(d, lane['p'], lane['n'], None)['segment_skew_mm'])

    def test_inline_same_layer_via_is_a_path_discontinuity(self):
        d = board()
        ep = leg(d, 'USB_P')
        d['vias'].append(dict(net='USB_P', x=nm(5), y=0, top='F.Cu', bottom='B.Cu', size=200000, drill=100000))
        p = paths.endpoint_path(d, 'USB_P', ep)
        self.assertEqual(p['vias'], 1)
        self.assertEqual(p['transitions'], [dict(kind='via', span=tuple(LAYERS), layers=('F.Cu','F.Cu'), at_mm=5)])


if __name__ == '__main__':
    unittest.main()
