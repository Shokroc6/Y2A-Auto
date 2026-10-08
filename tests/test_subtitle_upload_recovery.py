"""Run only through tools/run_offline_tests.py; no live providers/uploads."""
from pathlib import Path
from unittest.mock import MagicMock, patch
from contextlib import ExitStack
from test_subtitle_failure_policy import SubtitleFailurePolicyTests
from test_subtitle_translator_pairing import _make_translator, _make_items
from modules import task_manager as tm


class SubtitleUploadRecoveryTests(SubtitleFailurePolicyTests):
    def test_platform_entries_stop_on_real_cache_rejection(self):
        for method in ('_do_upload_to_acfun', '_do_upload_to_bilibili'):
            for reason in ('missing', 'invalid', 'sidecar'):
                with self.subTest(method=method, reason=reason):
                    source = Path(self.task_dir) / 'video.en.srt'
                    output = Path(self.task_dir) / 'translated_video.srt'
                    marker = Path(str(output) + '.translation-failed.json')
                    for path in (source, output, marker):
                        path.unlink(missing_ok=True)
                    task = self._task()
                    if reason != 'missing':
                        self._write_srt(str(source))
                        self._write_srt(str(output))
                        task.update(subtitle_path_original=str(source), subtitle_path_translated=str(output))
                    if reason == 'sidecar':
                        marker.write_text('{}')
                    processor = self._processor()
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(processor, '_recover_cover_path', return_value=''))
                        tags = stack.enter_context(patch.object(tm, 'resolve_upload_tags', return_value=[]))
                        acfun = stack.enter_context(patch('modules.acfun_uploader.AcfunUploader'))
                        bili = stack.enter_context(patch('modules.bilibili_uploader.BilibiliUploader'))
                        self._run_with_task_patches(task, lambda: getattr(processor, method)(self.task_id, MagicMock()))
                        tags.assert_not_called()
                        acfun.assert_not_called()
                        bili.assert_not_called()
                    self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])

    def test_failure_then_success_preserves_history_and_recovers_repeatedly(self):
        source = self.task_dir + '/video.en.srt'
        out = self.task_dir + '/translated_' + self.task_id + '.srt'
        self._write_srt(source, 'This sentence needs translation')
        translator = _make_translator()
        with patch.object(translator.llm_requester, 'translate_batch', return_value=['']), patch.object(translator.llm_requester, 'translate_batch_strict', return_value=['']), patch('modules.subtitle_translator.time.sleep'):
            self.assertFalse(translator._translate_concurrent(_make_items(['This sentence needs translation']), out))
        marker = Path(out + '.translation-failed.json')
        diagnostic = marker.read_bytes()
        task = self._task()
        task.update(error_category='subtitle_translation_failed', error_message='old subtitle failure', subtitle_path_original=source)
        processor = self._processor()
        with patch('modules.subtitle_translator.create_translator_from_config', return_value=translator), patch.object(translator.llm_requester, 'translate_batch', return_value=['这句话需要翻译']):
            self.assertTrue(self._run_with_task_patches(task, lambda: processor._translate_subtitle(self.task_id, MagicMock())))
        self.assertIsNone(task['error_category'])
        self.assertIsNone(task['error_message'])
        self.assertFalse(marker.exists())
        histories = list(Path(self.task_dir).glob('*.translation-history-*.json'))
        self.assertTrue(any(p.read_bytes() == diagnostic for p in histories))
        for _ in range(3):
            # Simulate an older checkpoint carrying only subtitle failure state.
            task.update(error_category='subtitle_translation_failed', error_message='old checkpoint')
            self.assertIsNotNone(self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock())))
            self.assertIsNone(task['error_category'])
            self.assertIsNone(task['error_message'])

    def test_validation_exception_and_failed_prepared_task_never_upload(self):
        for method in ('_do_upload_to_acfun', '_do_upload_to_bilibili'):
            for prepared in (False, True):
                with self.subTest(method=method, prepared=prepared):
                    task = self._task()
                    processor = self._processor()
                    source = self.task_dir + '/video.en.srt'
                    output = self.task_dir + '/translated_video.srt'
                    self._write_srt(source)
                    self._write_srt(output)
                    task.update(subtitle_path_original=source, subtitle_path_translated=output)
                    if prepared:
                        task.update(status=tm.TASK_STATES['FAILED'], error_category='subtitle_translation_failed')
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(processor, '_recover_cover_path', return_value=''))
                        stack.enter_context(patch('modules.subtitle_translator.SubtitleReader.read_srt', side_effect=OSError('test')))
                        tags = stack.enter_context(patch.object(tm, 'resolve_upload_tags'))
                        acfun = stack.enter_context(patch('modules.acfun_uploader.AcfunUploader'))
                        bili = stack.enter_context(patch('modules.bilibili_uploader.BilibiliUploader'))
                        self._run_with_task_patches(task, lambda: getattr(processor, method)(self.task_id, MagicMock(), subtitle_prepared=prepared))
                        tags.assert_not_called()
                        acfun.assert_not_called()
                        bili.assert_not_called()
                    self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])

    def test_successful_cache_archives_only_current_marker(self):
        source = self.task_dir + '/video.en.srt'
        output = self.task_dir + '/translated_video.srt'
        self._write_srt(source)
        self._write_srt(output, '你好世界')
        current = Path(output + '.translation-failed.json')
        historical = Path(self.task_dir) / 'other.srt.translation-failed.json'
        current.write_text('current diagnostic')
        historical.write_text('other output diagnostic')
        task = self._task()
        task.update(subtitle_path_original=source, subtitle_path_translated=output,
                    error_category='subtitle_translation_failed', error_message='old')
        processor = self._processor()
        for _ in range(3):
            self.assertIsNotNone(self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock())))
        self.assertFalse(current.exists())
        self.assertEqual(historical.read_text(), 'other output diagnostic')
        self.assertEqual(len(list(Path(self.task_dir).glob('*.translation-history-*.json'))), 1)

    def test_completed_subtitle_checkpoint_revalidates_cache_on_each_retry(self):
        import threading
        source = self.task_dir + '/video.en.srt'
        output = self.task_dir + '/translated_video.srt'
        self._write_srt(source)
        self._write_srt(output, '你好世界')
        task = self._task()
        task.update(subtitle_path_original=source, subtitle_path_translated=output)
        processor = self._processor()
        stages = set(tm.PIPELINE_STAGE_ORDER) - {tm.PIPELINE_STAGE_UPLOAD_TO_ACFUN}
        for _ in range(3):
            validations = []
            def upload_entry(task_id, logger, **kwargs):
                validations.append(processor._prepare_subtitle_for_upload(task_id, logger))
            with ExitStack() as stack:
                stack.enter_context(patch.object(tm, '_get_completed_stages', return_value=stages))
                stack.enter_context(patch.object(tm, '_persist_pipeline_checkpoint'))
                stack.enter_context(patch.object(tm, '_mark_stage_done', side_effect=lambda _id, done, stage: done | {stage}))
                stack.enter_context(patch.object(tm, 'clear_task_cancel'))
                stack.enter_context(patch.object(threading, 'Thread'))
                translate = stack.enter_context(patch.object(processor, '_translate_subtitle', side_effect=AssertionError('completed checkpoint must reuse cache')))
                stack.enter_context(patch.object(processor, '_upload_to_target', side_effect=upload_entry))
                self._run_with_task_patches(task, lambda: processor.process_task(self.task_id, slot_already_acquired=True, acquired_task_semaphore=threading.Semaphore(0)))
                translate.assert_not_called()
            self.assertEqual(len(validations), 1)
            self.assertIsNotNone(validations[0])

    def test_chinese_source_requires_valid_cues_before_prepare_or_upload(self):
        invalid = ('not a subtitle', '',
                   '1\n00:00:02,000 --> 00:00:01,000\n你好\n',
                   '1\n00:61:00,000 --> 00:62:00,000\n你好\n',
                   '1\n00:00:00,000 --> 00:00:01,000\n   \n')
        source = Path(self.task_dir) / 'video.zh.srt'
        for embed in (False, True):
            for content in invalid:
                with self.subTest(embed=embed, content=content):
                    source.write_text(content)
                    processor = self._processor()
                    processor.config['SUBTITLE_EMBED_IN_VIDEO'] = embed
                    for method in ('prepare', '_do_upload_to_acfun', '_do_upload_to_bilibili'):
                        task = self._task()
                        task.update(subtitle_path_original=str(source), subtitle_language_detected='zh')
                        with ExitStack() as stack:
                            ai = stack.enter_context(patch('modules.subtitle_translator.create_translator_from_config'))
                            burn = stack.enter_context(patch.object(processor, '_embed_subtitle_in_video'))
                            stack.enter_context(patch.object(processor, '_recover_cover_path', return_value=''))
                            tags = stack.enter_context(patch.object(tm, 'resolve_upload_tags'))
                            acfun = stack.enter_context(patch('modules.acfun_uploader.AcfunUploader'))
                            bili = stack.enter_context(patch('modules.bilibili_uploader.BilibiliUploader'))
                            if method == 'prepare':
                                self.assertIsNone(self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock())))
                            else:
                                self._run_with_task_patches(task, lambda: getattr(processor, method)(self.task_id, MagicMock()))
                            ai.assert_not_called()
                            burn.assert_not_called()
                            tags.assert_not_called()
                            acfun.assert_not_called()
                            bili.assert_not_called()
                        self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])

    def test_valid_chinese_source_reuses_reader_without_ai(self):
        for ext, content in (('srt', '00:00:00.000 --> 00:00:01.000\n你好\n'),
                             ('vtt', 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好\n')):
            source = Path(self.task_dir) / ('valid.zh.' + ext)
            source.write_text(content)
            for embed in (False, True):
                task = self._task()
                task.update(subtitle_path_original=str(source), subtitle_language_detected='zh')
                processor = self._processor()
                processor.config['SUBTITLE_EMBED_IN_VIDEO'] = embed
                with patch('modules.subtitle_translator.create_translator_from_config') as ai, patch.object(processor, '_embed_subtitle_in_video', return_value=self.video_path) as burn:
                    self.assertIsNotNone(self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock())))
                    ai.assert_not_called()
                    self.assertEqual(burn.call_count, int(embed))

    def test_quick_repair_propagates_real_writer_failure(self):
        source = str(Path(self.task_dir) / 'repair.srt')
        self._write_srt(source, 'Hello world')
        output = str(Path(self.task_dir) / 'repaired.srt')
        translator = _make_translator()
        real_open = open
        def fail_output(path, *args, **kwargs):
            if str(path) == output:
                raise OSError('injected output failure')
            return real_open(path, *args, **kwargs)
        with patch.object(translator.llm_requester, 'translate_batch_strict', return_value=['你好世界']), patch('builtins.open', side_effect=fail_output):
            self.assertFalse(translator.quick_repair_translated_file(source, output))

    def test_real_writer_oserror_preserves_active_marker(self):
        from modules.subtitle_translator import SubtitleWriter
        for extension in ('srt', 'vtt'):
            for bilingual in (False, True):
                for fault in ('open', 'write', 'close'):
                    with self.subTest(extension=extension, bilingual=bilingual, fault=fault):
                        output = str(Path(self.task_dir) / f'io-{extension}-{bilingual}-{fault}.{extension}')
                        marker = Path(output + '.translation-failed.json')
                        marker.write_text('retained failure')
                        translator = _make_translator(bilingual=bilingual)
                        real_open = open
                        def failing_open(path, *args, **kwargs):
                            if str(path) != output:
                                return real_open(path, *args, **kwargs)
                            if fault == 'open':
                                raise OSError('injected open failure')
                            class FailingFile:
                                def __enter__(self):
                                    return self
                                def write(self, text):
                                    if fault == 'write':
                                        raise OSError('injected write failure')
                                def __exit__(self, *exc):
                                    if fault == 'close':
                                        raise OSError('injected close failure')
                            return FailingFile()
                        with patch.object(translator.llm_requester, 'translate_batch', return_value=['你好世界']), patch('builtins.open', side_effect=failing_open):
                            self.assertFalse(translator._translate_concurrent(_make_items(['Hello world']), output))
                        self.assertEqual(marker.read_text(), 'retained failure')
                        self.assertFalse(list(Path(self.task_dir).glob(Path(output).name + '.translation-history-*.json')))
        for extension in ('srt', 'vtt'):
            for bilingual in (False, True):
                output = str(Path(self.task_dir) / f'ok-{extension}-{bilingual}.{extension}')
                marker = Path(output + '.translation-failed.json')
                marker.write_text('retained failure')
                translator = _make_translator(bilingual=bilingual)
                with patch.object(translator.llm_requester, 'translate_batch', return_value=['你好世界']):
                    self.assertTrue(translator._translate_concurrent(_make_items(['Hello world']), output))
                self.assertIn('你好世界', Path(output).read_text())
                self.assertFalse(marker.exists())
                self.assertEqual(len(list(Path(self.task_dir).glob(Path(output).name + '.translation-history-*.json'))), 1)
        for writer in (SubtitleWriter.write_srt, SubtitleWriter.write_vtt):
            output = str(Path(self.task_dir) / 'writer-direct.srt')
            self.assertIs(writer(_make_items(['Hello']), output), True)
            with patch('builtins.open', side_effect=OSError('injected')):
                self.assertIs(writer(_make_items(['Hello']), output), False)

    def test_write_failure_does_not_archive_diagnostic(self):
        output = self.task_dir + '/translated_video.srt'
        marker = Path(output + '.translation-failed.json')
        marker.write_text('retained failure')
        translator = _make_translator()
        with patch.object(translator.llm_requester, 'translate_batch', return_value=['你好世界']), patch.object(translator, '_write_translated_file', return_value=False):
            self.assertFalse(translator._translate_concurrent(_make_items(['Hello world']), output))
        self.assertEqual(marker.read_text(), 'retained failure')

    def test_partial_policy_matches_writer_and_preserves_other_errors(self):
        from modules.subtitle_translator import SubtitleWriter
        source = self.task_dir + '/video.en.srt'
        out = self.task_dir + '/translated_' + self.task_id + '.srt'
        texts = ['This sentence needs translation ' + str(i) for i in range(10)]
        SubtitleWriter.write_srt(_make_items(texts), source, translated=False)
        for bilingual in (False, True):
            with self.subTest(bilingual=bilingual):
                translator = _make_translator(allow_partial=True, bilingual=bilingual)
                with patch.object(translator.llm_requester, 'translate_batch', side_effect=lambda batch, *a, **k: [t if t.endswith('0') else '这句话需要翻译' for t in batch]), patch.object(translator.llm_requester, 'translate_batch_strict', side_effect=lambda batch, *a, **k: batch):
                    self.assertTrue(translator._translate_concurrent(_make_items(texts), out))
                task = self._task()
                task.update(subtitle_path_original=source, subtitle_path_translated=out, error_category='content_moderation_failed', error_message='keep other error')
                processor = self._processor()
                processor.config.update(SUBTITLE_TRANSLATION_ALLOW_PARTIAL=True, SUBTITLE_BILINGUAL_ENABLED=bilingual)
                for _ in range(3):
                    self.assertIsNotNone(self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock())))
                    self.assertEqual(task['error_category'], 'content_moderation_failed')
                    self.assertEqual(task['error_message'], 'keep other error')
                processor.config['SUBTITLE_TRANSLATION_ALLOW_PARTIAL'] = False
                self.assertIsNone(self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock())))
