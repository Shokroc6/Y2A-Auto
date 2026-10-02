"""Small structural guard for the branch-only, read-only preview build."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WindowsPreviewTests(unittest.TestCase):
    def test_branch_only_artifact_build(self):
        path = ROOT / '.github/workflows/windows-preview.yml'
        self.assertTrue(path.is_file(), 'Preview workflow is required')
        text = path.read_text(encoding='utf-8')
        for required in ('branches: [restart/bilingual]', 'workflow_dispatch:',
                         'contents: read', "github.ref == 'refs/heads/restart/bilingual'",
                         'persist-credentials: false', 'PYTHONUTF8:', 'GITHUB_PATH',
                         'tools/run_offline_tests.py', 'test_bilingual_vtt',
                         'test_offline_runner', 'python build_exe.py',
                         'actions/upload-artifact@', 'if-no-files-found: error',
                         'FFMPEG_GPLv3.txt', 'FFMPEG_README.txt'):
            self.assertIn(required, text)
        for forbidden in ('contents: write', 'gh release', 'secrets.', 'continue-on-error:',
                          'release_tag:', 'tags:', 'pull_request:', 'always()'):
            self.assertNotIn(forbidden, text)
