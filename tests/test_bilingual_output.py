from pathlib import Path
import logging
from modules import task_manager as tm


def test_task_rerun_never_uses_translated_output_as_source(tmp_path):
    from test_subtitle_translator_pairing import _make_translator, _PATCH_TARGET
    taskdir = tmp_path / 'offline'
    taskdir.mkdir()
    source = taskdir / 'source.srt'
    source.write_text('1\n00:00:00,100 --> 00:00:01,900\nHello there\n\n')
    output = taskdir / 'translated_offline.srt'
    output.write_text('1\n00:00:00,100 --> 00:00:01,900\n旧译文\nHello there\n\n')
    translator = _make_translator()
    translator.config.bilingual = True
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {'SUBTITLE_TRANSLATION_ENABLED': True, 'SUBTITLE_EMBED_IN_VIDEO': False, 'BILINGUAL_SUBTITLES': True}
    task = {'id': 'offline', 'video_path_local': str(taskdir / 'video.mp4')}
    with patch.object(tm.os, 'listdir', return_value=[output.name, source.name]), patch.object(tm, 'DOWNLOADS_DIR', str(tmp_path)), patch.object(tm, 'get_task', return_value=task), patch.object(tm, 'update_task'), patch.object(st, 'create_translator_from_config', return_value=translator), patch(_PATCH_TARGET, side_effect=translator.llm_requester.fake_create):
        assert processor._translate_subtitle('offline', logging.getLogger('offline'))
        assert processor._translate_subtitle('offline', logging.getLogger('offline'))
    assert output.read_text().count('Hello there') == 1
    assert '译文0\nHello there' in output.read_text()
    assert translator.llm_requester.sent_batches == [['Hello there'], ['Hello there']]


def test_task_vtt_translation_preserves_literal_brackets_and_all_lines(tmp_path):
    from test_subtitle_translator_pairing import _make_translator, _PATCH_TARGET
    taskdir = tmp_path / 'offline'
    taskdir.mkdir()
    source = taskdir / 'source.en.vtt'
    source.write_text(
        'WEBVTT\n\ncue-a\n00:00:00.100 --> 00:00:01.900 align:start\n'
        'Use &lt;vector&gt; and {value}\n<b>Second &amp; third</b>\nFourth line\n\n',
        encoding='utf-8',
    )
    expected = 'Use <vector> and {value}\nSecond & third\nFourth line'
    translator = _make_translator()
    translator.config.bilingual = True
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {
        'SUBTITLE_TRANSLATION_ENABLED': True,
        'SUBTITLE_EMBED_IN_VIDEO': False,
        'BILINGUAL_SUBTITLES': True,
    }
    task = {'id': 'offline', 'video_path_local': str(taskdir / 'video.mp4')}
    with patch.object(tm, 'DOWNLOADS_DIR', str(tmp_path)), \
         patch.object(tm, 'get_task', return_value=task), \
         patch.object(tm, 'update_task'), \
         patch.object(st, 'create_translator_from_config', return_value=translator), \
         patch(_PATCH_TARGET, side_effect=translator.llm_requester.fake_create):
        assert processor._translate_subtitle('offline', logging.getLogger('offline'))
    output = (taskdir / 'translated_offline.srt').read_text(encoding='utf-8')
    assert output == f'1\n00:00:00,100 --> 00:00:01,900\n译文0\n{expected}\n\n'
    assert translator.llm_requester.sent_batches == [[expected]]


def test_vtt_reader_handles_ids_settings_short_times_and_all_lines(tmp_path):
    path = tmp_path / 'source.vtt'
    path.write_text('WEBVTT\n\nNOTE ignore me\ncomment\n\ncue-a\n00:01.123 --> 00:02.456 align:start\nFirst &amp; second\nThird\nFourth\n\ncue-b\n00:03.000 --> 00:04.000\nLast\n')
    items = st.SubtitleReader.read_vtt(str(path))
    assert len(items) == 2
    assert items[0].start_time == '00:00:01,123'
    assert items[0].end_time == '00:00:02,456'
    assert items[0].source_text == 'First & second\nThird\nFourth'
    assert items[1].source_text == 'Last'


def test_bilingual_translation_to_ass_keeps_language_boundary(tmp_path):
    from test_subtitle_translator_pairing import _make_translator, _PATCH_TARGET
    translator = _make_translator()
    translator.config.bilingual = True
    source = tmp_path / 'source.srt'
    source.write_text('1\n00:00:00,100 --> 00:00:01,900\nFirst line\nSecond line\nThird line\n\n')
    out = tmp_path / 'translated_offline.srt'
    with patch(_PATCH_TARGET, side_effect=translator.llm_requester.fake_create):
        assert translator.translate_file(str(source), str(out))
    assert 'First line\nSecond line\nThird line' in out.read_text()
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {'BILINGUAL_SUBTITLES': True}
    ass = tmp_path / 'out.ass'
    assert processor._convert_srt_to_ass(str(out), str(ass), logging.getLogger('offline'), video_width=1280, video_height=720, font_family='DejaVu Sans')
    dialogue = next(line for line in ass.read_text().splitlines() if line.startswith('Dialogue:'))
    assert r'译文0\N' in dialogue
    for text in ('First line', 'Second line', 'Third line'):
        assert text in dialogue
    assert ',0:00:00.10,0:00:01.90,' in dialogue

