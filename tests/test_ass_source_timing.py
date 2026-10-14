"""Source timing regression; isolated TaskProcessor AST, no application import."""
import re
import logging
import tempfile
from pathlib import Path
import unittest

from modules.srt_transform_engine import SrtTransformConfig, SrtTransformEngine
from test_subtitle_rendering_layout import TaskProcessor

# The existing AST harness deliberately loads no task_manager module.
TaskProcessor._build_default_ass_document.__func__.__globals__['re'] = re


class AssSourceTimingTests(unittest.TestCase):
    def test_real_bilingual_conversion_branch_keeps_ten_ms_timing(self):
        TaskProcessor._convert_srt_to_ass.__globals__.update(
            __name__='modules._isolated_task_processor',
            _as_bool=lambda value: bool(value),
        )
        processor = TaskProcessor.__new__(TaskProcessor)
        processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True,
                            'SUBTITLE_TRANSLATION_ENABLED': True}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'translated_regression.srt'
            target = Path(directory) / 'output.ass'
            source.write_text('1\n00:00:31,990 --> 00:00:32,000\nEnglish\n中文\n\n'
                              '2\n00:00:32,000 --> 00:00:33,000\nNext\n下一条\n')
            self.assertTrue(processor._convert_srt_to_ass(
                str(source), str(target), logging.getLogger(__name__),
                video_width=640, video_height=360, font_family='Arial', style_overrides={}))
            events = [line.split(',', 9) for line in target.read_text().splitlines()
                      if line.startswith('Dialogue:')]
            self.assertEqual([(e[1], e[2]) for e in events],
                             [('0:00:31.99', '0:00:32.00'), ('0:00:32.00', '0:00:33.00')])
            self.assertEqual(events[0][-1], r'English\N中文')

    def test_authored_overlap_and_positive_short_timing_are_preserved(self):
        source = ('1\n00:00:01,000 --> 00:00:02,000\nmanual first\n\n'
                  '2\n00:00:01,500 --> 00:00:03,000\nmanual second\n\n'
                  '3\n00:00:03,000 --> 00:00:03,007\nshort\n')
        cues = SrtTransformEngine(SrtTransformConfig()).parse_srt(source, base_offset_s=10)
        self.assertEqual([(c['start'], c['end']) for c in cues],
                         [(11, 12), (11.5, 13), (13, 13.007)])
        ass = TaskProcessor._build_default_ass_document(cues, 'Arial', 640, 360)
        events = [line.split(',', 9) for line in ass.splitlines()
                  if line.startswith('Dialogue:')]
        self.assertEqual([(e[1], e[2]) for e in events],
                         [('0:00:11.00', '0:00:12.00'),
                          ('0:00:11.50', '0:00:13.00'),
                          ('0:00:13.00', '0:00:13.01')])

    def test_nonpositive_timing_keeps_existing_fallback(self):
        cues = SrtTransformEngine(SrtTransformConfig()).parse_srt(
            '1\n00:00:01,000 --> 00:00:01,000\ninvalid\n')
        self.assertEqual((cues[0]['start'], cues[0]['end']), (1, 1.5))

    def test_sub_centisecond_cue_collapsing_at_quantization_is_skipped(self):
        cues = SrtTransformEngine(SrtTransformConfig()).parse_srt(
            '1\n00:00:31,991 --> 00:00:31,994\nshort\n\n'
            '2\n00:00:31,994 --> 00:00:32,000\nnext\n')
        self.assertEqual(cues[0]['end'], 31.994)
        ass = TaskProcessor._build_default_ass_document(cues, 'Arial', 640, 360)
        events = [line.split(',', 9) for line in ass.splitlines()
                  if line.startswith('Dialogue:')]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1:3], ['0:00:31.99', '0:00:32.00'])
        self.assertEqual(events[0][-1], 'next')

    def test_ten_ms_transition_is_not_extended_across_next_cue(self):
        source = (
            '1\n00:00:31,990 --> 00:00:32,000\nEnglish\n中文\n\n'
            '2\n00:00:32,000 --> 00:00:33,000\nEnglish next\n继续\n中文\n'
        )
        cues = SrtTransformEngine(SrtTransformConfig()).parse_srt(source)
        self.assertEqual([(c['start'], c['end']) for c in cues],
                         [(31.99, 32.0), (32.0, 33.0)])
        for cue in cues:
            cue['preserve_lines'] = True
        ass = TaskProcessor._build_default_ass_document(cues, 'Arial', 640, 360)
        events = [line.split(',', 9) for line in ass.splitlines()
                  if line.startswith('Dialogue:')]
        self.assertEqual([(e[1], e[2]) for e in events],
                         [('0:00:31.99', '0:00:32.00'), ('0:00:32.00', '0:00:33.00')])
        self.assertEqual(events[0][-1], r'English\N中文')


if __name__ == '__main__':
    unittest.main()
