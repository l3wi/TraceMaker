# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import kicad_connectivity as connectivity


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.interpreter = self.root / 'fake-python'
        self.cli = self.root / 'fake-kicad-cli'
        self.cli.write_text(f'#!{sys.executable}\nprint("10.0.3+release")\n')
        self.cli.chmod(0o755)
        self.env = patch.dict(os.environ, {'KICAD_PYTHON': str(self.interpreter)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake(self, body):
        self.interpreter.write_text(f'#!{sys.executable}\n' + body)
        self.interpreter.chmod(0o755)

    def count(self, boards=None):
        return connectivity.count_unconnected(boards or [Path('a.kicad_pcb')], kicad_cli=str(self.cli))

    def success(self, extra=''):
        self.fake('import json, sys\n'
                  'request=json.loads(sys.argv[3])\n'
                  'rows={key: {"unconnected": 600, "per_net": {"GND": 600}, '
                  '"kicad_version": "10.0.3", "method": "pcbnew-connectivity"} '
                  'for key, path in request["boards"]}\n' + extra +
                  'print(json.dumps({"kicad_version":"10.0.3", "boards":rows}))\n')

    def test_success_batches_original_path_keys_and_uncapped_counts(self):
        log = self.root / 'calls'
        self.success(f'with open({str(log)!r}, "a") as f: f.write("call\\n")\n')
        boards = [Path('a.kicad_pcb'), Path('space dir/b.kicad_pcb')]
        rows = self.count(boards)
        self.assertEqual(set(rows), {str(path) for path in boards})
        self.assertEqual(rows[str(boards[0])]['unconnected'], 600)
        self.assertEqual(rows[str(boards[0])]['per_net'], {'GND': 600})
        self.assertEqual(log.read_text(), 'call\n')
        self.assertEqual(connectivity.kicad_version(str(self.cli)), '10.0.3')

    def test_crash_preserves_diagnostic(self):
        self.fake('import sys\nprint("pcbnew crashed", file=sys.stderr)\nsys.exit(7)\n')
        with self.assertRaisesRegex(connectivity.ConnectivityUnavailable, 'pcbnew crashed'):
            self.count()

    def test_bad_json(self):
        self.fake('print("not JSON")\n')
        with self.assertRaises(connectivity.ConnectivityUnavailable):
            self.count()

    def test_version_mismatch(self):
        self.fake('print(\'{"kicad_version": "10.0.6", "boards": {}}\')\n')
        with self.assertRaisesRegex(connectivity.ConnectivityVersionMismatch, '10.0.6.*10.0.3'):
            self.count()

    def test_invalid_roster_counts_and_provenance(self):
        for extra in ('rows.clear()\n', 'rows["a.kicad_pcb"]["unconnected"] = 599\n',
                      'rows["a.kicad_pcb"]["per_net"] = {"GND": True}\n',
                      'rows["a.kicad_pcb"]["per_net"] = {"GND": 600, "VCC": 0}\n',
                      'rows["a.kicad_pcb"]["method"] = "guess"\n'):
            with self.subTest(extra=extra):
                self.success(extra)
                with self.assertRaises(connectivity.ConnectivityUnavailable):
                    self.count()

    def test_missing_interpreter_and_cli(self):
        self.fake('print("unused")\n')
        with patch.dict(os.environ, {'KICAD_PYTHON': str(self.root / 'missing')}):
            with self.assertRaises(connectivity.ConnectivityUnavailable):
                self.count()
        with self.assertRaises(connectivity.ConnectivityUnavailable):
            connectivity.kicad_version(str(self.root / 'missing-cli'))

    def test_discovery_uses_resolved_bundle_and_override_first(self):
        with patch.dict(os.environ, {'KICAD_PYTHON': ''}):
            contents = self.root / 'KiCad.app/Contents'
            cli = contents / 'MacOS/kicad-cli'
            cli.parent.mkdir(parents=True)
            cli.write_text('')
            cli.chmod(0o755)
            bundled = contents / 'Frameworks/Python.framework/Versions/3.9/bin/python3'
            bundled.parent.mkdir(parents=True)
            bundled.write_text('')
            bundled.chmod(0o755)
            symlink = self.root / 'kicad-cli'
            symlink.symlink_to(cli)
            self.assertEqual(connectivity._interpreter(str(symlink)), str(bundled.resolve()))
        self.assertEqual(connectivity._interpreter(str(symlink)), str(self.interpreter))

    def test_linux_falls_back_to_docker_wrapper(self):
        with patch.dict(os.environ, {'KICAD_PYTHON': ''}), patch.object(connectivity.sys, 'platform', 'linux'):
            self.assertEqual(connectivity._interpreter(str(self.cli)), str(connectivity.ROOT / 'scripts/kicad-python'))

    def test_empty_batch_needs_no_kicad(self):
        self.assertEqual(connectivity.count_unconnected([]), {})

    def test_cli_prints_table(self):
        self.success()
        result = subprocess.run([sys.executable, str(connectivity.ROOT / 'bench/kicad_connectivity.py'),
                                 '--kicad-cli', str(self.cli), 'a.kicad_pcb'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Unconnected', result.stdout)
        self.assertIn('600', result.stdout)
        self.assertIn('pcbnew-connectivity', result.stdout)


class RealConnectivityTests(unittest.TestCase):
    def test_small_saved_fixture(self):
        try:
            expected_version = connectivity.kicad_version()
            interpreter = connectivity._interpreter(None)
            probe = subprocess.run([interpreter, '-c', 'import pcbnew; print(pcbnew.Version())'],
                                   capture_output=True, text=True, timeout=60)
            if probe.returncode:
                self.skipTest('KiCad Python unavailable: ' + probe.stderr.strip())
            if connectivity._version(probe.stdout) != expected_version:
                self.skipTest('KiCad Python version does not match the DRC judge')
        except (connectivity.ConnectivityUnavailable, OSError, subprocess.SubprocessError) as exc:
            self.skipTest(str(exc))
        # Two disconnected LINK pads; the zone is a keepout, not refillable copper.
        board = connectivity.ROOT / 'tests/boards/plane_smd/inpad_cost.kicad_pcb'
        original = board.read_bytes()
        row = connectivity.count_unconnected([board])[str(board)]
        self.assertEqual(row['unconnected'], 1)
        self.assertEqual(row['per_net'], {'LINK': 1})
        self.assertEqual(sum(row['per_net'].values()), row['unconnected'])
        self.assertEqual(board.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
