"""Structural guards for the read-only Windows artifact build."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class WindowsPreviewTests(unittest.TestCase):
    def test_branch_only_artifact_build(self):
        path = ROOT / '.github/workflows/windows-preview.yml'
        self.assertTrue(path.is_file(), 'Windows workflow is required')
        text = path.read_text(encoding='utf-8')
        for required in ('branches: [main, restart/bilingual]', 'workflow_dispatch:',
                         'contents: read', "github.ref == 'refs/heads/restart/bilingual'",
                         'persist-credentials: false', 'PYTHONUTF8:', 'GITHUB_PATH',
                         'tools/run_offline_tests.py', 'test_bilingual_vtt',
                         'test_offline_runner', 'test_upload_copyright_type', 'python build_exe.py',
                         'actions/upload-artifact@', 'if-no-files-found: error',
                         'FFMPEG_GPLv3.txt', 'FFMPEG_README.txt'):
            self.assertIn(required, text)
        for forbidden in ('contents: write', 'gh release', 'secrets.', 'continue-on-error:',
                          'release_tag:', 'tags:', 'pull_request:', 'always()'):
            self.assertNotIn(forbidden, text)

    def test_main_and_preview_yaml_contract(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/windows-preview.yml').read_text(encoding='utf-8'))
        self.assertEqual(set(workflow['on']), {'push', 'workflow_dispatch'})
        self.assertEqual(workflow['on']['push']['branches'], ['main', 'restart/bilingual'])
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        job = workflow['jobs']['build-windows']
        self.assertEqual(job['if'], "github.ref == 'refs/heads/main' || github.ref == 'refs/heads/restart/bilingual'")
        upload = [step for step in job['steps'] if step.get('uses', '').startswith('actions/upload-artifact@')]
        self.assertEqual(len(upload), 1)
        self.assertEqual(upload[0]['with']['retention-days'], 14)
        self.assertEqual(upload[0]['with']['if-no-files-found'], 'error')

    def test_new_features_run_on_windows_and_inherited_release_is_upstream_only(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/windows-preview.yml').read_text())
        commands = '\n'.join(step.get('run', '') for step in workflow['jobs']['build-windows']['steps'])
        for module in ('test_description_preservation', 'test_bundled_subtitle_fonts', 'test_platform_metadata_limits'):
            self.assertIn(module, commands)
        release = yaml.safe_load((ROOT / '.github/workflows/windows-release.yml').read_text())
        self.assertEqual(release['jobs']['build-windows']['if'], "github.repository == 'fqscfqj/Y2A-Auto'")

    def test_publish_workflows_do_not_trigger_on_main_push(self):
        workflows = ROOT / '.github/workflows'
        docker = yaml.safe_load((workflows / 'docker-publish.yml').read_text(encoding='utf-8'))
        self.assertEqual(docker['on'], {'push': {'tags': ['*.*.*']}})
        release = yaml.safe_load((workflows / 'windows-release.yml').read_text(encoding='utf-8'))
        self.assertEqual(set(release['on']), {'release', 'workflow_dispatch'})
        self.assertEqual(release['on']['release']['types'], ['published'])
        cleanup = yaml.safe_load((workflows / 'delete-old-packages.yml').read_text(encoding='utf-8'))
        self.assertEqual(set(cleanup['on']), {'schedule', 'workflow_dispatch'})
