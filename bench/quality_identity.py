# SPDX-License-Identifier: GPL-3.0-or-later
"""PCBench published/raw reference labels differ; match physical terminals instead."""
from collections import Counter, defaultdict
import copy
import math

import plane_metrics as pm

TOL_NM = 1000


def duplicates(board):
    root=pm.parse(board.read_text())
    refs=[]
    for fp in pm.children(root,'footprint')+pm.children(root,'module'):
        values=[x[2] for x in pm.children(fp,'property') if x[1]=='Reference']+[x[2] for x in pm.children(fp,'fp_text') if x[1]=='reference']
        if values:
            refs.append(values[0])
    return {ref for ref,n in Counter(refs).items() if n>1}


def canonical_human(data, board, metadata):
    result=copy.deepcopy(data)
    duplicate=duplicates(board)
    newmeta=dict(metadata)
    for p in result['pads']:
        original=p['ref']+'.'+p['num']
        p['source_ref']=p['ref']
        if p['ref'] in duplicate:
            # Duplicate footprint names are not valid logical terminal identities.
            p['ref'] += '@'+str(p['x'])+','+str(p['y'])
        p['physical_matched']=True
        newmeta[p['ref']+'.'+p['num']]=metadata.get(original,{})
    return result,newmeta


def match(data, human):
    result=copy.deepcopy(data)
    cells=defaultdict(list)
    for p in human['pads']:
        cells[(p['net'],tuple(sorted(p['layers'])),p['x']//TOL_NM,p['y']//TOL_NM)].append(p)
    labels=defaultdict(set)
    matched=unmatched=ambiguous=0
    for p in result['pads']:
        old=p['ref']+'.'+p['num']
        candidates=[]
        x,y=p['x']//TOL_NM,p['y']//TOL_NM
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                candidates += [q for q in cells.get((p['net'],tuple(sorted(p['layers'])),x+dx,y+dy),[]) if math.hypot(q['x']-p['x'],q['y']-p['y'])<=TOL_NM]
        # Repeated physical pad primitives with the same identity are one terminal.
        keys={(q['ref'],q['num']) for q in candidates}
        p['source_ref']=p['ref']
        if len(keys)==1:
            p['ref'],p['num']=next(iter(keys));p['physical_matched']=True
            labels[old].add(p['ref']+'.'+p['num']);matched+=1
        else:
            p['physical_matched']=False
            p['ref']='unmatched@'+p['ref']+'@'+str(p['x'])+','+str(p['y'])
            unmatched+=1;ambiguous+=len(keys)>1
    return result,{k:next(iter(v)) for k,v in labels.items() if len(v)==1},dict(matched=matched,unmatched=unmatched,ambiguous=ambiguous,tolerance_nm=TOL_NM)


def manifest_bindings(manifest, human, published_labels):
    """Unmatched/ambiguous role bindings are coverage holes, not route losses."""
    raw=defaultdict(set)
    canonical=set()
    for p in human['pads']:
        target=p['ref']+'.'+p['num'];canonical.add(target)
        raw[p.get('source_ref',p['ref'])+'.'+p['num']].add(target)
    def resolve(value):
        if value in published_labels:
            return published_labels[value]
        if value in canonical:
            return value
        if len(raw[value])==1:
            return next(iter(raw[value]))
        return 'unmatched-binding@'+value
    result=copy.deepcopy(manifest)
    for c in result.get('connections',[]):
        c['source_from'],c['source_to']=c['from'],c['to']
        c['from'],c['to']=resolve(c['from']),resolve(c['to'])
    return result
