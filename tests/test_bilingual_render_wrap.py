"""Regression for English-only reflow at the bilingual ASS boundary."""
import logging
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from test_subtitle_rendering_layout import _load_task_processor_class


SHORT = "and I'm driving home and\nit's still pitch black,"
CHINESE = '我开车回家，天也依然是一片漆黑，'
FONT = 'Noto Sans CJK SC'
TIMING = '00:00:01,000 --> 00:00:02,000'


@pytest.fixture
def convert(tmp_path, monkeypatch):
    # Keep this parser/renderer test independent of runtime DB initialization.
    # The isolated module uses the same reader class, extracted without imports.
    import ast
    import html
    import os
    import sys
    import types
    from typing import List
    from dataclasses import dataclass

    translator = Path(__file__).resolve().parents[1] / 'modules' / 'subtitle_translator.py'
    reader = types.ModuleType('modules.subtitle_translator')
    module = ast.parse(translator.read_text(encoding='utf-8'))
    module.body = [n for n in module.body
                   if isinstance(n, ast.ClassDef) and n.name in ('SubtitleItem', 'SubtitleReader')]
    reader.__dict__.update(re=re, html=html, os=os, List=List, dataclass=dataclass,
                           logger=logging.getLogger(__name__))
    monkeypatch.setitem(sys.modules, reader.__name__, reader)
    exec(compile(module, str(translator), 'exec'), reader.__dict__)
    cls = _load_task_processor_class()
    cls._convert_srt_to_ass.__globals__.update(
        re=re, __name__='modules._isolated_task_processor', _as_bool=bool,
    )
    processor = cls.__new__(cls)
    processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True}

    def run(source, paired, width=1920, *, source_name='video.en.srt',
            flags=None, source_timing=TIMING, legacy=False):
        if flags is not None:
            processor.config.update(flags)
        original = tmp_path / source_name
        translated = tmp_path / 'translated_cached.srt'
        output = tmp_path / 'video.ass'
        if source_name.endswith('.vtt'):
            raw = f'WEBVTT\r\n\r\n{source_timing.replace(",", ".")}\r\n{source}\r\n'.encode()
        else:
            raw = f'1\r\n{source_timing}\r\n{source}\r\n'.encode()
        if source is not None:
            original.write_bytes(raw)
        translated_raw = f'1\r\n{TIMING}\r\n{paired}\r\n'.encode()
        translated.write_bytes(translated_raw)
        assert processor._convert_srt_to_ass(
            str(translated), str(output), logging.getLogger(__name__),
            video_width=width, video_height=1080, font_family=FONT, style_overrides={},
            prefer_single_line=True, single_line_min_font_scale=cls._ASS_SINGLE_LINE_FONT_SCALE_MIN,
            bilingual_source_path=None if legacy else str(original),
        )
        if source is not None:
            assert original.read_bytes() == raw
        else:
            assert not original.exists()
        assert translated.read_bytes() == translated_raw
        ass = output.read_text(encoding='utf-8')
        event, = [line.split(',', 9) for line in ass.splitlines() if line.startswith('Dialogue:')]
        assert event[1:3] == ['0:00:01.00', '0:00:02.00']
        return ass, event[-1]

    return run


def test_short_continuation_joins_only_english_and_keeps_markup_cleanup(convert):
    source = "<b>and I'm driving home and</b>\nit's still pitch black,\\h"
    _, text = convert(source, source + '\n' + CHINESE + '\n还有另一行')
    assert text == SHORT.replace('\n', ' ') + r'\N' + CHINESE + r'\N还有另一行'


@pytest.mark.parametrize('source', [
    'I met my friends\nYesterday morning.',
    'Okay,\nyou go first',
    'Are you coming?\nWe should leave now.',
    'The cast includes\nRenée O’Connor.',
    'She said,\n“See you soon!”',
    'Meet there (if possible);\nI’ll call first…',
    'I chose one —\nNot both.',
])
def test_source_paired_prose_ignores_line_ending_and_next_line_case(convert, source):
    chinese = '听说 Renée O’Connor 也到了。\n这是另一个中文行。'
    _, text = convert(source, source + '\n' + chinese)
    assert text == source.replace('\n', ' ') + r'\N' + chinese.replace('\n', r'\N')


