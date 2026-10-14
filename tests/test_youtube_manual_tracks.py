import json
import logging
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modules import youtube_handler as yh
import yt_dlp

CONFIG = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True,
          'SUBTITLE_SOURCE_LANGUAGE': 'auto', 'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED': True}


def track(name='English - CC (English)', url='https://example.org/caption'):
    return [{'ext': 'srt', 'name': name, 'url': url}]


class ManualTrackTests(unittest.TestCase):
    def setUp(self):
        previous_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous_disable)

    def run_download(self, info, config=None, existing=True, only_video=True):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'downloads' / 'sample'
            directory.mkdir(parents=True)
            if existing:
                (directory / 'metadata.json').write_text(json.dumps(info))
            commands = []
            log = logging.getLogger('manual-track-test')
            def run(cmd, **kwargs):
                if not isinstance(cmd, list) or cmd[:3] != ['python', '-m', 'yt_dlp']:
                    return subprocess.CompletedProcess(cmd, 0, '', '')
                commands.append(cmd[:])
                (directory / 'video.mp4').write_bytes(b'offline-video-boundary')
                if '--sub-langs' in cmd:
                    opts = yt_dlp.parse_options(cmd[3:] + ['--no-warnings']).ydl_opts
                    with yt_dlp.YoutubeDL({**opts, 'quiet': True}) as ydl:
                        selected = ydl.process_subtitles('sample', info.get('subtitles'), info.get('automatic_captions'))
                    for key in selected or {}:
                        (directory / f'video.{key}.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nCaption\n')
                return subprocess.CompletedProcess(cmd, 0, '', '')
            with patch.object(yh, 'get_app_subdir', side_effect=lambda name: str(Path(tmp) / name)), \
                 patch.object(yh, 'setup_task_logger', return_value=log), \
                 patch.object(yh, '_find_yt_dlp_command', return_value=['python', '-m', 'yt_dlp']), \
                 patch.object(yh, 'test_video_availability', return_value=(True, json.dumps(info), None)), \
                 patch.object(yh, 'get_ffmpeg_path', return_value=None), \
                 patch.object(yh, 'is_docker_env', return_value=False), \
                 patch.object(yh, '_get_youtube_runtime_args', return_value=[]), \
                 patch.object(yh, 'load_config', return_value=config or CONFIG), \
                 patch.object(yh.subprocess, 'run', side_effect=run), self.assertLogs(log, level='INFO') as logs:
                ok, result = yh.download_video_data('https://www.youtube.com/watch?v=sample', 'sample', only_video=only_video)
            self.assertTrue(ok, result)
            keys = [Path(p).name.removeprefix('video.').removesuffix('.srt') for p in result['subtitles_paths']]
            return commands[-1], keys, '\n'.join(logs.output)

    def test_manual_suffix_wins_over_automatic_en_at_real_runner(self):
        info = {'subtitles': {'en-j3PyPqV-e1s': track()}, 'automatic_captions': {'en': track('English')}}
        cmd, keys, logs = self.run_download(info)
        self.assertEqual(keys, ['en-j3PyPqV-e1s'])
        self.assertNotIn('--write-auto-subs', cmd)
        self.assertNotIn('--all-subs', cmd)
        self.assertIn('category=manual', logs)
        self.assertIn('key=en-j3PyPqV-e1s', logs)
        self.assertIn('receipt=downloaded', logs)
        self.assertNotIn('https://example.org', logs)

    def test_manual_region_wins_over_auto_base(self):
        cmd, keys, _ = self.run_download({'subtitles': {'en-US': track()}, 'automatic_captions': {'en': track()}})
        self.assertEqual(keys, ['en-US'])
        self.assertNotIn('--write-auto-subs', cmd)

    def test_manual_order_exact_then_base_then_sorted(self):
        for config_source, expected in [('en-US', 'en-US'), ('en', 'en'), ('en-AU', 'en')]:
            with self.subTest(source=config_source):
                _, keys, _ = self.run_download({'subtitles': {'en-GB': track(), 'en-US': track(), 'en': track()}},
                                              {**CONFIG, 'SUBTITLE_SOURCE_LANGUAGE': config_source})
                self.assertEqual(keys, [expected])
        for tracks in [{'en-US': track(), 'en-GB': track()}, {'en-GB': track(), 'en-US': track()}]:
            _, keys, _ = self.run_download({'subtitles': tracks})
            self.assertEqual(keys, ['en-GB'])

    def test_manual_download_with_auto_disabled(self):
        cmd, keys, _ = self.run_download({'subtitles': {'en-j3': track()}},
                                        {**CONFIG, 'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED': False})
        self.assertEqual(keys, ['en-j3'])
        self.assertNotIn('--write-auto-subs', cmd)

    def test_same_key_manual_does_not_enable_auto_fallback(self):
        cmd, keys, _ = self.run_download({'subtitles': {'en': track()}, 'automatic_captions': {'en': track('Automatic')}})
        self.assertEqual(keys, ['en'])
        self.assertNotIn('--write-auto-subs', cmd)
        opts = yt_dlp.parse_options(cmd[3:] + ['--no-warnings']).ydl_opts
        with yt_dlp.YoutubeDL({**opts, 'quiet': True}) as ydl:
            self.assertFalse(ydl.process_subtitles('sample', {}, {'en': track()}))

    def test_auto_only_requires_opt_in_and_preserves_en_default(self):
        info = {'automatic_captions': {'en-orig': track(), 'en': track()}}
        cmd, keys, _ = self.run_download(info)
        self.assertEqual(keys, ['en'])
        self.assertIn('--write-auto-subs', cmd)
        cmd, keys, logs = self.run_download(info, {**CONFIG, 'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED': False})
        self.assertEqual(keys, [])
        self.assertIn('--no-write-subs', cmd)
        self.assertIn('not found', logs)

    def test_regex_safe_full_key_with_real_yt_dlp_parser(self):
        key = 'en-j3.foo+[CC]'
        _, keys, _ = self.run_download({'subtitles': {key: track(), 'en-j3XfooooC': track()}})
        self.assertEqual(keys, [key])

    def test_excludes_translation_and_false_prefix(self):
        _, keys, _ = self.run_download({'subtitles': {'enonsense': track(), 'en': track(url='https://example.org/caption?tlang=en'), 'en-GB': track()}})
        self.assertEqual(keys, ['en-GB'])
        _, keys, _ = self.run_download({'subtitles': {'enonsense': track(), 'en': track(url='https://example.org/caption?tlang=en')}})
        self.assertEqual(keys, [])

    def test_configured_other_language_is_preserved(self):
        _, keys, _ = self.run_download({'subtitles': {'ja-j3': track('Japanese'), 'en': track()}, 'automatic_captions': {'ja': track()}},
                                     {**CONFIG, 'SUBTITLE_SOURCE_LANGUAGE': 'ja'})
        self.assertEqual(keys, ['ja-j3'])

    def test_fresh_full_download_uses_preflight_metadata(self):
        _, keys, _ = self.run_download({'subtitles': {'en-j3': track()}}, existing=False, only_video=False)
        self.assertEqual(keys, ['en-j3'])

    def test_preflight_returns_metadata_json_without_logging_urls(self):
        info = {'subtitles': {'en-j3': track(url='https://example.org/caption?signature=secret')}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(info), '')
        with patch.object(yh, 'load_config', return_value={}), patch.object(yh, '_get_youtube_runtime_args', return_value=[]), \
             patch.object(yh.subprocess, 'run', return_value=completed) as run:
            ok, data, error = yh.test_video_availability('https://www.youtube.com/watch?v=sample', ['python', '-m', 'yt_dlp'])
        self.assertTrue(ok)
        self.assertEqual(json.loads(data), info)
        self.assertIn('--dump-single-json', run.call_args.args[0])
        self.assertNotIn('--write-subs', run.call_args.args[0])
