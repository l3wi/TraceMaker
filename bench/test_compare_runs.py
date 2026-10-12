# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic stop-policy, coverage and provenance gates; no router or KiCad required."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / 'compare_runs.py'
SETS = SCRIPT.parent / 'sets'


def run_dir(root, name, boards, summary=None, manifest=None, auto_final_pair=True):
    d = root / name
    (d / 'boards').mkdir(parents=True)
    rows = []
    for board, specification in boards.items():
        if isinstance(specification, dict):
            row = dict(board=board, added_errors={}, seconds=1.0)
            row.update(specification)
            stop = row.pop('route_stop', None)
        else:
            completion, stop, added = specification
            row = dict(board=board, clean=completion == 1.0 and not added, completion=completion,
                       added_errors=added, routed=1, connections=1, seconds=1.0)
        row.setdefault('judge', 'ok' if 'clean' in row else 'failed')
        rows.append(json.dumps(row))
        (d / 'boards' / f'{board}.kicad_pcb').write_text(name)
        if stop is not None:
            (d / 'boards' / f'{board}.kicad_pcb.route.json').write_text(json.dumps(dict(stop=stop)))
    (d / 'boards.jsonl').write_text('\n'.join(rows) + '\n')
    # Normal final fixtures publish the same two metadata files as run.py. Tests can disable
    # pairing to exercise an interrupted publication, and explicit bad counters stay untouched.
    summary = dict(summary) if summary is not None else None
    manifest = dict(manifest) if manifest is not None else None
    if auto_final_pair:
        if summary is not None and summary.get('state') == 'final' and manifest is None:
            manifest = dict(state='final', roster=summary.get('roster', list(boards)))
        if manifest is not None and manifest.get('state') == 'final' and summary is None:
            summary = dict(state='final', roster=manifest.get('roster', list(boards)))
    judged = sum(json.loads(row).get('judge') == 'ok' for row in rows)
    for metadata in (summary, manifest):
        if metadata is not None and metadata.get('state') == 'final':
            scheduled = len(metadata.get('roster', boards))
            for key, value in dict(scheduled=scheduled, attempted=len(rows), judged=judged,
                                   judge_failures=scheduled - judged).items():
                metadata.setdefault(key, value)
    if summary is not None:
        (d / 'summary.json').write_text(json.dumps(summary))
    if manifest is not None:
        (d / 'manifest.json').write_text(json.dumps(manifest))
    return d


def gate(before, after, *, before_summary=None, after_summary=None,
         before_manifest=None, after_manifest=None, args=(), identical_output=False, auto_final_pair=True):
    with tempfile.TemporaryDirectory() as t:
        root = pathlib.Path(t)
        b = run_dir(root, 'before', before, before_summary, before_manifest, auto_final_pair)
        a = run_dir(root, 'after', after, after_summary, after_manifest, auto_final_pair)
        if identical_output:
            for board in before.keys() & after.keys():
                filename = f'{board}.kicad_pcb'
                (a / 'boards' / filename).write_bytes((b / 'boards' / filename).read_bytes())
        p = subprocess.run([sys.executable, str(SCRIPT), str(b), str(a), *args], capture_output=True, text=True)
        return p.returncode, p.stdout


def provenance_summary(names=('x',)):
    return dict(state='final', roster=list(names), selection_mode='boards', full=False,
                provenance=dict(metric_schema_version=2, kicad_version='10.0.6',
                                judge_flags=['--format', 'json', '--all-track-errors'],
                                inputs={name: dict(board_sha256='board-hash', rules={'.kicad_pro': 'rules-hash'},
                                                   manifest_sha256='manifest-hash') for name in names}))


