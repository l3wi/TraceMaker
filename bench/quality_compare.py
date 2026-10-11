# SPDX-License-Identifier: GPL-3.0-or-later
"""Board-cluster comparisons; human parity is success, not a quality ceiling."""
import hashlib
import statistics


class CounterRng:
    """Seeded SHA-256 counter draws: reproducible, independent of Python's RNG."""
    def __init__(self, seed):
        self.seed = str(seed).encode()+b':'
        self.counter = 0

    def index(self, count):
        limit = (1 << 64)-((1 << 64) % count)
        while True:
            value = int.from_bytes(hashlib.sha256(self.seed+self.counter.to_bytes(8,'little')).digest()[:8],'little')
            self.counter += 1
            if value < limit:
                return value % count

# Practical equivalence bands, not electrical certification limits.
METRICS = {
    'decoupling': {'effective_mm': (False, 1.0), 'return_lead_mm': (False, 1.0)},
    'differential_pairs': {'coupled_fraction': (True, 0.001), 'skew_mm': (False, 0.01), 'via_imbalance': (False, 0),
                           'segment_skew_mm': (False, 0.01), 'via_layer_mismatch': (False, 0)},
    'return_paths': {'gap_fraction': (False, 0.001), 'no_return_1mm': (False, 0), 'no_return_5_08mm': (False, 0),
                     'wrong_reference_mm': (False, 0.01), 'floating_reference_mm': (False, 0.01)},
    'power': {'median_width_mm': (True, 0.000001), 'resistance_35um_ohm': (False, 0.000001), 'neck_fraction': (False, 0.001)},
    'thermal': {'via_count': (True, 0)},
    'pad_entry': {'off_axis': (False, 0), 'corner': (False, 0), 'wider_than_face': (False, 0)},
    'routing_craft': {'acute_junctions': (False, 0)},
    'efficiency': {'vias_per_connection': (False, 0.000001), 'mm_per_connection': (False, 0.01)},
}


def comparison(human, router, higher, epsilon):
    if router is None:
        return 'loss', None
    delta = human - router if higher else router - human
    return ('win' if delta < -epsilon else 'loss' if delta > epsilon else 'tie'), delta


def summarize(reference, arms):
    """Keep all obligations and a frozen all-arm connected/assessable intersection."""
    output = {}
    for category, metrics in METRICS.items():
        output[category] = {}
        for metric, (higher, epsilon) in metrics.items():
            boards = sorted(reference)
            common = {}
            for board in boards:
                hh = reference[board].get(category, {})
                common[board] = {key for key, item in hh.items()
                                 if item.get(metric) is not None and item.get('connected', True)
                                 and all(key in aa.get(board, {}).get(category, {})
                                         and aa[board][category][key].get(metric) is not None
                                         and aa[board][category][key].get('connected', True)
                                         for aa in arms.values())}
            rows = {}
            for arm, aa in arms.items():
                views = {}
                for view in ('all_obligations', 'common_connected'):
                    per_board, worst = {}, []
                    for board in boards:
                        hh = reference[board].get(category, {})
                        rr = aa.get(board, {}).get(category, {})
                        counts = dict(win=0, tie=0, loss=0, missing=0, not_assessable=0, eligible=0)
                        deltas = []
                        for key, h in sorted(hh.items()):
                            if h.get(metric) is None or not h.get('connected', True):
                                counts['not_assessable'] += 1
                                continue
                            if view == 'common_connected' and key not in common[board]:
                                counts['not_assessable'] += 1
                                continue
                            r = rr.get(key, {})
                            value = r.get(metric)
                            if 'connected' in r and r['connected'] is None:
                                counts['not_assessable'] += 1
                                continue
                            if not r.get('connected', False):
                                value = None
                                counts['missing'] += 1
                            elif value is None:
                                # Connected, but model input absent: coverage hole, not zero defects.
                                counts['not_assessable'] += 1
                                continue
                            state, delta = comparison(h[metric], value, higher, epsilon)
                            counts[state] += 1
                            counts['eligible'] += 1
                            if delta is not None:
                                deltas.append(delta)
                            if state == 'loss':
                                worst.append(dict(board=board, item=key, human=h[metric], router=value,
                                                  degradation=delta, missing=value is None))
                        counts['parity_pct'] = 100 * (counts['win'] + counts['tie']) / counts['eligible'] if counts['eligible'] else None
                        counts['intersection'] = len(common[board])
                        dd = sorted(deltas)
                        counts['signed_degradation'] = dict(median=statistics.median(dd) if dd else None,
                                                           p90=dd[int(.9*(len(dd)-1))] if dd else None,
                                                           maximum=max(dd) if dd else None)
                        per_board[board] = counts
                    sums = {key: sum(v[key] for v in per_board.values()) for key in ('win','tie','loss','missing','not_assessable','eligible')}
                    macro = [v['parity_pct'] for v in per_board.values() if v['parity_pct'] is not None]
                    sums.update(macro_parity_pct=statistics.mean(macro) if macro else None,
                                pooled_parity_pct=100*(sums['win']+sums['tie'])/sums['eligible'] if sums['eligible'] else None,
                                category_boards=len(macro), intersection=sum(len(v) for v in common.values()),
                                per_board=per_board,
                                worst=sorted(worst, key=lambda v: (not v['missing'], -(v['degradation'] or 0), v['board'], v['item']))[:10])
                    views[view] = sums
                rows[arm] = views
            differences = []
            names = list(arms)
            for i, a in enumerate(names):
                for b in names[i+1:]:
                    pa = rows[a]['common_connected']['per_board']
                    pb = rows[b]['common_connected']['per_board']
                    ds = [pb[k]['parity_pct'] - pa[k]['parity_pct'] for k in boards
                          if pa[k]['parity_pct'] is not None and pb[k]['parity_pct'] is not None]
                    rng = CounterRng(107)
                    samples = sorted(statistics.mean(ds[rng.index(len(ds))] for _ in ds) for _ in range(2000)) if ds else []
                    differences.append(dict(arm_a=a, arm_b=b, boards=len(ds), delta_pp=statistics.mean(ds) if ds else None,
                                            ci95_pp=[samples[49], samples[1949]] if samples else None,
                                            board_wins=sum(d>0 for d in ds), board_ties=sum(d==0 for d in ds), board_losses=sum(d<0 for d in ds)))
                    differences[-1].update(seed=107, resamples=2000, rng='sha256-counter')
            output[category][metric] = dict(higher_is_better=higher, epsilon=epsilon, arms=rows, paired_differences=differences)
    return output


def noise(reference, original):
    result = {}
    for category, metrics in METRICS.items():
        result[category] = {}
        for metric in metrics:
            deltas, self_deltas, unknown = [], [], 0
            for board, categories in reference.items():
                for key, h in categories.get(category, {}).items():
                    value = h.get(metric)
                    if value is not None:
                        self_deltas.append(abs(value-reference[board][category][key][metric]))
                    other = original.get(board, {}).get(category, {}).get(key, {}).get(metric)
                    if value is None or other is None:
                        unknown += 1
                    else:
                        deltas.append(abs(value-other))
            result[category][metric] = dict(self_disagreement=max(self_deltas) if self_deltas else None,
                                            original_vs_refilled_items=len(deltas), not_assessable=unknown,
                                            original_vs_refilled_changed=sum(d>1e-9 for d in deltas),
                                            original_vs_refilled_max_abs=max(deltas) if deltas else None,
                                            original_vs_refilled_mean_abs=statistics.mean(deltas) if deltas else None)
    return result