@pytest.mark.parametrize('source,suffix,filename,timing', [
    ('- Are you coming\n- not today', '你会来吗？\n- 今天不来', 'video.en.srt', TIMING),
    ('Are you coming\n—not today', '你会来吗？\n- 今天不来', 'video.en.srt', TIMING),
    ('Alice: Are you coming\nBob: not today', CHINESE, 'video.en.srt', TIMING),
    ('Are you coming\nBob: Not today', CHINESE, 'video.en.srt', TIMING),
    ('Welcome aboard\n[APPLAUSE]', CHINESE, 'video.en.srt', TIMING),
    ('Welcome aboard\n(Applause)', CHINESE, 'video.en.srt', TIMING),
    ('Welcome aboard\n1. First step', CHINESE, 'video.en.srt', TIMING),
    ('“Are you coming?”\n“— Not today.”', CHINESE, 'video.en.srt', TIMING),
    ('Save this as\nC:/notes/example.txt', CHINESE, 'video.en.srt', TIMING),
    ('Call the method\nopen(file)', CHINESE, 'video.en.srt', TIMING),
    ('Open the page\nhttps://example.test', CHINESE, 'video.en.srt', TIMING),
    ('The answer is\n42\nDo not erase it', CHINESE, 'video.en.srt', TIMING),
    (r'<b>Are you coming</b> \N <b>not today</b>', CHINESE, 'video.en.srt', TIMING),
    (SHORT, 'hello there', 'video.en.srt', TIMING),
    (SHORT, CHINESE, 'video.srt', TIMING),
    (SHORT, CHINESE, 'video.fr.srt', TIMING),
    (SHORT, CHINESE, 'video.en.srt', '00:00:01,001 --> 00:00:02,000'),
])
def test_uncertain_boundaries_and_explicit_breaks_keep_layout(convert, source, suffix, filename, timing):
    _, text = convert(source, source + '\n' + suffix, source_name=filename, source_timing=timing)
    expected = 'Are you coming\nnot today' if source.startswith('<b>') else source
    assert text == (expected + '\n' + suffix).replace('\n', r'\N')


def test_source_translation_mismatch_keeps_layout(convert):
    _, text = convert(SHORT + ' really', SHORT + '\n' + CHINESE)
    assert text == (SHORT + '\n' + CHINESE).replace('\n', r'\N')


def test_vtt_language_variant_pairs_against_decoded_source(convert):
    source = "and I&#39;m driving home and\nit's still pitch black,"
    _, text = convert(source, SHORT + '\n' + CHINESE, source_name='video.en-US.vtt')
    assert text == SHORT.replace('\n', ' ') + r'\N' + CHINESE


def test_missing_original_keeps_layout_without_failing_burn(convert):
    _, text = convert(None, SHORT + '\n' + CHINESE)
    assert text == (SHORT + '\n' + CHINESE).replace('\n', r'\N')


def test_other_language_pair_keeps_layout(convert):
    _, text = convert(SHORT, SHORT + '\n' + CHINESE, flags={'SUBTITLE_TARGET_LANGUAGE': 'ja'})
    assert text == (SHORT + '\n' + CHINESE).replace('\n', r'\N')


def test_single_language_never_uses_the_bilingual_layout(convert):
    _, text = convert(SHORT, SHORT, flags={'SUBTITLE_BILINGUAL_ENABLED': False})
    assert text == SHORT.replace('\n', ' ')


def _render(tmp_path, ass, width):
    fonts = Path(__file__).resolve().parents[1] / 'fonts'
    target = tmp_path / 'frame.ass'
    target.write_text(ass, encoding='utf-8')
    return subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-threads', '1',
        '-filter_threads', '1', '-f', 'lavfi', '-i', f'color=black:s={width}x1080:r=1',
        '-vf', f"setpts=PTS+1/TB,subtitles=frame.ass:fontsdir='{fonts}'", '-frames:v', '1',
        '-pix_fmt', 'gray', '-threads:v', '1', '-f', 'rawvideo', '-',
    ], cwd=tmp_path, check=True, capture_output=True, timeout=30).stdout


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg with libass required')
@pytest.mark.parametrize('width,source,line_count', [
    (1920, SHORT, 1),
    (860, SHORT, 2),
    (1920, SHORT + '\nand we have miles to go before we reach the little cabin near the lake,',
     2),
])
def test_libass_uses_actual_width_at_full_font_size(convert, tmp_path, width, source, line_count):
    chinese = '仍然很黑'  # Remains a single line at both widths.
    ass, text = convert(source, source + '\n' + chinese, width)
    legacy_ass, legacy = convert(source, source + '\n' + chinese, width, legacy=True)
    assert ass.partition('[Events]')[0] == legacy_ass.partition('[Events]')[0]
    assert '\nWrapStyle: 0\n' in ass
    english = source.replace('\n', ' ')
    assert text == english + r'\N' + chinese
    actual = _render(tmp_path, ass, width)
    legacy_frame = _render(tmp_path, legacy_ass, width)
    assert len(actual) == width * 1080 and max(actual) > 100
    # Different libass versions balance by breaking at different spaces.
    # Match a manually broken reference at full style size; disable wrapping
    # on the reference so it cannot get extra lines automatically.
    enabled_reference = ass.replace('WrapStyle: 0', 'WrapStyle: 2', 1)
    words = english.split()
    reference_lines = ([english] if line_count == 1 else
                       [r'\N'.join((' '.join(words[:i]), ' '.join(words[i:]))) for i in range(1, len(words))])
    assert any(actual == _render(tmp_path, enabled_reference.replace(text, line + r'\N' + chinese, 1), width)
               for line in reference_lines)
    if line_count == 1:
        assert actual != legacy_frame and r'\N' in legacy
    assert actual[-width * 110:] == legacy_frame[-width * 110:]
