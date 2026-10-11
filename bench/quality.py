#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Gated human-parity PCB scorecard (D114); no weighted quality total.

bench/quality.py RUN [RUN ...] --reference HUMAN_RUN --intents DIR --json OUT --md OUT
Requires Shapely >= 2, KiCad CLI, and tracemaker inspect (TM_BINARY override).
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import importlib
import json
from pathlib import Path
import statistics
import subprocess
import time

import intent_quality as iq
import quality_io as io
import quality_compare as qc
import quality_craft as craft
import quality_si as si
import quality_identity as identity
import quality_rules as qr
import interfaces as interface_io
import quality_returns as returns
import quality_paths as qpaths
import quality_spacing as spacing
from shapely.geometry import Point

# Module names, imported only for an explicitly selected profile; each exposes
# measure_rules(d, interfaces, profile, ctx) -> list of quality_rules.result dicts.
RULE_MODULES = ['quality_paths', 'quality_returns', 'quality_spacing']


def measure_rules(d, interfaces, profile, ctx):
    results = {}
    for module_name in RULE_MODULES:
        module = importlib.import_module(module_name)
        for entry in module.measure_rules(d, interfaces, profile, ctx):
            key = qr.key(entry)
            if key in results:
                raise ValueError('duplicate rule application: '+key)
            results[key] = entry
    return {key: results[key] for key in sorted(results)}

def effective_paths(d, manifest, fills, *, _return=True, _graphs=None, _leads=None):
    """Port align.py's lower bound, but require actual contact to the SAME fill island.

    Plane transport is still free: this is not loop inductance or current simulation.
    Different layer islands are not assumed connected by an arbitrary same-net via.
    """
    refs = {p['ref'] for p in d['pads']}
    pad_nets = {(p['ref'],p['num']):p['net'] for p in d['pads']}
    graphs, results, lead_cache = _graphs if _graphs is not None else {}, {}, _leads if _leads is not None else {}
    ground_graphs, ground_leads = {}, {}
    valid_fills, gd = None, None
    for c in manifest.get('connections',[]):
        key = c['from']+'>'+c['to']
        a, b = iq.split_pad(c['from'],refs), iq.split_pad(c['to'],refs)
        net = pad_nets.get(a)
        if net is None or pad_nets.get(b) != net:
            results[key] = dict(effective_mm=None,connected=False,reason='invalid manifest binding')
            continue
        if net not in graphs:
            graphs[net] = iq.NetGraph(d,net)
        g = graphs[net]
        ia, ib = g.pad_index(*a), g.pad_index(*b)
        t = g.shortest(('p',ia),lambda n:n==('p',ib))
        t = t/1e6 if t is not None else None
        plane = []
        for (fn,layer), union in fills.items():
            if fn != net:
                continue
            islands = list(union.geoms) if hasattr(union,'geoms') else [union]
            for index,island in enumerate(islands):
                def contact(node):
                    if node[0]=='v':
                        v=g.vias[node[1]]
                        ls=d['copper_layers']; lo,hi=sorted((ls.index(v['top']),ls.index(v['bottom'])))
                        return layer in ls[lo:hi+1] and island.intersects(Point(v['x']/1e6,v['y']/1e6).buffer(v['size']/2e6))
                    if node[0]=='p':
                        p=g.pads[node[1]]
                        return layer in p['layers'] and island.covers(Point(p['x']/1e6,p['y']/1e6))
                    return any(l==layer and island.covers(Point(x/1e6,y/1e6)) for l,x,y in (g.pos[k] for k in g.members[node]))
                leads=[]
                for i in (ia,ib):
                    ck=(net,i,layer,index)
                    if ck not in lead_cache:
                        value=0 if contact(('p',i)) else g.shortest(('p',i),contact,block_pads=True)
                        lead_cache[ck]=None if value is None else value/1e6
                    leads.append(lead_cache[ck])
                if all(v is not None for v in leads):
                    plane.append(sum(leads))
        p=min(plane) if plane else None
        values=[v for v in (t,p) if v is not None]
        results[key]=dict(effective_mm=min(values) if values else None,connected=bool(values),track_mm=t,plane_lead_mm=p,
                          net=net,plane_lower_bound=True)
        if _return:
            cap_ground = [p for p in d['pads'] if p['ref'] == a[0] and si._GROUND.fullmatch(p['net'])]
            pin = g.pads[ib]
            ic_ground = [p for p in d['pads'] if p['ref'] == b[0] and si._GROUND.fullmatch(p['net'])]
            choices = [(p, q) for p in cap_ground for q in ic_ground if p['net'] == q['net']]
            ground_nets = {p['net'] for p, _ in choices}
            results[key]['return_lead_mm'] = None
            results[key]['return_connected'] = None
            results[key]['return_reason'] = 'ground pads missing or reference ambiguous'
            if len(ground_nets) == 1:
                # Nearest IC ground pin to the obligated supply pin is frozen placement evidence.
                nearest = min((q['x']-pin['x'])**2+(q['y']-pin['y'])**2 for _, q in choices)
                choices = [(p, q) for p, q in choices if (q['x']-pin['x'])**2+(q['y']-pin['y'])**2 == nearest]
                if len({(q['x'], q['y'], tuple(q['layers'])) for _, q in choices}) == 1 and len(cap_ground) == 1:
                    p, q = min(choices, key=lambda pq: (pq[0]['x'], pq[0]['y'], pq[1]['num']))
                    ground_connection = {'from': p['ref']+'.'+p['num'], 'to': q['ref']+'.'+q['num']}
                    if valid_fills is None:
                        gd = returns.canonical_data(d)
                        # Only grounded islands can offer free plane transport.
                        grounded = returns.grounded_islands(gd, fills)
                        valid_fills = {}
                        from shapely.ops import unary_union
                        for (fn, layer, index), island in returns.islands(fills).items():
                            if grounded[fn, layer, index]:
                                valid_fills.setdefault((fn, layer), []).append(island)
                        valid_fills = {k: unary_union(v) for k, v in valid_fills.items()}
                    ground_result = effective_paths(gd, {'connections': [ground_connection]}, valid_fills, _return=False,
                                                    _graphs=ground_graphs, _leads=ground_leads)
                    ground_path = next(iter(ground_result.values()))
                    lead = ground_path['effective_mm']
                    results[key].update(return_lead_mm=lead, return_net=p['net'],
                                        return_connected=ground_path['connected'],
                                        return_endpoints=[{k: terminal[k] for k in ('x', 'y', 'w', 'h', 'angle')}
                                                          for terminal in (p, q)],
                                        return_reason=None if lead is not None else 'ground path unavailable')
    return results


