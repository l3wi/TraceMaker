# SPDX-License-Identifier: GPL-3.0-or-later
"""Single stdlib-only KiCad violation identity and connectivity metric contract."""
from collections import Counter
import json
import re
from kicad_connectivity import CAPS

METRIC_SCHEMA_VERSION = 4
JUDGE_CODE_VERSION = 'instance-v4-refill-errors'
JUDGE_FLAGS = ['--format', 'json', '--severity-all', '--all-track-errors']

# Routing cannot alter these library, footprint-placement or text-only findings.
# Keep their capped reports visible, but do not pretend they obscure copper errors.
ROUTING_IMMUTABLE_TYPES = frozenset({
    'lib_footprint_issues', 'lib_footprint_mismatch', 'footprint_type_mismatch',
    'duplicate_footprints', 'extra_footprint', 'missing_footprint',
    'courtyards_overlap', 'missing_courtyard', 'malformed_courtyard',
    'text_height', 'text_thickness',
})

# Refill can reveal damage from routed copper without naming the blocking track.
# Do not gate every pad/graphic-only delta: net propagation changes those reports,
# and pad-only solder_mask_bridge is observed refill noise (doc 10, Judge noise).
REFILL_ROUTING_TYPES = frozenset({'starved_thermal', 'isolated_copper'})


def violations(report, *, severity='error'):
    """Physical identities survive regenerated UUIDs and renamed footprint refs."""
    def signature(v):
        items = []
        for item in v.get('items', []):
            description = re.sub(r'\s+of\s+\S+', '', item.get('description', ''))
            position = item.get('pos', {})
            identity = item.get('uuid', '') if not position else ''
            items.append((identity, description, json.dumps(position, sort_keys=True)))
        return (v.get('type'), v.get('severity'), tuple(sorted(items)),
                v.get('description', '') if not items else '')
    return Counter(signature(v) for v in report.get('violations', [])
                   if severity is None or v.get('severity') == severity)


def instance_counts(instances, *, routing=None, severity_labels=False):
    counts = Counter()
    for signature, number in instances.items():
        copper = any(item[1].startswith(('Track', 'Via', 'Arc')) for item in signature[2])
        if routing is not None and copper != routing:
            continue
        label = f'{signature[0]} ({signature[1]})' if severity_labels else signature[0]
        counts[label] += number
    return dict(sorted(counts.items()))


def capped_types(report):
    counts = Counter(v.get('type') for v in report.get('violations', []))
    if len(report.get('unconnected_items', [])) >= CAPS['unconnected_items']:
        counts['unconnected_items'] = len(report['unconnected_items'])
    return {kind: count for kind, count in sorted(counts.items())
            if count >= CAPS.get(kind, CAPS['default'])}


def added_errors(before, after):
    """Gate copper and refill-derived errors; saturated deltas remain unknown."""
    capped = {'before': capped_types(before), 'after': capped_types(after)}
    excluded = set(capped['before']) | set(capped['after'])
    added = Counter({key: n for key, n in (violations(after) - violations(before)).items()
                     if key[0] not in excluded})
    routing = instance_counts({key: n for key, n in added.items()
                               if key[0] in REFILL_ROUTING_TYPES or
                               any(item[1].startswith(('Track', 'Via', 'Arc')) for item in key[2])})
    error_types = {v.get('type') for report in (before, after)
                   for v in report.get('violations', []) if v.get('severity') == 'error'}
    evaluated = (excluded & error_types) - ROUTING_IMMUTABLE_TYPES
    unknown = bool(evaluated)
    return dict(added_errors=routing,
                added_nonrouting=instance_counts(added, routing=False),
                added_any=instance_counts(added), capped=capped, capped_types=sorted(excluded),
                capped_evaluated_types=sorted(evaluated),
                capped_not_evaluated_types=sorted(excluded - evaluated),
                error_gate='unknown' if unknown else 'fail' if routing else 'pass')


def completion(before, after, *, before_connectivity=None, after_connectivity=None):
    list_censored = before >= CAPS['unconnected_items'] or after >= CAPS['unconnected_items']
    for label, listed, connectivity in (('before', before, before_connectivity),
                                        ('after', after, after_connectivity)):
        if connectivity is not None and listed < CAPS['unconnected_items'] and listed != connectivity['unconnected']:
            raise ValueError(f'{label}: uncapped DRC list {listed} != KiCad connectivity {connectivity["unconnected"]}')
    exact = before_connectivity is not None and after_connectivity is not None
    ub = before_connectivity['unconnected'] if exact else before
    ua = after_connectivity['unconnected'] if exact else after
    censored = list_censored and not exact
    result = dict(censored=censored, judge_list_censored=list_censored,
                  completion=None if censored else (1.0 if ub == 0 else max(0, 1-ua/ub)))
    if exact:
        result.update(unconnected_uncapped={'before': ub, 'after': ua},
                      unconnected_per_net={'before': before_connectivity['per_net'],
                                           'after': after_connectivity['per_net']},
                      connectivity_method=after_connectivity['method'],
                      kicad_python_version=after_connectivity['kicad_version'])
    return result


def bracket_nets(text):
    return re.findall(r'\[([^\]]+)\]', text)


def judge(before, after):
    ub, ua = len(before.get('unconnected_items', [])), len(after.get('unconnected_items', []))
    bc, ac = before.get('connectivity'), after.get('connectivity')
    metrics = completion(ub, ua, before_connectivity=bc, after_connectivity=ac)
    additions = added_errors(before, after)
    exact = 'unconnected_uncapped' in metrics
    denominator = bc['unconnected'] if exact else ub
    remaining = ac['unconnected'] if ac is not None else ua
    reasons = {label: report['connectivity_unavailable'] for label, report in (('before', before), ('after', after))
               if report.get('connectivity_unavailable')}
    return dict(judge='KiCad', common_denominator=denominator, unconnected=remaining,
                **metrics, **{key: value for key, value in additions.items() if key != 'added_errors'},
                added_errors=additions['added_any'],
                already_connected=denominator == 0,
                output_unconnected_censored=ua >= CAPS['unconnected_items'] and ac is None,
                electrical_pass=remaining == 0,
                drc_pass=None if additions['error_gate'] == 'unknown' else not additions['added_any'],
                denominator_method='pcbnew-connectivity' if exact else 'KiCad DRC list; unknown at saturation',
                connectivity_unavailable=reasons,
                engine_denominator_used=False,
                unconnected_nets=sorted(ac['per_net']) if ac is not None else
                    sorted({net for v in after.get('unconnected_items', [])
                            for item in v.get('items', [])
                            for net in bracket_nets(item.get('description', ''))}))
