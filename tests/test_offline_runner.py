"""Child-process checks for portable runtime and Windows-safe cleanup."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OfflineRunnerTests(unittest.TestCase):
    def test_runtime_cleanup_on_success_failure_and_exception(self):
        for outcome in ('success', 'failure', 'exception'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                code = f'''
import importlib.util, logging, os
from pathlib import Path
spec = importlib.util.spec_from_file_location('offline', {str(ROOT / 'tools/run_offline_tests.py')!r})
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runtime = runner.runtime
assert runtime.parent == Path({tmp!r}), runtime
handler = logging.FileHandler(runtime / 'open.log')
logging.getLogger().addHandler(handler)
cleanup = runner.cleanup_runtime
def guarded_cleanup():
    cleanup()
    assert handler.stream is None
    assert Path.cwd() != runtime
    assert not runtime.exists()
runner.cleanup_runtime = guarded_cleanup
import unittest
class Probe(unittest.TestCase):
    def runTest(self):
        self.assertNotEqual({outcome!r}, 'failure')
from unittest.mock import patch
try:
    with patch.object(unittest.defaultTestLoader, 'loadTestsFromNames', return_value=unittest.TestSuite([Probe()])):
        if {outcome!r} == 'exception':
            with patch.object(unittest.TextTestRunner, 'run', side_effect=RuntimeError('probe')):
                runner.main(['probe'])
        else:
            assert runner.main(['probe']) == (1 if {outcome!r} == 'failure' else 0)
except RuntimeError:
    assert {outcome!r} == 'exception'
assert not runtime.exists()
'''
                env = dict(os.environ, TMPDIR=tmp, TEMP=tmp, TMP=tmp, PYTHONUTF8='1')
                result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
