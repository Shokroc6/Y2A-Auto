"""Run tests with all application runtime paths isolated; deny network access."""
import os
import pathlib
import socket
import sys
import tempfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import modules.utils as utils
import pytest
from apscheduler.schedulers.background import BackgroundScheduler


def deny_network(*args, **kwargs):
    raise AssertionError('Network disabled in offline test runner')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='y2a-offline-') as directory:
        os.chdir(directory)
        with patch.object(utils, 'get_app_root_dir', return_value=directory), \
             patch.object(utils, 'get_app_subdir', side_effect=lambda name: os.path.join(directory, name)), \
             patch.object(BackgroundScheduler, 'start'), \
             patch.object(socket.socket, 'connect', deny_network), \
             patch.object(socket, 'create_connection', deny_network):
            args = [str(ROOT / arg) if arg.startswith('tests/') else arg for arg in sys.argv[1:]]
            if not args:
                # Never import app: it owns monitor/thread startup side effects.
                excluded = []
                args = []
                for path in sorted((ROOT / 'tests').glob('test_*.py')):
                    text = path.read_text()
                    if 'import app as ' in text or 'from app import' in text:
                        excluded.append(path.name)
                    else:
                        args.append(str(path))
                print('Excluded app-import tests:', ', '.join(excluded))
                args.append('-q')
            raise SystemExit(pytest.main(['-p', 'no:cacheprovider', *args]))
