import unittest
from modules.acfun_uploader import build_upload_description
from modules.bilibili_uploader import format_bilibili_description


class DescriptionPreservationTests(unittest.TestCase):
    def test_translation_description_is_not_cleaned_truncated_or_url_reviewed(self):
        from modules import ai_enhancer as ai
        from unittest.mock import patch, Mock
        body = '  Source\nhttps://example.org\nhttps://example.org\n' + 'x' * 1100
        output = '  中文简介\nhttps://example.org\nhttps://example.org\n' + '中' * 1100
        with patch.object(ai, 'setup_task_logger', return_value=Mock()), patch.object(ai, 'get_openai_client'), patch.object(ai, '_request_json_object', return_value={'description': output}) as request:
            result = ai.translate_video_metadata('', body, translate_title=False, openai_config={'OPENAI_API_KEY': 'offline-placeholder'})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs['payload']['description'], body)
        self.assertEqual(result['description'], output)
        self.assertTrue(result['success'])
        prompt = request.call_args.kwargs['system_prompt']
        self.assertIn('URL 原样保留', prompt)
        self.assertNotIn('删除导流、社媒、外链', prompt)

    def test_both_platforms_preserve_body_urls_and_metadata_notice(self):
        url = 'https://example.org/watch?v=offline'
        body = '  Source text\r\n' + url + '\nAgain ' + url + '\n\n  '
        expected = ('原视频：Source title\n原作者：Source author\n发布日期：2026-09-30\n视频链接：' + url + '\n\n' + body)
        for formatter in (build_upload_description, format_bilibili_description):
            with self.subTest(formatter=formatter.__name__):
                actual = formatter(body, original_url=url, original_uploader='Source author', original_upload_date='20260930', original_title='Source title')
                self.assertEqual(actual, expected)
                self.assertEqual(actual.count(url), 3)

    def test_task_metadata_reaches_both_platform_notices(self):
        import json
        from pathlib import Path
        from test_preset_tags_pipeline import UploadPathTagResolutionTests, _FakeAcfunUploader, _FakeBilibiliUploader
        harness = UploadPathTagResolutionTests()
        harness.setUp()
        try:
            metadata = Path(harness.temp_dir.name) / 'metadata.json'
            url = 'https://example.org/metadata'
            metadata.write_text(json.dumps({'title': 'Actual metadata title', 'uploader': 'Metadata author', 'upload_date': '20260930', 'webpage_url': url}))
            task = {'id': 'offline', 'upload_target': 'both', 'video_path_local': harness.video_path,
                    'cover_path_local': harness.cover_path, 'video_title_translated': 'Translated title',
                    'description_translated': 'Body\n' + url + '\n' + url,
                    'selected_partition_id_acfun': '1001', 'selected_partition_id_bilibili': '2001',
                    'metadata_json_path_local': str(metadata)}
            config = {'ACFUN_COOKIES_PATH': harness.cookie_path, 'BILIBILI_COOKIES_PATH': harness.cookie_path,
                      'UPLOAD_COPYRIGHT_TYPE': 'original', 'UPLOAD_APPEND_REPOST_NOTICE': True}
            ac = harness._run_upload('_do_upload_to_acfun', task, config, _FakeAcfunUploader, 'modules.acfun_uploader.AcfunUploader')
            bi = harness._run_upload('_do_upload_to_bilibili', task, config, _FakeBilibiliUploader, 'modules.bilibili_uploader.BilibiliUploader', is_bilibili=True)
            self.assertEqual(ac['original_title'], 'Actual metadata title')
            expected = build_upload_description(ac['description'], original_title=ac['original_title'], original_url=ac['original_url'], original_uploader=ac['original_uploader'], original_upload_date=ac['original_upload_date'])
            self.assertEqual(bi['description'], expected)
            self.assertEqual(expected.count(url), 3)
        finally:
            harness.tearDown()

    def test_payload_within_limit_is_complete(self):
        from unittest.mock import patch, Mock
        from modules import acfun_uploader as ac, bilibili_uploader as bili
        url = 'https://example.org/offline'
        body = '  Body\r\n' + url + '\n' + url + '\n  '
        uploader = ac.AcfunUploader.__new__(ac.AcfunUploader)
        uploader.log = Mock()
        uploader.login = Mock(return_value=True)
        uploader.create_douga = Mock(return_value=(True, {}))
        with patch.object(ac, 'setup_task_logger', return_value=Mock()), patch.object(ac.os.path, 'exists', return_value=True):
            self.assertTrue(uploader.upload_video('v', 'c', 'Title', body, [], 1, original_url=url, original_title='Metadata title')[0])
        self.assertEqual(uploader.create_douga.call_args.kwargs['desc'], '原视频：Metadata title\n视频链接：' + url + '\n\n' + body)
        uploader = bili.BilibiliUploader.__new__(bili.BilibiliUploader)
        uploader.cookie_file = 'not-read'
        final = format_bilibili_description(body, original_url=url, original_title='Metadata title')
        with patch.object(bili, 'setup_task_logger', return_value=Mock()), patch.object(bili.os.path, 'exists', return_value=True), patch.object(bili, 'configure_bilibili_runtime'), patch.object(bili, 'load_credential_from_file'), patch.object(bili, 'validate_credential_remote', return_value=(True, '')), patch.object(bili.video_uploader, 'VideoMeta') as meta, patch.object(bili.video_uploader, 'VideoUploaderPage', side_effect=RuntimeError('offline stop before upload')):
            uploader.upload_video('v', 'c', 'Title', final, [], 1, youtube_url=url)
        self.assertEqual(meta.call_args.kwargs['desc'], final)

    def test_missing_and_invalid_metadata_never_invented(self):
        for formatter in (build_upload_description, format_bilibili_description):
            self.assertEqual(formatter('Body', original_title='Only title', original_upload_date='20260230'), '原视频：Only title\n\nBody')
            self.assertEqual(formatter('  Body\n', append_repost_notice=False), '  Body\n')

    def test_final_full_description_over_limit_rejected_before_network(self):
        from unittest.mock import patch, Mock
        from modules import acfun_uploader as ac, bilibili_uploader as bili
        for module, cls, limit in ((ac, ac.AcfunUploader, 1000), (bili, bili.BilibiliUploader, 2000)):
            with self.subTest(platform=module.__name__):
                uploader = cls.__new__(cls)
                uploader.log = Mock()
                uploader.login = Mock(return_value=True)
                uploader.create_douga = Mock(return_value=(True, {}))
                uploader.cookie_file = 'not-read'
                with patch.object(module, 'setup_task_logger', return_value=Mock()), patch.object(module.os.path, 'exists', return_value=True):
                    if module is bili:
                        with patch.object(bili, 'configure_bilibili_runtime'), patch.object(bili, 'load_credential_from_file') as credentials, patch.object(bili, 'validate_credential_remote', return_value=(False, 'offline')):
                            ok, message = uploader.upload_video('video', 'cover', 'Title', 'x' * (limit + 1), [], 1)
                            credentials.assert_not_called()
                    else:
                        ok, message = uploader.upload_video('video', 'cover', 'Title', 'x' * (limit - 1), [], 1, original_url='https://example.org', original_uploader='Author', copyright_type='original')
                        uploader.login.assert_not_called()
                        uploader.create_douga.assert_not_called()
                self.assertFalse(ok)
                self.assertIn('请编辑简介', message)
                self.assertIn(str(limit), message)
