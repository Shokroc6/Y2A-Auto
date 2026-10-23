"""Offline regressions for the 4.11.0/fork conflict boundaries."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from modules.bilibili_uploader import BilibiliUploader
from modules.bili_sdk.utils.AsyncEvent import AsyncEvent
from modules.bili_sdk.video_uploader import VideoUploaderEvents


class MergeUploadErrorTests(unittest.TestCase):
    def test_timeout_after_603_keeps_hint_without_leaking_request_or_retrying(self):
        logger = Mock()
        starts = []

        class FakeUploader(AsyncEvent):
            def __init__(self, *args, **kwargs):
                super().__init__()
                self.pages = kwargs['pages']

            async def start(self):
                starts.append(True)
                self.dispatch(VideoUploaderEvents.SUBMIT_FALLBACK.value, {
                    'from': 'bcut_pc', 'to': 'web', 'code': 603, 'risk': True})
                raise TimeoutError('https://private.example/?token=PRIVATE headers=PRIVATE')

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            video = Path(directory) / 'video.mp4'
            cover = Path(directory) / 'cover.jpg'
            video.write_bytes(b'fake')
            cover.write_bytes(b'fake')
            for name, kwargs in {
                'setup_task_logger': {'return_value': logger},
                'configure_bilibili_runtime': {},
                'load_credential_from_file': {'return_value': object()},
                'validate_credential_remote': {'return_value': (True, 'ok')},
                'ensure_credential_buvid': {'return_value': False},
                'video_uploader.VideoMeta': {'return_value': Mock()},
                'video_uploader.VideoUploader': {'new': FakeUploader},
            }.items():
                stack.enter_context(patch('modules.bilibili_uploader.' + name, **kwargs))
            ok, message = BilibiliUploader('unused.json').upload_video(
                str(video), str(cover), 'title', 'description', [], 21)
        self.assertFalse(ok)
        self.assertEqual(len(starts), 1)
        self.assertIn('603', message)
        self.assertIn('结果未知', message)
        self.assertNotIn('PRIVATE', message)
        self.assertNotIn('PRIVATE', str(logger.mock_calls))


class MergeConfigTests(unittest.TestCase):
    def test_submit_api_and_fork_copyright_save_together(self):
        from modules.config_manager import update_config
        with patch('modules.config_manager.load_config', return_value={}), patch(
            'modules.config_manager.save_config', return_value=True
        ) as save:
            update_config({'BILIBILI_SUBMIT_API': 'bcut-pc', 'UPLOAD_COPYRIGHT_TYPE': 'original'})
        saved = save.call_args.args[0]
        self.assertEqual(saved['BILIBILI_SUBMIT_API'], 'bcut_pc')
        self.assertEqual(saved['UPLOAD_COPYRIGHT_TYPE'], 'original')


class MergeCookiePersistenceTests(unittest.TestCase):
    def test_rewrite_retains_recognized_extras_and_drops_unknown_browser_cookie(self):
        import json
        from modules.bilibili_auth import load_credential_from_file, save_credential_to_file
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.json'
            path.write_text(json.dumps({'SESSDATA': 'fake', 'bili_jct': 'fake',
                'DedeUserID': '42', 'sid': 'sid-value', '_uuid': 'uuid-value',
                'CURRENT_FID': 'browser-only'}))
            credential = load_credential_from_file(str(path))
            self.assertTrue(save_credential_to_file(credential, str(path)))
            cookies = {item['name']: item['value'] for item in json.loads(path.read_text())}
            self.assertEqual(cookies['sid'], 'sid-value')
            self.assertEqual(cookies['_uuid'], 'uuid-value')
            self.assertNotIn('CURRENT_FID', cookies)

    def test_atomic_replace_failure_preserves_original_and_removes_temporary_file(self):
        from modules.bilibili_auth import build_credential, save_credential_to_file
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.json'
            original = b'{"SESSDATA": "fake-original"}'
            path.write_bytes(original)
            credential = build_credential({'SESSDATA': 'fake-new'})
            with patch('modules.bilibili_auth.os.replace', side_effect=OSError('disk failure')):
                self.assertFalse(save_credential_to_file(credential, str(path)))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])
