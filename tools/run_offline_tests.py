"""Run explicitly selected offline tests in a disposable runtime, never app."""
import importlib.abc
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
original_cwd = Path.cwd()
original_tmpdir = os.environ.get('TMPDIR')
original_tempdir = tempfile.tempdir
runtime = Path(tempfile.mkdtemp(prefix='fresh-bilingual-'))
os.environ['TMPDIR'] = str(runtime)
tempfile.tempdir = str(runtime)
os.chdir(runtime)
from modules import utils
utils.get_app_root_dir = lambda: str(runtime)
utils.get_app_subdir = lambda name: str(runtime / name)


class NoApp(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app':
            raise ImportError('Offline runner forbids app startup')


sys.meta_path.insert(0, NoApp())
print('Fresh modules:', ROOT, 'Isolated runtime:', runtime, flush=True)


def cleanup_runtime():
    # Windows cannot remove open log files or the current working directory.
    logging.shutdown()
    os.chdir(original_cwd)
    tempfile.tempdir = original_tempdir
    if original_tmpdir is None:
        os.environ.pop('TMPDIR', None)
    else:
        os.environ['TMPDIR'] = original_tmpdir
    shutil.rmtree(runtime)


def main(names):
    import unittest
    try:
        if not names:
            raise ValueError('Select safe offline test modules explicitly')
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return int(not result.wasSuccessful())
    finally:
        cleanup_runtime()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