class GateTests(unittest.TestCase):
    def test_budget_stopped_board_is_listed_not_gated(self):
        code, out = gate({'x': (0.9, 'work', {})}, {'x': (0.8, 'work', {})})
        self.assertEqual(code, 0)
        self.assertIn('x: completion 90.0% -> 80.0% (stop work -> work; not gated)', out)

    def test_equal_or_improved_identical_work_outputs_are_still_listed_as_censored(self):
        for completion in (0.9, 1.0):
            with self.subTest(completion=completion):
                code, out = gate({'x': (0.9, 'work', {})}, {'x': (completion, 'work', {})},
                                 identical_output=True)
                self.assertEqual(code, 0)
                self.assertIn('routed boards byte-identical: 1/1', out)
                self.assertIn('Work-budget-stopped observations', out)
                self.assertIn(f'x: completion 90.0% -> {completion:.1%} (stop work -> work; not gated)', out)

    def test_finished_board_that_gets_worse_fails(self):
        code, out = gate({'x': (0.9, 'work', {})}, {'x': (0.8, 'complete', {})})
        self.assertEqual(code, 1)
        self.assertIn('x: worse', out)

    def test_board_that_stops_finishing_fails_even_if_better(self):
        for stop in ('complete', 'exhausted'):
            with self.subTest(stop=stop):
                code, out = gate({'x': (0.8, stop, {})}, {'x': (0.9, 'work', {})})
                self.assertEqual(code, 1)
                self.assertIn(f'x: no longer finishes (stop {stop} -> work)', out)

    def test_complete_to_exhausted_remains_finished(self):
        code, out = gate({'x': (0.8, 'complete', {})}, {'x': (0.9, 'exhausted', {})})
        self.assertEqual(code, 0)
        self.assertNotIn('no longer finishes', out)

    def test_exhausted_regression_is_gated(self):
        code, out = gate({'x': (0.9, 'work', {})}, {'x': (0.8, 'exhausted', {})})
        self.assertEqual(code, 1)
        self.assertIn('x: worse', out)
        self.assertNotIn('not gated)', out)

    def test_time_and_deadline_fail_on_either_side_even_if_improved(self):
        for stop in ('time', 'deadline'):
            for side in ('before', 'after'):
                with self.subTest(stop=stop, side=side):
                    bstop, astop = (stop, 'complete') if side == 'before' else ('work', stop)
                    code, out = gate({'x': (0.8, bstop, {})}, {'x': (0.9, astop, {})})
                    self.assertEqual(code, 1)
                    self.assertIn(f'{side} stopped by {stop}', out)

    def test_time_outcomes_are_invalid_not_quality_regressions(self):
        for stop in ('time', 'deadline'):
            for side in ('before', 'after'):
                with self.subTest(stop=stop, side=side):
                    bstop, astop = (stop, 'complete') if side == 'before' else ('complete', stop)
                    code, out = gate({'x': (1.0, bstop, {})}, {'x': (0.8, astop, {})})
                    self.assertEqual(code, 1)
                    self.assertIn(f'{side} stopped by {stop}', out)
                    self.assertNotIn(': worse (', out)
                    self.assertNotIn('no longer finishes', out)

    def test_invalid_time_outcomes_still_gate_added_errors(self):
        code, out = gate({'x': (0.9, 'complete', {})}, {'x': (0.8, 'time', {'clearance': 1})})
        self.assertEqual(code, 1)
        self.assertIn('after stopped by time', out)
        self.assertIn('added DRC errors', out)
        self.assertNotIn(': worse (', out)

    def test_added_errors_gated_on_unfinished_boards_not_clean_before(self):
        code, out = gate({'x': (0.9, 'work', {})}, {'x': (0.9, 'work', {'clearance': 1})})
        self.assertEqual(code, 1)
        self.assertIn('added DRC errors', out)

    def test_added_errors_gated_when_clean_before(self):
        code, out = gate({'x': (1.0, 'complete', {})}, {'x': (1.0, 'work', {'clearance': 1})})
        self.assertEqual(code, 1)
        self.assertIn('added DRC errors', out)

    def test_runs_without_or_with_unknown_stop_reason_keep_the_old_gate(self):
        for stop in (None, 'unknown', 'future-stop'):
            with self.subTest(stop=stop):
                code, out = gate({'x': (0.9, stop, {})}, {'x': (0.8, stop, {})})
                self.assertEqual(code, 1)
                self.assertIn('x: worse', out)
                self.assertNotIn('not gated)', out)

    def test_row_stop_takes_precedence_over_route_json(self):
        code, out = gate({'x': dict(clean=False, completion=0.9, stop='work')},
                         {'x': dict(clean=False, completion=0.8, stop='exhausted', route_stop='work')})
        self.assertEqual(code, 1)
        self.assertIn('x: worse', out)

    def test_unknown_baseline_to_work_is_censored_not_finished(self):
        code, out = gate({'x': (0.9, None, {})}, {'x': (0.8, 'work', {})})
        self.assertEqual(code, 0)
        self.assertIn('not gated', out)

    def test_legacy_completion_recomputed_from_counts(self):
        code, out = gate({'x': dict(clean=False, completion=0.1, unconnected_before=10, unconnected_after=1)},
                         {'x': dict(clean=False, completion=0.9, unconnected_before=10, unconnected_after=2)})
        self.assertEqual(code, 1)
        self.assertIn('completion 90.0% -> 80.0%', out)

    def test_saturated_counts_are_unknown_not_numeric_regressions(self):
        for before_count, after_count in ((499, 100), (500, 100), (100, 499), (499, 0)):
            with self.subTest(counts=(before_count, after_count)):
                code, out = gate({'x': (0.9, 'complete', {})},
                                 {'x': dict(clean=False, completion=0.1, stop='complete',
                                            unconnected_before=before_count, unconnected_after=after_count)})
                self.assertEqual(code, 0)
                self.assertIn('mean completion unknown → unknown', out)
                self.assertIn('paired known cohort 0/1; unknown before/after 0/1', out)
                self.assertIn('90.0% → unknown', out)

    def test_unknown_completion_still_gates_clean_regression(self):
        code, out = gate({'x': (1.0, 'complete', {})},
                         {'x': dict(clean=False, completion=None, unconnected_before=499, unconnected_after=20)})
        self.assertEqual(code, 1)
        self.assertIn('clean True -> False', out)

    def test_null_or_marked_censored_completion_is_supported(self):
        for specification in (dict(clean=False, completion=None),
                              dict(clean=False, completion=0.1, judge_list_censored=True),
                              dict(clean=False, completion=0.1, censored=True)):
            with self.subTest(row=specification):
                code, out = gate({'x': (0.9, 'complete', {})}, {'x': specification})
                self.assertEqual(code, 0)
                self.assertIn('unknown before/after 0/1', out)

    def test_mean_uses_same_known_pair_cohort(self):
        code, out = gate({'x': (0.9, None, {}), 'y': (0.6, None, {})},
                         {'x': dict(clean=False, completion=None), 'y': (0.6, None, {})})
        self.assertEqual(code, 0)
        self.assertIn('mean completion 60.0% → 60.0%', out)
        self.assertIn('paired known cohort 1/2', out)

    def test_human_reference_is_not_clean_completion_or_finish_regression(self):
        code, out = gate({'x': (1.0, 'complete', {})}, {'x': (0.8, 'work', {})}, before_summary=dict(human=True))
        self.assertEqual(code, 0)
        self.assertIn('Human baseline: reference comparison, not a regression gate', out)
        self.assertNotIn('x: worse', out)
        self.assertNotIn('no longer finishes', out)

    def test_human_reference_still_gates_added_errors(self):
        code, out = gate({'x': (1.0, None, {})}, {'x': (0.8, 'work', {'clearance': 1})},
                         before_summary=dict(human=True))
        self.assertEqual(code, 1)
        self.assertIn('added DRC errors', out)

    def test_missing_judge_is_failure(self):
        code, out = gate({'x': (0.9, None, {})}, {'x': dict(judge='failed')})
        self.assertEqual(code, 1)
        self.assertIn('judge failed (after)', out)

    def test_cpu_and_style_stay_informational(self):
        code, out = gate({'x': dict(clean=True, completion=1, cpu_s=1, style=dict(acute_angles=0))},
                         {'x': dict(clean=True, completion=1, cpu_s=20, style=dict(acute_angles=10))},
                         before_summary=dict(work=100), after_summary=dict(work=100))
        self.assertEqual(code, 0)
        self.assertIn('Route CPU time', out)
        self.assertIn('route CPU time +1900.0% in total', out)
        self.assertIn('Acute angles', out)
        self.assertIn('0 → 10', out)

    def test_identical_output_gate_is_preserved(self):
        code, out = gate({'x': (1, 'complete', {})}, {'x': (1, 'complete', {})}, args=('--expect-identical',))
        self.assertEqual(code, 1)
        self.assertIn('routed board differs', out)