def net_complete(d, bad, censored=False):
    """A capped list proves listed nets incomplete, never proves unlisted nets complete."""
    return {n: False if n in bad else None if censored else True
            for n in d['nets'] if n and not n.startswith('unconnected')}


def _return_terminal_unconnected(item, unconnected_items):
    """Identify judged return endpoints by physical position, never renamed refs."""
    for entry in unconnected_items:
        for endpoint in entry.get('items', []):
            if item['return_net'] not in io.bracket_nets(endpoint.get('description', '')):
                continue
            pos = endpoint.get('pos', {})
            if 'x' not in pos or 'y' not in pos:
                continue
            x, y = round(pos['x']*1e6), round(pos['y']*1e6)
            if any(iq.in_pad(p, x, y, margin=identity.TOL_NM-iq.TOL) for p in item['return_endpoints']):
                return True
    return False


def measure(d, manifest, board, meta, bad, *, censored=False, interfaces=None, profile=None, intended_references=None,
            pair_endpoints=None, unconnected_items=()):
    categories = {c:{} for c in qc.METRICS}
    ff=si.all_fills(board)
    categories['decoupling']=effective_paths(d,manifest,ff)
    manufacturing=craft.measure(d,meta)
    complete=net_complete(d,bad,censored)
    for item in categories['decoupling'].values():
        state=complete.get(item.get('net'),False)
        if item.get('connected') is False and state is not False:
            # A model-unrepresented path on a KiCad-connected net is unknown, not unroutable.
            item.update(connected=state,coverage_reason='censored connectivity' if state is None else 'path not represented by plane/track model')
        if 'return_connected' in item and item.get('return_net') is not None:
            if censored:
                item.update(return_connected=None, return_reason='censored ground connectivity')
            elif item['return_connected'] is not True:
                # A global ground break does not identify this cap's return loss.
                lost = (complete.get(item['return_net']) is False
                        and _return_terminal_unconnected(item, unconnected_items))
                item.update(return_connected=False if lost else None,
                            return_reason='ground terminal unconnected' if lost else 'ground path not represented')
    reference_error = None
    try:
        ground,broad,fill_hash=si.fills(board)
    except ValueError as error:
        # An invalid outline blocks reference-layer inference, not other categories.
        ground,broad,fill_hash={},set(),None
        reference_error=str(error)
    bindings, isolated = returns.reference_bindings(interfaces)
    if isolated:
        from shapely.ops import unary_union
        ground={layer:unary_union([fill for (net,l),fill in ff.items()
                                  if l==layer and net not in isolated and si._GROUND.fullmatch(net)])
                for layer in ground}
        isolated=isolated | {net for net,refs in bindings.items() if refs and set(refs) <= isolated}
    # The caller freezes inference on the human; direct callers must supply it.
    inferred = intended_references or {}
    grounded = returns.grounded_islands(d, ff)
    candidates=si.pairs(d)
    for p,m in candidates:
        a,b=si.pdata(d,p),si.pdata(d,m)
        key=p+'|'+m
        states=(complete.get(p,False),complete.get(m,False))
        connected=False if False in states else None if None in states else a is not None and b is not None
        if not connected:
            categories['differential_pairs'][key]=dict(connected=connected,coupled_fraction=None,skew_mm=None,via_imbalance=None,
                                                       segment_skew_mm=None,via_layer_mismatch=None)
        else:
            categories['differential_pairs'][key]=dict(connected=True,skew_mm=abs(a['length']-b['length']),
                                                       via_imbalance=abs(a['vias']-b['vias']),p=p,m=m)
            # Endpoint-path fields; endpoints are frozen from the human by the caller.
            categories['differential_pairs'][key].update(qpaths.parity_metrics(d,p,m,pair_endpoints,pad_metadata=meta))
        for n in (p,m):
            if n in categories['return_paths']:
                continue
            r=si.retmetric(d,[n],ground,broad,isolated=isolated)
            categories['return_paths'][n]=dict(r,connected=complete.get(n,False) and si.pdata(d,n) is not None,
                                              gap_fraction=r['gap_mm']/r['eligible_mm'] if r['gap_mm'] is not None and r['eligible_mm'] else None)
            categories['return_paths'][n].update(returns.domain_metrics(
                d,n,ff,broad,bindings.get(n) if n in bindings else inferred.get(n),
                grounded=grounded,isolated=isolated))
    for iface in (interfaces or {}).get('interfaces', []):
        if iface.get('confidence') != 'confirmed':
            continue
        for lane in iface['lanes']:
            if lane['role'] not in ('clock', 'single'):
                continue
            for n in sorted(n for n in (lane.get('p'), lane.get('n')) if n):
                if n in categories['return_paths']:
                    continue
                r=si.retmetric(d,[n],ground,broad,isolated=isolated)
                categories['return_paths'][n]=dict(r,connected=complete.get(n,False),
                    gap_fraction=r['gap_mm']/r['eligible_mm'] if r['gap_mm'] is not None and r['eligible_mm'] else None)
                categories['return_paths'][n].update(returns.domain_metrics(
                    d,n,ff,broad,bindings.get(n),grounded=grounded,isolated=isolated))
    for n,v in craft.power(d).items():
        categories['power'][n]=dict(connected=complete.get(n,False),median_width_mm=v['median_width'],
                                   resistance_35um_ohm=v['resistance_35um_ohm'],neck_fraction=None,
                                   class_neck_mm=v.get('class_neck_mm'),segments=v['segments'],length_mm=v['length_mm'])
    thermals=craft.thermal(d,meta)
    for key,v in thermals.items():
        categories['thermal'][key]=dict(v,via_count=v['count'],connected=complete.get(v['net'],False))
    pad_net={p['ref']+'.'+p['num']:p['net'] for p in d['pads']}
    for key,net in sorted(pad_net.items()):
        categories['pad_entry'][key]=dict(off_axis=None,corner=None,wider_than_face=None,
                                         connected=complete.get(net,False),local_contact=False)
    for key,v in manufacturing['per_pad_entry'].items():
        categories['pad_entry'][key]=dict(off_axis=int(v['off']),corner=int(v['corner']),wider_than_face=int(v['wide']),
                                         connected=complete.get(pad_net[key],False),local_contact=True)
    spacing_fields = spacing.measure(d, complete)
    for n in sorted(complete):
        categories['routing_craft'][n]=dict(acute_junctions=manufacturing.get('acute_by_net',{}).get(n,0),connected=complete[n])
        categories['routing_craft'][n].update(spacing_fields.get(n, {}))
        pads={(p['ref'],p['num']) for p in d['pads'] if p['net']==n}
        obligations=max(0,len(pads)-1)
        if obligations:
            categories['efficiency'][n]=dict(connected=complete[n],connections=obligations,
                                             vias_per_connection=sum(v['net']==n for v in d['vias'])/obligations,
                                             mm_per_connection=si.exact_copper_length(d,n)/obligations)
    coverage = dict(fill_hash=fill_hash,broad_reference_layers=sorted(broad),
                    reference_error=reference_error,manifest_obligations=len(manifest.get('connections',[])),
                    differential_candidates=len(candidates), thermal_candidates=len(thermals))
    if interfaces is not None or profile is not None:
        coverage['interfaces'] = interface_io.coverage(interfaces, d)
    if profile is not None:
        coverage['rules'] = measure_rules(d, interfaces, profile,
                                         dict(fills=ff, complete=complete, board_path=board, pad_metadata=meta, broad=broad))
    return categories, manufacturing, coverage


