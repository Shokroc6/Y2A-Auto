"""New offline tasks only; no provider calls or existing task database."""
import threading
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from test_subtitle_embed_only_flow import SubtitleEmbedOnlyFlowTests
from modules import task_manager as tm


class SubtitleFailurePolicyTests(SubtitleEmbedOnlyFlowTests):
    def _task(self):
        return {'id': self.task_id, 'youtube_url': 'https://example.org/offline',
                'status': tm.TASK_STATES['READY_FOR_UPLOAD'],
                'video_path_local': self.video_path}

    def _processor(self):
        # No scheduler/app initialization: exercise real processing methods only.
        processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        processor.config = {'SUBTITLE_TRANSLATION_ENABLED': True,
                            'SUBTITLE_EMBED_IN_VIDEO': False,
                            'SPEECH_RECOGNITION_ENABLED': False,
                            'AUTO_MODE_ENABLED': True}
        return processor

    def test_cached_untranslated_subtitle_blocks_upload(self):
        source = self.task_dir + '/video.en.srt'
        translated = self.task_dir + '/translated_video.en.srt'
        self._write_srt(source)
        self._write_srt(translated)
        task = self._task()
        task.update(subtitle_path_original=source, subtitle_path_translated=translated)
        result = self._run_with_task_patches(task, lambda: self._processor()._prepare_subtitle_for_upload(self.task_id, MagicMock()))
        self.assertIsNone(result)
        self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])

    def test_missing_source_blocks_upload(self):
        task = self._task()
        result = self._run_with_task_patches(task, lambda: self._processor()._prepare_subtitle_for_upload(self.task_id, MagicMock()))
        self.assertIsNone(result)
        self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])

    def test_failed_task_cannot_reuse_cached_embedded_video(self):
        task = self._task()
        task.update(status=tm.TASK_STATES['FAILED'], error_category='subtitle_translation_failed')
        processor = self._processor()
        with patch.object(processor, '_get_embedded_video_candidate', return_value=self.video_path):
            result = self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
        self.assertIsNone(result)

    def test_rejected_real_translation_keeps_diagnostic_items(self):
        from test_subtitle_translator_pairing import _make_translator
        from modules.subtitle_translator import SubtitleItem
        import json
        from pathlib import Path
        translator = _make_translator()
        items = [SubtitleItem(index=1, start_time='00:00:00,000', end_time='00:00:01,000', source_text='This sentence requires translation', translated_text='')]
        with patch.object(translator.llm_requester, 'translate_batch', return_value=['']), patch('modules.subtitle_translator.time.sleep'):
            assert translator._translate_concurrent(items, self.task_dir + '/translated.srt') is False
        diagnostic = json.loads(Path(self.task_dir + '/translated.srt.translation-failed.json').read_text())
        assert diagnostic['reason'] == 'translation_acceptance_failed'
        assert diagnostic['items'][0]['source'] == items[0].source_text
        assert not Path(self.task_dir + '/translated.srt').exists()

    def test_failed_translation_marks_failed_and_preserves_source(self):
        source = self.task_dir + '/video.en.srt'
        self._write_srt(source)
        task = self._task()
        translator = MagicMock()
        translator.translate_file.return_value = False
        with patch('modules.subtitle_translator.create_translator_from_config', return_value=translator):
            result = self._run_with_task_patches(task, lambda: self._processor()._translate_subtitle(self.task_id, MagicMock()))
        self.assertFalse(result)
        self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])
        self.assertEqual(task['error_category'], 'subtitle_translation_failed')
        self.assertIn('字幕翻译', task['error_message'])
        self.assertEqual(task['subtitle_path_original'], source)
        self.assertTrue(__import__('os').path.exists(source))

    def test_prepare_translates_existing_english_when_asr_disabled(self):
        self._write_srt(self.task_dir + '/video.en.srt')
        task = self._task()
        translator = MagicMock()
        translator.translate_file.return_value = False
        with patch('modules.subtitle_translator.create_translator_from_config', return_value=translator):
            result = self._run_with_task_patches(task, lambda: self._processor()._prepare_subtitle_for_upload(self.task_id, MagicMock()))
        translator.translate_file.assert_called_once()
        self.assertIsNone(result)
        self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])

    def test_real_process_entry_never_uploads_after_translation_rejection(self):
        self._write_srt(self.task_dir + '/video.en.srt')
        task = self._task()
        processor = self._processor()
        translator = MagicMock()
        translator.translate_file.return_value = False
        stages = set(tm.PIPELINE_STAGE_ORDER) - {tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE, tm.PIPELINE_STAGE_UPLOAD_TO_ACFUN}
        with ExitStack() as stack:
            stack.enter_context(patch('modules.subtitle_translator.create_translator_from_config', return_value=translator))
            stack.enter_context(patch.object(tm, '_get_completed_stages', return_value=stages))
            stack.enter_context(patch.object(tm, '_persist_pipeline_checkpoint'))
            stack.enter_context(patch.object(tm, '_mark_stage_done', side_effect=lambda _id, done, stage: done | {stage}))
            stack.enter_context(patch.object(tm, 'clear_task_cancel'))
            stack.enter_context(patch.object(threading, 'Thread'))
            upload = stack.enter_context(patch.object(processor, '_upload_to_target'))
            self._run_with_task_patches(task, lambda: processor.process_task(self.task_id, slot_already_acquired=True, acquired_task_semaphore=threading.Semaphore(0)))
        translator.translate_file.assert_called_once()
        upload.assert_not_called()
        self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])
