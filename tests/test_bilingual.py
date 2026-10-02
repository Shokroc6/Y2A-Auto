import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from modules import task_manager as tm


class BilingualSettingsTests(unittest.TestCase):
    def test_subtitle_help_source_above_translation_metadata_unchanged(self):
        template = (Path(__file__).resolve().parents[1] / 'templates/settings.html').read_text()
        self.assertIn('双语字幕（原文在上、译文在下）', template)
        self.assertIn('英语源、中文目标时，英文在上、中文在下', template)
        self.assertIn('双语标题（中文 | 原文）', template)
        self.assertIn('双语简介（中文在前）', template)

    def test_switches_default_off_and_are_saved(self):
        from modules.config_manager import DEFAULT_CONFIG
        root = Path(__file__).resolve().parents[1]
        import ast
        tree = ast.parse((root / 'app.py').read_text())
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SETTINGS_CHECKBOX_FIELDS' for t in n.targets))
        saved = {n.value for n in ast.walk(assignment) if isinstance(n, ast.Constant)}
        template = (root / 'templates/settings.html').read_text()
        for key in ['SUBTITLE_BILINGUAL_ENABLED', 'BILINGUAL_TITLE_ENABLED', 'BILINGUAL_DESCRIPTION_ENABLED']:
            self.assertIs(DEFAULT_CONFIG.get(key), False)
            self.assertIn(key, saved)
            self.assertIn(f'name="{key}"', template)


class BilingualDownloadTests(unittest.TestCase):
    def test_download_only_source_language_respects_auto_opt_in(self):
        from modules.youtube_handler import _build_subtitle_download_args
        config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True,
                  'SUBTITLE_SOURCE_LANGUAGE': 'en', 'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED': True}
        args = _build_subtitle_download_args(config, include_subtitles=True)
        self.assertNotIn('--all-subs', args)
        self.assertEqual(args[args.index('--sub-langs') + 1], 'en')
        self.assertIn('--write-subs', args)
        self.assertIn('--write-auto-subs', args)
        config['SUBTITLE_SOURCE_LANGUAGE'] = 'ja'
        config['YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED'] = False
        args = _build_subtitle_download_args(config, include_subtitles=True)
        self.assertIn('--write-subs', args)
        self.assertNotIn('--write-auto-subs', args)
        self.assertEqual(args[args.index('--sub-langs') + 1], 'ja')


class BilingualOutputTests(unittest.TestCase):
    def test_ass_preserves_bilingual_lines(self):
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'translated_offline.srt'
            output = Path(tmp) / 'out.ass'
            source.write_text('1\n00:00:00,000 --> 00:00:02,500\nLine one.\n42\nLine three.\n中文译文。\n\n')
            self.assertTrue(processor._convert_srt_to_ass(str(source), str(output), logging.getLogger('offline')))
            self.assertIn('Line one.\\N42\\NLine three.\\N中文译文。', output.read_text())

    def test_translation_preserves_multiline_numbers_and_timing(self):
        from test_subtitle_translator_pairing import _make_translator
        translator = _make_translator()
        translator.config.bilingual = True
        source_text = 'Line one.\n42\nLine three.'
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input.srt'
            output = Path(tmp) / 'output.srt'
            source.write_text('1\n00:00:00,000 --> 00:00:02,500\n' + source_text + '\n\n')
            def translate(items, output_path, *args):
                self.assertEqual(items[0].source_text, source_text)
                items[0].translated_text = '中文译文。'
                return translator._write_translated_file(items, output_path)
            with patch.object(translator, '_translate_concurrent', side_effect=translate):
                self.assertTrue(translator.translate_file(str(source), str(output)))
            self.assertEqual(output.read_text(), '1\n00:00:00,000 --> 00:00:02,500\n' + source_text + '\n中文译文。\n\n')


class BilingualTaskTests(unittest.TestCase):
    def test_metadata_composition_is_independent_bounded_and_repeatable(self):
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = {'TRANSLATE_TITLE': True, 'TRANSLATE_DESCRIPTION': True,
                            'BILINGUAL_TITLE_ENABLED': True, 'BILINGUAL_DESCRIPTION_ENABLED': True}
        task = {'video_title_original': 'Original title', 'description_original': 'Original description', 'upload_target': 'acfun'}
        response = {'success': True, 'requested_fields': ['title', 'description'], 'title': '中文标题', 'description': '中文简介'}
        with patch.object(tm, 'get_task', return_value=task), patch.object(tm, 'update_task') as update, patch('modules.ai_enhancer.translate_video_metadata', return_value=response):
            for _ in range(2):
                self.assertTrue(processor._translate_content('offline', logging.getLogger('offline')))
                values = update.call_args.kwargs
                self.assertEqual(values['video_title_translated'], '中文标题 | Original title')
                self.assertEqual(values['description_translated'], '中文简介\n\nOriginal description')
                task.update(values)
            processor.config['BILINGUAL_TITLE_ENABLED'] = False
            response['description'] = '中' * 3000
            task['description_original'] = 'E' * 3000
            processor._translate_content('offline', logging.getLogger('offline'))
            values = update.call_args.kwargs
            self.assertEqual(values['video_title_translated'], '中文标题')
            self.assertEqual(values['description_translated'], '中' * 3000 + '\n\n' + 'E' * 3000)

    def test_disabled_translation_keeps_monolingual_without_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / 'offline'
            task_dir.mkdir()
            (task_dir / 'video.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nEnglish source\n\n')
            processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
            processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': False, 'SUBTITLE_EMBED_IN_VIDEO': False}
            with patch.object(tm, 'DOWNLOADS_DIR', tmp), patch.object(tm, 'get_task', return_value={'video_path_local': 'unused'}), patch.object(tm, 'update_task') as update, patch('modules.subtitle_translator.create_translator_from_config') as factory:
                self.assertTrue(processor._translate_subtitle('offline', logging.getLogger('offline')))
                factory.assert_not_called()
                self.assertIsNone(update.call_args.kwargs['subtitle_path_translated'])

    def test_existing_chinese_does_not_bypass_source_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / 'offline'
            task_dir.mkdir()
            for language, text in [('zh', '已有中文'), ('en', 'Source 42.')]:
                (task_dir / f'video.{language}.srt').write_text(f'1\n00:00:00,000 --> 00:00:01,000\n{text}\n\n')
            processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
            processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True, 'SUBTITLE_EMBED_IN_VIDEO': False}
            task = {'video_path_local': 'unused', 'upload_target': 'acfun'}
            calls = []
            class Translator:
                def translate_file(self, source, output, **kwargs):
                    calls.append(source)
                    return True
            with patch.object(tm, 'DOWNLOADS_DIR', tmp), patch.object(tm, 'get_task', return_value=task), patch.object(tm, 'update_task'), patch('modules.subtitle_translator.create_translator_from_config', return_value=Translator()):
                self.assertTrue(processor._translate_subtitle('offline', logging.getLogger('offline')))
            self.assertEqual([Path(p).name for p in calls], ['video.en.srt'])
