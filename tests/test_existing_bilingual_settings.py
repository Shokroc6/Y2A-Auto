"""Settings DOM/save contract without importing the running Flask app."""
from pathlib import Path
from unittest.mock import patch
import pytest
from lxml import html
from modules import config_manager as cm

ROOT = Path(__file__).resolve().parents[1]
KEY = 'SUBTITLE_EXISTING_TARGET_STRATEGY'


def test_windows_workflow_runs_existing_track_contracts():
    workflow = (ROOT / '.github/workflows/windows-preview.yml').read_text(encoding='utf-8')
    assert 'tests/test_existing_bilingual_merge.py' in workflow
    assert 'tests/test_existing_bilingual_settings.py' in workflow


def test_help_explains_auto_multilingual_and_old_task_cache():
    doc = html.fromstring((ROOT / 'templates/settings.html').read_text(encoding='utf-8'))
    source, = doc.xpath('//*[@name="SUBTITLE_SOURCE_LANGUAGE"]')
    help_id = source.get('aria-describedby')
    assert help_id, 'Source-language control needs linked ambiguity help'
    source_help, = doc.xpath(f'//*[@id="{help_id}"]')
    for phrase in ('auto', 'all-subs', '多语', '原文语言', '单语回退'):
        assert phrase in source_help.text_content()
    strategy_help, = doc.xpath('//*[@id="subtitle-strategy-help"]')
    for phrase in ('旧任务', '缓存', '新建任务', '下载目录'):
        assert phrase in strategy_help.text_content()


def test_bilingual_control_has_own_grid_cell_and_short_label():
    doc = html.fromstring((ROOT / 'templates/settings.html').read_text(encoding='utf-8'))
    control, = doc.xpath('//*[@name="BILINGUAL_SUBTITLES"]')
    cell = control.xpath('ancestor::div[contains(@class,"col-md-6")][1]')[0]
    assert len(cell.xpath('.//input')) == 1
    assert control.getparent().text_content().strip() == '双语字幕'
    assert cell.xpath('./p[contains(@class,"help-text")]')
    assert 'subtitle-output-option' in cell.get('class')
    select, = doc.xpath(f'//select[@name="{KEY}"]')
    assert set(select.xpath('./option/@value')) == {'prefer_existing', 'retranslate'}
    assert '#vtab-subtitle .subtitle-output-option .checkbox-label' in doc.text_content()
    assert 'align-items: flex-start' in doc.text_content()


@pytest.mark.parametrize('submitted,expected', [('prefer_existing', 'prefer_existing'),
    ('retranslate', 'retranslate'), ('invalid', 'prefer_existing')])
def test_strategy_save_load_validation(tmp_path, submitted, expected):
    assert cm.DEFAULT_CONFIG.get(KEY) == 'prefer_existing'
    with patch.object(cm, 'get_app_subdir', return_value=str(tmp_path)):
        cm.update_config({KEY: submitted})
        saved = cm.load_config()
    assert saved[KEY] == expected
    assert saved['SUBTITLE_TRANSLATION_ENABLED'] is False
