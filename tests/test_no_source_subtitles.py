"""No source captions is a normal branch, never a catch-all for broken subtitles."""
import json
import logging
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from modules import task_manager as tm


class NoSourceSubtitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'offline'
        self.directory.mkdir()
        self.video = self.directory / 'video.mp4'
        self.video.write_bytes(b'offline fixture, no media processing')
        self.metadata = self.directory / 'metadata.json'
        self.metadata.write_text(json.dumps({'subtitles': {}, 'automatic_captions': {}}))
        self.task = {'id': 'offline', 'status': 'pending', 'video_path_local': str(self.video),
                     'metadata_json_path_local': str(self.metadata), 'upload_target': 'bilibili'}
        self.processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        self.processor.config = {'SUBTITLE_TRANSLATION_ENABLED': True, 'SUBTITLE_BILINGUAL_ENABLED': True,
                                 'SUBTITLE_EMBED_IN_VIDEO': True, 'SPEECH_RECOGNITION_ENABLED': False}
        self.logger = logging.getLogger('no-source-test')
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(tm, 'DOWNLOADS_DIR', self.tmp.name))
        stack.enter_context(patch.object(tm, 'get_task', side_effect=lambda _: dict(self.task)))
        stack.enter_context(patch.object(tm, 'update_task', side_effect=lambda _, **kw: self.task.update(kw)))
        stack.enter_context(patch.object(tm, '_raise_if_cancelled'))
        self.embed = stack.enter_context(patch.object(self.processor, '_embed_subtitle_in_video',
                                                     side_effect=AssertionError('must not burn')))

    def test_no_source_translation_continues_with_persistent_warning(self):
        self.assertTrue(self.processor._translate_subtitle('offline', self.logger))
        self.assertEqual(self.task.get('subtitle_warning_message'), 'no_source_subtitle')
        for _ in range(2):
            self.assertIsNotNone(self.processor._prepare_subtitle_for_upload('offline', self.logger))
            self.assertEqual(self.task.get('subtitle_warning_message'), 'no_source_subtitle')
        self.assertNotEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])
        self.embed.assert_not_called()

    def test_preflight_does_not_misclassify_missing_download_or_damaged_files(self):
        for metadata, artifact in [
            ({'subtitles': {'en': [{'ext': 'srt'}]}, 'automatic_captions': {}}, None),
            ({'subtitles': {}, 'automatic_captions': {'en': [{'ext': 'vtt'}]}}, None),
            ({}, None),
            ({'subtitles': {}, 'automatic_captions': {}}, 'video.en.srt'),
            ({'subtitles': {}, 'automatic_captions': {}}, 'translated_offline.srt.failure.json'),
        ]:
            with self.subTest(metadata=metadata, artifact=artifact):
                self.metadata.write_text(json.dumps(metadata))
                self.task.update(status='pending', error_category=None, subtitle_warning_message=None)
                path = self.directory / artifact if artifact else None
                if path:
                    path.write_text('')
                self.assertIsNone(self.processor._prepare_subtitle_for_upload('offline', self.logger))
                self.assertNotEqual(self.task.get('subtitle_warning_message'), 'no_source_subtitle')
                if path:
                    path.unlink()

    def test_existing_missing_error_recovers_only_on_explicit_absence(self):
        self.task.update(status='uploading', error_category='subtitle_translation_failed',
                         error_message='字幕翻译未通过验收，已停止自动上传；请检查保留的字幕产物。')
        self.assertIsNotNone(self.processor._prepare_subtitle_for_upload('offline', self.logger))
        self.assertIsNone(self.task['error_category'])
        self.assertIsNone(self.task['error_message'])

    def test_real_failure_is_not_cleared(self):
        self.task.update(error_category='subtitle_translation_failed', error_message='provider error')
        self.assertIsNone(self.processor._prepare_subtitle_for_upload('offline', self.logger))
        self.assertEqual(self.task['error_category'], 'subtitle_translation_failed')
        self.assertNotEqual(self.task.get('subtitle_warning_message'), 'no_source_subtitle')

    def test_asr_enabled_is_never_absence(self):
        self.processor.config['SPEECH_RECOGNITION_ENABLED'] = True
        self.assertFalse(self.processor._skip_unavailable_source_subtitles(
            'offline', self.task, str(self.directory), self.logger))

    def test_other_subtitle_warnings_are_not_overwritten(self):
        self.task['subtitle_warning_message'] = 'subtitle_embed_failed'
        self.assertIsNone(self.processor._prepare_subtitle_for_upload('offline', self.logger))
        self.assertEqual(self.task['subtitle_warning_message'], 'subtitle_embed_failed')

    def test_restart_preserves_absence_warning(self):
        import sqlite3
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE tasks (id TEXT, status TEXT, upload_target TEXT, '
                           'acfun_upload_response TEXT, bilibili_upload_response TEXT, updated_at TEXT, '
                           'subtitle_qc_failed INTEGER, subtitle_qc_reason TEXT, subtitle_qc_score REAL, '
                           'subtitle_qc_checked_at TEXT, subtitle_quality_state TEXT, '
                           'asr_warning_message TEXT, subtitle_warning_message TEXT)')
        connection.execute("INSERT INTO tasks (id,status,upload_target,subtitle_warning_message) "
                           "VALUES ('offline','translating_subtitle','bilibili','no_source_subtitle')")
        class Connection:
            execute = connection.execute
            commit = connection.commit
            def close(self):
                pass
        with patch.object(tm, 'get_db_connection', return_value=Connection()):
            self.assertEqual(tm.recover_interrupted_tasks_to_pending(), 1)
        self.assertEqual(connection.execute('SELECT subtitle_warning_message FROM tasks').fetchone()[0],
                         'no_source_subtitle')
        connection.close()

    def test_warning_is_rendered_in_list_and_detail_templates(self):
        root = Path(__file__).resolve().parents[1]
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(root / 'templates'))
        from collections import defaultdict
        task = defaultdict(lambda: None, self.task)
        task.update(subtitle_warning_message='no_source_subtitle', tags_generated='[]',
                    created_at='', updated_at='')
        from types import SimpleNamespace
        env.globals.update(request=SimpleNamespace(endpoint='tasks', args={}, path='/tasks'),
                           now=SimpleNamespace(year=2026), session={}, url_for=lambda *a, **kw: '/', get_partition_name=lambda *a: '',
                           task_status_color=lambda status: 'warning',
                           task_status_display=lambda status: status, get_flashed_messages=lambda **kw: [],
                           config={}, csrf_token=lambda: '', parse_json=lambda value: json.loads(value or '[]'))
        for name in ('partials/task_card.html', 'partials/task_row.html', 'edit_task.html', 'manual_review.html'):
            text = (root / 'templates' / name).read_text()
            self.assertIn('no_source_subtitle', text)
            self.assertIn('无可用字幕，已跳过字幕处理', text)
            self.assertIn('无字幕', text)
            rendered = env.get_template(name).render(task=task, tasks=[task], config={},
                                                     upload_target='bilibili', partitions=[], form_data={})
            self.assertIn('无字幕', rendered)
            self.assertIn('无可用字幕，已跳过字幕处理', rendered)


