# SPDX-License-Identifier: GPL-3.0-or-later
"""Single stdlib-only KiCad violation identity and connectivity metric contract."""
from collections import Counter
import json
import re

METRIC_SCHEMA_VERSION = 2
JUDGE_CODE_VERSION = 'instance-v2'
JUDGE_FLAGS = ['--format', 'json', '--severity-all', '--all-track-errors']


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


def added_errors(before, after):
    """A16 gates copper-involving errors; all other new instances stay visible."""
    added = violations(after) - violations(before)
    return dict(added_errors=instance_counts(added, routing=True),
                added_nonrouting=instance_counts(added, routing=False),
                added_any=instance_counts(added))


def completion(before, after):
    censored = before >= 499 or after >= 499
    return dict(censored=censored, judge_list_censored=censored,
                completion=None if censored else (1.0 if before == 0 else max(0, 1-after/before)))


def bracket_nets(text):
    return re.findall(r'\[([^\]]+)\]', text)


def judge(before, after):
    ub, ua = len(before.get('unconnected_items', [])), len(after.get('unconnected_items', []))
    added = violations(after) - violations(before)
    censored = ub >= 499 or ua >= 499
    return dict(judge='KiCad', common_denominator=ub, unconnected=ua, censored=censored,
                judge_list_censored=censored,
                completion=None if censored or ub == 0 else max(0, 1-ua/ub), already_connected=ub == 0,
                electrical_pass=ua == 0, added_errors=instance_counts(added), drc_pass=not added,
                denominator_method='uncapped KiCad unconnected-list ratio; unknown at 499 saturation',
                engine_denominator_used=False,
                unconnected_nets=sorted({net for v in after.get('unconnected_items', [])
                                        for item in v.get('items', [])
                                        for net in bracket_nets(item.get('description', ''))}))