def board_job(job):
    name,runs,reference,intents,version,*rule_options=job
    interface_dir,profile = rule_options if rule_options else (None,None)
    # Interfaces also steer return-path references, so they load without a profile.
    interfaces = qr.load_interfaces(interface_dir, name)
    interface_path = Path(interface_dir)/(name+'.interfaces.json') if interfaces is not None else None
    paths=[reference]+runs
    result=dict(board=name,runs={},errors={})
    manifest_path=Path(intents)/(name+'.intent.json') if intents else None
    manifest=json.loads(manifest_path.read_text()) if manifest_path and manifest_path.exists() else {}
    human_path=reference/'boards'/(name+'.kicad_pcb')
    fx=io.fixture(name,runs[0] if runs else reference)
    if not human_path.exists() and fx and (fx/'routed.kicad_pcb').exists():
        # Old human directories omitted some synthetic fixtures; use the actual
        # preserved original, never manufacture a reference from a router arm.
        human_path=fx/'routed.kicad_pcb'
    if not human_path.exists():
        result['errors'][str(reference)]='missing human board'
        return result
    meta=craft.metadata(human_path)
    human_geometry,meta=identity.canonical_human(io.inspect(human_path,reference,name),human_path,meta)
    intended_references=returns.inferred_references(human_geometry,human_geometry['nets'])
    pair_endpoints=qpaths.infer_endpoints(human_geometry)
    published_labels={}
    published_path=runs[0]/'boards'/(name+'.kicad_pcb')
    if published_path.exists():
        _,published_labels,_=identity.match(io.inspect(published_path,runs[0],name),human_geometry)
    manifest=identity.manifest_bindings(manifest,human_geometry,published_labels)
    # Freeze one common input and judge it once, independent of the router arm.
    input_report=None
    input_provenance=None
    if fx:
        try:
            _,input_report,input_provenance=io.prepare(fx/'unrouted.kicad_pcb',reference,name,version,supplied=False)
        except (RuntimeError,subprocess.SubprocessError,OSError,ValueError) as e:
            result['errors']['input']=str(e)
    for run in paths:
        source=human_path if run==reference else run/'boards'/(name+'.kicad_pcb')
        try:
            if not source.exists():
                raise ValueError('scheduled board artifact missing')
            filled,report,provenance=io.prepare(source,run,name,version)
            raw_data=io.inspect(source,run,name)
            data,_,terminal_coverage=identity.match(raw_data,human_geometry) if run!=reference else (human_geometry,{},dict(matched=len(human_geometry['pads']),unmatched=0,ambiguous=0,tolerance_nm=identity.TOL_NM))
            judged=io.judge(input_report,report) if input_report is not None else dict(electrical_pass=len(report.get('unconnected_items',[]))==0,
                                                                                     drc_pass=None,completion=None,censored=None,unconnected=len(report.get('unconnected_items',[])),unconnected_nets=sorted({n for u in report.get('unconnected_items',[]) for item in u.get('items',[]) for n in io.bracket_nets(item.get('description',''))}))
            bad_nets = judged['unconnected_nets']
            categories,mfg,coverage=measure(data,manifest,filled,meta,bad_nets,
                                            censored=len(report.get('unconnected_items',[]))>=499,
                                            unconnected_items=report.get('unconnected_items', []),
                                            interfaces=interfaces, profile=profile,
                                            intended_references=intended_references,pair_endpoints=pair_endpoints)
            matched_keys={p['ref']+'.'+p['num'] for p in data['pads'] if p.get('physical_matched')}
            unmatched_human={p['ref']+'.'+p['num'] for p in human_geometry['pads']} - matched_keys
            coverage['terminal_matching']=dict(terminal_coverage,unmatched_human=sorted(unmatched_human))
            preparation=roster(run).get(name,{}).get('preparation',{})
            if not preparation and fx and (fx/'preparation.json').exists():
                preparation=json.loads((fx/'preparation.json').read_text())
            coverage['preparation']=preparation
            route=source.with_suffix(source.suffix+'.route.json')
            diagnostic=json.loads(route.read_text()) if route.exists() else {}
            result['runs'][str(run)]=dict(categories=categories,manufacturing=mfg,coverage=coverage,judge=judged,
                                          provenance=provenance,input_provenance=input_provenance,
                                          manifest_sha256=io.digest(manifest_path) if manifest else None,
                                          placement_sha256=hashlib.sha256(json.dumps(sorted((p['net'],p['x'],p['y'],p['w'],p['h'],p['angle'],tuple(p['layers'])) for p in data['pads'])).encode()).hexdigest(),
                                          engine_diagnostic={k:diagnostic.get(k) for k in ('routed','connections','stop','expansions','seconds')},
                                          whole_board=dict(vias=len(data['vias']),track_arc_mm=sum(si.exact_copper_length(data,n) for n in data['nets'])))
            arm = result['runs'][str(run)]
            if profile is not None:
                arm['rules'] = coverage.pop('rules')
            if interfaces is not None:
                arm['interfaces_sha256'] = io.digest(interface_path)
                arm['provenance']['interfaces_sha256'] = arm['interfaces_sha256']
            if run==reference:
                original,_,_=measure(data,manifest,source,meta,judged['unconnected_nets'],interfaces=interfaces,
                                     censored=len(report.get('unconnected_items', []))>=499,
                                     unconnected_items=report.get('unconnected_items', []),
                                     intended_references=intended_references,pair_endpoints=pair_endpoints)
                result['original_human']=original
                result['human_data']=data
            else:
                # Gap target is frozen from the HUMAN, never fitted to each arm.
                result['runs'][str(run)]['pair_data']=data
        except (RuntimeError,subprocess.SubprocessError,OSError,ValueError,KeyError,IndexError) as e:
            result['errors'][str(run)]=str(e)
    h=result['runs'].get(str(reference))
    if h:
        hd=result.pop('human_data')
        for run in paths:
            arm=result['runs'].get(str(run))
            if not arm:
                continue
            dd=hd if run==reference else arm.pop('pair_data')
            for key,hp in h['categories']['differential_pairs'].items():
                p,m=key.split('|')
                gap=si.target_gap(si.segs(hd,p),si.segs(hd,m))
                rr=arm['categories']['differential_pairs'].setdefault(key,dict(connected=False))
                rr['target_gap_mm']=gap
                if rr.get('connected') and gap is not None:
                    coupled,total=si.coupled(si.segs(dd,p),si.segs(dd,m),gap)
                    rr['coupled_fraction']=coupled/total if total else None
                    rr['coupled_mm'],rr['total_mm']=coupled,total
                else:
                    rr['coupled_fraction']=None
            for n,hp in h['categories']['power'].items():
                rr=arm['categories']['power'].get(n)
                if rr:
                    rr['neck_fraction']=sum(l for w,l in rr['segments'] if w<hp['median_width_mm']-1e-6)/rr['length_mm']
            # Only nonzero human arrays define retention obligations.
            array={k:v for k,v in h['categories']['thermal'].items() if v.get('via_count') is not None and v['via_count']>0}
            arm['categories']['thermal']={k:arm['categories']['thermal'].get(k,dict(via_count=0,connected=False)) for k in array}
            unmatched=set(arm['coverage']['terminal_matching']['unmatched_human'])
            unknown_nets={p['net'] for p in hd['pads'] if p['ref']+'.'+p['num'] in unmatched}
            for category,metrics in qc.METRICS.items():
                for key,item in h['categories'][category].items():
                    physical_unknown = (key in unmatched if category in ('pad_entry','thermal') else
                                        any(endpoint in unmatched for endpoint in key.split('>')) if category=='decoupling' else
                                        any(net in unknown_nets for net in key.split('|')) if category=='differential_pairs' else key in unknown_nets)
                    preparation_unknown = (arm['coverage']['preparation'].get('design_copper_comparable') is False
                                           and (category in ('efficiency','return_paths') or category=='decoupling' and item.get('plane_lead_mm') is not None))
                    if physical_unknown or preparation_unknown:
                        unknown=arm['categories'][category].setdefault(key,{})
                        unknown.update(connected=True,coverage_reason='unmatched physical terminal' if physical_unknown else 'fixture design copper not comparable')
                        unknown.update({metric:None for metric in metrics})
                        if category == 'decoupling':
                            unknown['return_connected'] = None
            missing_arrays={k:dict(human=v['via_count'],router=arm['categories']['thermal'][k].get('via_count')) for k,v in array.items()
                            if arm['categories']['thermal'][k].get('via_count') is None or arm['categories']['thermal'][k]['via_count']<v['via_count']}
            mfg=arm['manufacturing']; exempt=h['manufacturing']['via_in_pad']>0
            risky = mfg['via_in_pad'] > 0 or mfg['via_near_pad_025'] > 0
            process_pass = True if exempt else False if risky else None if mfg['unsupported_pads'] else True
            rule_hashes = lambda a: {ext: v['sha256'] for ext,v in a['provenance']['rules'].items()}
            valid = arm['placement_sha256'] == h['placement_sha256'] and rule_hashes(arm) == rule_hashes(h) and input_provenance is not None
            arm['gates']=dict(evaluation_valid=valid,electrical=arm['judge']['electrical_pass'],new_drc=arm['judge']['drc_pass'],
                               small_pad_manufacturing=process_pass,
                               human_vip_exempt=exempt,thermal_retained=not missing_arrays,thermal_losses=missing_arrays)
            arm['gates']['eligible']=all(arm['gates'][k] is True for k in ('evaluation_valid','electrical','new_drc','small_pad_manufacturing','thermal_retained'))
        # Original-fill control shares immutable geometry; fill-dependent metrics differ only.
        for category in qc.METRICS:
            if category not in ('decoupling','return_paths'):
                result['original_human'][category]=h['categories'][category]
    return result


