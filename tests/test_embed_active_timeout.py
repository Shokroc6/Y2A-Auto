"""Synthetic burn timing through the real wrapper and local subprocess pipes."""
from contextlib import closing
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from modules import task_manager as tm


_DURATION = 293.474
_INITIAL = 3521

# Reconstructed from observed elapsed time/frame counters, not a user file/log.
# The UHD/59.94 run still encoded frames and reached 283.53s at its old cutoff.
_CHILD = '''
import pathlib, signal, sys, time
mode, start, finish, scale, out = sys.argv[1:]
start, finish, scale = map(float, (start, finish, scale))
if mode == 'term_success':
    def finish_after_signal(*_args):
        pathlib.Path(out).write_bytes(b'incomplete output')
        sys.exit(0)
    signal.signal(signal.SIGTERM, finish_after_signal)
while True:
    elapsed = (time.monotonic() - start) * scale
    output = min(293.474, elapsed * 283.53 / 3521)
    frame = int(elapsed * 16990 / 3521)
    if mode == 'advancing':
        print(f'frame={frame}', flush=True)
    elif mode == 'stall':
        print(f'frame={min(frame, 2400)}', flush=True)
    elif mode == 'repeat':
        print('frame=12', flush=True)
    elif mode == 'noise':
        print('\\nframe=N/A\\nframe=-7\\nframe=+15\\nframe=1=2', flush=True)
        print(f'frame={frame}', file=sys.stderr, flush=True)
    elif mode == 'error':
        print('Unknown encoder', file=sys.stderr, flush=True)
        sys.exit(1)
    if mode != 'silent':
        print(f'out_time_us={int(output * 1000000)}\\nprogress=continue', flush=True)
    if elapsed >= finish:
        break
    time.sleep(.02)
pathlib.Path(out).write_bytes(b'synthetic encoded output')
'''


def run_burn(tmp_path, scripts, *, encoder='cpu', codec='x264', cancel=False,
             scale=1000, configure=None, progress_write=None, on_poll=None, task_id='offline',
             real_db=False, retry_delay=0):
    real_clock = time.monotonic
    origin = real_clock()
    real_popen = subprocess.Popen
    children = []
    offset = 0
    clock = lambda: (real_clock() - origin) * scale + offset

    def start_child(command, **kwargs):
        if len(children) >= len(scripts):
            pytest.fail('An unexpected stage was launched')
        mode, finish = scripts[len(children)]
        started = real_clock()
        proc = real_popen([sys.executable, '-u', '-c', _CHILD, mode, str(started),
                           str(finish), str(scale), command[-1]], **kwargs)
        children.append((proc, command, started))
        return proc

    def cancelled(_task_id):
        if children:
            proc, _, started = children[-1]
            elapsed = (real_clock() - started) * scale
            if on_poll is not None:
                on_poll(proc, elapsed)
            if cancel and elapsed > 600:
                tm.request_task_cancel(task_id)
        return real_cancelled(task_id)

    real_cancelled, real_update, real_get = tm.is_task_cancelled, tm.update_task, tm.get_task

    def update(_task_id, **fields):
        if progress_write is not None and fields.get('upload_progress') is not None:
            progress_write(children[-1][0])
        return real_update(_task_id, **fields) if real_db else True

    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {'VIDEO_ENCODER': encoder, 'VIDEO_CPU_CODEC': codec,
                        'VIDEO_HW_QUALITY_BOOST': False}
    processor.config.update(configure or {})
    resolve_retry = processor._resolve_embed_retry_stages

    def after_first_stage(*args):
        nonlocal offset
        offset += retry_delay
        return resolve_retry(*args)
    video = tmp_path / 'synthetic.mp4'
    video.touch()
    captions = tmp_path / 'synthetic.srt'
    captions.write_text('1\n00:00:00,000 --> 00:00:01,000\nTest cue\n', encoding='utf-8')

    with (patch.object(tm, 'get_task', side_effect=real_get if real_db else lambda _: None),
          patch.object(tm, 'update_task', side_effect=update),
          patch.object(tm, 'is_task_cancelled', side_effect=cancelled),
          patch.object(processor, '_resolve_embed_retry_stages', side_effect=after_first_stage),
          patch.object(tm, 'get_ffmpeg_path', return_value=sys.executable),
          patch.object(tm, 'get_ffprobe_path', return_value=sys.executable),
          patch.object(processor, '_probe_hw_encoder_availability', return_value=(True, '', {})),
          patch.object(tm.subprocess, 'run') as probes,
          patch.object(tm.subprocess, 'Popen', side_effect=start_child),
          patch.object(processor, '_get_video_duration', return_value=_DURATION),
          patch.object(processor, '_get_video_stream_info', return_value={
              'width': 3840, 'height': 2160, 'fps': 60000 / 1001, 'codec_name': 'av1'}),
          patch.object(processor, '_get_audio_stream_info', return_value={}),
          patch.object(time, 'monotonic', side_effect=clock)):
        probes.return_value.returncode = 0
        probes.return_value.stdout = 'subtitles'
        try:
            output = processor._embed_subtitle_in_video(task_id, str(video), str(captions), logging.getLogger(__name__))
        finally:
            tm.clear_task_cancel(task_id, clear_flag=True)
            for proc, _, _ in children:
                with proc:
                    if proc.poll() is None:
                        proc.kill()

    assert len(children) == len(scripts)
    return output, [(proc.returncode, command) for proc, command, _ in children]


