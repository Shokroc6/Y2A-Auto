"""Offline acceptance: real application burn-in, synthetic video only."""
import json
import logging
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
import pytest
from modules import task_manager as tm
from modules.subtitle_translator import SubtitleItem, SubtitleWriter


def test_real_bilingual_burn_in(tmp_path):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('ffmpeg/ffprobe unavailable')
    video = tmp_path / 'synthetic.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=640x360:d=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(video)], check=True, timeout=30)
    subtitles = tmp_path / 'translated_offline.srt'
    SubtitleWriter.write_srt([SubtitleItem(1, '00:00:00,200', '00:00:01,800', 'Original source', '中文译文')], str(subtitles), bilingual=True)
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {'BILINGUAL_SUBTITLES': True, 'VIDEO_ENCODER': 'cpu', 'VIDEO_CPU_PRESET': 'ultrafast', 'SUBTITLE_FONT_NAME': 'DejaVu Sans', 'FFMPEG_AUTO_DOWNLOAD': False}
    with patch.object(tm, 'get_task', return_value={'status': 'translating_subtitle'}), patch.object(tm, 'update_task'), patch.object(tm, 'get_ffmpeg_path', return_value=ffmpeg), patch.object(tm, 'get_ffprobe_path', return_value=ffprobe):
        result = processor._embed_subtitle_in_video('offline-smoke', str(video), str(subtitles), logging.getLogger('offline-smoke'))
    assert result and Path(result).stat().st_size > 0
    probe = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', result], capture_output=True, check=True, timeout=30)
    stream = json.loads(probe.stdout)['streams'][0]
    assert (stream['width'], stream['height']) == (640, 360)
    assert float(stream['duration']) >= 1.9
    # Decoded pixels during the cue must differ from the black input frame.
    def frame(path):
        return subprocess.run([ffmpeg, '-v', 'error', '-ss', '1', '-i', str(path), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], capture_output=True, check=True, timeout=30).stdout
    assert frame(result) != frame(video)
