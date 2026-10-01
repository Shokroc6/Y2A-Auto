"""Offline task-level selection regression; never load application/user state."""
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from modules import task_manager as tm
from modules import subtitle_translator as st
from test_subtitle_translator_pairing import _make_translator, _PATCH_TARGET


def run_task(tmp_path, *, bilingual=True, source_language='auto', languages=('zh', 'en'),
             translation=True, qc_failed=False, real_embed=False):
    task_id = 'selection'
    taskdir = tmp_path / task_id
    taskdir.mkdir()
    texts = {'zh': '平台中文字幕', 'en': 'Original English source', 'ja': '日本語の原文'}
    for language in languages:
        (taskdir / f'video.{language}.srt').write_text(
            f'1\n00:00:00,200 --> 00:00:01,800\n{texts[language]}\n\n', encoding='utf-8')
    video = taskdir / 'video.mp4'
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {
        'SUBTITLE_TRANSLATION_ENABLED': translation, 'SUBTITLE_EMBED_IN_VIDEO': True,
        'BILINGUAL_SUBTITLES': bilingual, 'SUBTITLE_SOURCE_LANGUAGE': source_language,
        'SUBTITLE_QC_ENABLED': True, 'VIDEO_ENCODER': 'cpu', 'VIDEO_CPU_PRESET': 'ultrafast',
        'SUBTITLE_FONT_NAME': 'DejaVu Sans', 'FFMPEG_AUTO_DOWNLOAD': False,
    }
    task = {'id': task_id, 'video_path_local': str(video), 'subtitle_qc_failed': int(qc_failed),
            'subtitle_qc_reason': 'rejected' if qc_failed else None}
    translator = _make_translator(bilingual=tm._as_bool(bilingual), source_language=source_language)
    burned = []
    actual_embed = processor._embed_subtitle_in_video

    def embed(*args):
        burned.append(Path(args[2]))
        if real_embed:
            return actual_embed(*args)
        return str(taskdir / 'video_with_subtitle.mp4')

    def update(task_id, **values):
        values.pop('silent', None)
        task.update(values)

    from contextlib import ExitStack
    with ExitStack() as stack:
        if real_embed:
            import shutil
            import subprocess
            ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
            assert ffmpeg and ffprobe, 'Real burn acceptance requires ffmpeg and ffprobe'
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=640x360:d=2',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(video)], check=True, timeout=30)
            stack.enter_context(patch.object(tm, 'get_ffmpeg_path', return_value=ffmpeg))
            stack.enter_context(patch.object(tm, 'get_ffprobe_path', return_value=ffprobe))
        stack.enter_context(patch.object(tm, 'DOWNLOADS_DIR', str(tmp_path)))
        stack.enter_context(patch.object(tm, 'get_task', return_value=task))
        stack.enter_context(patch.object(tm, 'update_task', side_effect=update))
        stack.enter_context(patch.object(processor, '_embed_subtitle_in_video', side_effect=embed))
        stack.enter_context(patch.object(st, 'create_translator_from_config', return_value=translator))
        stack.enter_context(patch(_PATCH_TARGET, side_effect=translator.llm_requester.fake_create))
        assert processor._translate_subtitle(task_id, logging.getLogger('selection'))
    return task, burned, translator.llm_requester.sent_batches


@pytest.mark.parametrize('source_language,languages,expected_language,expected_text', [
    ('ja', ('zh', 'en', 'ja'), 'ja', '日本語の原文'),
    ('ja-JP', ('zh', 'en', 'ja'), 'ja', '日本語の原文'),
    ('auto', ('zh', 'ja'), 'ja', '日本語の原文'),
])
def test_task_respects_non_english_source(tmp_path, source_language, languages, expected_language, expected_text):
    task, burned, batches = run_task(tmp_path, source_language=source_language, languages=languages)
    assert Path(task['subtitle_path_original']).name == f'video.{expected_language}.srt'
    assert batches == [[expected_text]]
    assert f'译文0\n{expected_text}' in burned[0].read_text(encoding='utf-8')


@pytest.mark.parametrize('options', [
    {'bilingual': False}, {'bilingual': 'false'}, {'translation': False},
    {'languages': ('zh',)}, {'source_language': 'ja'}, {'source_language': 'zh'},
])
def test_task_keeps_chinese_single_language_fallback(tmp_path, options):
    task, burned, batches = run_task(tmp_path, **options)
    assert Path(task['subtitle_path_original']).name == 'video.zh.srt'
    assert task['subtitle_path_translated'] is None
    assert batches == []
    assert burned == [tmp_path / 'selection' / 'video.zh.srt']
    assert 'Original English source' not in burned[0].read_text(encoding='utf-8')


def test_bilingual_task_preserves_qc_block(tmp_path):
    task, burned, batches = run_task(tmp_path, qc_failed=True)
    assert Path(task['subtitle_path_original']).name == 'video.en.srt'
    assert batches == [['Original English source']]
    assert burned == []
    assert task['video_path_local'].endswith('video.mp4')
    assert task['subtitle_warning_message'] == 'subtitle_qc_rejected'


def test_bilingual_task_real_burn_from_download_directory(tmp_path):
    import json
    import shutil
    import subprocess
    task, burned, batches = run_task(tmp_path, real_embed=True)
    assert batches == [['Original English source']]
    assert burned == [tmp_path / 'selection' / 'translated_selection.srt']
    assert '译文0\nOriginal English source' in burned[0].read_text(encoding='utf-8')
    output = task['video_path_local']
    original = tmp_path / 'selection' / 'video.mp4'
    assert Path(output) != original and Path(output).stat().st_size > 0
    result = subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_streams', '-of', 'json', output],
                            capture_output=True, check=True, timeout=30)
    stream = json.loads(result.stdout)['streams'][0]
    assert (stream['width'], stream['height']) == (640, 360)
    assert float(stream['duration']) >= 1.9
    def frame(path):
        return subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-ss', '1', '-i', str(path),
                               '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                              capture_output=True, check=True, timeout=30).stdout
    assert frame(output) != frame(original)


def test_bilingual_task_selects_source_over_downloaded_chinese(tmp_path):
    task, burned, batches = run_task(tmp_path)
    assert Path(task['subtitle_path_original']).name == 'video.en.srt'
    assert batches == [['Original English source']]
    assert burned == [tmp_path / 'selection' / 'translated_selection.srt']
    assert task['subtitle_path_translated'] == str(burned[0])
    assert burned[0].read_text(encoding='utf-8') == (
        '1\n00:00:00,200 --> 00:00:01,800\n译文0\nOriginal English source\n\n')