class MetadataTests(unittest.TestCase):
    before = {'x': (0.9, 'complete', {})}
    after = {'x': (0.9, 'complete', {})}

    def test_compatible_provenance_passes(self):
        summary = provenance_summary()
        code, out = gate(self.before, self.after, before_summary=summary, after_summary=summary)
        self.assertEqual(code, 0)
        self.assertNotIn('legacy provenance', out)

    def test_each_provenance_dependency_mismatch_fails(self):
        for key in ('metric_schema_version', 'kicad_version', 'judge_flags',
                    'board_sha256', 'rules', 'manifest_sha256'):
            with self.subTest(key=key):
                before, after = provenance_summary(), provenance_summary()
                p = after['provenance']
                target = p['inputs']['x'] if key in ('board_sha256', 'rules', 'manifest_sha256') else p
                target[key] = {'changed': 'sha'} if key == 'rules' else ['changed'] if key == 'judge_flags' else 'changed'
                code, out = gate(self.before, self.after, before_summary=before, after_summary=after)
                self.assertEqual(code, 1)
                self.assertIn(f'provenance {key} differs', out)
                self.assertIn('Comparison refused', out)

    def test_rules_map_order_does_not_make_inputs_incompatible(self):
        before, after = provenance_summary(), provenance_summary()
        before['provenance']['inputs']['x']['rules'] = {'pro': 'a', 'dru': 'b'}
        after['provenance']['inputs']['x']['rules'] = {'dru': 'b', 'pro': 'a'}
        code, _ = gate(self.before, self.after, before_summary=before, after_summary=after)
        self.assertEqual(code, 0)

    def test_missing_new_input_provenance_fails(self):
        before, after = provenance_summary(), provenance_summary()
        after['provenance']['inputs'].pop('x')
        code, out = gate(self.before, self.after, before_summary=before, after_summary=after)
        self.assertEqual(code, 1)
        self.assertIn('provenance input is missing', out)

    def test_override_allows_provenance_mismatch_but_says_so(self):
        before, after = provenance_summary(), provenance_summary()
        after['provenance']['kicad_version'] = '10.1'
        code, out = gate(self.before, self.after, before_summary=before, after_summary=after,
                         args=('--allow-incompatible',))
        self.assertEqual(code, 0)
        self.assertIn('explicitly overridden (--allow-incompatible)', out)
        self.assertIn('kicad_version differs', out)

    def test_override_does_not_suppress_quality_gates(self):
        before, after = provenance_summary(), provenance_summary()
        after['provenance']['metric_schema_version'] = 3
        code, out = gate(self.before, {'x': (0.8, 'complete', {})}, before_summary=before,
                         after_summary=after, args=('--allow-incompatible',))
        self.assertEqual(code, 1)
        self.assertIn('x: worse', out)

    def test_missing_legacy_provenance_is_not_a_crash_or_gate_failure(self):
        for after_summary in (None, provenance_summary()):
            with self.subTest(summary=after_summary):
                code, out = gate(self.before, self.after, after_summary=after_summary)
                self.assertEqual(code, 0)
                self.assertIn('before: legacy provenance absent', out)

    def test_nonfinal_manifest_refused_even_with_final_summary_or_override(self):
        for state in ('running', 'failed', None):
            with self.subTest(state=state):
                manifest = dict(roster=['x'])
                if state is not None:
                    manifest['state'] = state
                code, out = gate(self.before, self.after, after_summary=provenance_summary(),
                                 after_manifest=manifest, args=('--allow-incompatible',))
                self.assertEqual(code, 1)
                self.assertIn('manifest is not final', out)

    def test_running_summary_refused_without_manifest(self):
        code, out = gate(self.before, self.after, after_summary=dict(state='running', roster=['x']))
        self.assertEqual(code, 1)
        self.assertIn('summary is not final', out)

    def test_final_manifest_and_summary_pass(self):
        summary, manifest = provenance_summary(), dict(state='final', roster=['x'])
        code, _ = gate(self.before, self.after, before_summary=summary, after_summary=summary,
                       before_manifest=manifest, after_manifest=manifest)
        self.assertEqual(code, 0)

    def test_final_summary_without_manifest_is_not_legacy(self):
        code, out = gate(self.before, self.after, after_summary=provenance_summary(), auto_final_pair=False)
        self.assertEqual(code, 1)
        self.assertIn('manifest is not final (state missing)', out)

    def test_final_manifest_without_summary_is_not_legacy(self):
        code, out = gate(self.before, self.after, after_manifest=dict(state='final', roster=['x']), auto_final_pair=False)
        self.assertEqual(code, 1)
        self.assertIn('summary is not final (state missing)', out)

    def test_final_coverage_counters_must_match_rows_and_each_other(self):
        for source in ('summary', 'manifest'):
            for key in ('scheduled', 'attempted', 'judged', 'judge_failures'):
                with self.subTest(source=source, key=key):
                    summary, manifest = provenance_summary(), dict(state='final', roster=['x'])
                    (summary if source == 'summary' else manifest)[key] = 99
                    code, out = gate(self.before, self.after, after_summary=summary, after_manifest=manifest)
                    self.assertEqual(code, 1)
                    self.assertIn(f'{source} {key} inconsistent with roster/results', out)

    def test_final_metadata_requires_nonempty_rosters(self):
        code, out = gate({}, {}, after_summary=dict(state='final', roster=[]))
        self.assertEqual(code, 1)
        self.assertIn('summary roster is empty', out)

    def test_legacy_stateless_manifest_remains_supported(self):
        code, out = gate(self.before, self.after, after_manifest=dict(roster=['x']))
        self.assertEqual(code, 0)
        self.assertIn('legacy provenance absent', out)

    def test_missing_declared_result_fails_even_when_both_runs_lost_it(self):
        for source in ('summary', 'manifest'):
            with self.subTest(source=source):
                metadata = dict(state='final', roster=['x', 'lost'])
                kwargs = {f'before_{source}': metadata, f'after_{source}': metadata}
                code, out = gate(self.before, self.after, **kwargs)
                self.assertEqual(code, 1)
                self.assertIn('missing declared results', out)
                self.assertIn('lost', out)

    def test_results_outside_roster_fail(self):
        code, out = gate(self.before, self.after, after_summary=dict(roster=[]))
        self.assertEqual(code, 1)
        self.assertIn('results outside declared roster', out)

    def test_missing_board_and_no_common_boards_fail(self):
        for after in ({}, {'other': (0.9, 'complete', {})}):
            with self.subTest(after=after):
                code, out = gate(self.before, after)
                self.assertEqual(code, 1)
                self.assertIn('no board is in both runs', out)


