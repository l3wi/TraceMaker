# SPDX-License-Identifier: GPL-3.0-or-later
"""Standard-library-only regressions for dashboard, tool selection and fixture fetching."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard = load_module("support_dashboard", ROOT / "devsite/server.py")
check_env = load_module("support_check_env", ROOT / "bench/check_env.py")
bench_runner = load_module("support_bench_runner", ROOT / "tests/run_bench_unittests.py")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.root_patch = patch.object(dashboard, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def publish(self, name="run", *, state="final", roster=None, attempted=None, judged=None):
        roster = ["one"] if roster is None else roster
        attempted = len(roster) if attempted is None else attempted
        judged = attempted if judged is None else judged
        record = dict(state=state, roster=roster, scheduled=len(roster), attempted=attempted,
                      judged=judged, judge_failures=attempted - judged, boards=judged,
                      set="modern-planes", clean_pass=judged / len(roster) if roster else None)
        directory = self.root / "bench/results" / name
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps(record))
        (directory / "summary.json").write_text(json.dumps(record))
        return directory, record

    def test_final_small_roster_is_visible(self):
        self.publish()
        runs = dashboard.benchmarks()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["scheduled"], 1)
        self.assertEqual(runs[0]["run"], "run")
        self.assertNotIn("state_note", runs[0])

    def test_running_manifest_hides_stale_final_summary(self):
        directory, record = self.publish(roster=[f"board-{i}" for i in range(30)])
        record["state"] = "running"
        (directory / "manifest.json").write_text(json.dumps(record))
        self.assertEqual(dashboard.benchmarks(), [])

    def test_running_summary_hidden_even_with_final_manifest(self):
        directory, record = self.publish()
        record["state"] = "running"
        (directory / "summary.json").write_text(json.dumps(record))
        self.assertEqual(dashboard.benchmarks(), [])

    def test_judge_failure_and_zero_judged_runs_remain_visible(self):
        self.publish("partial-judge", roster=["one", "two"], judged=1)
        self.publish("failed-judge", judged=0)
        runs = {row["run"]: row for row in dashboard.benchmarks()}
        self.assertEqual(set(runs), {"partial-judge", "failed-judge"})
        self.assertEqual(runs["partial-judge"]["scheduled"], 2)
        self.assertEqual(runs["partial-judge"]["attempted"], 2)
        self.assertEqual(runs["partial-judge"]["judged"], 1)
        self.assertEqual(runs["partial-judge"]["judge_failures"], 1)
        self.assertEqual(runs["failed-judge"]["judged"], 0)
        self.assertEqual(runs["failed-judge"]["judge_failures"], 1)

    def test_final_summary_requires_matching_declared_roster(self):
        directory, record = self.publish()
        record["roster"] = ["different"]
        (directory / "summary.json").write_text(json.dumps(record))
        self.assertEqual(dashboard.benchmarks(), [])

    def test_duplicate_roster_is_not_coverage(self):
        self.publish(roster=["one", "one"])
        self.assertEqual(dashboard.benchmarks(), [])

    def test_final_summary_without_manifest_is_not_legacy(self):
        directory, _ = self.publish()
        (directory / "manifest.json").unlink()
        self.assertEqual(dashboard.benchmarks(), [])

    def test_invalid_manifest_is_not_legacy(self):
        directory, record = self.publish()
        record.pop("state")
        (directory / "summary.json").write_text(json.dumps(record))
        (directory / "manifest.json").write_text("{incomplete")
        self.assertEqual(dashboard.benchmarks(), [])

    def test_counts_must_match_manifest_and_roster(self):
        for key, value in (("scheduled", 2), ("attempted", 0), ("judge_failures", 1)):
            with self.subTest(key=key):
                directory, record = self.publish(name=key)
                record[key] = value
                (directory / "summary.json").write_text(json.dumps(record))
        self.assertEqual(dashboard.benchmarks(), [])

    def test_legacy_summary_is_explicitly_unverified(self):
        directory, record = self.publish()
        (directory / "manifest.json").unlink()
        record.pop("state")
        for key in ("roster", "scheduled", "attempted", "judged"):
            record.pop(key)
        (directory / "summary.json").write_text(json.dumps(record))
        runs = dashboard.benchmarks()
        self.assertEqual(len(runs), 1)
        self.assertIn("Legacy", runs[0]["state_note"])
        self.assertIn("coverage unverified", runs[0]["state_note"])
        self.assertNotIn("scheduled", runs[0])


class EnvironmentTests(unittest.TestCase):
    def test_binary_default_is_release_not_arbitrary_build(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(check_env, "ROOT", pathlib.Path("/repo")):
            self.assertEqual(check_env.engine_binary(), pathlib.Path("/repo/build/release/src/app/tracemaker"))

    def test_binary_override_preserves_absolute_and_relative_paths(self):
        for value in ("/outside/custom/tracemaker", "build/cpu-only/src/app/tracemaker"):
            with self.subTest(value=value), patch.dict(os.environ, {"TM_BINARY": value}, clear=True):
                self.assertEqual(check_env.engine_binary(), pathlib.Path(value))

    def check(self, root):
        with patch.object(check_env, "ROOT", root), patch.object(check_env, "FREEROUTING_CANDIDATES", []), \
                patch.object(check_env.shutil, "which", return_value=None), \
                patch.object(check_env, "run", return_value=(0, "10.0.6")) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = check_env.main()
        return result, run.call_args_list, output.getvalue()

    def test_missing_release_does_not_probe_other_build(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = pathlib.Path(directory)
            other = root / "build/asan/src/app/tracemaker"
            other.parent.mkdir(parents=True)
            other.touch()
            result, calls, output = self.check(root)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("build/release/src/app/tracemaker", output)

    def test_override_is_the_engine_probed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            binary = root / "custom-engine"
            binary.touch()
            with patch.dict(os.environ, {"TM_BINARY": str(binary)}, clear=True):
                result, calls, output = self.check(root)
        self.assertEqual(result, 0)
        self.assertEqual(calls[1].args[0], [str(binary), "version"])
        self.assertIn(str(binary), output)


class BenchRunnerTests(unittest.TestCase):
    def test_missing_shapely_skips_before_discovery(self):
        error = ModuleNotFoundError("No module named 'shapely'", name="shapely")
        with patch.object(bench_runner.importlib, "import_module", side_effect=error), \
                patch.object(bench_runner.unittest.defaultTestLoader, "discover") as discover, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bench_runner.main(), 77)
        discover.assert_not_called()
        self.assertIn("SKIP", output.getvalue())

    def test_unrelated_import_failure_is_not_skipped(self):
        error = ModuleNotFoundError("No module named 'unexpected_dependency'", name="unexpected_dependency")
        with patch.object(bench_runner.importlib, "import_module", side_effect=error):
            with self.assertRaises(ModuleNotFoundError):
                bench_runner.main()

    def test_discovery_result_controls_exit(self):
        for success in (True, False):
            with self.subTest(success=success), patch.object(bench_runner.importlib, "import_module"), \
                    patch.object(bench_runner.unittest.defaultTestLoader, "discover", return_value="suite") as discover, \
                    patch.object(bench_runner.unittest, "TextTestRunner") as runner:
                runner.return_value.run.return_value.wasSuccessful.return_value = success
                self.assertEqual(bench_runner.main(), 0 if success else 1)
                discover.assert_called_once_with(str(bench_runner.ROOT / "bench"), pattern="test_*.py")
                runner.return_value.run.assert_called_once_with("suite")


@unittest.skipUnless(shutil.which("bash"), "fixture helper requires Bash")
class FixtureCheckoutTests(unittest.TestCase):
    def checkout(self, ref, *, fetch_fails=False):
        # Exercise the shared shell helper with a fake Git, never a repository/network operation.
        script = (ROOT / "scripts/fetch_fixtures.sh").read_text()
        helper = script[script.index("pin_checkout() {"):script.index("# Sets built from the downloads:")]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fixture = root / "data/fixture"
            fixture.mkdir(parents=True)
            log = root / "git.jsonl"
            git = binary_dir / "git"
            git.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['MOCK_GIT_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
command = args[2]
if command == 'rev-parse':
    print('pinned')
elif command == 'fetch' and os.environ.get('MOCK_FETCH_FAIL') == '1':
    sys.exit(9)
elif command == 'checkout':
    pathlib.Path(args[1], 'materialised').write_text(args[-1])
''')
            git.chmod(0o755)
            environment = dict(os.environ, PATH=str(binary_dir) + os.pathsep + os.environ.get("PATH", ""),
                               MOCK_GIT_LOG=str(log), MOCK_FETCH_FAIL="1" if fetch_fails else "0")
            result = subprocess.run([shutil.which("bash"), "-c",
                                     'set -euo pipefail\nDATA="$1"\n' + helper + '\npin_checkout fixture "$2"',
                                     "fixture-test", str(root / "data"), ref],
                                    env=environment, capture_output=True, text=True, timeout=10)
            calls = [json.loads(line)[2:] for line in log.read_text().splitlines()]
            materialised = (fixture / "materialised").read_text() if (fixture / "materialised").exists() else None
            return result.returncode, calls, materialised

    def test_latest_fetches_remote_head_and_checks_out_fetched_commit(self):
        code, calls, materialised = self.checkout("latest")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["fetch", "--quiet", "--depth", "1", "origin", "HEAD"],
                                 ["checkout", "--quiet", "--detach", "FETCH_HEAD"]])
        self.assertEqual(materialised, "FETCH_HEAD")

    def test_pinned_head_still_materialises_no_checkout_tree(self):
        code, calls, materialised = self.checkout("pinned")
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1], ["checkout", "--quiet", "--detach", "pinned"])
        self.assertFalse(any(call[0] == "fetch" for call in calls))
        self.assertEqual(materialised, "pinned")

    def test_different_pin_checks_out_fetched_commit(self):
        code, calls, materialised = self.checkout("new-pin")
        self.assertEqual(code, 0)
        self.assertEqual(calls[-2], ["fetch", "--quiet", "--depth", "1", "origin", "new-pin"])
        self.assertEqual(materialised, "FETCH_HEAD")

    def test_fetch_failure_is_not_swallowed_or_checked_out(self):
        code, calls, materialised = self.checkout("latest", fetch_fails=True)
        self.assertEqual(code, 9)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(materialised)


if __name__ == "__main__":
    unittest.main()
