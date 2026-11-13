"""Retry/re-burn/upload recovery regressions. Run via tools/run_offline_tests.py.

Real entries: process_task (start button), retry_failed_tasks, force_upload_task,
_prepare_subtitle_for_upload. Mocked: task DB, AI translator, ffmpeg embed, uploaders;
resolve_upload_tags is the pre-network sentinel.
"""
import asyncio
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from modules import task_manager as tm

TID = 'retry-recovery-task'
SRC = "1\n00:00:00,000 --> 00:00:02,000\nHello world\n\n2\n00:00:02,000 --> 00:00:04,000\nGood morning everyone\n"
BI = "1\n00:00:00,000 --> 00:00:02,000\nHello world\n你好世界\n\n2\n00:00:02,000 --> 00:00:04,000\nGood morning everyone\n大家早上好\n"
MONO = "1\n00:00:00,000 --> 00:00:02,000\n你好世界\n\n2\n00:00:02,000 --> 00:00:04,000\n大家早上好\n"
CFG = {
    'SUBTITLE_TRANSLATION_ENABLED': True, 'SUBTITLE_BILINGUAL_ENABLED': True,
    'SUBTITLE_EMBED_IN_VIDEO': False, 'SPEECH_RECOGNITION_ENABLED': False,
    'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED': True, 'SUBTITLE_TRANSLATION_ALLOW_PARTIAL': True,
    'SUBTITLE_TARGET_LANGUAGE': 'zh-CN', 'TRANSLATE_TITLE': False, 'TRANSLATE_DESCRIPTION': False,
    'GENERATE_TAGS': False, 'RECOMMEND_PARTITION': False, 'CONTENT_MODERATION_ENABLED': False,
    'SUBTITLE_QC_ENABLED': False,
}


class _Boundary(Exception):
    pass


class RetryRecoveryFixesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.downloads = os.path.join(self.tmp, 'downloads')
        self.dir = Path(self.downloads) / TID
        self.dir.mkdir(parents=True)
        (self.dir / 'video.mp4').write_bytes(b'original')
        (self.dir / 'cover.jpg').write_bytes(b'cover')
        (self.dir / 'video.en.srt').write_text(SRC, encoding='utf-8')
        self.cache = self.dir / f'translated_{TID}.srt'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, **kw):
        task = {'id': TID, 'youtube_url': 'https://www.youtube.com/watch?v=x', 'upload_target': 'bilibili',
                'video_path_local': str(self.dir / 'video.mp4'), 'cover_path_local': str(self.dir / 'cover.jpg'),
                'video_title_original': 'T', 'tags_generated': '[]', 'metadata_json_path_local': '',
                'subtitle_path_original': str(self.dir / 'video.en.srt'), 'subtitle_language_detected': 'en'}
        task.update(kw)
        return task

    def _processor(self, **cfg):
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)  # no schedulers
        processor.config = dict(CFG, **cfg)
        return processor

    def _translator(self):
        translator = MagicMock()
        translator.translate_file.side_effect = lambda src, out, **kw: Path(out).write_text(BI, encoding='utf-8') or True
        return translator

    def _run(self, task, fn, processor, translator=None, embed=None):
        seen = {'upload': 0}

        def boundary(*a, **k):
            seen['upload'] += 1
            raise _Boundary()

        with ExitStack() as s:
            s.enter_context(patch.object(tm, 'DOWNLOADS_DIR', self.downloads))
            s.enter_context(patch.object(tm, 'get_task', side_effect=lambda _id: dict(task) if _id == TID else None))
            s.enter_context(patch.object(tm, 'update_task', side_effect=lambda _id, **kw: task.update(
                {k: v for k, v in kw.items() if k != 'silent'}) or True))
            s.enter_context(patch.object(tm, '_persist_pipeline_checkpoint'))
            s.enter_context(patch.object(tm, 'clear_task_cancel'))
            s.enter_context(patch.object(threading, 'Thread'))
            s.enter_context(patch.object(tm, 'resolve_upload_tags', side_effect=boundary))
            s.enter_context(patch.object(tm, 'get_global_task_processor', return_value=processor))
            s.enter_context(patch.object(tm, '_start_background_upload_retry',
                                         side_effect=lambda tid, cfg: tm.force_upload_task(tid, cfg)))
            s.enter_context(patch.object(tm, 'get_tasks_by_status', side_effect=lambda st: [dict(task)] if task['status'] == st else []))
            s.enter_context(patch.object(tm, 'setup_task_logger', return_value=MagicMock()))
            s.enter_context(patch.object(processor, '_recover_cover_path', side_effect=lambda _id, path, _lg: path))
            s.enter_context(patch.object(processor, '_embed_subtitle_in_video', embed or MagicMock(side_effect=AssertionError('no burn'))))
            s.enter_context(patch('modules.subtitle_translator.create_translator_from_config',
                                  return_value=translator or MagicMock(side_effect=AssertionError('no AI'))))
            s.enter_context(patch('modules.bilibili_uploader.BilibiliUploader', side_effect=AssertionError('network')))
            s.enter_context(patch('modules.acfun_uploader.AcfunUploader', side_effect=AssertionError('network')))
            try:
                fn()
            except _Boundary:
                pass
        return seen['upload']

    @staticmethod
    def _start(processor):
        return lambda: processor.process_task(TID, slot_already_acquired=True, acquired_task_semaphore=threading.Semaphore(0))

    # #1 FAILED upload task retried via start button must reach the upload again.
    def test_start_button_retries_failed_upload(self):
        self.cache.write_text(BI, encoding='utf-8')
        cases = (('bilibili', {}), ('both', {'acfun_upload_response': '{"ac": 1}'}))
        for target, extra in cases:
            with self.subTest(target=target):
                task = self._task(status=tm.TASK_STATES['FAILED'], upload_target=target,
                                  error_message='bilibili上传异常: 网络请求失败（curl=56）',
                                  subtitle_path_translated=str(self.cache), **extra)
                processor = self._processor(AUTO_MODE_ENABLED=True)
                self.assertEqual(self._run(task, self._start(processor), processor), 1)

    # #2 recorded translation failure must re-run the subtitle stage on start (AUTO off/on).
    def test_start_button_retranslates_after_translation_failure(self):
        for auto in (False, True):
            with self.subTest(auto=auto):
                self.cache.unlink(missing_ok=True)
                task = self._task(status=tm.TASK_STATES['FAILED'], error_category='subtitle_translation_failed',
                                  error_message='字幕翻译未通过验收，已停止自动上传；请检查保留的字幕产物。')
                self.assertNotIn(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE, tm._get_completed_stages(task, CFG))
                processor, translator = self._processor(AUTO_MODE_ENABLED=auto), self._translator()
                uploads = self._run(task, self._start(processor), processor, translator)
                self.assertEqual(translator.translate_file.call_count, 1)
                self.assertEqual(uploads, int(auto))
                if not auto:
                    self.assertEqual(task['status'], tm.TASK_STATES['READY_FOR_UPLOAD'])

    # #4 a rejected cached translation is moved aside so the next batch retry re-translates.
    def test_rejected_cache_is_retranslated_on_batch_retry(self):
        self.cache.write_text(MONO, encoding='utf-8')  # stale monolingual cache, bilingual now on
        task = self._task(status=tm.TASK_STATES['READY_FOR_UPLOAD'], subtitle_path_translated=str(self.cache))
        processor, translator = self._processor(AUTO_MODE_ENABLED=True), self._translator()
        self.assertEqual(self._run(task, lambda: tm.force_upload_task(TID, processor.config), processor, translator), 0)
        self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])
        self.assertEqual(Path(str(self.cache) + '.rejected').read_text(encoding='utf-8'), MONO)
        self.assertEqual(self._run(task, lambda: tm.retry_failed_tasks(processor.config), processor, translator), 1)
        self.assertEqual(translator.translate_file.call_count, 1)
        self.assertEqual(self.cache.read_text(encoding='utf-8'), BI)

    # #6 re-translation must burn onto the original video, never onto an already-burned one.
    def test_retranslation_burns_from_original_video(self):
        burned = self.dir / 'video_with_subtitle.mp4'
        burned.write_bytes(b'burned-old')
        self.cache.write_text(MONO, encoding='utf-8')
        task = self._task(status=tm.TASK_STATES['FAILED'], error_category='subtitle_translation_failed',
                          error_message='缓存译文未通过源文配对/翻译验收，已停止上传；保留原文件，请重新处理字幕。',
                          video_path_local=str(burned), subtitle_path_translated=str(self.cache))
        processor, translator = self._processor(AUTO_MODE_ENABLED=True, SUBTITLE_EMBED_IN_VIDEO=True), self._translator()
        def burn(_id, video, sub, _lg):
            out = Path(video).with_name(Path(video).stem + '_with_subtitle.mp4')
            out.write_bytes(b'burned-new')
            return str(out)
        embed = MagicMock(side_effect=burn)
        self.assertEqual(self._run(task, self._start(processor), processor, translator, embed), 1)
        self.assertEqual([Path(c.args[1]).name for c in embed.call_args_list], ['video.mp4'])
        self.assertEqual(Path(task['video_path_local']).name, 'video_with_subtitle.mp4')

    # #13 failing to archive the old marker must not reject a valid translation.
    def test_marker_rename_failure_keeps_successful_translation(self):
        self.cache.write_text(BI, encoding='utf-8')
        marker = Path(str(self.cache) + '.translation-failed.json')
        marker.write_text('{}')
        task = self._task(status=tm.TASK_STATES['READY_FOR_UPLOAD'], subtitle_path_translated=str(self.cache),
                          error_category='subtitle_translation_failed', error_message='old')
        processor = self._processor(AUTO_MODE_ENABLED=True)
        result = {}
        with patch('modules.subtitle_translator.Path.rename', side_effect=PermissionError('locked')):
            self._run(task, lambda: result.setdefault('task', processor._prepare_subtitle_for_upload(TID, MagicMock())), processor)
        self.assertIsNotNone(result['task'])
        self.assertNotEqual(task['status'], tm.TASK_STATES['FAILED'])
        self.assertTrue(marker.exists())