def resolve_run(value):
    p=Path(value)
    return p.resolve() if p.exists() else (io.ROOT/'bench/results'/value).resolve()


def roster(run):
    p=run/'boards.jsonl'
    if p.exists():
        return {json.loads(line)['board']:json.loads(line) for line in p.read_text().splitlines() if line.strip()}
    return {p.stem:{} for p in sorted((run/'boards').glob('*.kicad_pcb')) if not p.stem.endswith('.refilled')}


def fmt(value):
    if value is None:
        return 'N/A'
    return f'{value:.2f}' if isinstance(value,float) else str(value)


def markdown(result):
    lines=['# PCB human-parity scorecard','',f"Reference: `{result['reference']}`. KiCad {result['kicad_version']}; runtime {result['seconds']:.1f} s.",'',
           '## Gates and evaluation coverage','',
           '| Run | Scheduled | Judged | Valid inputs | Electrical pass | No new DRC | Small-pad process pass | Thermal retained | Eligible | Censored |',
           '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for run,g in result['gates'].items():
        lines.append('| '+run+' | '+' | '.join(str(g[k]) for k in ('scheduled','judged','evaluation_valid','electrical','new_drc','small_pad_manufacturing','thermal_retained','eligible','censored'))+' |')
    for run,g in result['gates'].items():
        lines.append(f"\nCompletion {run}: macro={fmt(g['completion_macro_pct'])}%, pooled={fmt(g['completion_pooled_pct'])}% on {g['completion_boards']} uncensored nonzero-input boards; {g['already_connected']} already-connected inputs.")
    lines += ['', 'Gate failures cannot be traded for category wins. N/A is not zero defects. Censored completion never uses router planned/routed counts.', '', '## Graded categories', '']
    for category,metrics in result['categories'].items():
        lines += ['### '+category.replace('_',' ').title(),'']
        for metric,v in metrics.items():
            lines += ['#### '+metric, '', '| Run | Common items | Macro parity % | Pooled parity % | Wins / ties / losses | Whole-obligation parity % | Missing | Not assessable |', '|---|---:|---:|---:|---|---:|---:|---:|']
            for run,views in v['arms'].items():
                c,a=views['common_connected'],views['all_obligations']
                lines.append(f"| {metric} / {run} | {c['intersection']} | {fmt(c['macro_parity_pct'])} | {fmt(c['pooled_parity_pct'])} | {c['win']} / {c['tie']} / {c['loss']} | {fmt(a['pooled_parity_pct'])} | {a['missing']} | {a['not_assessable']} |")
            for delta in v['paired_differences']:
                lines.append(f"\n{metric}: {delta['arm_b']} − {delta['arm_a']} = {fmt(delta['delta_pp'])} macro pp; paired-board 95% bootstrap CI {delta['ci95_pp']}, n={delta['boards']}.")
            worst=[dict(run=run,**item) for run,views in v['arms'].items() for item in views['all_obligations']['worst'][:1]]
            if worst:
                lines.append('\nWorst items: '+'; '.join(f"{x['run']} `{x['board']}:{x['item']}` human={fmt(x['human'])}, router={fmt(x['router'])}" for x in worst)+'.')
        lines.append('')
    lines += ['## Measurement noise floor','', '| Category / metric | Self disagreement | Original-fill vs refilled changed / assessable | Max absolute change | Not assessable |', '|---|---:|---:|---:|---:|']
    for category,metrics in result['noise_floor'].items():
        for metric,n in metrics.items():
            lines.append(f"| {category} / {metric} | {fmt(n['self_disagreement'])} | {n['original_vs_refilled_changed']} / {n['original_vs_refilled_items']} | {fmt(n['original_vs_refilled_max_abs'])} | {n['not_assessable']} |")
    lines += ['', '## Limits', '']+[ '- '+s for s in result['limits']]
    if result['errors']:
        lines += ['', '## Unassessable boards', '', '```json', json.dumps(result['errors'],indent=2), '```']
    if 'rules' in result:
        lines += ['', qr.markdown(result['rules'], list(result['gates'])[1:]).rstrip(),
                  '## Interface coverage', '',
                  '| Board | Run | Interface | Confidence | Lanes | Not bound |',
                  '|---|---|---|---|---:|---:|']
        for board in result['boards']:
            for run, arm in board['runs'].items():
                cov = arm.get('coverage', {}).get('interfaces') or interface_io.coverage(None, {})
                for iface in cov['interfaces']:
                    missing = sum(l['interface'] == iface['id'] for l in cov['lanes_not_bound'])
                    lines.append(f"| {board['board']} | {Path(run).name} | {iface['id']} | {iface['confidence']} | {iface['lanes']} | {missing} |")
                if not cov['interfaces']:
                    lines.append(f"| {board['board']} | {Path(run).name} | {cov['status'] or 'no detected interfaces'} | N/A | 0 | N/A |")
    return '\n'.join(lines)+'\n'


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('runs',nargs='+');ap.add_argument('--reference',required=True)
    ap.add_argument('--intents');ap.add_argument('--json');ap.add_argument('--md')
    ap.add_argument('--interfaces', help='frozen input interface manifests')
    ap.add_argument('--profile', help='opt-in rule profile ID or JSON path; default: no rules')
    ap.add_argument('--jobs',type=int,default=6)
    args=ap.parse_args()
    profile = qr.load_profile(args.profile) if args.profile else None
    if not 1<=args.jobs<=6:
        ap.error('--jobs must be between 1 and 6')
    started=time.monotonic()
    reference=resolve_run(args.reference);runs=list(dict.fromkeys(resolve_run(p) for p in args.runs))
    version=subprocess.run([io.KICAD,'version'],check=True,capture_output=True,text=True).stdout.strip()
    # The compared runs schedule the boards (a set without its `#slow` boards, D115); the human run is only their reference.
    names=sorted(set().union(*(set(roster(r)) for r in runs)))
    jobs=[(name,runs,reference,args.intents,version,args.interfaces,profile) for name in names]
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        boards=list(pool.map(board_job,jobs))
    refs={b['board']:b['runs'][str(reference)]['categories'] for b in boards if str(reference) in b['runs']}
    arms={r.name:{b['board']:b['runs'][str(r)]['categories'] for b in boards if str(r) in b['runs']} for r in runs}
    gates={}
    for run in [reference]+runs:
        rows=[b['runs'][str(run)] for b in boards if str(run) in b['runs']]
        gates[run.name]=dict(scheduled=len(names),judged=len(rows),censored=sum(bool(r['judge'].get('censored')) for r in rows),
                             **{k:sum(r.get('gates',{}).get(k) is True for r in rows) for k in ('evaluation_valid','electrical','new_drc','small_pad_manufacturing','thermal_retained','eligible')})
        judged=[r['judge'] for r in rows if r['judge'].get('completion') is not None]
        denominator=sum(r['common_denominator'] for r in judged)
        gates[run.name].update(completion_macro_pct=100*statistics.mean(r['completion'] for r in judged) if judged else None,
                               completion_pooled_pct=100*(1-sum(r['unconnected'] for r in judged)/denominator) if denominator else None,
                               completion_boards=len(judged),already_connected=sum(r['judge'].get('already_connected',False) for r in rows))
    limits=[
        'Human parity is a practical reference, not SI/current/thermal/assembly certification. Categories have no weighted total.',
        'KiCad is the independent connectivity authority. tracemaker drc has not established uncapped plane/net-tie parity; 499-saturated lists are censored. The ratio is an interim disconnected-item denominator, not proven terminal-component rank.',
        'DRC additions use violation-instance differences, including non-track errors. UUID/description changes and KiCad solder-mask noise can cause baseline differences; inspect reported instances before sign-off.',
        'Decoupling supply and return leads use free-plane-hop lower bounds between contacts to the same actual filled island; return hops exclude unanchored islands. Multilayer island joins and full loop impedance are not modeled. Human-invalid paths are coverage holes, not an allowed defect rate.',
        'Differential nets are name-selected, two-terminal graph-qualified candidates. Coupling uses the human modal gap, not a certified impedance target; skew excludes package/vertical-via delay. NetGraph approximates arcs with two chords for connectivity only.',
        'Historical returns screen adjacent broad GND fills. New fields preserve intended net/island identity using frozen manifest references or unambiguous nearest endpoint-IC ground pins on the human. Anchoring means pad connectivity, not earth grounding; power-transfer capacitors and actual return-current fields are not certified. Declared Ethernet isolation is excluded from missing-ground penalties; all-layer projected isolation gaps are not safety/creepage sign-off.',
        'Supply resistance is a 35 um copper series-track diagnostic, not a loaded network: planes and parallel paths are omitted. Neck fraction is length below human median width; class neck length is separate.',
        'Thermal pads inferred from central geometry are candidates, not datasheet-certified obligations. Retention counts same-net in-pad vias, not receiving-plane thermal conductance. Small-pad exemptions are board-wide when human has via-in-pad; raw counts remain published.',
        'Pad-entry and acute-junction thresholds are prototype craft heuristics, not IPC errors. Unsupported/custom shapes are unassessable; arcs are omitted from pad-entry/acute screens.',
        'Efficiency uses a fixed distinct pad-ID spanning-rank proxy per net, not the router denominator. Beneficial thermal/return vias and tuning copper are not identified as waste. Legacy quick/mid router inputs omit pours, confounding electrical human comparisons.',
        'Bootstrap intervals resample paired boards (seed 107, 2000 draws); they describe curated-set sensitivity, not population confidence or routing-seed variance.',
        'Noise self-control tests deterministic measurement identity, not independent KiCad refill repeatability or systematic model accuracy. Original-fill comparisons are noise controls only and are never graded.',
    ]
    errors={b['board']:b['errors'] for b in boards if b['errors']}
    result=dict(schema=1,reference=str(reference),runs=[str(r) for r in runs],kicad_version=version,
                seconds=time.monotonic()-started,gates=gates,categories=qc.summarize(refs,arms),
                noise_floor=qc.noise(refs,{b['board']:b.get('original_human',{}) for b in boards}),
                boards=boards,errors=errors,limits=limits)
    if profile is not None:
        result['profile'] = profile
        result['rules'] = qr.summarize(
            {b['board']:b['runs'][str(reference)]['rules'] for b in boards if str(reference) in b['runs']},
            {r.name:{b['board']:b['runs'][str(r)]['rules'] for b in boards if str(r) in b['runs']} for r in runs})
    text=markdown(result)
    print(text)
    destination=Path(args.json) if args.json else runs[0]/'quality.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    if args.md:
        Path(args.md).parent.mkdir(parents=True,exist_ok=True)
        Path(args.md).write_text(text)
    return 1 if errors else 0


if __name__=='__main__':
    raise SystemExit(main())
