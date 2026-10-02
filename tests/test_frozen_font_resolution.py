"""Actual path/font functions, AST-isolated: no app/config/db initialization.
Frozen layout follows the actual packager; optional archive verification is not an EXE run.
"""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = os.environ.get('Y2A_PORTABLE_ZIP')
FONTS = ('Roboto-Medium.ttf', 'NotoSansCJKsc-Regular.otf')


def isolated_processor(system):
    namespace = {'os': os, 'sys': system, 'shutil': shutil,
                 '__file__': str(ROOT / 'modules/utils.py')}
    tree = ast.parse((ROOT / 'modules/utils.py').read_text(encoding='utf-8-sig'))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ('get_app_root_dir', 'get_app_subdir')]
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<real-utils>', 'exec'), namespace)
    tree = ast.parse((ROOT / 'modules/task_manager.py').read_text(encoding='utf-8-sig'))
    processor = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TaskProcessor')
    methods = [n for n in processor.body if isinstance(n, ast.FunctionDef) and
               (n.name.startswith('_') and ('font' in n.name))]
    probe = ast.ClassDef(name='TaskProcessor', bases=[], keywords=[], body=methods, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[])), '<real-font-functions>', 'exec'), namespace)
    cls = namespace['TaskProcessor']
    cls._BUNDLED_FONT_EXTENSIONS = {'.ttf', '.otf', '.ttc'}
    return cls, namespace


class FrozenFontResolutionTests(unittest.TestCase):
    def test_release_layout_loads_roboto_and_noto_without_redirecting_writable_root(self):
        if ARCHIVE:
            with zipfile.ZipFile(ARCHIVE) as archive:
                members = set(archive.namelist())
            for name in FONTS:
                self.assertIn('_internal/fonts/' + name, members)
                self.assertNotIn('fonts/' + name, members)
        # Cross-platform CI checks the real packager's fonts mapping, not a local ZIP.
        packager = (ROOT / 'build-tools/build_exe.py').read_text(encoding='utf-8')
        self.assertIn("('../fonts', 'fonts')", packager)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / '_internal/fonts'
            bundled.mkdir(parents=True)
            for name in FONTS:
                shutil.copy2(ROOT / 'fonts' / name, bundled / name)
            # EROS may have an empty EXE/fonts directory; it must not mask _internal.
            (root / 'fonts').mkdir()
            system = SimpleNamespace(frozen=True, executable=str(root / 'Y2A-Auto.exe'), _MEIPASS=str(root / '_internal'))
            cls, ns = isolated_processor(system)
            self.assertEqual(set(cls._iter_bundled_font_paths()), {str(bundled / n) for n in FONTS})
            processor = cls()
            processor.config = {}
            output = root / 'render-fonts'
            output.mkdir()
            result = processor._resolve_subtitle_font(None, str(output))
            self.assertEqual(result['font_family'], 'Roboto Medium')
            self.assertEqual({p.name for p in output.iterdir()}, set(FONTS))
            # Exercise libass with both files discovered through the frozen resolver.
            ass = ('[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n'
                   '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
                   'Style: Default,Roboto Medium,24,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n'
                   '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                   'Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello\\N{\\fnNoto Sans CJK SC}中文字幕\n')
            (root / 'sub.ass').write_text(ass, encoding='utf-8')
            rendered = subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'debug',
                '-f', 'lavfi', '-i', 'color=c=black:s=640x360:d=0.1', '-vf',
                'subtitles=sub.ass:fontsdir=render-fonts', '-frames:v', '1', '-f', 'null', '-'],
                cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            self.assertRegex(rendered.stderr, r'fontselect:.*Roboto-Medium')
            self.assertRegex(rendered.stderr, r'fontselect:.*NotoSansCJKsc-Regular')
            self.assertNotIn('failed to find any fallback', rendered.stderr)
            for name in ('config', 'db', 'logs'):
                self.assertEqual(ns['get_app_subdir'](name), str(root / name))

    def test_frozen_fallback_and_custom_font_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fonts = root / '_internal/fonts'
            fonts.mkdir(parents=True)
            for name in FONTS:
                shutil.copy2(ROOT / 'fonts' / name, fonts / name)
            for meipass in (None, str(root / 'missing-meipass')):
                system = SimpleNamespace(frozen=True, executable=str(root / 'Y2A-Auto.exe'))
                if meipass:
                    system._MEIPASS = meipass
                cls, _ = isolated_processor(system)
                processor = cls()
                processor.config = {'SUBTITLE_FONT_NAME': 'NotoSansCJKsc-Regular.otf'}
                with tempfile.TemporaryDirectory() as output:
                    result = processor._resolve_subtitle_font(None, output)
                    self.assertEqual(result['font_family'], 'Noto Sans CJK SC')
                    self.assertEqual({p.name for p in Path(output).iterdir()}, {FONTS[1]})

    def test_meipass_has_priority_and_source_root_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extracted = root / 'extracted/fonts'
            extracted.mkdir(parents=True)
            shutil.copy2(ROOT / 'fonts' / FONTS[1], extracted / FONTS[1])
            cls, _ = isolated_processor(SimpleNamespace(frozen=True, executable=str(root / 'Y2A-Auto.exe'), _MEIPASS=str(extracted.parent)))
            self.assertEqual(cls._iter_bundled_font_paths(), [str(extracted / FONTS[1])])
        cls, _ = isolated_processor(SimpleNamespace(frozen=False))
        self.assertIn(str(ROOT / 'fonts' / FONTS[0]), cls._iter_bundled_font_paths())


if __name__ == '__main__':
    unittest.main()