from test_retry_recovery_fixes import RetryRecoveryFixesTests, TID


class NoSourceEntryTests(RetryRecoveryFixesTests):
    def test_no_source_start_retry_force_and_batch_preserve_auto_mode(self):
        (self.dir / 'video.en.srt').unlink()
        metadata = self.dir / 'metadata.json'
        metadata.write_text(json.dumps({'subtitles': {}, 'automatic_captions': {}}))
        for entry in ('start', 'force', 'batch'):
            for auto in (False, True):
                with self.subTest(entry=entry, auto=auto):
                    task = self._task(status=(tm.TASK_STATES['READY_FOR_UPLOAD'] if entry == 'force'
                                              else tm.TASK_STATES['FAILED']), subtitle_path_original=None,
                                      metadata_json_path_local=str(metadata),
                                      error_category='subtitle_translation_failed',
                                      error_message='字幕翻译未通过验收，已停止自动上传；请检查保留的字幕产物。')
                    processor = self._processor(AUTO_MODE_ENABLED=auto)
                    if entry == 'start':
                        run = self._start(processor)
                    elif entry == 'force':
                        run = lambda: tm.force_upload_task(TID, processor.config)
                    else:
                        run = lambda: tm.retry_failed_tasks(processor.config)
                    uploads = self._run(task, run, processor)
                    self.assertEqual(uploads, int(auto or entry != 'start'))
                    self.assertEqual(task.get('subtitle_warning_message'), 'no_source_subtitle')
                    self.assertIsNone(task.get('error_category'))
                    if not auto and entry == 'start':
                        self.assertEqual(task['status'], tm.TASK_STATES['READY_FOR_UPLOAD'])
