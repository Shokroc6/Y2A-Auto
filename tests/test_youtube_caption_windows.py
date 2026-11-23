"""Offline regression for YouTube's non-rolling automatic display windows."""
from copy import deepcopy
from io import StringIO
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegSubtitlesConvertorPP

from modules import youtube_handler as yh
from test_youtube_manual_tracks import CONFIG

BASE = 'https://www.youtube.com/api/timedtext?v=sample&lang=en&kind=asr&fmt='


def timing(ms, separator=','):
    return f'00:00:{ms // 1000:02d}{separator}{ms % 1000:03d}'


def event(start=200, end=5300, window=1, text='The first sentence', **extra):
    return {'tStartMs': start, 'dDurationMs': end - start, 'wWinId': window,
            'segs': [{'utf8': text}], **extra}


def source(events):
    return ''.join(f'{i}\n{timing(e["tStartMs"])} --> '
                   f'{timing(e["tStartMs"] + e["dDurationMs"])}\n'
                   f'{e["segs"][0]["utf8"]}\n\n' for i, e in enumerate(events, 1))


class CaptionWindowTests(unittest.TestCase):
    def setUp(self):
        self.events = [event(), event(2800, 8300, text='A different sentence')]
        self.original = source(self.events)
        self.expected = self.original.replace('05,300', '02,800')

    def download(self, *, automatic=True, receipt='vtt', cached=None, retry=False, retain=False,
                 config=None, progress=None):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'downloads' / 'sample'
            directory.mkdir(parents=True)
            srt = directory / 'video.en-orig.srt'
            if cached:
                (directory / cached).write_text(self.original, encoding='utf-8')
            if retain:
                srt.write_text(self.original, encoding='utf-8')
            info = {('automatic_captions' if automatic else 'subtitles'):
                    {'en-orig': [{'ext': 'vtt', 'url': BASE + 'vtt'}]}}
            n = 0
            original_run, original_popen = subprocess.run, subprocess.Popen

            def stream(cmd, **kwargs):
                if cmd[0] != 'python':
                    return original_popen(cmd, **kwargs)
                return SimpleNamespace(stdout=StringIO(run(cmd).stdout + '\n'
                    '[download] 50.0% of 2MiB at 1MiB/s ETA 00:01\n'), returncode=0, pid=123, wait=lambda: None)

            def run(cmd, **kwargs):
                nonlocal n
                if cmd[0] != 'python':
                    return original_run(cmd, **kwargs)
                n += 1
                (directory / 'video.mp4').touch()
                if retry and n == 1:
                    srt.write_text(self.original, encoding='utf-8')
                    raise subprocess.CalledProcessError(2, cmd, 'Requested format is not available')
                opts = yt_dlp.parse_options(cmd[3:] + ['--no-warnings']).ydl_opts
                with yt_dlp.YoutubeDL({**opts, 'quiet': True}) as ydl:
                    tracks = ydl.process_subtitles('sample', info.get('subtitles', {}), info.get('automatic_captions', {}))
                    sub = tracks['en-orig'] | {'filepath': str(directory / f'video.en-orig.{receipt}'), 'ext': receipt}
                    if receipt != 'missing':
                        # Exercise both the CLI's processed template and the real VTT -> SRT converter.
                        vtt = Path(sub['filepath'])
                        vtt.write_text('WEBVTT\n\n' + self.original.replace(',', '.').replace('1\n00:', '00:').replace('2\n00:', '00:'), encoding='utf-8')
                        data = {'id': 'sample', 'extractor_key': 'Youtube', 'requested_subtitles': {'en-orig': sub}}
                        output = '\n'.join(ydl.evaluate_outtmpl(t, data) for t in opts['forceprint'].get('before_dl', []))
                        if receipt == 'vtt':
                            pp = FFmpegSubtitlesConvertorPP(ydl, 'srt')
                            remove, _ = pp.run({'requested_subtitles': deepcopy(data['requested_subtitles']),
                                                '__files_to_move': {str(vtt): str(vtt)}})
                            for old in remove:
                                Path(old).unlink()
                        else:
                            srt.write_text(self.original, encoding='utf-8')
                    else:
                        output = ''
                        srt.write_text(self.original, encoding='utf-8')
                return subprocess.CompletedProcess(cmd, 0, output, '')

            log = logging.getLogger('window-test')
            log_stream = StringIO()
            handler = logging.StreamHandler(log_stream)
            prior_disable = logging.root.manager.disable
            logging.disable(logging.NOTSET)
            self.addCleanup(logging.disable, prior_disable)
            log.setLevel(logging.DEBUG)
            log.addHandler(handler)
            self.addCleanup(log.removeHandler, handler)
            with patch.object(yh, 'get_app_subdir', side_effect=lambda p: str(Path(tmp) / p)), \
                 patch.object(yh, 'setup_task_logger', return_value=logging.getLogger('window-test')), \
                 patch.object(yh, '_find_yt_dlp_command', return_value=['python', '-m', 'yt_dlp']), \
                 patch.object(yh, 'test_video_availability', return_value=(True, json.dumps(info), None)), \
                 patch.object(yh, 'get_ffmpeg_path', return_value=None), \
                 patch.object(yh, 'is_docker_env', return_value=False), \
                 patch.object(yh, 'load_config', return_value=config or CONFIG), \
                 patch.object(yh, '_get_youtube_runtime_args', return_value=[]), \
                 patch('yt_dlp.utils.get_executable_path', return_value=tmp), \
                 patch('yt_dlp.utils.get_system_config_dirs', return_value=[]), \
                 patch('yt_dlp.utils.get_user_config_dirs', return_value=[]), \
                 patch('modules.youtube_caption_windows.getproxies', return_value={}), \
                 patch.object(yh.time, 'sleep'), patch.object(yh.subprocess, 'run', side_effect=run), \
                 patch.object(yh.subprocess, 'Popen', side_effect=stream):
                result = yh.download_video_data('https://www.youtube.com/watch?v=sample', 'sample',
                                                only_video=True, progress_callback=progress)
            self.assertTrue(result[0], result[1])
            self.assertNotIn('/api/timedtext', log_stream.getvalue())
            return srt.read_text(encoding='utf-8').strip()

    def test_download_trims_only_previous_end_before_translation(self):
        from unittest.mock import Mock
        with patch('http.client.HTTPSConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({'wireMagic': 'pb3', 'events': [
                {'id': 1, 'tStartMs': 0, 'wpWinPosId': 1, 'wsWinStyleId': 1}, *self.events]}).encode()
            self.assertEqual(self.download(), self.expected.strip())
            progress = Mock()
            self.assertEqual(self.download(progress=progress), self.expected.strip())
            self.assertEqual(progress.call_args.args[0]['percent'], 50.0)
            request = connection.return_value.request.call_args
            self.assertIn('fmt=json3', request.args[1])

    def test_uses_no_direct_lookup_when_youtube_proxy_is_configured(self):
        config = CONFIG | {'YOUTUBE_PROXY_ENABLED': True, 'YOUTUBE_PROXY_URL': 'http://proxy.invalid:8000'}
        with patch('http.client.HTTPSConnection') as connection:
            self.assertEqual(self.download(config=config), self.original.strip())
            connection.assert_not_called()

    def test_unverified_downloads_do_not_request_json3(self):
        cases: tuple[dict[str, Any], ...] = (
            {'automatic': False}, {'receipt': 'missing'}, {'receipt': 'srt'},
            {'cached': 'translated_sample.srt'}, {'cached': 'video.en-orig.vtt'},
            {'retain': True}, {'retry': True},
        )
        for args in cases:
            with self.subTest(args=args), patch('http.client.HTTPSConnection') as conn:
                self.assertEqual(self.download(**args), self.original.strip())
                conn.assert_not_called()

    def test_bad_http_evidence_keeps_original_without_redirecting(self):
        for status, body in ((302, b''), (404, b''), (200, b'invalid JSON')):
            with self.subTest(status=status), patch('http.client.HTTPSConnection') as conn:
                response = conn.return_value.getresponse.return_value
                response.status = status
                response.read.return_value = body
                self.assertEqual(self.download(), self.original.strip())
                conn.assert_called_once_with('www.youtube.com', timeout=5)

    def test_conservative_event_matching(self):
        from modules.youtube_caption_windows import trim_same_window_replacements
        for change in ({'wWinId': 2}, {'aAppend': True}, {'aAppend': '0'}, {'wWinId': None},
                       {'tStartMs': 200}, {'tStartMs': 2801}, {'dDurationMs': 5499},
                       {'segs': [{'utf8': 'Different content'}]},
                       {'segs': [{'utf8': 'A different sentence', 'tOffsetMs': 1}]},
                       {'segs': []}):
            with self.subTest(change=change):
                payload = {'wireMagic': 'pb3', 'events': [self.events[0], self.events[1] | change]}
                self.assertEqual(trim_same_window_replacements(self.original, payload), self.original)

    def test_missing_window_rolling_and_rerun(self):
        from modules.youtube_caption_windows import trim_same_window_replacements
        payload = {'wireMagic': 'pb3', 'events': deepcopy(self.events)}
        fixed = trim_same_window_replacements(self.original, payload)
        self.assertEqual(fixed, self.expected)
        self.assertEqual(trim_same_window_replacements(fixed, payload), fixed)
        payload['events'][0].pop('wWinId')
        self.assertEqual(trim_same_window_replacements(self.original, payload), self.original)
        rolled = [event(text='First caption\nBits repeated'),
                  event(2800, 8300, text='Bits repeated\nLast caption')]
        self.assertEqual(trim_same_window_replacements(source(rolled), {'wireMagic': 'pb3', 'events': rolled}),
                         source(rolled))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg with libass is required')
    def test_bilingual_window_switch_draws_only_one_caption(self):
        from modules.youtube_caption_windows import trim_same_window_replacements
        from modules.subtitle_translator import SubtitleReader, SubtitleWriter
        from test_subtitle_translator_pairing import _make_requester, _make_translator, _PATCH_TARGET
        from test_subtitle_rendering_layout import _load_task_processor_class

        fixed = trim_same_window_replacements(self.original, {'wireMagic': 'pb3', 'events': self.events})
        processor_cls = _load_task_processor_class()
        processor_cls._convert_srt_to_ass.__globals__.update(
            re=re, __name__='modules._isolated_task_processor', _as_bool=bool,
        )
        processor = processor_cls.__new__(processor_cls)
        processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            def prepare(name, srt, only=None):
                src = directory / 'source.srt'
                translated = directory / f'translated_{name}.srt'
                ass = directory / f'{name}.ass'
                src.write_text(srt, encoding='utf-8')
                items = SubtitleReader.read_srt(str(src), preserve_lines=True)
                if only is None:
                    zh = ('第一段字幕', '第二段字幕')
                    requester = _make_requester(lambda _texts: json.dumps({'translations': [
                        {'index': i, 'translation': text} for i, text in enumerate(zh)]}))
                    translator = _make_translator(requester, bilingual=True)
                    with patch(_PATCH_TARGET, side_effect=getattr(requester, 'fake_create')):
                        self.assertTrue(translator.translate_file(str(src), str(translated)))
                    paired = SubtitleReader.read_srt(str(translated), preserve_lines=True)
                    self.assertEqual([item.time_range for item in paired], [item.time_range for item in items])
                    self.assertEqual([item.source_text for item in paired],
                                     [item.source_text + '\n' + text for item, text in zip(items, zh)])
                else:
                    item = items[only]
                    item.translated_text = item.source_text + '\n' + ('第一段字幕', '第二段字幕')[only]
                    self.assertTrue(SubtitleWriter.write_srt([item], str(translated)))
                self.assertTrue(processor._convert_srt_to_ass(str(translated), str(ass), logging.getLogger(__name__),
                                                             video_width=640, video_height=360,
                                                             font_family='Noto Sans CJK SC', style_overrides={}))
                return ass

            def frame(ass, at):
                return subprocess.check_output(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=640x360:r=100',
                                                '-vf', f'setpts=PTS+{at}/TB,ass={ass.name}',
                                                '-frames:v', '1', '-f', 'rawvideo', '-threads', '1', '-pix_fmt', 'gray', '-'],
                                               cwd=directory)

            working = prepare('fixed', fixed)
            dialogue = [line.split(',', 9) for line in working.read_text(encoding='utf-8').splitlines()
                        if line.startswith('Dialogue:')]
            self.assertEqual([item[1:3] for item in dialogue],
                             [['0:00:00.20', '0:00:02.80'], ['0:00:02.80', '0:00:08.30']])
            self.assertEqual([item[-1] for item in dialogue],
                             [r'The first sentence\N第一段字幕', r'A different sentence\N第二段字幕'])
            before, after = frame(working, '2.79'), frame(working, '2.80')
            self.assertNotEqual(before, after)
            self.assertEqual(before, frame(prepare('first', fixed, 0), '2.79'))
            self.assertEqual(after, frame(prepare('second', fixed, 1), '2.80'))
            self.assertNotEqual(after, frame(prepare('original', self.original), '2.80'))
