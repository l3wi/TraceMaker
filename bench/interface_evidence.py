# SPDX-License-Identifier: GPL-3.0-or-later
"""Native component/pin evidence for input-only interface review and terminals."""
from collections import defaultdict
import re

import plane_metrics as pm


def native(board, d):
    by_label = defaultdict(list)
    parts = {}
    root = pm.parse(board.read_text())
    for fp in pm.children(root, 'footprint')+pm.children(root, 'module'):
        props = {p[1]: p[2] for p in pm.children(fp, 'property')}
        for text in pm.children(fp, 'fp_text'):
            if text[1] in ('reference', 'value'):
                props.setdefault(text[1].title(), text[2])
        ref = props.get('Reference')
        if not ref:
            continue
        part = dict(ref=ref, value=props.get('Value', ''), footprint=fp[1], description=props.get('Description', ''))
        parts[ref] = part
        for pad in pm.children(fp, 'pad'):
            functions, types = pm.children(pad, 'pinfunction'), pm.children(pad, 'pintype')
            by_label[ref, pad[1]].append(dict(part, pin=pad[1], function=functions[0][1] if functions else '',
                                           pin_type=types[0][1] if types else ''))
    roles, ambiguous = {}, set()
    for p in d['pads']:
        matches = by_label.get((p['ref'], p['num']), [])
        if len(matches) == 1:
            key = p['net'], p['x'], p['y'], tuple(sorted(p['layers']))
            if key in roles and roles[key] != matches[0]:
                roles.pop(key)
                ambiguous.add(key)
            elif key not in ambiguous:
                roles[key] = matches[0]
    return roles, parts, root


_RAIL = re.compile(r'(?i)(?:^|[/_])(?:GND|AGND|DGND|PGND|VCC|VDD|VSS|VBUS|[+]?\d+(?:V\d*)?)(?:$|_)')
_PROTECTION = re.compile(r'(?i)(?:ESD|TVS|PESD|PUSB3F|PRTR|TPD[1-9]|USBLC|RCLAMP|NUP\d|IP42\d|SRV05|SP30\d)')


def role(p, metadata, parts_pads):
    """Unknown is never silently discarded as a tap."""
    if metadata is None:
        return 'unknown'
    ref = metadata['ref']
    text = metadata['value']+' '+metadata['footprint']+' '+metadata.get('description', '')
    if re.match(r'^(?:TP|TEST)', ref, re.I) or re.search(r'TestPoint|Test_Point', text, re.I):
        return 'tap'
    if _PROTECTION.search(text):
        return 'tap'
    if re.match(r'^[RCD]\d', ref):
        other = {q['net'] for q in parts_pads.get(ref, []) if q['net'] != p['net']}
        if other and all(_RAIL.search(n) for n in other):
            return 'tap'
        return 'unknown'
    if re.match(r'^(?:J|P|CN)\d', ref) or re.search(r'(?:Connector|Conn_|PinHeader|PinSocket|FFC|FPC|RJ45|USB_[ABC])', text, re.I):
        return 'connector'
    if re.match(r'^(?:IC|U|T)\d', ref):
        return 'device'
    return 'unknown'


def choose(ps, metadata, series_ends):
    """Two frozen terminals per leg, with independently identified branch taps.

    Unknown non-series pads or multiple device endpoints preserve ambiguity.
    Repeated equal-function connector contacts use a canonical physical contact;
    the other contacts remain explicit taps and connectivity is judged separately.
    """
    from interfaces import endpoint, endpoint_key
    parts_pads = defaultdict(list)
    for p in ps:
        parts_pads[p['ref']].append(p)
    # Caller supplies the complete board population for rail-side classification.
    parts_pads = metadata.get('parts_pads', parts_pads) if metadata else parts_pads
    bindings = metadata.get('pads', {}) if metadata else {}
    physical = {}
    for p in sorted(ps, key=lambda p: (endpoint_key(endpoint(p)), p['ref'], p['num'])):
        physical[endpoint_key(endpoint(p))] = p
    outer = [p for k, p in sorted(physical.items()) if k not in series_ends]
    classified = [(p, role(p, bindings.get(endpoint_key(endpoint(p))), parts_pads)) for p in outer]
    if len(outer) <= 2:
        return [endpoint(p) for p in outer], [], 'exactly two outer pads' if len(outer) == 2 else 'insufficient outer pads'
    taps = [p for p, r in classified if r == 'tap']
    unknown = [p for p, r in classified if r == 'unknown']
    terminals = defaultdict(list)
    for p, r in classified:
        if r in ('device', 'connector'):
            terminals[p['ref']].append((p, r))
    selected = []
    reason = 'unknown pad role or multiple terminal components'
    if not unknown and len(terminals) == 2:
        for _, candidates in sorted(terminals.items()):
            if len(candidates) == 1:
                selected.append(candidates[0][0])
            elif all(r == 'connector' for _, r in candidates):
                functions = {bindings[endpoint_key(endpoint(p))]['function'] for p, _ in candidates}
                if len(functions) != 1 or not next(iter(functions)):
                    break
                ordered = sorted((p for p, _ in candidates), key=lambda p: endpoint_key(endpoint(p)))
                selected.append(ordered[0]); taps += ordered[1:]
            else:
                break
        if len(selected) == 2:
            return sorted((endpoint(p) for p in selected), key=endpoint_key), sorted((endpoint(p) for p in taps), key=endpoint_key), 'two component terminals; protection/pull-up/test/duplicate-contact taps excluded'
    return [endpoint(p) for p in outer], [], reason


