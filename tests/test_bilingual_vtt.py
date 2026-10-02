"""Fresh task VTT regression: real indexed translation, writer and ASS."""
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from modules import task_manager as tm
from modules.subtitle_translator import SubtitleReader
from test_subtitle_translator_pairing import _make_requester, _make_translator


class BilingualVttTests(unittest.TestCase):
    def run_task(self, directory, vtt, embed=False):
        task_dir = Path(directory) / 'offline'
        task_dir.mkdir()
        source = task_dir / 'video.en.vtt'
        source.write_text(vtt, encoding='utf-8')
        (task_dir / 'video.zh.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\n已有中文\n\n', encoding='utf-8')
        requester = _make_requester()
        translator = _make_translator(requester, bilingual=True)
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True,
                            'SUBTITLE_EMBED_IN_VIDEO': embed, 'VIDEO_ENCODER': 'cpu', 'VIDEO_CPU_PRESET': 'ultrafast'}
        task = {'video_path_local': str(Path(directory) / 'input.mp4'), 'upload_target': 'acfun', 'status': tm.TASK_STATES['TRANSLATING_SUBTITLE']}
        ass_text = []
        convert = processor._convert_srt_to_ass
        def capture(*args, **kwargs):
            result = convert(*args, **kwargs)
            if result:
                ass_text.append(Path(args[1]).read_text(encoding='utf-8'))
            return result
        def update(task_id, **kwargs):
            task.update(kwargs)
        with patch.object(tm, 'DOWNLOADS_DIR', directory), patch.object(tm, 'get_task', return_value=task), patch.object(tm, 'update_task', side_effect=update), patch('modules.subtitle_translator.create_translator_from_config', return_value=translator), patch('modules.subtitle_translator.openai_chat_create_with_thinking_control', side_effect=requester.fake_create), patch.object(processor, '_convert_srt_to_ass', side_effect=capture):
            self.assertTrue(processor._translate_subtitle('offline', logging.getLogger('vtt-test')))
            output = task_dir / 'translated_offline.srt'
            if not embed:
                self.assertTrue(processor._convert_srt_to_ass(str(output), str(task_dir / 'paired.ass'), logging.getLogger('vtt-test')))
        return requester.sent_batches, SubtitleReader.read_srt(str(output), preserve_lines=True), ass_text, task

    def test_monolingual_keeps_legacy_preconversion(self):
        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory) / 'offline'
            task_dir.mkdir()
            (task_dir / 'video.en.vtt').write_text('WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello world.\n\n00:00:05.000 --> 00:00:06.000\nHello world.\n\n', encoding='utf-8')
            for bilingual, translation in [(False, True), (True, False)]:
                processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
                processor.config = {'SUBTITLE_BILINGUAL_ENABLED': bilingual, 'SUBTITLE_TRANSLATION_ENABLED': translation, 'SUBTITLE_EMBED_IN_VIDEO': False}
                requester = _make_requester()
                translator = _make_translator(requester)
                with patch.object(tm, 'DOWNLOADS_DIR', directory), patch.object(tm, 'get_task', return_value={'video_path_local': 'unused'}), patch.object(tm, 'update_task'), patch.object(processor, '_convert_vtt_to_srt', wraps=processor._convert_vtt_to_srt) as convert, patch('modules.subtitle_translator.create_translator_from_config', return_value=translator), patch('modules.subtitle_translator.openai_chat_create_with_thinking_control', side_effect=requester.fake_create):
                    self.assertTrue(processor._translate_subtitle('offline', logging.getLogger('vtt-test')))
                    convert.assert_called_once()
                items = SubtitleReader.read_srt(str(task_dir / 'video.en.srt'))
                self.assertEqual([(x.start_time, x.end_time) for x in items], [('00:00:01,000', '00:00:06,000')])

    def test_real_embed_wrapper_preserves_gap_and_literal(self):
        import shutil
        import subprocess
        import json
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        self.assertTrue(ffmpeg and ffprobe, 'FFmpeg and ffprobe required')
        vtt = ('WEBVTT\n\n00:00:00.000 --> 00:00:00.750\nLiteral &lt;tag&gt; {value} &amp; 42\n42\nKeep this.\n\n'
               '00:00:01.000 --> 00:00:02.000\nHello world.\n\n00:00:05.000 --> 00:00:06.000\nHello world.\n\n')
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'input.mp4'
            subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=640x360:r=24:d=7', '-c:v', 'libx264', '-preset', 'ultrafast', str(video)], check=True, capture_output=True)
            # Keep real subprocess execution; close upstream wrapper's pipe handles.
            from contextlib import ExitStack
            real_popen = subprocess.Popen
            with ExitStack() as resources:
                def managed_popen(*args, **kwargs):
                    return resources.enter_context(real_popen(*args, **kwargs))
                with patch.object(tm, 'get_ffmpeg_path', return_value=ffmpeg), patch.object(tm, 'get_ffprobe_path', return_value=ffprobe), patch.object(tm.subprocess, 'Popen', side_effect=managed_popen):
                    batches, paired, ass, task = self.run_task(directory, vtt, embed=True)
            output = task['video_path_local']
            self.assertNotEqual(output, str(video))
            self.assertTrue(Path(output).is_file())
            self.assertEqual(len(paired), 3)
            self.assertIn(tm.TaskProcessor._escape_ass_text('Literal <tag> {value} & 42\n42\nKeep this.'), ass[0])
            self.assertEqual(ass[0].count('Dialogue:'), 3)
            probe = json.loads(subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', output], check=True, capture_output=True, text=True).stdout)
            self.assertEqual(probe['streams'][0]['codec_type'], 'video')
            def frame(path, second):
                return subprocess.run([ffmpeg, '-v', 'error', '-ss', str(second), '-i', str(path), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], check=True, capture_output=True).stdout
            for second in (0.3, 1.5, 5.5):
                self.assertNotEqual(frame(output, second), frame(video, second))
                self.assertGreater(max(frame(output, second)), 100)
            self.assertEqual(frame(output, 3), frame(video, 3), 'Silent gap must have no subtitle pixels')

    def test_real_114_cue_source_preserved(self):
        import hashlib
        import html
        import re
        source = Path('/home/ubuntu/arclight/y2a-migration-audit/real-subtitles-XN3xNJvWXsc/XN3xNJvWXsc.en.vtt')
        if not source.is_file():
            self.skipTest('Optional external 114-cue sample is not distributed; synthetic regressions always run')
        original = source.read_bytes()
        # Independent raw payload/timestamp oracle, not the production reader.
        raw = re.findall(r'(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})\n(.*?)(?=\n\n|\Z)', original.decode(), re.S)
        self.assertEqual(len(raw), 114)
        with tempfile.TemporaryDirectory() as directory:
            batches, paired, ass, _ = self.run_task(directory, original.decode())
            expected = [html.unescape(text.strip()) for _, _, text in raw]
            self.assertEqual([text for batch in batches for text in batch], expected)
            self.assertEqual(len(paired), 114)
            self.assertEqual(ass[0].count('Dialogue:'), 114)
            for (start, end, _), text, item in zip(raw, expected, paired):
                self.assertEqual((item.start_time, item.end_time), (start.replace('.', ','), end.replace('.', ',')))
                self.assertTrue(item.source_text.startswith(text + '\n'))
                self.assertIn(tm.TaskProcessor._escape_ass_text(text), ass[0])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), hashlib.sha256(original).hexdigest())
        print('Real source: 114 raw/AI/paired/ASS cues; SHA256', hashlib.sha256(original).hexdigest())

    def test_literal_payload_survives_complete_task(self):
        expected = 'Literal <tag> {value} & 42\n42\nKeep this.'
        with tempfile.TemporaryDirectory() as directory:
            batches, paired, ass, task = self.run_task(directory, 'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nLiteral &lt;tag&gt; {value} &amp; 42\n42\nKeep this.\n\n')
            self.assertEqual([text for batch in batches for text in batch], [expected])
            self.assertEqual(paired[0].source_text, expected + '\n译文0')
            self.assertIn(tm.TaskProcessor._escape_ass_text(expected + '\n译文0'), ass[0])
            self.assertEqual(Path(task['subtitle_path_original']).suffix, '.vtt')

    def test_identical_cues_keep_silent_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            batches, paired, ass, _ = self.run_task(directory, 'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello world.\n\n00:00:05.000 --> 00:00:06.000\nHello world.\n\n')
            self.assertEqual([text for batch in batches for text in batch], ['Hello world.', 'Hello world.'])
            self.assertEqual([(x.start_time, x.end_time) for x in paired], [('00:00:01,000', '00:00:02,000'), ('00:00:05,000', '00:00:06,000')])
            self.assertEqual(ass[0].count('Dialogue:'), 2)
            self.assertIn('0:00:01.00,0:00:02.00', ass[0])
            self.assertIn('0:00:05.00,0:00:06.00', ass[0])
