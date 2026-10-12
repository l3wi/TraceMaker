# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
import drc_judge as judge
import quality_io


def violation(kind='Track', x=1, uuid='a', severity='error', typ='clearance', ref='R1'):
    return dict(type=typ, severity=severity, items=[dict(uuid=uuid, description=f'{kind} [GND] of {ref} on F.Cu', pos={'x': x, 'y': 2})])


class InstanceJudgeTests(unittest.TestCase):
    def test_equal_type_counts_cannot_cancel_new_instance(self):
        before = {'violations': [violation(x=1)]}
        after = {'violations': [violation(x=2)]}
        self.assertEqual(judge.added_errors(before, after)['added_errors'], {'clearance': 1})

    def test_regenerated_uuid_and_reference_do_not_change_physical_identity(self):
        before = {'violations': [violation(uuid='a', ref='R1')]}
        after = {'violations': [violation(uuid='b', ref='REF123')]}
        self.assertEqual(judge.added_errors(before, after)['added_any'], {})

    def test_multiset_and_nonrouting_errors_are_not_silenced(self):
        v = violation(kind='Pad', typ='starved_thermal')
        result = judge.added_errors({'violations': [v]}, {'violations': [v, v, v]})
        self.assertEqual(result['added_errors'], {})
        self.assertEqual(result['added_nonrouting'], {'starved_thermal': 2})
        self.assertEqual(result['added_any'], {'starved_thermal': 2})

    def test_layers_nets_kind_position_and_fallback_uuid_are_identity(self):
        base = violation()
        for description in ('Via [GND] on F.Cu', 'Track [VCC] on F.Cu', 'Track [GND] on B.Cu'):
            changed = violation()
            changed['items'][0]['description'] = description
            self.assertEqual(judge.added_errors({'violations': [base]}, {'violations': [changed]})['added_errors'], {'clearance': 1})
        a, b = violation(uuid='a'), violation(uuid='b')
        del a['items'][0]['pos']
        del b['items'][0]['pos']
        self.assertNotEqual(judge.violations({'violations': [a]}), judge.violations({'violations': [b]}))

    def test_all_severity_pass_judge_keeps_warnings(self):
        report = {'violations': [violation(severity='warning')]}
        self.assertEqual(judge.violations(report), {})
        self.assertEqual(judge.instance_counts(judge.violations(report, severity=None), severity_labels=True), {'clearance (warning)': 1})

    def test_caps_both_sides_are_unknown(self):
        for before, after in ((499, 0), (498, 499), (500, 1)):
            result = judge.completion(before, after)
            self.assertTrue(result['censored'])
            self.assertIsNone(result['completion'])
        self.assertEqual(judge.completion(100, 20)['completion'], 0.8)

    def test_scorecard_uses_single_judge_definition(self):
        # The scorecard adds cohort policy (missing input, already-connected boards) on top of the
        # shared judge; every field it does not override must come from the one shared definition.
        self.assertIs(quality_io.violations, judge.violations)
        before = dict(unconnected_items=[dict(items=[])]*5, violations=[])
        after = dict(unconnected_items=[dict(items=[])]*2, violations=[])
        self.assertEqual(quality_io.judge(before, after), judge.judge(before, after))
        missing = quality_io.judge(None, after)
        self.assertEqual((missing['completion'], missing['error_gate']), (None, 'unknown'))


if __name__ == '__main__':
    unittest.main()
