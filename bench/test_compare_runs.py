# SPDX-License-Identifier: GPL-3.0-or-later
"""The "no board worse" gate counts only boards the router finished (doc 10 §5, D129)."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent/'compare_runs.py'


def run_dir(root, name, boards):
    d = root/name
    (d/'boards').mkdir(parents=True)
    rows = []
    for board, (completion, stop, added) in boards.items():
        rows.append(json.dumps(dict(board=board, clean=completion == 1.0 and not added, completion=completion,
                                    added_errors=added, routed=1, connections=1, seconds=1.0)))
        if stop is not None:
            (d/'boards'/f'{board}.kicad_pcb.route.json').write_text(json.dumps(dict(stop=stop)))
    (d/'boards.jsonl').write_text('\n'.join(rows)+'\n')
    return d


def gate(before, after):
    with tempfile.TemporaryDirectory() as t:
        root = pathlib.Path(t)
        b, a = run_dir(root, 'before', before), run_dir(root, 'after', after)
        p = subprocess.run([sys.executable, str(SCRIPT), str(b), str(a)], capture_output=True, text=True)
        return p.returncode, p.stdout


class GateTests(unittest.TestCase):
    def test_budget_stopped_board_is_listed_not_gated(self):
        code, out = gate({'x': (0.9, 'work', {})}, {'x': (0.8, 'work', {})})
        self.assertEqual(code, 0)
        self.assertIn('x: completion 90.0% -> 80.0% (stop work -> work; not gated)', out)

    def test_finished_board_that_gets_worse_fails(self):
        code, out = gate({'x': (0.9, 'work', {})}, {'x': (0.8, 'complete', {})})
        self.assertEqual(code, 1)
        self.assertIn('x: worse', out)

    def test_board_that_stops_finishing_fails_even_if_better(self):
        code, out = gate({'x': (0.8, 'complete', {})}, {'x': (0.9, 'work', {})})
        self.assertEqual(code, 1)
        self.assertIn('x: no longer finishes (stop complete -> work)', out)

    def test_added_errors_gated_on_unfinished_boards(self):
        code, out = gate({'x': (1.0, 'complete', {})}, {'x': (1.0, 'work', {'clearance': 1})})
        self.assertEqual(code, 1)
        self.assertIn('added DRC errors', out)

    def test_runs_without_stop_reason_keep_the_old_gate(self):
        code, out = gate({'x': (0.9, None, {})}, {'x': (0.8, None, {})})
        self.assertEqual(code, 1)
        self.assertIn('x: worse', out)


if __name__ == '__main__':
    unittest.main()
