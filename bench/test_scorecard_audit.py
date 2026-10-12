# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import math
from pathlib import Path
import unittest

from shapely.geometry import box
import quality
import quality_compare as compare
import quality_paths as paths
import quality_returns as returns
import quality_rules as rules
import quality_si as si
import quality_spacing as spacing
from test_quality_paths import board, leg, manifest, pad, endpoint, track
from test_quality_returns import board as return_board, interface, rule


def arc(net, radius=5, angles=(0, 135, 270), offset=0):
    points = [(round(offset+radius*1e6*math.cos(math.radians(a))),
               round(offset+radius*1e6*math.sin(math.radians(a)))) for a in angles]
    return dict(net=net, layer='F.Cu', width=200000,
                **{k+axis:value for k,p in zip(('s','m','e'),points) for axis,value in zip(('x','y'),p)})


class AuditTests(unittest.TestCase):
    def test_arc_reference_and_isolation_are_measured(self):
        d = return_board()
        d['tracks'] = []
        d['arcs'] = [arc('S', 1, (0, 90, 180))]
        fills = {('DGND','In1.Cu'):box(-2,-2,2,2)}
        metrics = returns.domain_metrics(d,'S',fills,{'In1.Cu'},['DGND'],
                                         grounded={('DGND','In1.Cu',0):True})
        self.assertEqual(metrics['reference_uncovered_mm'],0)
        d['pads'] = []
        d['tracks'] = [track('DGND',(5,-1),(5,1))]
        gap, reason = returns.isolation_gap(d,{},['S'])
        self.assertIsNone(reason)
        self.assertAlmostEqual(gap,3.85,places=3)

    def test_mixed_arc_line_mode_order_and_smallest_tie(self):
        d = dict(tracks=[track('P',(20,0),(30,0),width=.2),track('N',(20,.3),(30,.3),width=.2)],
                 arcs=[arc('P',5),arc('N',5.4)])
        a,b = si.segs(d,'P'),si.segs(d,'N')
        self.assertEqual(si.target_gap(a,b),si.target_gap(a[::-1],b[::-1]))
        line = dict(layer='F.Cu',sx=0,sy=0,ex=10,ey=0,width=.2,len=10)
        neighbours = [dict(line,sy=.3,ey=.3),dict(line,sy=.4,ey=.4)]
        self.assertAlmostEqual(si.target_gap([line],neighbours),.1)
        self.assertEqual(si.target_gap([line],neighbours),si.target_gap([line],neighbours[::-1]))

    def test_shared_arc_geometry_major_reversed_large_coordinates(self):
        for offset in (0,10**15):
            for angles in ((0,135,270),(270,135,0)):
                a = arc('P',5,angles,offset)
                canonical = dict(a)
                first,last = sorted(((a['sx'],a['sy']),(a['ex'],a['ey'])))
                canonical.update(sx=first[0],sy=first[1],ex=last[0],ey=last[1])
                circle = si._arc_parameters(canonical)
                curve = paths._Curve(a,True)
                geometry,length,_ = spacing._arc_line(a)
                self.assertLess(math.dist(curve.c,(circle['cx']*1e6,circle['cy']*1e6)),1)
                self.assertAlmostEqual(curve.r,circle['radius']*1e6,delta=1)
                self.assertAlmostEqual(curve.sweep,circle['sweep'],delta=1e-6)
                self.assertAlmostEqual(curve.length,length,delta=1)
                self.assertGreater(abs(curve.sweep),math.pi)
                self.assertAlmostEqual(length/1e6,5*math.pi*1.5,places=5)
                for x,y in geometry.coords:
                    self.assertAlmostEqual(math.dist((x,y),curve.c)/1e6,curve.r/1e6,places=5)
                self.assertAlmostEqual(si.exact_copper_length(dict(tracks=[],arcs=[a]),'P'),length/1e6,places=5)

    def test_classified_spacing_needs_frozen_class_and_retains_diagnostic(self):
        d = board()
        endpoints = leg(d,'S',10)
        endpoints += leg(d,'M',10,.4)
        leg(d,'FAST',10,1)
        leg(d,'SLOW',10,5)
        for p in d['pads']:
            p['shape']='rect'
        m = manifest([dict(id='s',role='data',p='S',n='M',endpoints=endpoints)])
        m['status']='reviewed'
        f=m['interfaces'][0]
        f['interface']='ethernet_mdi'
        r=rule('other_gap_mm',2,'>',kind='ethernet_mdi')
        r.update(id='ethernet_mdi.other_gap.low_speed',aggressor_class='low_speed')
        ctx=dict(complete={n:True for n in d['nets']},pad_metadata={})
        before=spacing.measure_rules(d,m,{'rules':[r]},ctx)[0]
        self.assertEqual(before['outcome'],'na')
        self.assertIn('class',before['reason'])
        self.assertLess(before['unclassified_nearest_gap_mm'],1)
        f['aggressor_classes']={'low_speed':['SLOW']}
        after=spacing.measure_rules(d,m,{'rules':[r]},ctx)[0]
        self.assertEqual(after['outcome'],'pass')
        self.assertGreater(after['value'],4)
        self.assertEqual(before['unclassified_nearest_gap_mm'],after['unclassified_nearest_gap_mm'])

    def test_conditional_lengths_use_metadata_not_just_mode(self):
        d=board()
        lane=dict(id='s',role='single',p='S',n=None,endpoints=leg(d,'S',10))
        m=manifest([lane])
        f=m['interfaces'][0]
        for id,kind,mode,limit,metadata in [
            ('rgb_lcd.length.display','rgb_lcd','display',100,dict(flex_length_mm=90,cable_length_mm=5)),
            ('i2c.length.standard','i2c','standard',450,dict(device_load_pf=500,capacitance_per_mm_pf=1,max_capacitance_pf=400,cable_length_mm=0,length_convention='total_copper')),
            ('i2c.length.fast','i2c','fast',200,dict(device_load_pf=0,capacitance_per_mm_pf=1,max_capacitance_pf=400,cable_length_mm=201,length_convention='total_copper'))]:
            f.update(interface=kind,mode=mode)
            r=rule('max_length_mm',limit,'<',kind)
            r.update(id=id,mode=mode)
            lane.pop('length_metadata',None)
            before=paths.measure_rules(d,m,{'rules':[r]},dict(pad_metadata={}))[0]
            self.assertEqual(before['outcome'],'na')
            self.assertEqual(before['pcb_length_mm'],10)
            lane['length_metadata']=metadata
            after=paths.measure_rules(d,m,{'rules':[r]},dict(pad_metadata={}))[0]
            self.assertEqual(after['outcome'],'fail')

    def test_vga_section_uses_physical_filter_boundary(self):
        d=board()
        a,ca,cb,b=pad('P',0,0),pad('P',4,0,'L1'),pad('P2',5,0,'L1','2'),pad('P2',25,0,'J1')
        d.update(pads=[a,ca,cb,b],nets=['P','P2'],tracks=[track('P',(0,0),(4,0)),track('P2',(5,0),(25,0))])
        lane=dict(id='s',role='single',p='P',n=None,endpoints=[endpoint(a),endpoint(b)])
        series=[dict(ref='L1',a='P',b='P2',endpoints=[endpoint(ca),endpoint(cb)])]
        m=manifest([lane],series,mode='section_c')
        m['interfaces'][0]['interface']='vga'
        r=rule('max_length_mm',15,'<','vga');r.update(id='vga.length.section_c',mode='section_c')
        self.assertEqual(paths.measure_rules(d,m,{'rules':[r]},dict(pad_metadata={}))[0]['outcome'],'na')
        lane['length_metadata']=dict(filter_ref='L1',filter_boundary=endpoint(cb),section_endpoints=[endpoint(cb),endpoint(b)])
        result=paths.measure_rules(d,m,{'rules':[r]},dict(pad_metadata={}))[0]
        self.assertEqual(result['value'],20)
        self.assertEqual(result['section_pcb_length_mm'],20)
        self.assertEqual(result['pcb_length_mm'],24)
        self.assertEqual(result['outcome'],'fail')

    def test_reference_gap_checks_downstream_series_and_missing(self):
        d=return_board();d['nets'].append('S2');d['tracks'].append(track('S2',(11,0),(15,0)))
        m=interface();m['interfaces'][0]['series']=[dict(ref='C1',a='S',b='S2')]
        ctx=dict(fills={('DGND','In1.Cu'):box(-1,-2,11,2)},broad={'In1.Cu'},complete={'S':True,'S2':True})
        profile={'rules':[rule('reference_uncovered_mm',0)]}
        self.assertEqual(returns.measure_rules(d,m,profile,ctx)[0]['value'],4)
        ctx['complete'].pop('S2')
        r=returns.measure_rules(d,m,profile,ctx)[0]
        self.assertEqual(r['outcome'],'na');self.assertIn('S2',r['reason'])

    def test_near_pad_gate_both_directions_and_vip_exemption(self):
        human=dict(via_in_pad=0,via_near_pad_025=4,unsupported_pads=0)
        router=dict(human,via_near_pad_025=1)
        result=quality.process_gate(human,router)
        self.assertTrue(result['small_pad_manufacturing']);self.assertFalse(result['absolute_small_pad_screen_pass'])
        self.assertFalse(quality.process_gate(router,human)['small_pad_manufacturing'])
        self.assertTrue(quality.process_gate(dict(human,via_in_pad=1),dict(router,via_in_pad=2))['small_pad_manufacturing'])

    def test_profile_inventory_every_rule_and_inapplicable_not_na(self):
        profile=rules.load_profile('toradex-2015')
        d=board();lane=dict(id='s',role='single',p='S',n=None,endpoints=leg(d,'S'))
        m=manifest([lane],mode=None);m['status']='reviewed';m['interfaces'][0]['interface']='i2c'
        m['interfaces'][0]['id']='i2c:I2C4_ID'
        applications=quality.measure_rules(d,m,profile,dict(pad_metadata={},fills={},broad=set(),complete={'S':True}))
        inventory=rules.coverage_inventory(profile,m,applications)
        self.assertEqual(set(inventory),{r['id'] for r in profile['rules']})
        self.assertEqual(inventory['i2c.length.standard'][0]['status'],'unknown mode')
        self.assertEqual(inventory['general.reference_gap'][0]['status'],'prerequisite missing')
        self.assertEqual(inventory['sata.intra'][0]['status'],'absent interface family')
        paironly=[a for a in applications.values() if a.get('applicability')=='not applicable']
        self.assertTrue(paironly)
        summary=rules.summarize({'b':{rules.key(a):a for a in paironly}}, {})
        self.assertTrue(all(r['human']['na']==0 for r in summary.values()))

    def test_budget_service_and_common_finished_decisions(self):
        reference={'b':{'power':{'S':dict(connected=True,median_width_mm=.2)}}}
        arms={'A':copy.deepcopy(reference),'B':{'b':{'power':{'S':dict(connected=False)}}}}
        stops={'A':{'b':'complete'},'B':{'b':'work'}}
        service=compare.summarize(reference,arms,stops=stops)['power']['median_width_mm']['arms']['B']['all_obligations']
        self.assertEqual(service['loss'],1);self.assertEqual(service['not_delivered_at_budget'],1)
        self.assertEqual(service['worst'][0]['interpretation'],'not delivered at this budget')
        runs=[Path('A'),Path('B')]
        boards=[dict(board='b',runs={str(r):dict(router_stop=stops[r.name]['b'],judge=dict(drc_pass=True,completion=1)) for r in runs})]
        finished=quality.finished_comparison(boards,runs,Path('human'),reference,arms,stops)
        self.assertEqual(finished['boards'],[]);self.assertFalse(finished['comparisons'][0]['pass_gate'])
        boards[0]['runs']['A']['router_stop']='work'
        boards[0]['runs']['B']['judge']['drc_pass']=False
        self.assertFalse(quality.finished_comparison(boards,runs,Path('human'),reference,arms,stops)['comparisons'][0]['pass_gate'])
        boards[0]['runs']['B']['judge']['drc_pass']=True
        stops['A']['b']='exhausted';stops['B']['b']='complete'
        boards[0]['runs']['A']['router_stop']='exhausted'
        boards[0]['runs']['B']['router_stop']='complete'
        finished=quality.finished_comparison(boards,runs,Path('human'),reference,arms,stops)
        self.assertEqual(finished['boards'],['b'])
        self.assertTrue(finished['comparisons'][0]['pass_gate'])
        boards[0]['runs']['B']['router_stop']='time'
        result=quality.finished_comparison(boards,runs,Path('human'),reference,arms,stops)['comparisons'][0]
        self.assertFalse(result['valid']);self.assertIsNone(result['pass_gate'])
        stops['B']['b']='time'
        service=compare.summarize(reference,arms,stops=stops)['power']['median_width_mm']['arms']['B']['all_obligations']
        self.assertEqual(service['not_delivered_at_budget'],0)
        self.assertEqual(service['worst'][0]['interpretation'],'invalid equal-work observation')
        boards[0]['runs']['B']['router_stop']='unknown'
        boards[0]['runs']['B']['judge']['completion']=.5
        self.assertFalse(quality.finished_comparison(boards,runs,Path('human'),reference,arms,stops)['comparisons'][0]['pass_gate'])
        for stop in ('complete','work','time','exhausted','unknown'):
            self.assertEqual(quality.router_stop(dict(stop=stop)),stop)


if __name__=='__main__':
    unittest.main()