def _function(meta):
    f = meta['function'].upper()
    # Converted libraries append the pad number to the original pin function.
    suffix = '_'+meta['pin'].upper()
    return f[:-len(suffix)] if f.endswith(suffix) else f


def supports(kind, lane, polarity, meta):
    """Corroborate protocol from actual pin semantics or standard contacts.

    Generic FPGA SERDES, an FFC outline, capacitor values and programmable GPIO
    do not prove a protocol. Device-family tests below require signal pin names.
    """
    f = _function(meta)
    text = (meta['value']+' '+meta['footprint']).upper()
    positive = polarity == 'p'
    sign = '+' if positive else '-'
    pn = 'P' if positive else 'N'
    match = False
    source = ''
    if kind == 'i2c':
        expected = 'SCL' if lane['role'] == 'clock' else 'SDA'
        match = bool(re.search(r'(?:^|[._])'+expected+r'(?:$|[/_~])', f))
        if not f:
            # Package-specific manufacturer pin tables, not name-only guesses.
            if re.fullmatch(r'INA226[A-Z]*', meta['value'].upper()) and re.search(r'MSOP.?10|VSSOP.?10|DGS', text):
                match = {'4':'SDA', '5':'SCL'}.get(meta['pin']) == expected
                source = 'TI SBOS547C table 4-1: https://www.ti.com/lit/ds/symlink/ina226.pdf'
            elif re.match(r'^24(?:AA|LC|FC)512(?:$|[-/])', meta['value'].upper()) and re.search(r'DFN|SOIC|SOIJ|PDIP|DIP8|TSSOP.?8', text):
                match = {'5':'SDA', '6':'SCL'}.get(meta['pin']) == expected
                source = 'Microchip DS21754M table 2-1 (8-pin/DFN): http://ww1.microchip.com/downloads/en/devicedoc/21754m.pdf'
    elif kind == 'usb2':
        # A standard data pin on an identified USB connector/PHY/controller,
        # not a generic ESD-array pin or an analogue converter D+/D- label.
        usb_part = bool(re.search(r'USB|CY7C68013|LPC43|FT4233|AM335|XE216', text))
        match = usb_part and bool(re.search(r'(?:^|_)D(?:P|M)(?:$|_)', f) and
                                  re.search(r'(?:^|_)D'+('P' if positive else 'M')+r'(?:$|_)', f)
                                  or f in ('D'+sign, 'USB_D'+sign))
        match |= bool(re.fullmatch(r'USB\d*_(?:D'+re.escape(sign)+'|D'+('P' if positive else 'M')+')', f))
        if usb_part and not f:
            connector = bool(re.search(r'USB[_ -](?:A|B|MICRO|MINI)|CONN[_ -]USB', text))
            match = connector and meta['pin'] == ('3' if positive else '2')
    elif kind == 'usb3':
        match = bool(re.search(r'(?:^|_)USB.*(?:SS)?(?:TX|RX).*'+pn+r'(?:$|_)', f))
        match |= bool(re.search(r'(?:SS)?(?:TX|RX)'+re.escape(sign)+r'$', f) and 'USB' in text)
        match |= bool(re.search(r'USB_SSR?T?X'+('P' if positive else 'M')+r'(?:_|$)', f) and 'TUSB' in text)
        # TUSB8041 encodes the polarity immediately before UP/DN1.
        match |= bool(re.search(r'USB_SS(?:TX|RX)'+('P' if positive else 'M')+r'(?:_|$)', f) and 'TUSB' in text)
    elif kind == 'pcie':
        match = bool(re.search(r'PCIE.*(?:_|)'+pn+r'$', f))
        match |= bool(re.fullmatch(r'(?:PER|PET|REFCLK)'+pn+r'\d*', f))
        match |= bool(re.fullmatch(r'(?:PER|PET)\d*'+re.escape(sign), f))
        match |= bool(re.fullmatch(r'(?:REFCLK|CLK)[A-Z]?_'+pn, f) and
                      re.search(r'M\.2|NGFF|67910|2199230|2199119|PCIE|PCIEXPRESS', text))
        match = match and (('CLK' in f) == (lane['role'] == 'clock'))
        # Explicit M.2 Key E/M connector contact contracts, not generic headers.
        if not f or f == meta['pin'].upper():
            if re.search(r'NGFF.KEY.E|M[.]2.*KEY.E|2199230-4', text):
                known = {35:'P',37:'N',41:'P',43:'N',47:'P',49:'N',
                         59:'P',61:'N',65:'P',67:'N',71:'P',73:'N'}
                clocks = {47,49,71,73}
            elif re.search(r'M[.]2.*KEY.M|2199230-6', text):
                known = {5:'N',7:'P',11:'N',13:'P',17:'N',19:'P',23:'N',25:'P',
                         29:'N',31:'P',35:'N',37:'P',41:'N',43:'P',47:'N',49:'P',53:'N',55:'P'}
                clocks = {53,55}
            else:
                known = {}
                clocks = set()
            pin = int(meta['pin']) if meta['pin'].isdigit() else -1
            match |= known.get(pin) == pn and ((pin in clocks) == (lane['role'] == 'clock'))
    elif kind == 'hdmi':
        match = bool(re.search(r'(?:HDMI|TMDS).*'+pn+r'$', f))
        match |= bool(re.fullmatch(r'(?:D[0-2]|CK|CLK)'+re.escape(sign), f) and 'HDMI' in text)
    elif kind == 'ethernet_mdi':
        match = bool(re.search(r'(?:ETH\d*\.PAIR_[A-D]|TRX\d)_'+pn+r'$', f))
        match |= bool(re.fullmatch(r'(?:T[RX]D|TDR|TRD)\d'+re.escape(sign), f))
        match |= bool(re.fullmatch(r'ETH\d*\.PAIR_[A-D]'+re.escape(sign), f))
        match |= bool(re.fullmatch(r'TXRX'+('P' if positive else 'M')+r'_[A-D]', f) and 'KSZ' in text)
        match |= bool(re.search(r'(?:TX|RX)_'+pn+r'$', f) and re.search(r'KSZ|DP838|LAN\d', text))
        if not f and re.match(r'^DP83848C', meta['value'].upper()) and re.search(r'PT48|LQFP.?48', text):
            match = {'13':'N', '14':'P', '16':'N', '17':'P'}.get(meta['pin']) == pn
            source = 'TI SNLS266E section 4.2: https://www.ti.com/lit/ds/symlink/dp83848c.pdf'
    elif kind == 'mipi_dsi':
        match = bool(re.search(r'DSI.*_'+pn+r'$', f))
        match |= bool(re.fullmatch(r'DA(?:[0-3]|C)'+pn, f) and 'SN65DSI' in text)
    elif kind == 'mipi_csi':
        match = bool(re.search(r'CSI.*_'+pn+r'$', f))
        match |= bool(re.fullmatch(r'(?:D[AB][0-3]|CK[AB])_'+pn, f) and 'MAX96724' in text)
    elif kind == 'lvds_lcd':
        match = bool(re.fullmatch(r'A_(?:Y[0-3]|CLK)'+pn, f) and 'SN65DSI84' in text)
        match |= bool(re.search(r'LVDS.*_'+pn+r'$', f) and 'LCD' in text)
    elif kind == 'sdio':
        label = re.sub(r'^(?:DAT|DATA)', 'D', lane['id'])
        named = re.sub(r'(?:DATA|DAT)(?=\d)', 'D', f)
        match = bool(re.search(r'(?:SDIO|SD\d|MMC).*'+re.escape(label)+r'$', named))
        match |= bool('EMMC' in text and named == label)
        if re.search(r'NGFF.KEY.E|M[.]2.*KEY.E|2199230-4', text) and not f:
            match |= {'9':'CLK', '11':'CMD', '13':'D0', '15':'D1', '17':'D2', '19':'D3'}.get(meta['pin']) == label
        if re.search(r'MICRO[_ ]?SD|MICRO_SD', text):
            standard = {'1':'D2', '2':'D3', '3':'CMD', '5':'CLK', '7':'D0', '8':'D1'}
            label = re.sub(r'^(?:DAT|DATA)', 'D', lane['id'])
            match |= standard.get(meta['pin']) == label
    detail = meta['function'] or ('manufacturer pin table' if source else 'standard contact')
    return f"{meta['ref']}({meta['value']}) pin {meta['pin']}={detail}"+('; '+source if source else '') if match else None