def test_advancing_burn_can_finish_past_old_absolute_cap(tmp_path):
    output, [(status, cmd)] = run_burn(tmp_path, [('advancing', 4990)], codec='X264', scale=1500)
    assert output is not None, 'the running CPU encode was killed at its old absolute cap'
    assert status == 0 and 'libx264' in cmd


def test_advancing_uhd_burn_can_finish_past_initial_budget(tmp_path):
    assert tm.TaskProcessor._estimate_embed_timeout(_DURATION, 'x264', 3840, 2160, '60000/1001') == _INITIAL
    # The configured casing is normalized by the same production encoder path.
    output, [(status, cmd)] = run_burn(tmp_path, [('advancing', 3620)], codec='X264')
    assert output is not None, 'the running CPU encode was killed at its initial budget'
    assert status == 0 and 'libx264' in cmd
    assert Path(output).read_bytes() == b'synthetic encoded output'


@pytest.mark.parametrize('mode', ['silent', 'repeat', 'noise', 'stall'])
def test_no_new_frame_terminates_burn(tmp_path, caplog, mode):
    output, [(status, _)] = run_burn(tmp_path, [(mode, 2800)])
    assert output is None and status != 0
    assert '连续 20 分钟无新视频帧' in caplog.text


def test_natural_exit_before_first_frame_deadline(tmp_path, caplog):
    output, [(status, _)] = run_burn(tmp_path, [('silent', 600)])
    assert output is not None and status == 0
    assert 'FFmpeg处理超时' not in caplog.text


def test_natural_exit_is_not_a_timeout_when_observed_late(tmp_path, caplog):
    def poll_late(proc, _elapsed):
        assert proc.wait(timeout=5) == 0
        time.sleep(1.25)

    output, [(status, _)] = run_burn(tmp_path, [('silent', 400)], on_poll=poll_late)
    assert output is not None and status == 0
    assert 'FFmpeg处理超时' not in caplog.text


@pytest.mark.skipif(sys.platform == 'win32', reason='Popen.terminate uses TerminateProcess on Windows')
def test_terminated_zero_exit_cannot_publish_partial_output(tmp_path):
    output, [(status, _)] = run_burn(tmp_path, [('term_success', 2800)])
    assert output is None and status == 0
    assert not list(tmp_path.glob('*with_subtitle*.mp4'))


def test_cancel_during_advancing_burn(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger=__name__):
        output, [(status, _)] = run_burn(tmp_path, [('advancing', 4100)], cancel=True)
    assert output is None and status != 0
    assert '取消请求' in caplog.text
    assert 'FFmpeg处理超时' not in caplog.text


def test_hardware_budget_unchanged_and_cpu_fallback_has_no_cap(tmp_path, caplog):
    output, [(hw_status, hw), (cpu_status, cpu)] = run_burn(
        tmp_path, [('advancing', 2500), ('advancing', 4990)], encoder='nvidia', scale=1500)
    assert output is not None and hw_status != 0 and cpu_status == 0
    assert 'hevc_nvenc' in hw and 'libx264' in cpu
    assert '初始预算 1800 秒' in caplog.text


def test_x265_fallback_can_start_after_the_old_overall_deadline(tmp_path, caplog):
    output, [(first, x265), (second, x264)] = run_burn(
        tmp_path, [('error', 0), ('advancing', 1400)], codec='x265', retry_delay=60000)
    assert output is not None and first != 0 and second == 0
    assert 'libx265' in x265 and 'libx264' in x264
    assert 'Unknown encoder' in caplog.text


