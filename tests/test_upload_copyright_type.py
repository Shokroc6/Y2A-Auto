"""Upload type setting: repost (default) or self-made, shared by both platforms."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modules import acfun_uploader, bilibili_uploader
from modules.config_manager import DEFAULT_CONFIG


class UploadTypeConfigTests(unittest.TestCase):
    def test_default_is_repost(self):
        self.assertEqual(DEFAULT_CONFIG['UPLOAD_COPYRIGHT_TYPE'], 'repost')

    def test_normalize_rejects_unknown_values(self):
        from modules.config_manager import normalize_upload_copyright_type
        self.assertEqual(normalize_upload_copyright_type('original'), 'original')
        self.assertEqual(normalize_upload_copyright_type(' ORIGINAL '), 'original')
        self.assertEqual(normalize_upload_copyright_type('bogus'), 'repost')
        self.assertEqual(normalize_upload_copyright_type(None), 'repost')

    def test_settings_page_offers_both_choices(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'templates', 'settings.html'), encoding='utf-8') as f:
            html = f.read()
        self.assertIn('name="UPLOAD_COPYRIGHT_TYPE"', html)
        self.assertIn('value="repost"', html)
        self.assertIn('value="original"', html)


class _FileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = os.path.join(self.tmp.name, 'v.mp4')
        self.cover = os.path.join(self.tmp.name, 'c.jpg')
        for path in (self.video, self.cover):
            with open(path, 'wb') as f:
                f.write(b'x')

    def tearDown(self):
        self.tmp.cleanup()


class AcfunUploadTypeTests(_FileCase):
    def _upload(self, **kwargs):
        uploader = object.__new__(acfun_uploader.AcfunUploader)
        uploader.logger = None
        uploader.log = lambda *a, **k: None
        uploader.login = lambda: True
        uploader.create_douga = MagicMock(return_value=(True, {'ac_number': 'ac1'}))
        uploader.upload_video(
            self.video, self.cover, 'title', 'desc', ['t'], 1,
            original_url='https://www.youtube.com/watch?v=x',
            original_uploader='Someone', original_upload_date='20260101',
            **kwargs,
        )
        return uploader.create_douga.call_args.kwargs

    def test_repost_by_default(self):
        call = self._upload()
        self.assertEqual(call['creation_type'], 1)
        self.assertEqual(call['original_url'], 'https://www.youtube.com/watch?v=x')
        self.assertEqual(call['desc'], '原作者：Someone\n发布日期：2026-01-01\n视频链接：https://www.youtube.com/watch?v=x\n\ndesc')

    def test_original_drops_source_and_notice(self):
        call = self._upload(copyright_type='original')
        self.assertEqual(call['creation_type'], 3)
        self.assertEqual(call['original_url'], '')
        self.assertNotIn('转载', call['desc'])


class BilibiliUploadTypeTests(_FileCase):
    def _upload(self, **kwargs):
        captured = {}

        class FakeMeta:
            def __init__(self, **meta):
                captured.update(meta)

        uploader = object.__new__(bilibili_uploader.BilibiliUploader)
        uploader.cookie_file = 'unused'
        with patch.object(bilibili_uploader, 'configure_bilibili_runtime'), \
                patch.object(bilibili_uploader, 'load_credential_from_file'), \
                patch.object(bilibili_uploader, 'validate_credential_remote', return_value=(True, '')), \
                patch.object(bilibili_uploader.video_uploader, 'VideoMeta', FakeMeta), \
                patch.object(bilibili_uploader.video_uploader, 'VideoUploaderPage', side_effect=RuntimeError('stop')):
            uploader.upload_video(
                self.video, self.cover, 'title', 'desc', ['t'], 17,
                youtube_url='https://www.youtube.com/watch?v=x', **kwargs,
            )
        return captured

    def test_repost_by_default(self):
        meta = self._upload()
        self.assertFalse(meta['original'])
        self.assertEqual(meta['source'], 'https://www.youtube.com/watch?v=x')

    def test_original_has_no_source(self):
        meta = self._upload(copyright_type='original')
        self.assertTrue(meta['original'])
        self.assertIsNone(meta['source'])

    def test_original_description_has_no_repost_notice(self):
        desc = bilibili_uploader.format_bilibili_description(
            'body', original_url='https://www.youtube.com/watch?v=x',
            original_uploader='Someone', copyright_type='original',
        )
        self.assertEqual(desc, 'body')



class UpdateConfigUploadTypeTests(unittest.TestCase):
    """设置页保存走 update_config：自制能保存，非法值回退为转载。"""

    def setUp(self):
        import shutil
        from modules import config_manager
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-copyright-config-')
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        p = patch.object(config_manager, 'get_app_subdir', side_effect=lambda _n: self.tmpdir)
        p.start()
        self.addCleanup(p.stop)
        self.config_manager = config_manager

    def _saved(self):
        import json
        with open(os.path.join(self.tmpdir, 'config.json'), encoding='utf-8') as f:
            return json.load(f)['UPLOAD_COPYRIGHT_TYPE']

    def test_save_original(self):
        self.config_manager.update_config({'UPLOAD_COPYRIGHT_TYPE': 'original'})
        self.assertEqual(self._saved(), 'original')

    def test_save_invalid_falls_back(self):
        self.config_manager.update_config({'UPLOAD_COPYRIGHT_TYPE': 'hack'})
        self.assertEqual(self._saved(), 'repost')

if __name__ == '__main__':
    unittest.main()