import ast
from unittest.mock import patch
from modules import subtitle_translator as st
from modules.config_manager import DEFAULT_CONFIG


def test_settings_checkbox_save_roundtrip_without_starting_app(tmp_path):
    from modules import config_manager as cm
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'app.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_perform_settings_save')
    loop = next(n for n in ast.walk(function) if isinstance(n, ast.For) and isinstance(n.iter, ast.Name) and n.iter.id == 'SETTINGS_CHECKBOX_FIELDS')
    keys = ['BILINGUAL_TITLE', 'BILINGUAL_DESCRIPTION', 'BILINGUAL_SUBTITLES']
    for enabled in (True, False):
        form = {key: 'on' for key in keys} if enabled else {}
        exec(compile(ast.Module(body=[loop], type_ignores=[]), '<settings checkbox normalization>', 'exec'), {'SETTINGS_CHECKBOX_FIELDS': keys, 'form_data': form})
        with patch.object(cm, 'get_app_subdir', return_value=str(tmp_path)):
            cm.update_config(form)
            result = cm.load_config()
        assert all(result[key] is enabled for key in keys)


def test_settings_factory_and_translation_output(tmp_path):
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'app.py').read_text())
    checkbox = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SETTINGS_CHECKBOX_FIELDS' for t in n.targets))
    registered = {n.value for n in ast.walk(checkbox) if isinstance(n, ast.Constant)}
    template = (root / 'templates/settings.html').read_text()
    for key in ('BILINGUAL_SUBTITLES', 'BILINGUAL_TITLE', 'BILINGUAL_DESCRIPTION'):
        assert DEFAULT_CONFIG.get(key) is True
        assert key in registered
        assert f'name="{key}"' in template
    for value in (True, False, 'false'):
        with patch.object(st, 'SubtitleTranslator', side_effect=lambda config, task_id: config):
            config = st.create_translator_from_config({'OPENAI_API_KEY': 'offline', 'BILINGUAL_SUBTITLES': value})
        translator = st.SubtitleTranslator.__new__(st.SubtitleTranslator)
        translator.config = config
        translator.writer = st.SubtitleWriter()
        translator.logger = st.logger
        out = tmp_path / 'out.srt'
        assert translator._write_translated_file([st.SubtitleItem(1, '00:00:00,000', '00:00:01,000', 'Source', '译文')], str(out))
        assert ('译文\nSource' in out.read_text()) == (value is True)

import pytest


@pytest.mark.parametrize('source,target,expected', [('same', 'same', 'same'), ('source', '', 'source'), ('', '译文', '译文')])
def test_bilingual_equal_or_missing_text_is_not_duplicated(tmp_path, source, target, expected):
    item = SubtitleItem(1, '00:00:00,000', '00:00:01,000', source, target)
    out = tmp_path / 'out.srt'
    SubtitleWriter.write_srt([item], str(out), bilingual=True)
    assert out.read_text().split('-->')[1].split('\n', 1)[1].strip() == expected


@pytest.mark.parametrize('translated,bilingual', [(True, True), (True, False), (False, False)])
def test_vtt_writer_escapes_entities_and_roundtrips_text(tmp_path, translated, bilingual):
    source = 'Use <vector> and {value}\nA & B; literal &lt;name&gt;\nFourth > third'
    target = '使用 <向量> 和 {值}\n甲 & 乙; 字面 &lt;名&gt;'
    item = SubtitleItem(1, '00:00:01,123', '00:00:04,567', source, target)
    expected = target + '\n' + source if bilingual else (target if translated else source)
    out = tmp_path / 'escaped.vtt'
    SubtitleWriter.write_vtt([item], str(out), translated=translated, bilingual=bilingual)
    text = out.read_text(encoding='utf-8')
    assert '<vector>' not in text and '<向量>' not in text
    assert '&amp;lt;' in text
    assert '&lt;' in text and '&gt;' in text
    for _ in range(2):
        items = st.SubtitleReader.read_vtt(str(out))
        assert len(items) == 1
        assert items[0].source_text == expected
        assert (items[0].start_time, items[0].end_time) == (item.start_time, item.end_time)
        SubtitleWriter.write_vtt(items, str(out), translated=False)


@pytest.mark.parametrize('enabled,expected', [(True, '译文。\nSource.'), (False, '译文')])
def test_vtt_output_switch(tmp_path, enabled, expected):
    item = SubtitleItem(1, '00:00:00,000', '00:00:01,000', 'Source.', '译文。')
    out = tmp_path / 'out.vtt'
    SubtitleWriter.write_vtt([item], str(out), bilingual=enabled)
    assert out.read_text() == f'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n{expected}\n\n'

from modules.subtitle_translator import SubtitleItem, SubtitleWriter


def test_bilingual_srt_preserves_all_lines_and_timeline(tmp_path):
    item = SubtitleItem(7, '00:00:01,123', '00:00:04,567', 'One & two\nThree\nFour.', '一和二\n三\n四。')
    out = tmp_path / 'out.srt'
    SubtitleWriter.write_srt([item], str(out), bilingual=True)
    assert out.read_text() == '7\n00:00:01,123 --> 00:00:04,567\n一和二\n三\n四。\nOne & two\nThree\nFour.\n\n'
    before = out.read_bytes()
    SubtitleWriter.write_srt([item], str(out), bilingual=True)
    assert out.read_bytes() == before
    assert item.translated_text == '一和二\n三\n四。'
