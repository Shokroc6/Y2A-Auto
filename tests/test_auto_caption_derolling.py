"""YouTube automatic-caption rolling window: offline, AI mocked at the HTTP call."""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import task_manager as tm
from modules.subtitle_translator import SubtitleItem, SubtitleReader, deroll_auto_captions
from test_subtitle_translator_pairing import _make_requester, _make_translator

# Self-authored fixture in the exact yt-dlp --write-auto-subs --convert-subs srt shape.
ROLLING_CUES = [
    ('00:00:00,240', '00:00:02,149', ' \nFirst line here'),
    ('00:00:02,149', '00:00:02,159', 'First line here\n '),
    ('00:00:02,159', '00:00:04,150', 'First line here\nsecond line, 42'),
    ('00:00:04,150', '00:00:04,160', 'second line, 42\n '),
    ('00:00:04,160', '00:00:06,150', 'second line, 42\nthird line'),
    ('00:00:06,150', '00:00:06,160', 'third line\n '),
    ('00:00:06,160', '00:00:08,000', 'third line\nthird line'),  # genuinely repeated speech
    ('00:00:08,000', '00:00:08,010', 'third line\n '),
    ('00:00:08,010', '00:00:09,000', ' '),                         # empty cue
    ('00:00:12,000', '00:00:14,000', ' \n[Music]'),              # fresh window after a gap
    ('00:00:14,000', '00:00:14,010', '[Music]\n '),
    ('00:01:00,000', '00:01:02,500', ' \nAfter the music'),
]
SINGLE_LINES = ['First line here', 'second line, 42', 'third line', 'third line', '[Music]', 'After the music']
# Two adjacent new lines per screen, no repetition; a >1.5s silence or the last odd line stays single.
EXPECTED = [
    ('00:00:00,240', '00:00:04,160', 'First line here second line, 42'),
    ('00:00:04,160', '00:00:08,010', 'third line third line'),
    ('00:00:12,000', '00:00:14,010', '[Music]'),
    ('00:01:00,000', '00:01:02,500', 'After the music'),
]
MANUAL_CUES = [
    ('00:00:01,000', '00:00:03,000', 'PATRICK: The uniform\ncode of justice'),
    ('00:00:03,000', '00:00:05,000', 'specifies court martial'),
    ('00:00:05,000', '00:00:07,000', 'for any officer\nwho sends a soldier'),
    ('00:00:07,000', '00:00:09,000', 'who sends a soldier\ninto battle.'),  # one incidental repeat
]


def _srt(cues):
    return ''.join(f'{i}\n{s} --> {e}\n{t}\n\n' for i, (s, e, t) in enumerate(cues, 1))


def _vtt(cues):
    return 'WEBVTT\n\n' + ''.join(f"{s.replace(',', '.')} --> {e.replace(',', '.')}\n{t}\n\n" for s, e, t in cues)


def _items(cues):
    return [SubtitleItem(index=i, start_time=s, end_time=e, source_text=t.strip())
            for i, (s, e, t) in enumerate(cues, 1) if t.strip()]


def _lines(items):
    return [line.strip() for item in items for line in item.source_text.split('\n') if line.strip()]


def _collapse(lines):
    return [line for i, line in enumerate(lines) if i == 0 or line != lines[i - 1]]


class DerollFunctionTests(unittest.TestCase):
    def test_rolling_window_becomes_two_adjacent_new_lines_per_cue(self):
        out = deroll_auto_captions(_items(ROLLING_CUES))
        self.assertEqual([(x.start_time, x.end_time, x.source_text) for x in out], EXPECTED)
        self.assertEqual([x.index for x in out], list(range(1, len(EXPECTED) + 1)))

    def test_no_words_lost_and_no_rolling_left(self):
        source = _items(ROLLING_CUES)
        out = deroll_auto_captions(source)
        # Every source line survives, in order (only window repetition removed).
        self.assertEqual(_collapse(SINGLE_LINES), _collapse(_lines(source)))
        self.assertEqual(' '.join(_lines(out)).split(), ' '.join(SINGLE_LINES).split())
        self.assertTrue(all(len(x.source_text.split('\n')) == 1 for x in out))

    def test_non_rolling_track_is_returned_unchanged(self):
        source = _items(MANUAL_CUES)
        self.assertIs(deroll_auto_captions(source), source)
        self.assertEqual(deroll_auto_captions([]), [])

    def test_derolled_output_is_a_fixed_point(self):
        once = deroll_auto_captions(_items(ROLLING_CUES))
        self.assertIs(deroll_auto_captions(once), once)


