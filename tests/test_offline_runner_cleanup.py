"""Exercise runner teardown in a child so logging shutdown is process-local."""
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('outcome,expected_code', [('0', 0), ('1', 1), ('error', 1)])
def test_runner_closes_logs_and_restores_cwd_before_cleanup(tmp_path, outcome, expected_code):
    harness = r'''
import logging
import os
from pathlib import Path
import runpy
import sys
import tempfile
from unittest.mock import patch
import pytest

runner, outcome = sys.argv[1:]
original_cwd = Path.cwd()
handler = None
original_temporary_directory = tempfile.TemporaryDirectory

class CheckedDirectory(original_temporary_directory):
    def cleanup(self):
        # Emulate Windows' refusal to delete open files on every platform.
        if handler is not None and handler.stream is not None:
            raise PermissionError('log file still open during temporary cleanup')
        assert Path.cwd() == original_cwd, 'cwd still inside temporary directory'
        super().cleanup()
        assert not Path(self.name).exists()
        print('CLEANUP_VERIFIED', flush=True)

def run_tests(args):
    global handler
    Path('logs').mkdir()
    handler = logging.FileHandler('logs/task_manager.log', encoding='utf-8')
    logging.getLogger('cleanup-regression').addHandler(handler)
    if outcome == 'error':
        raise RuntimeError('test runner failure sentinel')
    return int(outcome)

with patch.object(tempfile, 'TemporaryDirectory', CheckedDirectory), \
     patch.object(pytest, 'main', run_tests), \
     patch.object(sys, 'argv', [runner, '-q']):
    runpy.run_path(runner, run_name='__main__')
'''
    result = subprocess.run(
        [sys.executable, '-c', harness, str(ROOT / 'tests/run_offline.py'), outcome],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert 'CLEANUP_VERIFIED' in result.stdout, result.stdout + result.stderr
    assert result.returncode == expected_code, result.stdout + result.stderr
    if outcome == 'error':
        assert 'test runner failure sentinel' in result.stderr
