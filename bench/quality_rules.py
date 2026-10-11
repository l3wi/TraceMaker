# SPDX-License-Identifier: GPL-3.0-or-later
"""Absolute layout-rule checks for the scorecard (doc 10 §9, guide rule profiles).

A rule profile (``bench/profiles/<id>.json``) lists published limits, e.g. the
2015 Toradex layout guide's interface tables. An interface manifest
(``<dir>/<board>.interfaces.json``) freezes which nets form which interface,
lane and role; it is drafted from the input board and reviewed, never derived
from the routed board under evaluation.

Every check has four outcomes. A value is a clear pass or a clear fail only
outside a stated uncertainty band around the limit; inside the band it is
``boundary``. Missing roles, geometry or data give ``na`` and never count as a
pass. The human board is checked with the same code and its failures stay
visible: these are absolute rules, not human parity.
"""
import json
from pathlib import Path

PROFILES = Path(__file__).resolve().parent/'profiles'
OUTCOMES = ('pass', 'fail', 'boundary', 'na')
# Geometric uncertainty for millimetre comparisons; integer counts use 0.
BAND_MM = 0.001
COMPARATORS = ('<', '<=', '>', '>=')
TREATMENTS = ('rule', 'report')
SCHEMA = 1


def check(value, limit, comparator, band=BAND_MM):
    """Three-state comparison of ``value`` against ``limit``.

    ``<``/``>`` are strict and ``<=``/``>=`` inclusive; within ``band`` of the
    limit the outcome is ``boundary`` for every comparator, so a measurement
    error cannot flip a verdict. ``None`` value or limit gives ``na``.
    """
    if comparator not in COMPARATORS:
        raise ValueError('unknown comparator '+repr(comparator))
    if value is None or limit is None:
        return 'na'
    if band > 0 and abs(value-limit) <= band:
        return 'boundary'
    if comparator == '<':
        return 'pass' if value < limit else 'fail'
    if comparator == '<=':
        return 'pass' if value <= limit else 'fail'
    if comparator == '>':
        return 'pass' if value > limit else 'fail'
    return 'pass' if value >= limit else 'fail'


def result(rule, item, value, *, reason=None, band=None):
    """One rule application. ``rule`` is a profile entry, ``item`` a stable key
    such as ``"usb0:D"`` (interface id and lane). ``reason`` explains ``na``."""
    band = rule.get('band', BAND_MM) if band is None else band
    outcome = check(value, rule['limit'], rule['comparator'], band)
    if outcome == 'na' and not reason:
        reason = 'value not measurable'
    return dict(rule=rule['id'], item=item, value=value, limit=rule['limit'], comparator=rule['comparator'],
                band=band, outcome=outcome, treatment=rule.get('treatment', 'rule'), reason=reason)


def key(entry):
    return entry['rule']+'@'+entry['item']


def load_profile(name_or_path):
    path = Path(name_or_path)
    if not path.exists():
        path = PROFILES/(str(name_or_path)+'.json')
    profile = json.loads(path.read_text())
    seen = set()
    for rule in profile['rules']:
        for field in ('id', 'interface', 'measure', 'comparator', 'limit', 'strength', 'source', 'treatment'):
            if field not in rule:
                raise ValueError(f"{path}: rule {rule.get('id')} lacks {field}")
        if rule['comparator'] not in COMPARATORS or rule['treatment'] not in TREATMENTS:
            raise ValueError(f"{path}: rule {rule['id']} has invalid comparator/treatment")
        if rule['id'] in seen:
            raise ValueError(f"{path}: duplicate rule {rule['id']}")
        seen.add(rule['id'])
    return profile


def rules_for(profile, interface, measure, mode=None):
    """Profile rules for one interface kind and measurement, in file order.

    ``interface: "*"`` rules apply to every interface. A rule with a ``mode``
    applies only when the manifest mode equals it; unknown mode (None) matches
    only mode-free rules, so a mode-specific limit is never guessed.
    """
    return [r for r in profile['rules'] if r['interface'] in (interface, '*') and r['measure'] == measure
            and r.get('mode') in (None, mode)]