def test_custom_encoder_keeps_original_budget(tmp_path, caplog):
    output, [(status, cmd)] = run_burn(tmp_path, [('advancing', 4100)], configure={
        'VIDEO_CUSTOM_PARAMS_ENABLED': True, 'VIDEO_CUSTOM_PARAMS': '-c:v libx265',
    })
    assert output is None and status != 0 and 'libx265' in cmd
    assert '初始预算 3521 秒' in caplog.text


def test_libx265_keeps_its_original_budget(tmp_path):
    output, [(status, cmd)] = run_burn(tmp_path, [('silent', 1500)], codec='x265')
    assert output is not None and status == 0 and 'libx265' in cmd


def test_first_and_last_frame_exact_idle_boundaries():
    state = tm._EmbedFrameDeadline(0)
    state.observe(0, 1199.999)
    assert state.deadline == 1200
    state.observe(3, 1200)
    assert state.deadline == 1200  # A frame at the cutoff is too late.

    state = tm._EmbedFrameDeadline(0)
    state.observe(8, 1199.999)
    assert state.deadline == 2399.999
    state.observe(8, 2000)
    state.observe(7, 2399.998)
    state.observe(9, 2399.999)
    assert state.deadline == 2399.999


@pytest.mark.parametrize(('scheduled', 'manual_scan'), [(True, False), (False, False), (False, True)],
                         ids=['scheduled', 'forced', 'manual-recovery'])
def test_stale_reaper_skips_only_the_running_ffmpeg(tmp_path, monkeypatch, scheduled, manual_scan):
    task_id = f'x264-watch-{scheduled}-{manual_scan}'
    monkeypatch.setattr(tm, 'DB_PATH', str(tmp_path / 'tasks.db'))
    tm.init_db()
    with closing(tm.get_db_connection()) as db, db:
        db.execute('INSERT INTO tasks (id, youtube_url, status) VALUES (?, ?, ?)',
                   (task_id, 'offline', 'ready_for_upload'))

    def make_stale():
        with closing(tm.get_db_connection()) as db, db:
            db.execute('UPDATE tasks SET updated_at = ? WHERE id = ?', ('2000-01-01', task_id))

    recoveries = []

    def scan(proc, elapsed):
        if recoveries or elapsed < 1800 or proc.poll() is not None:
            return
        make_stale()
        recoveries.append(tm.reset_stuck_tasks(skip_active=not manual_scan, cancel_active=not manual_scan))
        recoveries.append(tm.get_task(task_id)['status'])

    if scheduled:
        tm._mark_task_active(task_id)
    try:
        output, [(status, _)] = run_burn(tmp_path, [('advancing', 2650)], on_poll=scan,
                                        real_db=True, task_id=task_id)
        assert recoveries == [0, 'encoding_video']
        assert output is not None and status == 0
        tm.update_task(task_id, status='encoding_video')
        make_stale()
        assert tm.reset_stuck_tasks(skip_active=True, cancel_active=True) == (0 if scheduled else 1)
        assert tm.is_task_cancelled(task_id) == scheduled
    finally:
        tm._mark_task_inactive(task_id)
        tm.clear_task_cancel(task_id, clear_flag=True)
        with closing(tm.get_db_connection()) as db, db:
            db.execute('DELETE FROM tasks WHERE id = ?', (task_id,))


@pytest.mark.parametrize('cancel', [False, True], ids=['stall', 'cancel'])
def test_blocked_progress_write_cannot_delay_child_shutdown(tmp_path, cancel):
    exits = []

    def blocked(proc):
        if not exits:
            if cancel:
                tm.request_task_cancel('offline')
            exits.append(proc.wait(timeout=5))

    output, [(status, _)] = run_burn(tmp_path, [('advancing' if cancel else 'repeat', 2500)],
                                    progress_write=blocked)
    assert exits and exits[0] != 0
    assert output is None and status != 0


def test_cancel_after_child_exits_during_progress_write_discards_output(tmp_path):
    exits = []

    def cancel_after_exit(proc):
        if not exits:
            exits.append(proc.wait(timeout=5))
            tm.request_task_cancel('offline')

    output, [(status, _)] = run_burn(tmp_path, [('advancing', 500)], progress_write=cancel_after_exit)
    assert exits == [0] and status == 0
    assert output is None
    assert not list(tmp_path.glob('*with_subtitle*.mp4'))