class ChunkRetryClassifierTests(unittest.TestCase):
    # #12 restore upstream retry for transient non-network chunk failures.
    def test_classifier(self):
        from modules.upload_errors import retryable_chunk_error
        from modules.bili_sdk.exceptions.ApiException import ApiException
        from modules.bili_sdk.exceptions.NetworkException import NetworkException
        self.assertFalse(retryable_chunk_error(ApiException('分块上传响应异常'), 200))
        self.assertFalse(retryable_chunk_error(RuntimeError('client failure'), None))
        self.assertTrue(retryable_chunk_error(NetworkException(503, 'x'), 503))
        self.assertFalse(retryable_chunk_error(NetworkException(403, 'x'), 403))
        self.assertFalse(retryable_chunk_error(RuntimeError('curl: (60) certificate'), None))

    def test_bad_put_body_stops_without_retry(self):
        from modules.bili_sdk.utils.AsyncEvent import AsyncEvent
        from modules.bili_sdk.video_uploader import VideoUploader, VideoUploaderEvents, VideoUploaderPage
        from modules.bili_sdk.exceptions.ApiException import ApiException
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'video.mp4')
            Path(path).write_bytes(b'abcdefgh')
            uploader = object.__new__(VideoUploader)
            AsyncEvent.__init__(uploader)
            uploader.line = None
            failures = []
            uploader.add_event_listener(VideoUploaderEvents.CHUNK_FAILED.value, failures.append)
            response = Mock(code=200, utf8_text=Mock(return_value='<html>unexpected</html>'))
            client = Mock(request=AsyncMock(return_value=response))
            preupload = {'endpoint': '//upload.example.com', 'upos_uri': 'upos://bucket/video.mp4',
                         'upload_id': 'u', 'chunk_size': 8, 'auth': 'a'}
            with patch('modules.bili_sdk.video_uploader.get_client', return_value=client), \
                 patch('modules.bili_sdk.video_uploader.asyncio.sleep', new=AsyncMock()):
                with self.assertRaises(ApiException):
                    asyncio.run(uploader._upload_chunk(VideoUploaderPage(path, 't'), 0, 0, 1, preupload))
        self.assertEqual(client.request.await_count, 1)
        self.assertEqual([f['retrying'] for f in failures], [False])
        self.assertEqual({f['max_attempts'] for f in failures}, {6})
        self.assertEqual(failures[0]['reason'], 'permanent')


if __name__ == '__main__':
    unittest.main()