def review(manifest, d, metadata, root):
    """Apply a conservative board-evidence review; retain rejected obligations."""
    from interfaces import endpoint_key, endpoint
    netcodes = {str(n[1]): n[2] for n in pm.children(root, 'net') if len(n) > 2}
    zone_nets = set()
    for zone in pm.children(root, 'zone'):
        n = pm.children(zone, 'net')
        if n and not pm.children(zone, 'keepout'):
            name = netcodes.get(str(n[0][1]), str(n[0][1]))
            if name and name != '0':
                zone_nets.add(name)
    pads = metadata['pads']
    parts_pads = metadata['parts_pads']
    for iface in manifest['interfaces']:
        evidence, missing, conflicts = [], [], []
        all_nets = set()
        for lane in iface['lanes']:
            for leg in ('p', 'n'):
                if not lane.get(leg):
                    continue
                nets = {lane[leg]}
                while True:
                    expanded = nets | {s[k] for s in iface['series'] if s['a'] in nets or s['b'] in nets for k in ('a', 'b')}
                    if expanded == nets:
                        break
                    nets = expanded
                all_nets |= nets
                matches = []
                for p in d['pads']:
                    if p['net'] not in nets:
                        continue
                    meta = pads.get(endpoint_key(endpoint(p)))
                    if meta is None or role(p, meta, parts_pads) == 'tap':
                        continue
                    if iface['interface'] == 'pcie' and re.search(r'^(?:CSI|DSI|QSPI)', _function(meta)):
                        conflicts.append(f"{meta['ref']}.{meta['pin']} is {_function(meta)}, not confirmed PCIe")
                    proof = supports(iface['interface'], lane, leg, meta)
                    if proof:
                        matches.append(proof)
                if not matches:
                    missing.append(lane['id']+':'+leg)
                evidence += matches
        if not missing and not conflicts:
            iface['confidence'] = 'confirmed'
            iface['review_decision'] = 'confirmed'
        else:
            iface['confidence'] = 'candidate'
            iface['review_decision'] = 'rejected_for_grading'
        note = '; '.join(sorted(set(evidence)))
        if missing:
            note += '; no independent protocol pin/contact evidence for '+', '.join(missing)
        if conflicts:
            note += '; '+ '; '.join(sorted(set(conflicts)))
        iface['mode'] = None
        relevant = [m for key, m in sorted(pads.items()) if key[0] in all_nets]
        # Explicit card/slot connectors establish topology, never operating rate.
        slot = any(re.search(r'67910|NGFF.KEY.[EM]|M[.]2.*KEY.[EM]|2199230-[46]', m['value']+' '+m['footprint'], re.I) for m in relevant)
        if iface['confidence'] == 'confirmed' and iface['interface'] == 'pcie' and slot:
            iface['mode'] = 'slot'
            note += '; explicit Mini PCIe/M.2 connector contacts: slot mode'
        if iface['confidence'] == 'confirmed' and iface['interface'] == 'usb3':
            non_taps = [(p, pads.get(endpoint_key(endpoint(p)))) for p in d['pads'] if p['net'] in all_nets]
            classes = {role(p, m, parts_pads) for p, m in non_taps if role(p, m, parts_pads) in ('device', 'connector')}
            terminal_refs = {m['ref'] for p, m in non_taps if role(p, m, parts_pads) in ('device', 'connector')}
            if classes == {'device'} and len(terminal_refs) == 2:
                iface['mode'] = 'device_down'
                note += '; soldered USB controller/hub endpoints: device_down'
            elif 'connector' in classes:
                iface['mode'] = 'connector'
                note += '; USB connector data contacts: connector mode'
        # An input with one explicit GND pour domain and local component GND
        # contacts supports domain intent, not an electromagnetic stackup claim.
        endpoint_keys = {endpoint_key(e) for lane in iface['lanes'] for e in lane['endpoints']}
        endpoint_refs = {pads[k]['ref'] for k in sorted(endpoint_keys) if k in pads}
        grounded_refs = {ref for ref in endpoint_refs if any(p['net'] == 'GND' for p in parts_pads[ref])}
        if zone_nets == {'GND'} and endpoint_keys <= pads.keys() and endpoint_refs and grounded_refs == endpoint_refs:
            iface['reference_nets'] = ['GND']
            note += '; only input pour domain GND, relevant parts have GND contacts'
        else:
            iface['reference_nets'] = []
            note += '; reference domain not unambiguously established'
        iface['isolated_nets'] = []
        if iface['interface'] == 'ethernet_mdi':
            note += '; no separate on-board cable-side magnetics domain proven; integrated jack internals are not board nets'
        iface['review'] = note.lstrip('; ')
    manifest['status'] = 'reviewed'
    return manifest
