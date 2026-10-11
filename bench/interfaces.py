# SPDX-License-Identifier: GPL-3.0-or-later
"""Draft and conservatively review frozen interface obligations from INPUT boards.

Names establish candidates, independent native pin functions establish corroboration.
No operating mode, intended reference domain or external cable is guessed.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

import quality_io as io
import quality_identity as identity
import plane_metrics as pm
from prepare_inputs import set_boards
import interface_evidence as terminal_evidence


def tokens(name):
    # KiCad hierarchy remains part of the group identity, not a protocol substring.
    return re.sub(r'[_{} .:+-]+', '_', name.upper()).strip('_')


def polarity(name):
    """Return the stem and polarity; never treat ID+/QD+ as bare USB D+."""
    for pattern in (r'^(.*?)([+−-])$', r'^(.*?)[_.]([PN])$',
                    r'^(.*?(?:D|TX\d*|RX\d*|CLK|CK))([PN])$',
                    r'^(.*?)(D[PNM])$', r'^(.*?\d)([pn])$'):
        m = re.match(pattern, name, re.I if '[pn]' not in pattern else 0)
        if m:
            suffix = m[2].upper()
            if suffix in ('DP', 'DN', 'DM'):
                return m[1]+'D', 'p' if suffix == 'DP' else 'n'
            return m[1], 'p' if suffix in ('P', '+') else 'n'
    return None


def classify(stem, differential):
    """Return protocol, group stem, lane label, role, with token-aware exclusions."""
    path, _, leaf = stem.rpartition('/')
    t = tokens(leaf)
    # Explicit digital side and ancillary lines must not inherit sheet protocols.
    if re.search(r'(^|_)(NCSI|HPD|CEC|DDC|VBUS|CC[12]?|ULPI|RGMII|RMII)(_|$)', t):
        return None
    kind = None
    hierarchy = tokens(path.replace('/', '_'))
    patterns = [('pcie', r'PCIE(?:\d+)?'), ('sata', r'SATA(?:\d+)?'),
                ('mipi_dsi', r'DSI(?:\d+)?'), ('mipi_csi', r'CSI(?:\d+)?'),
                ('hdmi', r'(?:HDMI|TMDS|DVI)(?:\d+)?'),
                ('usb3', r'(?:USB\w*|SS)(?=_(?:SS)?(?:TX|RX))'),
                ('usb2', r'USB[A-Z0-9]*'), ('ethernet_mdi', r'(?:ETHERNET|ETH\d*|MDI\d*)'),
                ('lvds_lcd', r'LVDS(?:\d+)?')]
    if differential:
        for protocol, pattern in patterns:
            m = re.search(r'(^|_)('+pattern+r')(?=_|$)', t)
            if m:
                kind = protocol
                break
        if kind is None and re.search(r'(^|_)(?:SSTX|SSRX)\d*($|_)', t):
            kind = 'usb3'
        if kind is None and re.search(r'(^|_)(?:PHY|CONTROL|TARGET(?:_FS_MONITOR)?)_D$', t):
            kind = 'usb2'
        if kind is None and re.fullmatch(r'(?:D\d*|DATA\d+|PAIR_[A-D]|TX\d*|RX\d*|RCLKC|REFCLK|CLK|CK|ETH\d+)', t):
            for protocol, pattern in patterns:
                if re.search(r'(^|_)('+pattern+r')(?=_|$)', hierarchy):
                    kind = protocol
                    break
        # A generic LVDS sheet/Z expansion is not an LCD protocol declaration.
        if kind == 'lvds_lcd' and not re.search(r'(^|_)(?:D\d+|DATA\d+|CLK|CK)(_|$)', t):
            return None
        if kind == 'ethernet_mdi' and not re.search(r'(^|_)(?:D\d+|PAIR_[A-D]|[A-D]|TX\d*|RX\d*|ETH\d+)(_|$)', t):
            return None
        if kind == 'usb2' and not (re.search(r'(^|_)(?:D|DP|DM)$', t) or re.fullmatch(r'USB[A-Z0-9]*(?:_(?:EXT|HOST|DEVICE|DATA|PORT|FS|HS))*', t)):
            return None
        if kind is None:
            return None
        # Keep port, controller and sheet identities; remove only the lane suffix.
        t = re.sub(r'((?:SS)?(?:TX|RX)\d*)_D$', r'\1', t)
        m = re.search(r'(?:^|_)(RCLKC|REFCLK\d*|CLK\d*|CK\d*|(?:SS)?TX\d*|(?:SS)?RX\d*|PAIR_[A-D]|DATA\d+|D\d*|ETH\d+|[A-D])$', t)
        lane = m[1] if m else 'D'
        role = 'clock' if re.fullmatch(r'(?:RCLKC|REFCLK|CLK|CK)\d*', lane) or re.match(r'^CLK_', t) else 'tx' if 'TX' in lane else 'rx' if 'RX' in lane else 'data'
        group = t[:m.start()].rstrip('_') if m else t
    else:
        m = re.search(r'(^|_)(SDA|SCL)$', t)
        if m and not re.search(r'(^|_)(I2S|SPI)(_|$)', t):
            kind, lane = 'i2c', m[2]
            group = t[:m.start()].rstrip('_') or 'I2C'
        else:
            m = re.search(r'(^|_)(SDIO\d*|SD\d*|MMC\d*|EMMC\d*)(?:_|\.)(CLK|CK|CMD|DAT[A]?\d+|D\d+)$', t)
            if m:
                kind, lane = 'sdio', m[3]
                group = t[:m.start(3)].rstrip('_')
            else:
                m = re.search(r'(^|_)(LCD|RGB|CAM|CAMERA|VGA)(\d*)_(PCLK|CLK|HSYNC|VSYNC|HREF|DE|[RGB]\d*|D(?:ATA)?\d+)$', t)
                if not m:
                    return None
                family, lane = m[2], m[4]
                kind = 'vga' if family == 'VGA' else 'parallel_camera' if family in ('CAM', 'CAMERA') else 'rgb_lcd'
                group = t[:m.start(4)].rstrip('_')
        role = 'clock' if lane in ('SCL', 'CLK', 'CK', 'PCLK') else 'single'
    return kind, (path+'/' if path else '')+group, lane, role


def endpoint(p):
    return dict(x_nm=p['x'], y_nm=p['y'], layers=sorted(p['layers']), net=p['net'])


def endpoint_key(p):
    return p['net'], p['x_nm'], p['y_nm'], tuple(p['layers'])



def lane_stem(stem):
    # C is also a genuine Ethernet lane. Only a suffix AFTER a signal lane is
    # an AC-cap-side spelling, never PAIR_C or ETH0_C itself.
    stem = re.sub(r'((?:TX|RX)\d*(?:[_.](?:AC|C|CAP))?|CLK|CK)[_.]D$',
                  r'\1', stem, flags=re.I)
    return re.sub(r'((?:TX|RX)\d*|D\d+|CLK|CK)[_.](?:AC|C|CAP)$',
                  r'\1', stem, flags=re.I)

def native_pin_functions(board):
    """Actual pin functions, plus agreed standard USB connector contacts.

    A USB-looking footprint alone never confirms a bus: require its physical
    contact number AND encoded D+/D- function to agree with the connector map.
    """
    out = defaultdict(set)
    root = pm.parse(Path(board).read_text())
    for fp in pm.children(root, 'footprint')+pm.children(root, 'module'):
        name = fp[1].rsplit(':', 1)[-1]
        usb_c = name.startswith('USB_C_')
        usb_ab = bool(re.match(r'^USB_(?:A|B|Micro-B|Mini-B)_', name))
        mapping = {'A6':'D+', 'B6':'D+', 'A7':'D-', 'B7':'D-'} if usb_c else {'3':'D+', '2':'D-'} if usb_ab else {}
        for pad in pm.children(fp, 'pad'):
            nets, functions = pm.children(pad, 'net'), pm.children(pad, 'pinfunction')
            if nets and functions:
                net, function = nets[0][-1], functions[0][1]
                out[net].add(function)
                if mapping.get(pad[1]) == function:
                    out[net].add('USB_'+function)
    return out


def draft(d, board, pin_functions=None, pad_metadata=None):
    names = sorted(n for n in d['nets'] if n and not n.startswith('unconnected'))
    pins = pin_functions or {}
    pads = defaultdict(list)
    parts = defaultdict(list)
    for p in d['pads']:
        pads[p['net']].append(p)
        parts[p['ref']].append(p)
    # Only recognizable passive, two-terminal bridges; shunt caps never merge lanes.
    bridges = []
    for ref, ps in sorted(parts.items()):
        unique = {}
        for p in ps:
            key = p['num'], p['net'], p['x'], p['y']
            if key not in unique:
                unique[key] = dict(p, layers=sorted(p['layers']))
            else:
                unique[key]['layers'] = sorted(set(unique[key]['layers']) | set(p['layers']))
        ps = sorted(unique.values(), key=lambda p: (p['x'], p['y'], p['num'], p['net']))
        if len(ps) == 2 and re.match(r'^[CRL]\d', ref) and ps[0]['net'] != ps[1]['net'] and all(p['net'] in names for p in ps):
            bridges.append(dict(ref=ref, a=ps[0]['net'], b=ps[1]['net'],
                                endpoints=[endpoint(p) for p in ps]))
    parent = {n: n for n in names}
    def root(n):
        while parent[n] != n:
            n = parent[n]
        return n
    # A bridge is series only if both halves name the same lane (optional AC suffix)
    # or one half is anonymous. Never join two different known bus signals.
    def signature(n):
        p = polarity(n)
        if p:
            c = candidate(n)
            return ((c[0], c[1].rsplit('/',1)[-1], c[2], c[3]), p[1]) if c else None
        c = classify(n, False)
        return (c, 'single') if c else None
    def candidate(n):
        pol = polarity(n)
        if not pol:
            return classify(n, False)
        stem = lane_stem(pol[0])
        c = classify(stem, True)
        if c:
            return c
        # Explicit USB connector pin evidence can identify AUX_D/CONTROL_MCU_D
        # without accepting analogue ID/QD or generic ESD D+/D- pin functions.
        path, _, leaf = stem.rpartition('/')
        t = tokens(leaf)
        if re.search(r'(^|_)D$', t):
            corroborated = []
            for function in pins.get(n, ()):
                p = polarity(function)
                pc = classify(p[0], True) if p else None
                if pc and pc[0] == 'usb2' and p[1] == pol[1]:
                    corroborated.append(pc)
            if corroborated:
                return 'usb2', (path+'/' if path else '')+t[:-2], 'D', 'data'
        return None
    selected = []
    pending = list(bridges)
    while pending:
        changed = False
        for b in list(pending):
            ra, rb = root(b['a']), root(b['b'])
            sa = {signature(n) for n in names if root(n) == ra and signature(n)}
            sb = {signature(n) for n in names if root(n) == rb and signature(n)}
            anonymous = lambda r: all(n.startswith('Net-(') for n in names if root(n) == r)
            if ra != rb and ((sa and sa == sb) or (sa and anonymous(rb)) or (sb and anonymous(ra))):
                parent[max(ra, rb)] = min(ra, rb)
                selected.append(b)
                pending.remove(b)
                changed = True
        if not changed:
            break
    members = defaultdict(list)
    for n in names:
        members[root(n)].append(n)
    pair_names = defaultdict(lambda: defaultdict(list))
    singles = []
    for n in names:
        p = polarity(n)
        if p:
            c = candidate(n)
            if c:
                pair_names[c][p[1]].append(n)
        else:
            c = classify(n, False)
            if c:
                singles.append((c, n))
    groups = {}
    def add(c, p, n):
        kind, group, label, role = c
        iface = groups.setdefault((kind, group), dict(id=kind+':'+group, interface=kind, mode=None,
            confidence='candidate', lanes=[], series=[], reference_nets=[], isolated_nets=[]))
        nets = sorted(set(members[root(p)] + (members[root(n)] if n else [])))
        endpoints, taps, selections = [], [], []
        inner = {endpoint_key(e) for b in selected for e in b['endpoints']}
        for leg in (p, n):
            if not leg:
                continue
            ps = [q for net in members[root(leg)] for q in pads[net]]
            chosen, branch, reason = terminal_evidence.choose(ps, pad_metadata, inner)
            endpoints += chosen; taps += branch; selections.append(reason)
        lane = dict(id=label, role=role, p=p, n=n, endpoints=sorted(endpoints, key=endpoint_key),
                    taps=sorted(taps, key=endpoint_key), endpoint_selection='; '.join(selections))
        iface['lanes'].append(lane)
        for b in selected:
            if b['a'] in nets and b['b'] in nets and b not in iface['series']:
                iface['series'].append(b)
        # Independent named device/connector pins must corroborate both legs.
        evidence = []
        for net in nets:
            for function in sorted(pins.get(net, ())):
                pol = polarity(function)
                pc = classify(pol[0], True) if pol else classify(function, False)
                expected_pol = 'p' if net in members[root(p)] else 'n'
                if pc and pc[0] == kind and (not n or (pol and pol[1] == expected_pol)):
                    evidence.append(dict(net=net, pin_function=function))
        if evidence:
            lane['corroboration'] = evidence
    used_pairs = set()
    for c, polarities in sorted(pair_names.items()):
        rp = sorted({root(net) for net in polarities['p']})
        rn = sorted({root(net) for net in polarities['n']})
        if len(rp) == len(rn) == 1 and rp != rn and (rp[0], rn[0]) not in used_pairs:
            add(c, min(polarities['p']), min(polarities['n']))
            used_pairs.add((rp[0], rn[0]))
    used_singles = set()
    for c, net in sorted(singles):
        if root(net) not in used_singles:
            add(c, net, None)
            used_singles.add(root(net))
    result = []
    for _, iface in sorted(groups.items()):
        iface['lanes'].sort(key=lambda l: (l['id'], l['p'], l['n'] or ''))
        iface['series'].sort(key=lambda b: (endpoint_key(b['endpoints'][0]), endpoint_key(b['endpoints'][1])))
        # Require a full named bus (not a lone SCL), clock and data for display/camera.
        labels = {l['id'] for l in iface['lanes']}
        if iface['interface'] == 'i2c' and not {'SCL', 'SDA'} <= labels:
            continue
        if iface['interface'] in ('rgb_lcd', 'parallel_camera') and not (any(l['role'] == 'clock' for l in iface['lanes']) and any(re.match(r'[RGBD]\d', l['id']) for l in iface['lanes'])):
            continue
        if all(l.get('corroboration') and (not l['n'] or all(any(e['net'] in members[root(net)] for e in l['corroboration']) for net in (l['p'], l['n']))) for l in iface['lanes']):
            iface['confidence'] = 'confirmed'
        result.append(iface)
    return dict(schema=1, board=board, status='draft', interfaces=result)


def coverage(manifest, d):
    if manifest is None:
        return dict(status='missing', interfaces=[], lanes_not_bound=[])
    unbound, entries = [], []
    for iface in manifest['interfaces']:
        entries.append(dict(id=iface['id'], interface=iface['interface'], confidence=iface['confidence'], lanes=len(iface['lanes'])))
        for lane in iface['lanes']:
            missing = []
            for ep in lane['endpoints']:
                matches = {(p['x'], p['y'], tuple(sorted(p['layers']))) for p in d['pads']
                           if p['net'] == ep['net'] and sorted(p['layers']) == sorted(ep['layers'])
                           and (p['x']-ep['x_nm'])**2+(p['y']-ep['y_nm'])**2 <= identity.TOL_NM**2}
                if len(matches) != 1:
                    missing.append(ep)
            if missing or not lane['endpoints']:
                unbound.append(dict(interface=iface['id'], lane=lane['id'], reason='missing or ambiguous physical endpoints', endpoints=missing))
    return dict(status=manifest.get('status'), interfaces=entries, lanes_not_bound=unbound)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--set', choices=('modern-planes', 'quick', 'mid'))
    ap.add_argument('--board', help='prepared board name; optionally restrict --set')
    ap.add_argument('--input', type=Path, help='explicit INPUT/human .kicad_pcb; requires --board')
    ap.add_argument('--fixtures', type=Path, help='prepared input root override')
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--review', action='store_true', help='review protocol identity against input pin/part evidence; never guess missing modes')
    args = ap.parse_args()
    if not args.set and not args.board:
        ap.error('supply --set or --board')
    if args.input and not args.board:
        ap.error('--input requires --board')
    names = [args.board] if args.board else set_boards(io.ROOT/'bench/sets'/(args.set+'.txt'))
    roots = ([args.fixtures] if args.fixtures else
             [io.ROOT/'build'/'modern-planes', io.ROOT/'build'/'pcbench'] if args.set is None else
             [io.ROOT/'build'/('modern-planes' if args.set == 'modern-planes' else 'pcbench')])
    args.out.mkdir(parents=True, exist_ok=True)
    for name in sorted(names):
        source = args.input or next((r/name/'unrouted.kicad_pcb' for r in roots if (r/name/'unrouted.kicad_pcb').exists()), None)
        if source is None:
            raise FileNotFoundError('prepared INPUT missing: '+name)
        d = io.inspect(source, args.out, name)
        physical, _, native_root = terminal_evidence.native(source, d)
        parts_pads = defaultdict(list)
        for p in d['pads']:
            parts_pads[p['ref']].append(p)
        metadata = dict(pads=physical, parts_pads=parts_pads)
        manifest = draft(d, name, native_pin_functions(source), metadata)
        if args.review:
            terminal_evidence.review(manifest, d, metadata, native_root)
        manifest['source'] = dict(path=str(source.resolve()), sha256=io.digest(source))
        (args.out/(name+'.interfaces.json')).write_text(json.dumps(manifest, indent=2)+'\n')
        print(name, len(manifest['interfaces']), sum(len(i['lanes']) for i in manifest['interfaces']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