class SelectionTests(unittest.TestCase):
    def set_rosters(self):
        lines = (SETS / 'mid.txt').read_text().splitlines()
        slow = {line.split()[1] for line in lines if line.startswith('#slow ')}
        everyday = {line.strip() for line in lines if line.strip() and not line.startswith('#')}
        row = (0.9, 'complete', {})
        return {name: row for name in everyday}, {name: row for name in everyday | slow}

    def test_exact_known_slow_difference_compares_common_cohort_in_both_directions(self):
        everyday, full = self.set_rosters()
        es = dict(set='mid', selection_mode='everyday', full=False, roster=list(everyday))
        fs = dict(set='mid', selection_mode='full', full=True, roster=list(full))
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                before, after, bs, ass = (full, everyday, fs, es) if reverse else (everyday, full, es, fs)
                code, out = gate(before, after, before_summary=bs, after_summary=ass)
                self.assertEqual(code, 0)
                self.assertIn("exactly the set's #slow boards", out)
                self.assertIn('common cohort only', out)

    def test_legacy_full_flag_also_identifies_exact_slow_difference(self):
        everyday, full = self.set_rosters()
        code, out = gate(everyday, full, before_summary=dict(set='mid', full=False),
                         after_summary=dict(set='mid', full=True))
        self.assertEqual(code, 0)
        self.assertIn('common cohort only', out)

    def test_legacy_exact_full_roster_without_selection_metadata_is_inferred_and_labelled(self):
        everyday, full = self.set_rosters()
        code, out = gate(full, everyday, before_summary=dict(set='mid'),
                         after_summary=dict(set='mid', full=False))
        self.assertEqual(code, 0)
        self.assertIn('legacy selection inferred from exact known set roster', out)
        self.assertIn('common cohort only', out)

    def test_legacy_partial_roster_cannot_infer_selection(self):
        everyday, full = self.set_rosters()
        missing = next(iter(everyday))
        everyday.pop(missing)
        full.pop(missing)
        code, out = gate(full, everyday, before_summary=dict(set='mid'),
                         after_summary=dict(set='mid', full=False))
        self.assertEqual(code, 1)
        self.assertIn('not an exact everyday/full', out)
        self.assertNotIn('legacy selection inferred', out)

    def test_other_missing_board_fails(self):
        everyday, full = self.set_rosters()
        full.pop(next(iter(everyday)))
        code, out = gate(everyday, full, before_summary=dict(set='mid', selection_mode='everyday'),
                         after_summary=dict(set='mid', selection_mode='full'))
        self.assertEqual(code, 1)
        self.assertIn('not an exact everyday/full', out)

    def test_partial_slow_difference_fails(self):
        everyday, full = self.set_rosters()
        full.pop(next(iter(full.keys() - everyday.keys())))
        code, out = gate(everyday, full, before_summary=dict(set='mid', full=False),
                         after_summary=dict(set='mid', full=True))
        self.assertEqual(code, 1)
        self.assertIn('not an exact everyday/full', out)

    def test_explicit_board_or_sample_selection_cannot_claim_full_exemption(self):
        everyday, full = self.set_rosters()
        for mode in ('boards', 'sample'):
            with self.subTest(mode=mode):
                code, out = gate(everyday, full,
                                 before_summary=dict(set='mid', selection_mode=mode, full=False),
                                 after_summary=dict(set='mid', selection_mode='full', full=True))
                self.assertEqual(code, 1)
                self.assertIn('not an exact everyday/full', out)

    def test_declared_missing_slow_result_is_not_selection_exemption(self):
        everyday, full = self.set_rosters()
        code, out = gate(everyday, everyday, before_summary=dict(set='mid', selection_mode='everyday', roster=list(everyday)),
                         after_summary=dict(set='mid', selection_mode='full', roster=list(full)))
        self.assertEqual(code, 1)
        self.assertIn('missing declared results', out)

    def test_different_named_sets_do_not_allow_slow_exemption(self):
        everyday, full = self.set_rosters()
        code, out = gate(everyday, full, before_summary=dict(set='mid', full=False),
                         after_summary=dict(set='modern-planes', full=True))
        self.assertEqual(code, 1)
        self.assertIn('not an exact everyday/full', out)


if __name__ == '__main__':
    unittest.main()
