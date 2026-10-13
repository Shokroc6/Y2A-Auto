"""Disposable-runtime regression: paid AI/network and large transcodes mocked."""
import os
import threading
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from test_subtitle_failure_policy import SubtitleFailurePolicyTests
from modules import task_manager as tm


class SubtitleBurnIdempotencyTests(SubtitleFailurePolicyTests):
    def _assets(self, burned=True):
        source = Path(self.task_dir) / 'video.en.srt'
        output = Path(self.task_dir) / ('translated_' + self.task_id + '.srt')
        self._write_srt(str(source))
        self._write_srt(str(output), '你好世界')
        task = self._task()
        task.update(subtitle_path_original=str(source), subtitle_path_translated=str(output),
                    subtitle_language_detected='en')
        if burned:
            Path(self.embedded_path).write_bytes(b'offline burned fixture')
            os.utime(self.embedded_path, (source.stat().st_mtime + 10, source.stat().st_mtime + 10))
            task['video_path_local'] = self.embedded_path
        processor = self._processor()
        processor.config['SUBTITLE_EMBED_IN_VIDEO'] = True
        return task, processor, source, output

    def test_accepted_burn_reused_after_three_prepares(self):
        task, processor, _, _ = self._assets()
        with patch.object(processor, '_embed_subtitle_in_video') as burn, patch.object(processor, '_translate_subtitle') as translate, patch.object(processor, '_ensure_asr_subtitle_qc', return_value=(True, None)) as qc:
            for _ in range(3):
                result = self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
                self.assertEqual(result['video_path_local'], self.embedded_path)
            burn.assert_not_called()
            translate.assert_not_called()
            self.assertEqual(qc.call_count, 3)

    def test_updated_subtitle_reburns_original_once(self):
        task, processor, _, output = self._assets()
        os.utime(output, (Path(self.embedded_path).stat().st_mtime + 10,) * 2)
        def embed(task_id, media, subtitle, logger):
            self.assertEqual(media, self.video_path)
            os.utime(self.embedded_path, (output.stat().st_mtime + 10,) * 2)
            return self.embedded_path
        with patch.object(processor, '_embed_subtitle_in_video', side_effect=embed) as burn:
            for _ in range(3):
                result = self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
                self.assertEqual(result['video_path_local'], self.embedded_path)
            burn.assert_called_once()

    def test_updated_subtitle_without_original_fails_not_stacks(self):
        task, processor, _, output = self._assets()
        Path(self.video_path).unlink()
        os.utime(output, (Path(self.embedded_path).stat().st_mtime + 10,) * 2)
        with patch.object(processor, '_embed_subtitle_in_video') as burn:
            result = self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
            self.assertIsNone(result)
            burn.assert_not_called()
        self.assertEqual(task['status'], tm.TASK_STATES['FAILED'])
        self.assertEqual(task['video_path_local'], self.embedded_path)

    def test_corrupt_cached_pair_cannot_reuse_burn(self):
        for which in ('source', 'output'):
            task, processor, source, output = self._assets()
            (source if which == 'source' else output).write_text('damaged captions')
            os.utime(self.embedded_path, (max(source.stat().st_mtime, output.stat().st_mtime) + 10,) * 2)
            with patch.object(processor, '_embed_subtitle_in_video') as burn, patch.object(processor, '_translate_subtitle') as translate:
                self.assertIsNone(self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock())))
                burn.assert_not_called()
                translate.assert_not_called()

    def test_qc_rejection_precedes_fresh_burn_reuse(self):
        task, processor, _, _ = self._assets()
        with patch.object(processor, '_ensure_asr_subtitle_qc', return_value=(False, None)) as qc, patch.object(processor, '_embed_subtitle_in_video') as burn:
            self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
            qc.assert_called_once()
            burn.assert_not_called()
        self.assertEqual(task['video_path_local'], self.embedded_path)

    def test_chinese_source_update_reburns_original(self):
        task, processor, source, output = self._assets()
        output.unlink()
        self._write_srt(str(source), '你好世界')
        task.update(subtitle_path_translated=None, subtitle_language_detected='zh')
        os.utime(source, (Path(self.embedded_path).stat().st_mtime + 10,) * 2)
        with patch.object(processor, '_embed_subtitle_in_video', return_value=self.embedded_path) as burn:
            result = self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
            self.assertEqual(result['video_path_local'], self.embedded_path)
            self.assertEqual(burn.call_args.args[1], self.video_path)

    def test_embed_off_reuses_valid_cache_without_burn(self):
        task, processor, _, _ = self._assets()
        processor.config['SUBTITLE_EMBED_IN_VIDEO'] = False
        with patch.object(processor, '_embed_subtitle_in_video') as burn:
            result = self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
            self.assertEqual(result['video_path_local'], self.embedded_path)
            burn.assert_not_called()

    def test_real_ffmpeg_wrapper_burn_once_then_three_prepares(self):
        import shutil
        import subprocess
        ffmpeg = shutil.which('ffmpeg')
        ffprobe = shutil.which('ffprobe')
        self.assertTrue(ffmpeg and ffprobe, 'Real FFmpeg/ffprobe required for this regression')
        task, processor, _, _ = self._assets(burned=False)
        Path(self.embedded_path).unlink(missing_ok=True)
        processor.config.update(VIDEO_CPU_CODEC='h264', VIDEO_CPU_PRESET='ultrafast', VIDEO_ENCODING_MODE='cpu')
        subprocess.run([ffmpeg, '-y', '-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=10:d=0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', self.video_path], check=True, capture_output=True)
        with patch.object(tm, 'get_ffmpeg_path', return_value=ffmpeg), patch.object(tm, 'get_ffprobe_path', return_value=ffprobe), patch.object(processor, '_embed_subtitle_in_video', wraps=processor._embed_subtitle_in_video) as burn:
            result = self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
            self.assertEqual(result['video_path_local'], self.embedded_path)
            before = Path(self.embedded_path).read_bytes()
            for _ in range(3):
                result = self._run_with_task_patches(task, lambda: processor._prepare_subtitle_for_upload(self.task_id, MagicMock()))
                self.assertEqual(result['video_path_local'], self.embedded_path)
                self.assertEqual(Path(self.embedded_path).read_bytes(), before)
            burn.assert_called_once()
            self.assertGreater(len(before), 1000)

    def test_process_initial_burn_then_both_upload_entries_and_checkpoint(self):
        for method in ('_do_upload_to_acfun', '_do_upload_to_bilibili'):
            task, processor, source, output = self._assets(burned=False)
            Path(self.embedded_path).unlink(missing_ok=True)
            # Real translation orchestration; only provider itself is mocked.
            output.unlink()
            translator = MagicMock()
            def translate_file(src, dest, **kwargs):
                self._write_srt(dest, '你好世界')
                return True
            translator.translate_file.side_effect = translate_file
            def embed(task_id, media, subtitle, logger):
                self.assertEqual(media, self.video_path)
                Path(self.embedded_path).write_bytes(b'offline burned fixture')
                os.utime(self.embedded_path, (Path(subtitle).stat().st_mtime + 10,) * 2)
                return self.embedded_path
            processor.config['UPLOAD_TARGET'] = 'bilibili' if method.endswith('bilibili') else 'acfun'
            task['upload_target'] = processor.config['UPLOAD_TARGET']
            stages = set(tm.PIPELINE_STAGE_ORDER) - {tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE, tm.PIPELINE_STAGE_UPLOAD_TO_ACFUN}
            reached_upload_boundary = []
            def stop_before_network(*args, **kwargs):
                reached_upload_boundary.append(task['video_path_local'])
                raise RuntimeError('offline upload boundary')
            def upload_entry(task_id, logger, **kwargs):
                # Actual platform caller proceeds through real preparation, then
                # a mocked tag boundary halts before credentials/network.
                with patch.object(processor, '_recover_cover_path', return_value=''), patch.object(tm, 'resolve_upload_tags', side_effect=stop_before_network):
                    try:
                        getattr(processor, method)(task_id, logger)
                    except RuntimeError as exc:
                        self.assertEqual(str(exc), 'offline upload boundary')
                self.assertEqual(task['video_path_local'], self.embedded_path)
                task.update(status=tm.TASK_STATES['FAILED'], error_category='upload_failed')
            with ExitStack() as stack:
                stack.enter_context(patch('modules.subtitle_translator.create_translator_from_config', return_value=translator))
                burn = stack.enter_context(patch.object(processor, '_embed_subtitle_in_video', side_effect=embed))
                stack.enter_context(patch.object(tm, '_get_completed_stages', side_effect=lambda *args, **kwargs: stages))
                stack.enter_context(patch.object(tm, '_persist_pipeline_checkpoint'))
                def mark(_id, done, stage):
                    stages.add(stage)
                    return done | {stage}
                stack.enter_context(patch.object(tm, '_mark_stage_done', side_effect=mark))
                stack.enter_context(patch.object(tm, 'clear_task_cancel'))
                stack.enter_context(patch.object(threading, 'Thread'))
                stack.enter_context(patch.object(processor, '_upload_to_target', side_effect=upload_entry))
                self._run_with_task_patches(task, lambda: processor.process_task(self.task_id, slot_already_acquired=True, acquired_task_semaphore=threading.Semaphore(0)))
                for _ in range(3):
                    task.update(status=tm.TASK_STATES['READY_FOR_UPLOAD'])
                    self._run_with_task_patches(task, lambda: upload_entry(self.task_id, MagicMock()))
                task.update(status=tm.TASK_STATES['READY_FOR_UPLOAD'])
                self._run_with_task_patches(task, lambda: processor.process_task(self.task_id, slot_already_acquired=True, acquired_task_semaphore=threading.Semaphore(0)))
                translator.translate_file.assert_called_once()
                burn.assert_called_once()
                self.assertGreaterEqual(len(reached_upload_boundary), 4, repr(task))
                self.assertTrue(all(path == self.embedded_path for path in reached_upload_boundary))
