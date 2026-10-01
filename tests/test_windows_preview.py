"""Local contract checks; never launch the application or download binaries."""
from pathlib import Path
import yaml
import importlib.util
import pytest


def load_verifier():
    path = ROOT / 'build-tools/verify_preview.py'
    assert path.is_file(), 'Preview package verifier missing'
    spec = importlib.util.spec_from_file_location('preview_verifier', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_bundle(root):
    for name in ('Y2A-Auto.exe', 'start.bat', 'README.txt', 'LICENSE',
                 'ffmpeg/ffmpeg.exe', 'ffmpeg/ffprobe.exe',
                 'ffmpeg/FFMPEG_GPLv3.txt', 'ffmpeg/FFMPEG_README.txt'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic test fixture', encoding='utf-8')


def test_preview_package_boundary(tmp_path):
    verifier = load_verifier()
    make_bundle(tmp_path)
    # Empty first-run directories are fine; their contents are never distributable.
    (tmp_path / 'config').mkdir()
    verifier.verify(tmp_path)
    for relative in ('config/private.json', '_internal/cookies/session.txt',
                     'db/tasks.sqlite', 'logs/app.log', 'temp/video.mp4',
                     'acfunid/id_mapping.json', '_internal/.venv/pyvenv.cfg',
                     '_internal/credentials.json', '.env', 'cookies.txt'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic forbidden fixture', encoding='utf-8')
        with pytest.raises(ValueError, match='Forbidden'):
            verifier.verify(tmp_path)
        path.unlink()
    (tmp_path / 'ffmpeg/ffprobe.exe').unlink()
    with pytest.raises(ValueError, match='Missing or empty'):
        verifier.verify(tmp_path)


def test_preview_steps_test_before_build_and_use_tracked_allowlist():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))
    job = workflow['jobs']['windows-preview']
    assert job['env']['PYTHONUTF8'] == '1'
    steps = job['steps']
    run = '\n'.join(s.get('run', '') for s in steps)
    assert run.index('tests/run_offline.py') < run.index('python build_exe.py') < run.index('python build-tools/verify_preview.py $bundle')
    for name in ('metadata', 'output', 'smoke'):
        assert f'tests/test_bilingual_{name}.py' in run
    assert "shutil.which('ffmpeg') and shutil.which('ffprobe')" in run
    assert '$env:GITHUB_PATH' in run
    assert 'HEAD app.py modules templates static fonts build-tools requirements.txt LICENSE' in run
    assert "'build-tools/dist/Y2A-Auto'" in run
    assert 'python build-tools/verify_preview.py --source $stage' in run
    assert 'python -m pytest tests/\n' not in run  # not an unsafe full-suite runner
    assert 'python -m pytest tests/ -' not in run
    assert '${{ github.ref' not in run and '${{ inputs.' not in run


def test_preview_allows_public_ca_bundle_but_rejects_source_runtime(tmp_path):
    verifier = load_verifier()
    make_bundle(tmp_path)
    public_ca = tmp_path / '_internal/certifi/cacert.pem'
    public_ca.parent.mkdir(parents=True)
    public_ca.write_text('synthetic public certificate', encoding='utf-8')
    verifier.verify(tmp_path)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'app.py').write_text('# synthetic source', encoding='utf-8')
    verifier.verify(source, source=True)
    (source / 'config').mkdir()
    with pytest.raises(ValueError, match='Forbidden source root'):
        verifier.verify(source, source=True)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/windows-preview.yml'


def test_preview_workflow_is_read_only_and_branch_scoped():
    assert WORKFLOW.is_file(), 'Independent preview workflow is missing'
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))
    assert 'on' in workflow and True not in workflow  # Quote YAML 1.1's on key.
    assert workflow['on'] == {'push': {'branches': ['preview']}, 'workflow_dispatch': None}
    assert workflow['permissions'] == {'contents': 'read'}
    job = workflow['jobs']['windows-preview']
    assert job['runs-on'] == 'windows-latest'
    assert job['if'] == "github.ref == 'refs/heads/preview' || (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')"
    assert 'permissions' not in job
    steps = job['steps']
    for step in steps:
        assert 'continue-on-error' not in step
        if step.get('uses', '').startswith('actions/checkout@'):
            assert step['with']['persist-credentials'] is False
            assert 'ref' not in step['with']
    upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact@'))
    assert upload['with']['path'] == 'Y2A-Auto-windows-preview.zip'
    assert upload['with']['retention-days'] == 7
    assert upload['with']['if-no-files-found'] == 'error'
    assert 'if' not in upload  # Do not upload after a failed test/build/verification.
    text = WORKFLOW.read_text(encoding='utf-8')
    for forbidden in ('secrets.', 'gh release', 'packages: write', 'contents: write', 'release_tag', '${{ inputs.', 'continue-on-error'):
        assert forbidden not in text