class DerollTaskPathTests(unittest.TestCase):
    """Real TaskProcessor._translate_subtitle, indexed translator, writer; AI call mocked."""

    def run_task(self, directory, name, content, metadata):
        task_dir = Path(directory) / 'offline'
        task_dir.mkdir(exist_ok=True)
        source = task_dir / name
        if not source.exists():
            source.write_text(content, encoding='utf-8')
        if metadata is not None:
            (task_dir / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
        requester = _make_requester()
        translator = _make_translator(requester, bilingual=True)
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True,
                            'SUBTITLE_SOURCE_LANGUAGE': 'en', 'SUBTITLE_EMBED_IN_VIDEO': False,
                            'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED': True}
        task = {'video_path_local': str(task_dir / 'video.mp4'), 'upload_target': 'acfun',
                'status': tm.TASK_STATES['TRANSLATING_SUBTITLE']}
        with patch.object(tm, 'DOWNLOADS_DIR', directory), \
                patch.object(tm, 'get_task', return_value=task), \
                patch.object(tm, 'update_task', side_effect=lambda _id, **kw: task.update(kw)), \
                patch('modules.subtitle_translator.create_translator_from_config', return_value=translator), \
                patch('modules.subtitle_translator.openai_chat_create_with_thinking_control',
                      side_effect=requester.fake_create):
            self.assertTrue(processor._translate_subtitle('offline', logging.getLogger('deroll-test')))
        sent = [text for batch in requester.sent_batches for text in batch]
        paired = SubtitleReader.read_srt(str(task_dir / 'translated_offline.srt'), preserve_lines=True)
        return source, sent, paired, task

    AUTO_META = {'subtitles': {}, 'automatic_captions': {'en': [{'ext': 'srv1'}], 'en-orig': [{'ext': 'srv1'}]}}

    def test_automatic_srt_ai_receives_derolled_items_and_pairs(self):
        raw = _srt(ROLLING_CUES)
        with tempfile.TemporaryDirectory() as directory:
            source, sent, paired, task = self.run_task(directory, 'video.en.srt', raw, self.AUTO_META)
            self.assertEqual(sent, [t for _, _, t in EXPECTED])
            self.assertEqual([(x.start_time, x.end_time) for x in paired], [(s, e) for s, e, _ in EXPECTED])
            self.assertEqual([x.source_text.split('\n') for x in paired],
                             [[t, f'译文{i % 3}'] for i, (_, _, t) in enumerate(EXPECTED)])
            self.assertEqual(task['subtitle_path_original'], str(source))
            backup = Path(str(source) + '.rolling')
            self.assertEqual(backup.read_text(encoding='utf-8'), raw)
            derolled = source.read_bytes()
            # Retry: source reused untouched, backup kept, AI sees the same items.
            _, sent_again, paired_again, _ = self.run_task(directory, 'video.en.srt', raw, self.AUTO_META)
            self.assertEqual(source.read_bytes(), derolled)
            self.assertEqual(backup.read_text(encoding='utf-8'), raw)
            self.assertEqual(sent_again, sent)
            self.assertEqual(len(paired_again), len(EXPECTED))

    def test_automatic_en_orig_vtt_is_derolled(self):
        with tempfile.TemporaryDirectory() as directory:
            cues = ROLLING_CUES[:4] + [('00:00:04,160', '00:00:06,150', 'second line, 42\nfish &amp; chips &lt;b&gt;')]
            meta = {'subtitles': {}, 'automatic_captions': {'en-orig': [{'ext': 'srv1'}]}}
            source, sent, paired, _ = self.run_task(directory, 'video.en-orig.vtt', _vtt(cues), meta)
            self.assertEqual(sent, ['First line here second line, 42', 'fish & chips <b>'])
            self.assertEqual(len(paired), 2)
            self.assertEqual([x.source_text for x in SubtitleReader.read_vtt(str(source), preserve_lines=True)], sent)

    def test_manual_track_is_byte_identical_and_untouched(self):
        meta = {'subtitles': {'en': [{'ext': 'vtt'}]}, 'automatic_captions': self.AUTO_META['automatic_captions']}
        for cues in (MANUAL_CUES, ROLLING_CUES):  # even rolling-looking text under a manual key
            raw = _srt(cues)
            with tempfile.TemporaryDirectory() as directory:
                source, sent, paired, _ = self.run_task(directory, 'video.en.srt', raw, meta)
                self.assertEqual(source.read_text(encoding='utf-8'), raw)
                self.assertFalse(Path(str(source) + '.rolling').exists())
                self.assertEqual(sent, [x.source_text for x in _items(cues)])
                self.assertEqual(len(paired), len(_items(cues)))

    def test_missing_metadata_or_non_rolling_auto_is_untouched(self):
        for cues, meta in ((ROLLING_CUES, None), (MANUAL_CUES, self.AUTO_META)):
            raw = _srt(cues)
            with tempfile.TemporaryDirectory() as directory:
                source, sent, _, _ = self.run_task(directory, 'video.en.srt', raw, meta)
                self.assertEqual(source.read_text(encoding='utf-8'), raw)
                self.assertFalse(Path(str(source) + '.rolling').exists())
                self.assertEqual(sent, [x.source_text for x in _items(cues)])


if __name__ == '__main__':
    unittest.main()