def load_interfaces(directory, board):

    """Interface manifest for ``board`` or None. Validates the frozen schema.

    Schema 1::

        {"schema": 1, "board": str, "status": "draft" | "reviewed",
         "interfaces": [{
            "id": str,                      # unique per board, e.g. "usb0"
            "interface": str,               # profile interface kind: usb2, pcie, mipi_dsi, ...
            "mode": str | null,             # profile mode, e.g. "slot", "device_down"; null = unknown
            "confidence": "confirmed" | "candidate",
            "lanes": [{"id": str,           # e.g. "D0", "CLK", "TX0"
                       "role": "data" | "clock" | "tx" | "rx" | "single",
                       "p": net, "n": net | null,          # n null for single-ended
                       "endpoints": [{"x_nm": int, "y_nm": int, "layers": [str], "net": net}]}],
            "series": [{"ref": str, "a": net, "b": net}],  # parts joining nets within a lane path
            "reference_nets": [net],        # intended return reference(s)
            "isolated_nets": [net]          # deliberately separate domains (e.g. Ethernet cable side)
         }]}
    """
    if not directory:
        return None
    path = Path(directory)/(board+'.interfaces.json')
    if not path.exists():
        return None
    manifest = json.loads(path.read_text())
    if manifest.get('schema') != SCHEMA:
        raise ValueError(f'{path}: unsupported interface schema {manifest.get("schema")}')
    ids = set()
    for iface in manifest['interfaces']:
        if iface['id'] in ids:
            raise ValueError(f'{path}: duplicate interface id {iface["id"]}')
        ids.add(iface['id'])
        for lane in iface['lanes']:
            if lane['role'] != 'single' and not lane.get('n'):
                raise ValueError(f'{path}: {iface["id"]}.{lane["id"]} differential lane lacks n')
    return manifest


def summarize(reference, arms):
    """Per-rule outcome counts for the human and each arm, with per-item pairing.

    ``reference``/``arms[arm]`` map board -> {key: result}. Items are frozen by
    the human's keys; a key the arm lacks counts as ``na`` with reason
    ``missing``. Output rows are sorted by rule id.
    """
    rules = sorted({r['rule'] for board in reference.values() for r in board.values()})
    out = {}
    for rule in rules:
        rows = dict(human=dict.fromkeys(OUTCOMES, 0), arms={})
        items = [(b, k) for b in sorted(reference) for k in sorted(reference[b]) if reference[b][k]['rule'] == rule]
        for b, k in items:
            rows['human'][reference[b][k]['outcome']] += 1
        for arm, boards in arms.items():
            counts = dict.fromkeys(OUTCOMES, 0)
            transitions = dict(fixed=0, broke=0, both_fail=0)
            for b, k in items:
                outcome = boards.get(b, {}).get(k, {}).get('outcome', 'na')
                counts[outcome] += 1
                human = reference[b][k]['outcome']
                transitions['fixed'] += human == 'fail' and outcome == 'pass'
                transitions['broke'] += human == 'pass' and outcome == 'fail'
                transitions['both_fail'] += human == 'fail' and outcome == 'fail'
            rows['arms'][arm] = dict(counts, **transitions)
        first = next((reference[b][k] for b, k in items), {})
        rows.update(comparator=first.get('comparator'), limit=first.get('limit'), treatment=first.get('treatment'))
        out[rule] = rows
    return out


def markdown(summary, arm_names):
    """Rule table: pass/fail/boundary/n.a. per side, plus fixed/broke versus human."""
    if not summary:
        return ''
    lines = ['## Layout rule profile', '',
             '| Rule | Limit | Human pass/fail/boundary/n.a. | '+' | '.join(f'{a} pass/fail/boundary/n.a. (fixed, broke)' for a in arm_names)+' |',
             '|---|---|---|'+'---|'*len(arm_names)]
    fmt = lambda c: f"{c['pass']}/{c['fail']}/{c['boundary']}/{c['na']}"
    for rule, row in summary.items():
        cells = [fmt(row['arms'][a])+f" ({row['arms'][a]['fixed']}, {row['arms'][a]['broke']})" for a in arm_names]
        tag = '' if row['treatment'] == 'rule' else ' (report)'
        lines.append(f"| {rule}{tag} | {row['comparator']} {row['limit']} | {fmt(row['human'])} | "+' | '.join(cells)+' |')
    return '\n'.join(lines)+'\n'