def test_same_percentage_keeps_task_fresh_at_natural_exit(tmp_path, monkeypatch):
    task_id = 'rounded-progress'
    monkeypatch.setattr(tm, 'DB_PATH', str(tmp_path / 'tasks.db'))
    tm.init_db()
    with closing(tm.get_db_connection()) as db, db:
        db.execute('INSERT INTO tasks (id, youtube_url, status) VALUES (?, ?, ?)',
                   (task_id, 'synthetic', 'ready_for_upload'))

    real_clock = time.monotonic
    origin, wall = real_clock(), datetime.now()

    class ScaledWall(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall - timedelta(seconds=max(0, 2600 - 1000 * (real_clock() - origin)))

    monkeypatch.setattr(tm, 'datetime', ScaledWall)
    # All samples advance by one 59.94 fps frame; all display as 100.0%.
    monkeypatch.setitem(globals(), '_CHILD', '''
import pathlib, sys, time
_, start, finish, scale, out = sys.argv[1:]
start, finish, scale = map(float, (start, finish, scale))
last_index = -1
while True:
    elapsed = (time.monotonic() - start) * scale
    index = min(6, int(elapsed // 410))
    if index != last_index:
        print(f'frame={17585 + index}', flush=True)
        print(f'out_time_us={293474000 - (6 - index) * 16683}', flush=True)
        last_index = index
    if elapsed >= finish:
        break
    time.sleep(.01)
pathlib.Path(out).write_bytes(b'synthetic encoded output')
''')
    scanned = {}

    def scan_on_exit(proc, _elapsed):
        if proc.poll() != 0 or scanned:
            return
        row = tm.get_task(task_id)
        scanned['age'] = (datetime.now() - datetime.fromisoformat(row['updated_at'])).total_seconds()
        scanned['progress'] = row['upload_progress']
        tm.reset_stuck_tasks(skip_active=True, cancel_active=True)
        scanned['cancelled'] = tm.is_task_cancelled(task_id)

    tm._mark_task_active(task_id)
    try:
        output, [(status, _)] = run_burn(tmp_path, [('advancing', 2600)], task_id=task_id,
                                         real_db=True, on_poll=scan_on_exit)
    finally:
        tm._mark_task_inactive(task_id)
    assert status == 0 and scanned['progress'] == '100.0%'
    assert scanned['age'] < 30 * 60, scanned
    assert not scanned['cancelled'] and Path(output).read_bytes() == b'synthetic encoded output'


def test_real_short_ffmpeg_burn_changes_pixels(tmp_path):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not (ffmpeg and ffprobe):
        pytest.skip('FFmpeg/ffprobe required for synthetic burn smoke')
    video = tmp_path / 'smoke.mp4'
    captions = tmp_path / 'cue.srt'
    captions.write_text('1\n00:00:00,000 --> 00:00:00,500\nSynthetic cue\n', encoding='utf-8')
    subprocess.run([ffmpeg, '-y', '-f', 'lavfi', '-i', 'color=black:s=320x180:r=10:d=0.5',
                    '-c:v', 'libx264', str(video)], check=True, capture_output=True)
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {'VIDEO_ENCODER': 'cpu', 'VIDEO_CPU_CODEC': 'x264'}
    with (patch.object(tm, 'get_task', return_value=None), patch.object(tm, 'update_task'),
          patch.object(tm, 'get_ffmpeg_path', return_value=ffmpeg),
          patch.object(tm, 'get_ffprobe_path', return_value=ffprobe)):
        output = processor._embed_subtitle_in_video('offline', str(video), str(captions), logging.getLogger(__name__))
    assert output is not None
    def frame(path):
        return subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(path), '-frames:v', '1',
                                        '-pix_fmt', 'gray', '-f', 'rawvideo', '-threads', '1', '-'])
    before, after = frame(video), frame(output)
    assert len(before) == len(after) == 320 * 180
    assert before != after
    fields = 'stream=codec_name,width,height,avg_frame_rate,duration'

    def info(path):
        return json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                                                   '-show_entries', fields, '-of', 'json', str(path)]))['streams'][0]

    assert info(video) == info(output) == {
        'codec_name': 'h264', 'width': 320, 'height': 180, 'avg_frame_rate': '10/1', 'duration': '0.500000',
    }
