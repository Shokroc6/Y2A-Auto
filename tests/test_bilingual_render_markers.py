"""Bilingual burn-in preserves authored cue text around known format tokens."""
import logging
from pathlib import Path
import re
import tempfile
import unittest

from test_subtitle_rendering_layout import _load_task_processor_class


class BilingualRenderMarkerTests(unittest.TestCase):
    def setUp(self):
        processor_class = _load_task_processor_class()
        processor_class._convert_srt_to_ass.__globals__.update(
            re=re, __name__='modules._isolated_task_processor', _as_bool=bool,
        )
        self.processor = processor_class.__new__(processor_class)
        self.processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True,
                                 'SUBTITLE_TRANSLATION_ENABLED': True}

    def render(self, text, filename='translated_cached.srt'):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / filename
            target = Path(directory) / 'video.ass'
            source.write_text(text, encoding='utf-8')
            self.assertTrue(self.processor._convert_srt_to_ass(
                str(source), str(target), logging.getLogger(__name__),
                video_width=1920, video_height=1080, font_family='Arial', style_overrides={}))
            self.assertEqual(source.read_text(encoding='utf-8'), text)
            return [line.split(',', 9) for line in target.read_text(encoding='utf-8').splitlines()
                    if line.startswith('Dialogue:')]

    def test_known_markers_removed_without_reflow_or_retiming_cached_bilingual_srt(self):
        cues = ('1\n00:00:01,000 --> 00:00:02,000\n'
                '\u200b\u200b<b>\u200b \u200b- Are you ready?\u200b \u200b</b>\u200b\n'
                '<B>- Yes.</B>\\h\n'
                '你准备好了吗？\n- 好了。\n\n'
                '2\n00:00:01,500 --> 00:00:03,000\n'
                'Keep \\h \\h this line\\h\\h\n- And this one\\H\n保留。\n')
        events = self.render(cues)
        self.assertEqual([row[1:3] for row in events],
                         [['0:00:01.00', '0:00:02.00'], ['0:00:01.50', '0:00:03.00']])
        self.assertEqual([row[-1] for row in events],
                         [r'- Are you ready?\N- Yes.\N你准备好了吗？\N- 好了。',
                          r'Keep this line\N- And this one\N保留。'])

    def test_preserves_technical_literals_and_unrelated_unicode(self):
        text = (r'Use <T>, a < b > c, C:\home\h' '\n'
                r'Keep \\h and `\h` with \help and &lt;b&gt;' '\n'
                'ภาษา\u200bไทย \u200d 中文')
        events = self.render('1\n00:00:01,000 --> 00:00:02,000\n' + text + '\n')
        self.assertEqual(events[0][-1], self.processor._escape_ass_text(text))

    def test_other_subtitle_inputs_keep_legacy_conversion(self):
        cue = '1\n00:00:01,000 --> 00:00:02,000\n<b>First</b>\\h\nSecond\n'
        for flags, filename in [((False, True), 'translated_cached.srt'),
                                ((True, False), 'translated_cached.srt'),
                                ((True, True), 'video.en.srt')]:
            with self.subTest(flags=flags, filename=filename):
                self.processor.config.update(SUBTITLE_BILINGUAL_ENABLED=flags[0],
                                             SUBTITLE_TRANSLATION_ENABLED=flags[1])
                events = self.render(cue, filename)
                self.assertEqual(len(events), 1)
                self.assertIn('<b>First</b>', events[0][-1])
                self.assertNotIn(r'\N', events[0][-1])
