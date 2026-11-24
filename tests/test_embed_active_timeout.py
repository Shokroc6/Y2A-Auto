"""Synthetic burn timing through the real wrapper and local subprocess pipes."""
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
        print(f'frame={min(frame, 17200)}', flush=True)
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
             scale=1000, configure=None):
    real_clock = time.monotonic
    origin = real_clock()
    real_popen = subprocess.Popen
    children = []
    clock = lambda: (real_clock() - origin) * scale

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
        return cancel and children and (real_clock() - children[-1][2]) * scale > _INITIAL + 40

    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {'VIDEO_ENCODER': encoder, 'VIDEO_CPU_CODEC': codec,
                        'VIDEO_HW_QUALITY_BOOST': False}
    processor.config.update(configure or {})
    video = tmp_path / 'synthetic.mp4'
    video.touch()
    captions = tmp_path / 'synthetic.srt'
    captions.write_text('1\n00:00:00,000 --> 00:00:01,000\nTest cue\n', encoding='utf-8')

    with (patch.object(tm, 'get_task', return_value=None), patch.object(tm, 'update_task'),
          patch.object(tm, 'is_task_cancelled', side_effect=cancelled),
          patch.object(tm, 'get_ffmpeg_path', return_value=sys.executable),
          patch.object(tm, 'get_ffprobe_path', return_value=sys.executable),
          patch.object(processor, '_probe_hw_encoder_availability', return_value=(True, '', {})),
          patch.object(tm.subprocess, 'run') as probes,
          patch.object(tm.subprocess, 'Popen', side_effect=start_child),
          patch.object(processor, '_get_video_duration', return_value=_DURATION),
          patch.object(processor, '_get_video_stream_info', return_value={
              'width': 3840, 'height': 2160, 'fps': 60000 / 1001, 'codec_name': 'av1'}),
          patch.object(processor, '_get_audio_stream_info', return_value={}),
          patch.object(time, 'time', side_effect=clock),
          patch.object(time, 'monotonic', side_effect=clock)):
        probes.return_value.returncode = 0
        probes.return_value.stdout = 'subtitles'
        try:
            output = processor._embed_subtitle_in_video('offline', str(video), str(captions), logging.getLogger(__name__))
        finally:
            for proc, _, _ in children:
                with proc:
                    if proc.poll() is None:
                        proc.kill()

    assert len(children) == len(scripts)
    return output, [(proc.returncode, command) for proc, command, _ in children]


def test_advancing_uhd_burn_can_finish_past_initial_budget(tmp_path):
    assert tm.TaskProcessor._estimate_embed_timeout(_DURATION, 'x264', 3840, 2160, '60000/1001') == _INITIAL
    # The configured casing is normalized by the same production encoder path.
    output, [(status, cmd)] = run_burn(tmp_path, [('advancing', 3620)], codec='X264')
    assert output is not None, 'the running CPU encode was killed at its initial budget'
    assert status == 0 and 'libx264' in cmd
    assert Path(output).read_bytes() == b'synthetic encoded output'


@pytest.mark.parametrize(('mode', 'finish', 'extended'), [
    ('repeat', 4100, False), ('noise', 4100, False), ('silent', 4700, False),
    ('stall', 4300, True), ('advancing', 4990, True),
])
def test_unreliable_progress_stall_and_cap_terminate(tmp_path, caplog, mode, finish, extended):
    with caplog.at_level(logging.INFO, logger=__name__):
        output, [(status, _)] = run_burn(tmp_path, [(mode, finish)])
    assert output is None and status != 0
    assert ('有限延长' in caplog.text) == extended
    assert 'FFmpeg处理超时' in caplog.text


def test_early_stall_does_not_shorten_original_budget(tmp_path):
    output, [(status, _)] = run_burn(tmp_path, [('repeat', 3200)])
    assert output is not None and status == 0


@pytest.mark.skipif(sys.platform == 'win32', reason='Popen.terminate uses TerminateProcess on Windows')
def test_terminated_zero_exit_cannot_publish_partial_output(tmp_path):
    output, [(status, _)] = run_burn(tmp_path, [('term_success', 4100)])
    assert output is None and status == 0
    assert not list(tmp_path.glob('*with_subtitle*.mp4'))


def test_cancel_during_extension(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger=__name__):
        output, [(status, _)] = run_burn(tmp_path, [('advancing', 4100)], cancel=True)
    assert output is None and status != 0
    assert '取消请求' in caplog.text
    assert 'FFmpeg处理超时' not in caplog.text


def test_cpu_extension_works_after_hw_used_its_full_budget(tmp_path, caplog):
    # Hardware gets 1800 seconds and cannot extend. Together with the original
    # 3521 CPU seconds the overall budget is exhausted before CPU can finish.
    with caplog.at_level(logging.INFO, logger=__name__):
        output, [(hw_status, hw), (cpu_status, cpu)] = run_burn(
            tmp_path, [('advancing', 2500), ('advancing', 3620)], encoder='nvidia')
    assert output is not None and hw_status != 0 and cpu_status == 0
    assert 'hevc_nvenc' in hw and 'libx264' in cpu
    assert caplog.text.count('有限延长') == 1


def test_x265_fallback_has_fresh_progress_state(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger=__name__):
        output, [(first, x265), (second, x264)] = run_burn(
            tmp_path, [('error', 0), ('advancing', 3620)], codec='x265')
    assert output is not None and first != 0 and second == 0
    assert 'libx265' in x265 and 'libx264' in x264
    assert caplog.text.count('有限延长') == 1
    assert 'Unknown encoder' in caplog.text


def test_custom_encoder_keeps_original_budget(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger=__name__):
        output, [(status, cmd)] = run_burn(tmp_path, [('advancing', 4100)], configure={
            'VIDEO_CUSTOM_PARAMS_ENABLED': True, 'VIDEO_CUSTOM_PARAMS': '-c:v libx265',
        })
    assert output is None and status != 0 and 'libx265' in cmd
    assert '有限延长' not in caplog.text


def test_deadline_requires_recent_monotonic_frame_advances():
    state = tm._EmbedProgressDeadline(1000, 1250)
    state.observe(4, 810)
    state.observe(5, 815)
    assert state.deadline == 1000  # Was active, now stale.
    state.observe(6, 998)
    assert state.deadline == 1000  # Only one new report after the gap.
    state.observe(7, 999)
    assert state.deadline == 1000  # A buffered burst does not buy time.
    state.observe(8, 1000)
    assert state.deadline == 1000  # Exact cutoff cannot revive the attempt.


def test_deadline_stops_after_stall_or_at_hard_cap():
    state = tm._EmbedProgressDeadline(1000, 1120)
    state.observe(1, 965)
    state.observe(2, 970)
    assert state.deadline == 1090
    state.observe(2, 1087)
    state.observe(1, 1088)
    assert state.deadline == 1090
    state.observe(3, 1089)
    assert state.deadline == 1120
    state.observe(4, 1120)
    assert state.deadline == 1120


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
