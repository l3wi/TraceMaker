# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import interfaces as iface
import quality
import quality_rules as qr


def board(names):
    pads = []
    for index, name in enumerate(names):
        for end in (0, 1):
            pads.append(dict(ref='U'+str(end+1),num=str(index+1),net=name,x=end*10000000,
                             y=index*1000000,w=200000,h=200000,angle=0,layers=['F.Cu']))
    return dict(nets=list(names),pads=pads,tracks=[],arcs=[],vias=[],copper_layers=['F.Cu','B.Cu'])


class InterfaceTests(unittest.TestCase):
    def test_pair_naming_variants(self):
        names = ['/a/USB_EXT.P','/a/USB_EXT.N', '/a/USB0_D+','/a/USB0_D-', '/a/USB1_DP','/a/USB1_DN',
                 '/a/USB2_DP','/a/USB2_DM','/a/USBH1.P','/a/USBH1.N',
                 '/a/USB3_p','/a/USB3_n','PCIE_RX0_P','PCIE_RX0_N',
                 'PCIE_TX_DP','PCIE_TX_DN','SATA_TX+','SATA_TX-',
                 'DSI_D0_p','DSI_D0_n','CSI0.CLK_P','CSI0.CLK_N',
                 'ETH0_PAIR_A_P','ETH0_PAIR_A_N','LVDS_D0_P','LVDS_D0_N',
                 'HDMI_CLK+','HDMI_CLK-','USB_SSTX_P','USB_SSTX_N',
                 'SSTX0_P','SSTX0_N']
        manifest = iface.draft(board(names),'synthetic')
        lanes = [l for i in manifest['interfaces'] for l in i['lanes']]
        self.assertEqual(len(lanes),len(names)//2)
        self.assertTrue(all(i['confidence']=='candidate' and i['mode'] is None for i in manifest['interfaces']))
        self.assertEqual({l['role'] for i in manifest['interfaces'] if i['interface']=='pcie' for l in i['lanes']},{'tx','rx'})
        self.assertTrue(any(i['interface']=='usb3' for i in manifest['interfaces']))

    def test_exclusion_traps(self):
        names = ['NCSI_RXD0_P','NCSI_RXD0_N','/ADC, DAC/ID+','/ADC, DAC/ID-',
                 '/ADC, DAC/QD+','/ADC, DAC/QD-','USB_CC1_P','USB_CC1_N',
                 'USB_VBUS_P','USB_VBUS_N','HDMI_HPD_P','HDMI_HPD_N',
                 'HDMI_CEC_P','HDMI_CEC_N','HDMI_DDC_P','HDMI_DDC_N',
                 'SPI_SCLK','SPI_SDA','/FPGA/RAM + LVDS/Z0_P','/FPGA/RAM + LVDS/Z0_N',
                 '/Ethernet/RGMII_RXD0_P','/Ethernet/RGMII_RXD0_N','CSI_CAM_CLK']
        self.assertEqual(iface.draft(board(names),'traps')['interfaces'],[])

    def test_clock_data_groups_and_single_ended(self):
        names=['DSI_CLK_P','DSI_CLK_N','DSI_D0_P','DSI_D0_N','DSI_D1_P','DSI_D1_N',
               'SDIO_CLK','SDIO_CMD','SDIO_D0','I_{2}C1.SCL','I_{2}C1.SDA',
               'LCD_PCLK','LCD_R0','LCD_G0','CAM_PCLK','CAM_D0','VGA_R','VGA_G','VGA_B']
        manifest=iface.draft(board(names),'group')
        groups={i['interface']:i for i in manifest['interfaces']}
        self.assertEqual(set(groups),{'mipi_dsi','sdio','i2c','rgb_lcd','parallel_camera','vga'})
        self.assertEqual(len(groups['mipi_dsi']['lanes']),3)
        self.assertEqual({l['role'] for l in groups['i2c']['lanes']},{'single','clock'})
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'group.interfaces.json').write_text(json.dumps(manifest))
            self.assertEqual(qr.load_interfaces(directory,'group'),manifest)

    def test_series_cap_merges_and_renamed_endpoints_bind(self):
        d=board(['PCIE_TX_P','PCIE_TX_N','Net-(C1-Pad2)','Net-(C2-Pad2)'])
        # Replace artificial anonymous pad population with explicit chain caps.
        d['pads']=d['pads'][:4]+[dict(d['pads'][4],ref='J1',num='1'),dict(d['pads'][6],ref='J1',num='2')]
        for index,(ref,a,b) in enumerate([('C1','PCIE_TX_P','Net-(C1-Pad2)'),('C2','PCIE_TX_N','Net-(C2-Pad2)')]):
            for num,net in enumerate((a,b)):
                d['pads'].append(dict(d['pads'][0],ref=ref,num=str(num+1),net=net,x=5000000+num*1000000,y=index*1000000))
        manifest=iface.draft(d,'caps')
        self.assertEqual(len(manifest['interfaces']),1)
        i=manifest['interfaces'][0]
        self.assertEqual(len(i['lanes']),1)
        self.assertEqual(len(i['series']),2)
        self.assertEqual({e['net'] for e in i['lanes'][0]['endpoints']},set(d['nets']))
        renamed=copy.deepcopy(d)
        for p in renamed['pads']:
            p['ref']='renamed_'+p['ref']
        self.assertEqual(iface.coverage(manifest,renamed)['lanes_not_bound'],[])
        renamed['pads'][0]['x']+=1001
        self.assertEqual(len(iface.coverage(manifest,renamed)['lanes_not_bound']),1)

    def test_series_named_halves_and_shunt_control(self):
        d=board(['PCIE_TX_P','PCIE_TX_N','PCIE_TX_C_P','PCIE_TX_C_N','GND'])
        for ref,a,b,y in [('C1','PCIE_TX_P','PCIE_TX_C_P',0),('C2','PCIE_TX_N','PCIE_TX_C_N',1000000),('C3','PCIE_TX_P','GND',2000000)]:
            d['pads'] += [dict(d['pads'][0],ref=ref,num=str(j+1),net=n,x=4000000+j*1000000,y=y) for j,n in enumerate((a,b))]
        manifest=iface.draft(d,'named')
        self.assertEqual(len(manifest['interfaces'][0]['lanes']),1)
        self.assertEqual({s['ref'] for s in manifest['interfaces'][0]['series']},{'C1','C2'})
        reversed_board=copy.deepcopy(d)
        for values in reversed_board.values():
            values.reverse()
        self.assertEqual(iface.draft(reversed_board,'named'),manifest)

    def test_ethernet_c_lane_and_hierarchical_bare_signals(self):
        names=['/USB/D+','/USB/D-','ETH0_C+','ETH0_C-', 'ETH0_D+','ETH0_D-',
               '/Ethernet/ETH1_P','/Ethernet/ETH1_N','/Eth-KSZ8081/RX_P','/Eth-KSZ8081/RX_N',
               'DSI_CLKP','DSI_CLKN']
        manifest=iface.draft(board(names),'hierarchy')
        self.assertEqual(sum(len(i['lanes']) for i in manifest['interfaces']),6)
        self.assertEqual({l['id'] for i in manifest['interfaces'] if i['id']=='ethernet_mdi:ETH0' for l in i['lanes']},{'C','D'})

    def test_usb_connector_corroboration_and_pin_polarity_conflict(self):
        d=board(['AUX_D+','AUX_D-'])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'usb.kicad_pcb'
            path.write_text('(kicad_pcb (footprint "custom:USB_C_Receptacle_example" '
                            '(pad "A6" smd rect (net "AUX_D+") (pinfunction "D+")) '
                            '(pad "A7" smd rect (net "AUX_D-") (pinfunction "D-"))))')
            pins=iface.native_pin_functions(path)
            manifest=iface.draft(d,'usb',pins)
            self.assertEqual(manifest['interfaces'][0]['confidence'],'confirmed')
            self.assertEqual(iface.draft(d,'usb',{'AUX_D+':['D+'],'AUX_D-':['D-']})['interfaces'],[])
        self.assertEqual(iface.draft(board(['USB_DP','USB_DM']),'usb',
                                    {'USB_DP':['USB_DM'],'USB_DM':['USB_DP']})['interfaces'][0]['confidence'],'candidate')

    def test_shuffled_items_identical(self):
        d=board(['DSI_CLK_P','DSI_CLK_N','DSI_D0_P','DSI_D0_N','USB0_DP','USB0_DN'])
        expected=iface.draft(d,'stable')
        shuffled=copy.deepcopy(d)
        rng=random.Random(107)
        for values in shuffled.values():
            rng.shuffle(values)
        self.assertEqual(iface.draft(shuffled,'stable'),expected)

    def test_confirmation_needs_independent_both_leg_evidence(self):
        d=board(['USB_DP','USB_DM'])
        self.assertEqual(iface.draft(d,'usb',{'USB_DP':['USB_DP']})['interfaces'][0]['confidence'],'candidate')
        self.assertEqual(iface.draft(d,'usb',{'USB_DP':['USB_DP'],'USB_DM':['USB_DM']})['interfaces'][0]['confidence'],'confirmed')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'usb.kicad_pcb'
            path.write_text('(kicad_pcb (footprint "USB" (pad "1" smd rect (net 1 "USB_DP") (pinfunction "USB_DP"))))')
            self.assertEqual(dict(iface.native_pin_functions(path)),{'USB_DP':{'USB_DP'}})

    def test_profile_limits_and_band_edges(self):
        profile=qr.load_profile('toradex-2015')
        self.assertEqual(len(profile['rules']),64)
        kinds={r['interface'] for r in profile['rules']}
        self.assertEqual(len(kinds),15)
        self.assertEqual(qr.rules_for(profile,'pcie','max_length_mm'),[])
        self.assertEqual(qr.rules_for(profile,'pcie','max_length_mm','slot')[0]['limit'],200)
        usb_gap=qr.rules_for(profile,'usb3','pair_gap_mm')[0]
        self.assertEqual((usb_gap['comparator'],usb_gap['treatment']),('<','report'))
        for r in profile['rules']:
            if r['measure'].endswith('_mm'):
                self.assertEqual(qr.result(r,'x',r['limit']-.001)['outcome'],'boundary')
                self.assertEqual(qr.result(r,'x',r['limit']+.001)['outcome'],'boundary')
        count=qr.rules_for(profile,'pcie','vias_per_leg')[0]
        self.assertEqual(qr.result(count,'x',2)['outcome'],'pass')
        self.assertEqual(qr.result(count,'x',3)['outcome'],'fail')

    def test_lazy_fake_module_and_no_profile_legacy_identity(self):
        d=board([])
        manifest=iface.draft(d,'empty')
        profile=qr.load_profile('toradex-2015')
        seen=[]
        module=types.ModuleType('fake_interface_rule')
        def measure_rules(data,interfaces,profile,ctx):
            seen.append((interfaces,ctx))
            return [qr.result(profile['rules'][0],'frozen',.2)]
        module.measure_rules=measure_rules
        mfg=dict(per_pad_entry={})
        with patch.dict(sys.modules,{'fake_interface_rule':module}),patch.object(quality,'RULE_MODULES',['fake_interface_rule']),patch.object(quality.si,'all_fills',return_value={}),patch.object(quality.si,'fills',return_value=({},set(),'hash')),patch.object(quality.craft,'measure',return_value=mfg),patch.object(quality.craft,'thermal',return_value={}),patch.object(quality.craft,'power',return_value={}):
            old=quality.measure(d,{},Path('unused'),{},[])
            new=quality.measure(d,{},Path('unused'),{},[],interfaces=manifest,profile=profile)
            self.assertEqual(new[:2],old[:2])
            self.assertEqual(new[2]['rules']['pcie.intra@frozen']['outcome'],'fail')
            self.assertEqual(seen[0][1],dict(fills={},complete={},board_path=Path('unused')))
            self.assertEqual(quality.measure(d,{},Path('unused'),{},[],interfaces=manifest),old)
            self.assertEqual(len(seen),1)
        # Compare the pre-change implementation's exact serialized measurement and
        # Markdown bytes against the default path, not just selected metric values.
        legacy_source=subprocess.run(['git','show','e9bc174:bench/quality.py'],check=True,capture_output=True,text=True).stdout
        legacy=types.ModuleType('legacy_quality')
        exec(compile(legacy_source,'legacy_quality.py','exec'),legacy.__dict__)
        with patch.object(quality.si,'all_fills',return_value={}),patch.object(quality.si,'fills',return_value=({},set(),'hash')),patch.object(quality.craft,'measure',return_value=mfg),patch.object(quality.craft,'thermal',return_value={}),patch.object(quality.craft,'power',return_value={}):
            self.assertEqual(json.dumps(quality.measure(d,{},Path('unused'),{},[])),json.dumps(legacy.measure(d,{},Path('unused'),{},[])))
        result=dict(reference='human',kicad_version='test',seconds=0,gates={},categories={},noise_floor={},limits=[],errors={},boards=[])
        self.assertEqual(quality.markdown(result).encode(),legacy.markdown(result).encode())
        entry=qr.result(profile['rules'][0],'frozen',.2)
        result.update(rules=qr.summarize({'b':{qr.key(entry):entry}},{'router':{'b':{qr.key(entry):entry}}}),gates={'human':dict.fromkeys(('scheduled','judged','evaluation_valid','electrical','new_drc','small_pad_manufacturing','thermal_retained','eligible','censored','completion_macro_pct','completion_pooled_pct','completion_boards','already_connected'),0),'router':dict.fromkeys(('scheduled','judged','evaluation_valid','electrical','new_drc','small_pad_manufacturing','thermal_retained','eligible','censored','completion_macro_pct','completion_pooled_pct','completion_boards','already_connected'),0)})
        self.assertIn('Layout rule profile',quality.markdown(result))
        self.assertIn('Interface coverage',quality.markdown(result))

    def test_board_job_freezes_manifest_hash_and_measures_human(self):
        from contextlib import ExitStack
        d=board([])
        profile=qr.load_profile('toradex-2015')
        module=types.ModuleType('fake_board_rule')
        calls=[]
        def measure_rules(data,manifest,profile,ctx):
            calls.append(ctx['board_path'])
            return [qr.result(profile['rules'][0],'frozen',.2)]
        module.measure_rules=measure_rules
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root=Path(directory); human=root/'human'; router=root/'router'
            for run in (human,router):
                (run/'boards').mkdir(parents=True)
                (run/'boards'/'b.kicad_pcb').write_text('(kicad_pcb)')
            manifest=iface.draft(d,'b')
            path=root/'b.interfaces.json'
            path.write_text(json.dumps(manifest))
            stack.enter_context(patch.dict(sys.modules,{'fake_board_rule':module}))
            stack.enter_context(patch.object(quality,'RULE_MODULES',['fake_board_rule']))
            stack.enter_context(patch.object(quality.io,'fixture',return_value=None))
            stack.enter_context(patch.object(quality.io,'inspect',return_value=d))
            stack.enter_context(patch.object(quality.io,'prepare',side_effect=lambda source,*a,**kw:(source,dict(unconnected_items=[],violations=[]),dict(rules={}))))
            stack.enter_context(patch.object(quality.craft,'metadata',return_value={}))
            stack.enter_context(patch.object(quality.si,'all_fills',return_value={}))
            stack.enter_context(patch.object(quality.si,'fills',return_value=({},set(),'hash')))
            stack.enter_context(patch.object(quality.craft,'measure',return_value=dict(per_pad_entry={},via_in_pad=0,via_near_pad_025=0,unsupported_pads=0)))
            stack.enter_context(patch.object(quality.craft,'thermal',return_value={}))
            stack.enter_context(patch.object(quality.craft,'power',return_value={}))
            result=quality.board_job(('b',[router],human,None,'test',root,profile))
            self.assertEqual(result['errors'],{})
            self.assertEqual(len(calls),2)
            for run in (human,router):
                arm=result['runs'][str(run)]
                self.assertEqual(arm['rules']['pcie.intra@frozen']['outcome'],'fail')
                self.assertEqual(arm['interfaces_sha256'],quality.io.digest(path))
                self.assertEqual(arm['provenance']['interfaces_sha256'],quality.io.digest(path))
                self.assertEqual(arm['coverage']['interfaces']['lanes_not_bound'],[])
            calls.clear()
            default=quality.board_job(('b',[router],human,None,'test',root,None))
            self.assertEqual(calls,[])
            self.assertNotIn('rules',default['runs'][str(human)])
            legacy_source=subprocess.run(['git','show','e9bc174:bench/quality.py'],check=True,capture_output=True,text=True).stdout
            legacy=types.ModuleType('legacy_quality')
            exec(compile(legacy_source,'legacy_quality.py','exec'),legacy.__dict__)
            self.assertEqual(json.dumps(default),json.dumps(legacy.board_job(('b',[router],human,None,'test'))))


if __name__ == '__main__':
    unittest.main()
