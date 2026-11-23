"""Structural checks for the fork's release and CI boundaries."""
from pathlib import Path
import unittest

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / '.github/workflows'


class WorkflowTests(unittest.TestCase):
    def test_no_windows_build_or_release_workflows(self):
        for name in ('windows-preview.yml', 'windows-release.yml'):
            self.assertFalse((WORKFLOWS / name).exists(), name)

    def test_linux_full_suite_remains_enabled(self):
        ci = yaml.safe_load((WORKFLOWS / 'ci.yml').read_text(encoding='utf-8'))
        self.assertIn('pull_request', ci['on'])
        self.assertEqual(ci['on']['push']['branches'], ['main'])
        self.assertEqual(ci['jobs']['test']['runs-on'], 'ubuntu-latest')
        self.assertIn('python -m pytest tests/', [step.get('run') for step in ci['jobs']['test']['steps']])

    def test_docker_workflows_do_not_publish_on_main_push(self):
        docker = yaml.safe_load((WORKFLOWS / 'docker-publish.yml').read_text(encoding='utf-8'))
        self.assertEqual(docker['on'], {'push': {'tags': ['*.*.*']}})
        cleanup = yaml.safe_load((WORKFLOWS / 'delete-old-packages.yml').read_text(encoding='utf-8'))
        self.assertEqual(set(cleanup['on']), {'schedule', 'workflow_dispatch'})
