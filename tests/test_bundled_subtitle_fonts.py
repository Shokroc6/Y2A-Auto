"""Offline bundled subtitle font regression checks (no app/provider access)."""
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modules import config_manager as cm
from modules import task_manager as tm

NOTO = 'NotoSansCJKsc-Regular.otf'
ROBOTO = 'Roboto-Medium.ttf'


class BundledSubtitleFontsTests(unittest.TestCase):
    def resolve(self, config):
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = config
        with tempfile.TemporaryDirectory() as directory:
            result = processor._resolve_subtitle_font(None, directory)
            files = {p.name for p in Path(directory).iterdir()}
        return result, files

    def test_saved_roboto_and_full_name_keep_chinese_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cm, 'get_app_subdir', return_value=directory):
                cm.save_config({**cm.DEFAULT_CONFIG, 'SUBTITLE_FONT_NAME': ROBOTO})
                saved = cm.load_config()
        self.assertEqual(saved['SUBTITLE_FONT_NAME'], ROBOTO)
        for config in (saved, {'SUBTITLE_FONT_NAME': 'Roboto Medium'}):
            result, files = self.resolve(config)
            self.assertEqual(result['font_family'], 'Roboto Medium')
            self.assertEqual(files, {ROBOTO, NOTO})

    def test_custom_bundled_selection_is_preserved(self):
        selected = 'SourceHanSansHWSC-VF.otf'
        result, files = self.resolve({'SUBTITLE_FONT_NAME': selected})
        self.assertEqual(Path(result['font_path']).name, selected)
        self.assertEqual(files, {selected})
        document = tm.TaskProcessor._build_default_ass_document(
            [{'start': 0, 'end': 1, 'text': 'English\n中文', 'preserve_lines': True}],
            result['font_family'], 640, 360)
        self.assertIn('English\\N中文', document)
        self.assertNotIn('\\fn', document)

    def test_bundled_fonts_are_independent_of_writable_runtime_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(tm, 'get_app_subdir', return_value=directory):
                _, files = self.resolve({'SUBTITLE_FONT_NAME': NOTO})
        self.assertEqual(files, {NOTO})

    def render(self, config, text):
        ffmpeg = shutil.which('ffmpeg')
        self.assertIsNotNone(ffmpeg, 'FFmpeg with libass is required for this renderer check')
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fonts = root / 'fonts'
            fonts.mkdir()
            font = processor._resolve_subtitle_font(None, str(fonts))
            srt, ass = root / 'translated_fixture.srt', root / 'sub.ass'
            srt.write_text(f'1\n00:00:00,000 --> 00:00:01,000\n{text}\n', encoding='utf-8')
            self.assertTrue(processor._convert_srt_to_ass(
                str(srt), str(ass), logging.getLogger(__name__), video_width=640,
                video_height=360, font_family=font['font_family']))
            command = [ffmpeg, '-hide_banner', '-loglevel', 'debug', '-f', 'lavfi',
                       '-i', 'color=c=black:s=640x360:d=0.1', '-vf',
                       'subtitles=sub.ass:fontsdir=fonts', '-frames:v', '1', '-f', 'null', '-']
            completed = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn('failed to find any fallback', completed.stderr)
            return ass.read_text(encoding='utf-8'), [
                line.split(' -> ', 1)[-1] for line in completed.stderr.splitlines() if 'fontselect:' in line]

    def test_real_ffmpeg_uses_only_noto_for_latin_and_cjk_by_default(self):
        for script, text in (('latin', 'Hello world!'), ('cjk', '中文字幕测试。')):
            with self.subTest(script=script):
                document, selected = self.render({}, text)
                self.assertIn('Style: Default,Noto Sans CJK SC,', document)
                self.assertIn(text, document)
                self.assertNotIn('\\fn', document)
                self.assertTrue(selected)
                self.assertTrue(all('NotoSansCJKsc-Regular' in line for line in selected), selected)

    def test_real_ffmpeg_keeps_explicit_roboto_with_its_cjk_fallback(self):
        document, selected = self.render({
            'SUBTITLE_FONT_NAME': ROBOTO, 'SUBTITLE_BILINGUAL_ENABLED': True,
            'SUBTITLE_TRANSLATION_ENABLED': True,
        }, 'Hello world\n中文字幕测试')
        self.assertIn('Hello world\\N{\\fnNoto Sans CJK SC}中文字幕测试{\\fnRoboto Medium}', document)
        self.assertTrue(any('Roboto-Medium' in line for line in selected), selected)
        self.assertTrue(any('NotoSansCJKsc-Regular' in line for line in selected), selected)

    def test_clean_config_and_blank_values_select_only_noto(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cm, 'get_app_subdir', return_value=directory):
                clean_config = cm.load_config()
        self.assertEqual(clean_config['SUBTITLE_FONT_NAME'], NOTO)
        for config in (clean_config, {}, {'SUBTITLE_FONT_NAME': '   '}, {'SUBTITLE_FONT_NAME': NOTO}):
            with self.subTest(config_font=config.get('SUBTITLE_FONT_NAME')):
                result, files = self.resolve(config)
                self.assertEqual(result['configured_font_name'], NOTO)
                self.assertEqual(result['font_family'], 'Noto Sans CJK SC')
                self.assertEqual(files, {NOTO})


if __name__ == '__main__':
    unittest.main()
