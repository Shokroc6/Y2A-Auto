"""Offline task-chain contracts for existing-track reuse."""
from pathlib import Path
import pytest
from test_bilingual_selection import run_task
from modules.subtitle_translator import SubtitleItem, SubtitleReader, SubtitleWriter


NUMERIC_TEXT = 'The answer is\n42\nDo not lose this line'
NUMERIC_CUE = f'1\n00:00:00,200 --> 00:00:01,800\n{NUMERIC_TEXT}\n\n'


@pytest.mark.parametrize('preserve_lines', [False, True])
@pytest.mark.parametrize('variant', ['standard', 'crlf', 'short-hour-dot', 'no-index', 'no-blank'])
def test_reader_preserves_numeric_body_and_next_cue(tmp_path, preserve_lines, variant):
    text = NUMERIC_CUE + '2\n00:00:02,000 --> 00:00:03,000\nNext cue\n'
    if variant == 'crlf':
        text = text.replace('\n', '\r\n')
    elif variant == 'short-hour-dot':
        text = text.replace('00:00:', '0:00:').replace(',', '.')
    elif variant == 'no-index':
        text = text[2:].replace('\n2\n', '\n')
    elif variant == 'no-blank':
        text = text.replace('\n\n', '\n')
    source = tmp_path / 'source.srt'
    source.write_bytes(text.encode('utf-8'))
    items = SubtitleReader.read_srt(source, preserve_lines=preserve_lines)
    assert [item.source_text for item in items] == [
        NUMERIC_TEXT if preserve_lines else 'The answer is 42 Do not lose this line', 'Next cue']
    assert items[0].start_time == ('0:00:00,200' if variant == 'short-hour-dot' else '00:00:00,200')
    assert items[0].end_time == ('0:00:01,800' if variant == 'short-hour-dot' else '00:00:01,800')


def test_merge_preserves_numeric_body_exactly(tmp_path):
    from modules.existing_subtitle_merge import merge_existing_tracks
    source, target, output = [tmp_path / name for name in ('source.srt', 'target.srt', 'out.srt')]
    source.write_text(NUMERIC_CUE, encoding='utf-8')
    target.write_text(NUMERIC_CUE.replace(NUMERIC_TEXT, '答案是四十二'), encoding='utf-8')
    merge_existing_tracks(source, target, output)
    assert output.read_text(encoding='utf-8') == NUMERIC_CUE.replace(NUMERIC_TEXT, '答案是四十二\n' + NUMERIC_TEXT)


@pytest.mark.parametrize('prepare_upload', [False, True])
def test_numeric_body_survives_real_task_burn(tmp_path, prepare_upload):
    import json
    import shutil
    import subprocess
    task, burned, batches = run_task(tmp_path, strategy=None, translation=False,
        real_embed=True, prepare_upload=prepare_upload, subtitles={'video.en.srt': NUMERIC_CUE})
    assert batches == [] and task['test_translator_created'] == 0
    assert task['subtitle_path_translated'] == str(burned[0])
    assert burned[0].read_text(encoding='utf-8') == NUMERIC_CUE.replace(NUMERIC_TEXT, '平台中文字幕\n' + NUMERIC_TEXT)
    dialogue = next(line for line in task['test_ass_text'].splitlines() if line.startswith('Dialogue:'))
    import re
    # Rendering may insert font-size overrides without changing authored text.
    plain_dialogue = re.sub(r'\{\\[^}]*\}', '', dialogue)
    assert r'The answer is\N42\NDo not lose this line' in plain_dialogue
    output = task['video_path_local']
    probe = subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_streams', '-of', 'json', output],
                           capture_output=True, check=True, timeout=30)
    stream = json.loads(probe.stdout)['streams'][0]
    assert (stream['width'], stream['height']) == (640, 360)
    assert float(stream['duration']) >= 1.9
    def frame(path):
        return subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-ss', '1', '-i', str(path),
            '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
            capture_output=True, check=True, timeout=30).stdout
    original = frame(burned[0].parent / 'video.mp4')
    rendered = frame(output)
    assert len(rendered) == len(original) == 640 * 360
    assert rendered != original


def track(*cues):
    return ''.join(f'{i}\n00:00:{start:06.3f} --> 00:00:{end:06.3f}\n{text}\n\n'.replace('.', ',', 2)
                   for i, (start, end, text) in enumerate(cues, 1))


@pytest.mark.parametrize('reverse', [False, True])
def test_time_alignment_groups_one_to_many_without_text_loss(tmp_path, reverse):
    from modules.existing_subtitle_merge import merge_existing_tracks
    a = track((0.2, 1.8, 'Whole\n<literal> & {brace}'))
    b = track((0.2, 0.9, '第一\n第二'), (0.9, 1.8, '第三'))
    source, target = tmp_path / 'source.srt', tmp_path / 'target.srt'
    source.write_text(b if reverse else a, encoding='utf-8')
    target.write_text(a if reverse else b, encoding='utf-8')
    output = tmp_path / 'out.srt'
    merge_existing_tracks(str(source), str(target), str(output))
    items = SubtitleReader.read_srt(str(output), preserve_lines=True)
    assert len(items) == 1
    assert items[0].source_text == ('Whole\n<literal> & {brace}\n第一\n第二\n第三' if reverse
                                    else '第一\n第二\n第三\nWhole\n<literal> & {brace}')
    assert (items[0].start_time, items[0].end_time) == ('00:00:00,200', '00:00:01,800')


