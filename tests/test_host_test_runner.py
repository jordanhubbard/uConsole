import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from run_host_tests import main


class HostTestRunnerTests(unittest.TestCase):
    def fixture(self, body, interval=10):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test_fixture.py').write_text('import time, unittest\n'
                'class Fixture(unittest.TestCase):\n    def test_fixture(self):\n        '+body+'\n')
            source = ('import sys; sys.path.insert(0, '+repr(str(Path(__file__).resolve().parents[1]/'tools'))+'); '
                'from run_host_tests import main; main('+repr(['discover', '-s', str(root), '-v'])+
                ', dump_after='+repr(interval)+')')
            return subprocess.run([sys.executable, '-c', source], capture_output=True,
                                  text=True, timeout=15, env=dict(os.environ))

    def test_success_preserves_unittest_exit_and_count(self):
        result = self.fixture('self.assertTrue(True)')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 1 test', result.stderr)
        self.assertIn('OK', result.stderr)

    def test_failure_preserves_nonzero_exit(self):
        result = self.fixture('self.fail("fixture failure")')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('FAILED (failures=1)', result.stderr)

    def test_slow_test_emits_stack_without_changing_result(self):
        result = self.fixture('time.sleep(0.4)', interval=0.05)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Timeout (', result.stderr)
        self.assertIn('test_fixture.py', result.stderr)
        self.assertIn('OK', result.stderr)

    def test_invalid_interval_cannot_start_suite(self):
        with patch('run_host_tests.unittest.main') as suite:
            for value in (0, -1, float('inf'), float('nan'), True, '300'):
                with self.assertRaises(ValueError): main([], dump_after=value)
            suite.assert_not_called()
