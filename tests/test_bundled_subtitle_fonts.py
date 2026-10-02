"""Offline bundled subtitle font regression checks (no app/provider access)."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.config_manager import DEFAULT_CONFIG
from modules import task_manager as tm

ROOT = Path(__file__).resolve().parents[1]


class BundledSubtitleFontsTests(unittest.TestCase):
    def resolve(self, config):
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = config
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(tm, 'get_app_subdir', return_value=str(ROOT / 'fonts')):
                result = processor._resolve_subtitle_font(None, directory)
            files = {p.name for p in Path(directory).iterdir()}
        return result, files

    def test_roboto_loads_bundled_chinese_fallback(self):
        result, files = self.resolve({'SUBTITLE_FONT_NAME': 'Roboto-Medium.ttf'})
        self.assertEqual(files, {'Roboto-Medium.ttf', 'NotoSansCJKsc-Regular.otf'})

    def test_custom_bundled_selection_is_preserved(self):
        result, files = self.resolve({'SUBTITLE_FONT_NAME': 'NotoSansCJKsc-Regular.otf'})
        self.assertEqual(result['font_family'], 'Noto Sans CJK SC')
        self.assertEqual(files, {'NotoSansCJKsc-Regular.otf'})

    def test_roboto_full_name_and_blank_setting(self):
        for selection in ('Roboto Medium', '   '):
            result, files = self.resolve({'SUBTITLE_FONT_NAME': selection})
            self.assertEqual(result['font_family'], 'Roboto Medium')
            self.assertIn('NotoSansCJKsc-Regular.otf', files)

    def test_custom_font_keeps_ass_text_unmodified(self):
        document = tm.TaskProcessor._build_default_ass_document(
            [{'start': 0, 'end': 1, 'text': 'English\n中文', 'preserve_lines': True}],
            'Noto Sans CJK SC', 640, 360)
        self.assertIn('English\\N中文', document)
        self.assertNotIn('\\fn', document)

    def test_windows_packaging_covers_font_license_directory(self):
        import ast
        tree = ast.parse((ROOT / 'build-tools/build_exe.py').read_text(encoding='utf-8-sig'))
        pairs = [ast.literal_eval(node) for node in ast.walk(tree)
                 if isinstance(node, ast.Tuple) and len(node.elts) == 2
                 and all(isinstance(x, ast.Constant) for x in node.elts)]
        self.assertIn(('../fonts', 'fonts'), pairs)
        self.assertIn('Apache License', (ROOT / 'fonts/Roboto-LICENSE.txt').read_text())

    def test_real_ffmpeg_selects_roboto_and_noto_for_chinese(self):
        import os
        import shutil
        import subprocess
        ffmpeg = shutil.which('ffmpeg')
        self.assertIsNotNone(ffmpeg, 'FFmpeg with libass is required for this renderer check')
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = {}
        with tempfile.TemporaryDirectory() as directory:
            fonts = Path(directory) / 'fonts'
            fonts.mkdir()
            with patch.object(tm, 'get_app_subdir', return_value=str(ROOT / 'fonts')):
                font = processor._resolve_subtitle_font(None, str(fonts))
            document = processor._build_default_ass_document(
                [{'start': 0, 'end': 1, 'text': 'Hello world\n中文字幕测试', 'preserve_lines': True}],
                font['font_family'], 640, 360)
            self.assertIn('Hello world\\N{\\fnNoto Sans CJK SC}中文字幕测试{\\fnRoboto Medium}', document)
            (Path(directory) / 'sub.ass').write_text(document, encoding='utf-8')
            command = [ffmpeg, '-hide_banner', '-loglevel', 'debug', '-f', 'lavfi',
                       '-i', 'color=c=black:s=640x360:d=0.1', '-vf',
                       'subtitles=sub.ass:fontsdir=fonts', '-frames:v', '1', '-f', 'null', '-']
            completed = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=30)
            if os.environ.get('Y2A_FONT_LOG'):
                Path(os.environ['Y2A_FONT_LOG']).write_text(
                    'COMMAND: ' + repr(command) + '\n' + completed.stderr, encoding='utf-8')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertRegex(completed.stderr, r'fontselect:.*Roboto-Medium')
            self.assertRegex(completed.stderr, r'fontselect:.*NotoSansCJKsc-Regular')
            self.assertNotIn('failed to find any fallback', completed.stderr)

    def test_factory_default_selects_static_roboto_medium(self):
        self.assertEqual(DEFAULT_CONFIG['SUBTITLE_FONT_NAME'], 'Roboto-Medium.ttf')
        result, files = self.resolve({})
        self.assertEqual(result['configured_font_name'], 'Roboto-Medium.ttf')
        self.assertEqual(result['font_family'], 'Roboto Medium')
        self.assertIn('Roboto-Medium.ttf', files)


if __name__ == '__main__':
    unittest.main()