@pytest.mark.parametrize('cues', [
    ((0.5, 1.8, 'shift'),),  # >200ms boundary drift
    ((0.2, 0.3, 'tiny'), (1.7, 1.8, 'tiny2')),  # <80% coverage
    ((0.2, 1.0, 'a'), (0.9, 1.8, 'overlap')),  # overlapping same track
    ((0.2, 1.8, 'ok'), (2.0, 3.0, 'extra')),
    ((1.8, 2.8, 'touch only'),),
    ((0.2, 0.2, 'zero'),),
    ((0.2, 20.0, 'long'),),
    ((0.2, 0.5, 'a'), (0.5, 0.8, 'b'), (0.8, 1.1, 'c'), (1.1, 1.4, 'd'), (1.4, 1.8, 'e')),
    ((0.9, 1.8, 'late'), (0.2, 0.9, 'early')),
])
def test_unreliable_alignment_rejected_atomically(tmp_path, cues):
    from modules.existing_subtitle_merge import merge_existing_tracks
    a, b, out = [tmp_path / name for name in ('a.srt', 'b.srt', 'out.srt')]
    a.write_text(track((0.2, 1.8, 'source')), encoding='utf-8')
    b.write_text(track(*cues), encoding='utf-8')
    with pytest.raises(ValueError):
        merge_existing_tracks(str(a), str(b), str(out))
    assert not out.exists()


@pytest.mark.parametrize('translation', [False, True])
def test_failed_alignment_falls_back_with_visible_reason(tmp_path, translation):
    task, burned, batches = run_task(tmp_path, strategy=None, translation=translation,
        subtitles={'video.zh.srt': track((3, 4, '不匹配'))})
    assert bool(batches) is translation
    assert '时间' in task['subtitle_warning_message']
    if translation:
        assert '不匹配' not in burned[0].read_text(encoding='utf-8')
    else:
        assert task['subtitle_path_translated'] is None
        assert burned[0].name == 'video.zh.srt'


@pytest.mark.parametrize('source,target,languages', [
    ('ja-JP', 'en-US', ('zh', 'en', 'ja')),
    ('zh', 'ja', ('zh', 'ja')),
    ('auto', 'ja', ('ja', 'en')),
])
def test_reuse_respects_configured_languages(tmp_path, source, target, languages):
    task, burned, batches = run_task(tmp_path, strategy=None, source_language=source,
                                   target_language=target, languages=languages)
    assert batches == []
    expected_source = source.split('-')[0] if source != 'auto' else 'en'
    assert Path(task['subtitle_path_original']).name == f'video.{expected_source}.srt'
    assert task['subtitle_path_translated'] == str(burned[0])


@pytest.mark.parametrize('languages,subtitles,source', [
    (('zh',), {}, 'auto'),
    (('zh', 'en', 'ja'), {}, 'auto'),
    (('zh',), {'video.unknown.srt': track((0.2, 1.8, 'unknown'))}, 'auto'),
    (('zh', 'en'), {}, 'ja'),
])
def test_missing_or_ambiguous_source_is_explicit_monolingual(tmp_path, languages, subtitles, source):
    task, burned, batches = run_task(tmp_path, strategy=None, languages=languages,
                                   subtitles=subtitles, source_language=source)
    assert batches == []
    assert task['subtitle_path_translated'] is None
    assert '单语' in task['subtitle_warning_message']
    assert burned[0].name == 'video.zh.srt'


def test_existing_vtt_merge_real_multiline_burn_without_translation(tmp_path):
    import shutil
    import subprocess
    task, burned, batches = run_task(tmp_path, languages=(), strategy=None, translation=False,
        real_embed=True, subtitles={
            'video.en.vtt': 'WEBVTT\n\n00:00:00.200 --> 00:00:01.800\nFirst &lt;vector&gt;\nSecond &amp; {value}\nThird\n\n',
            'video.zh.srt': track((0.2, 0.9, '第一行'), (0.9, 1.8, '第二行')),
        })
    assert batches == []
    text = burned[0].read_text(encoding='utf-8')
    assert '第一行\n第二行\nFirst <vector>\nSecond & {value}\nThird' in text
    dialogue = next(line for line in task['test_ass_text'].splitlines()
                    if line.startswith('Dialogue:'))
    assert dialogue.count(r'\N') >= 4
    assert 'First <vector>' in dialogue and 'Third' in dialogue
    def frame(path):
        return subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-ss', '1', '-i', str(path),
            '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
            capture_output=True, check=True, timeout=30).stdout
    original = frame(burned[0].parent / 'video.mp4')
    output = frame(task['video_path_local'])
    assert len(output) == len(original) == 640 * 360
    assert output != original


@pytest.mark.parametrize('translation', [False, True])
def test_missing_target_respects_translation_switch(tmp_path, translation):
    task, burned, batches = run_task(tmp_path, strategy=None, languages=('en',), translation=translation)
    assert bool(batches) is translation
    assert (task['subtitle_path_translated'] is not None) is translation


def test_existing_merge_still_obeys_qc_gate(tmp_path):
    task, burned, batches = run_task(tmp_path, strategy=None, qc_failed=True)
    assert batches == [] and burned == []
    assert task['subtitle_warning_message'] == 'subtitle_qc_rejected'


@pytest.mark.parametrize('translation', [False, True])
def test_explicit_retranslation_requires_translation_switch(tmp_path, translation):
    task, burned, batches = run_task(tmp_path, strategy='retranslate', translation=translation)
    assert bool(batches) is translation
    assert ('译文0' in burned[0].read_text(encoding='utf-8')) is translation


@pytest.mark.parametrize('translation', [False, True])
def test_monolingual_keeps_existing_target_without_ai(tmp_path, translation):
    task, burned, batches = run_task(tmp_path, strategy=None, bilingual=False, translation=translation)
    assert batches == [] and task['subtitle_path_translated'] is None
    assert burned[0].name == 'video.zh.srt'


@pytest.mark.parametrize('delta,accepted', [(0.2, True), (0.201, False)])
def test_alignment_drift_threshold(tmp_path, delta, accepted):
    from modules.existing_subtitle_merge import merge_existing_tracks
    a, b, out = [tmp_path / name for name in ('a.srt', 'b.srt', 'out.srt')]
    a.write_text(track((0.2, 1.8, 'source')), encoding='utf-8')
    b.write_text(track((0.2 + delta, 1.8 + delta, 'target')), encoding='utf-8')
    if accepted:
        merge_existing_tracks(str(a), str(b), str(out))
        assert len(SubtitleReader.read_srt(str(out))) == 1
    else:
        with pytest.raises(ValueError):
            merge_existing_tracks(str(a), str(b), str(out))


@pytest.mark.parametrize('translation', [False, True])
def test_upload_preparation_uses_same_merge_chain(tmp_path, translation):
    task, burned, batches = run_task(tmp_path, strategy=None, translation=translation, prepare_upload=True)
    assert batches == []
    assert task['subtitle_path_translated'] == str(burned[0])
    assert '平台中文字幕\nOriginal English source' in burned[0].read_text(encoding='utf-8')


def test_multiple_target_tracks_decline_merge_without_ai_when_disabled(tmp_path):
    task, burned, batches = run_task(tmp_path, strategy=None, translation=False,
        subtitles={'other.zh.srt': track((0.2, 1.8, '另一中文'))})
    assert batches == [] and task['subtitle_path_translated'] is None
    assert '多个' in task['subtitle_warning_message']


def test_malformed_cue_cannot_be_silently_dropped(tmp_path):
    from modules.existing_subtitle_merge import merge_existing_tracks
    a, b, out = [tmp_path / name for name in ('a.srt', 'b.srt', 'out.srt')]
    a.write_text(track((0.2, 1.8, 'source')), encoding='utf-8')
    b.write_text(track((0.2, 1.8, 'target')) + '2\n00:xx:02,000 --> 00:00:03,000\nMust not disappear\n', encoding='utf-8')
    with pytest.raises(ValueError):
        merge_existing_tracks(str(a), str(b), str(out))
    assert not out.exists()


def test_missing_configured_source_never_translates_other_language(tmp_path):
    task, burned, batches = run_task(tmp_path, strategy=None, languages=('en',), source_language='ja')
    assert batches == []
    assert task['subtitle_path_translated'] is None
    assert '源语言' in task['subtitle_warning_message']


def test_merge_writer_failure_is_not_reported_as_success(tmp_path):
    from modules.existing_subtitle_merge import merge_existing_tracks
    a, b = tmp_path / 'a.srt', tmp_path / 'b.srt'
    a.write_text(track((0.2, 1.8, 'source')), encoding='utf-8')
    b.write_text(track((0.2, 1.8, 'target')), encoding='utf-8')
    with pytest.raises(OSError):
        merge_existing_tracks(str(a), str(b), str(tmp_path / 'missing' / 'out.srt'))


def test_monolingual_translation_respects_explicit_source(tmp_path):
    task, burned, batches = run_task(tmp_path, strategy=None, bilingual=False,
                                   source_language='ja', languages=('en', 'ja'))
    assert batches == [['日本語の原文']]
    assert Path(task['subtitle_path_original']).name == 'video.ja.srt'


@pytest.mark.parametrize('translation', [False, True])
def test_default_merges_existing_tracks_without_ai(tmp_path, translation):
    task, burned, batches = run_task(tmp_path, strategy=None, translation=translation)
    assert batches == []
    assert task['test_translator_created'] == 0
    assert Path(task['subtitle_path_original']).name == 'video.en.srt'
    assert task['subtitle_path_translated'] == str(burned[0])
    assert burned[0].read_text(encoding='utf-8') == (
        '1\n00:00:00,200 --> 00:00:01,800\n平台中文字幕\nOriginal English source\n\n')
