"""Read-only real VTT acceptance. STUB translation, no AI or upload."""
import run_offline_tests as isolation
from pathlib import Path
import hashlib
import json
import logging
import shutil
import subprocess
from unittest.mock import patch
from modules import task_manager as tm
from modules.subtitle_translator import SubtitleReader
from test_subtitle_translator_pairing import _make_translator, _make_requester

source = Path('/home/ubuntu/arclight/y2a-migration-audit/real-subtitles-XN3xNJvWXsc/XN3xNJvWXsc.en.vtt')
before = hashlib.sha256(source.read_bytes()).hexdigest()
task_dir = isolation.runtime / 'downloads' / 'real'
task_dir.mkdir(parents=True)
shutil.copyfile(source, task_dir / 'video.en.vtt')
(task_dir / 'video.zh.srt').write_text('1\n00:00:00,500 --> 00:00:06,031\n不应选择的已有中文\n\n')
requester = _make_requester()
translator = _make_translator(requester, bilingual=True)
processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
processor.config = {'SUBTITLE_BILINGUAL_ENABLED': True, 'SUBTITLE_TRANSLATION_ENABLED': True, 'SUBTITLE_EMBED_IN_VIDEO': False}
task = {'video_path_local': 'unused', 'upload_target': 'acfun'}
with patch.object(tm, 'DOWNLOADS_DIR', str(task_dir.parent)), patch.object(tm, 'get_task', return_value=task), patch.object(tm, 'update_task'), patch('modules.subtitle_translator.create_translator_from_config', return_value=translator), patch('modules.subtitle_translator.openai_chat_create_with_thinking_control', side_effect=requester.fake_create):
    assert processor._translate_subtitle('real', logging.getLogger('acceptance'))
original = SubtitleReader.read_vtt(str(source), preserve_lines=True)
output = task_dir / 'translated_real.srt'
paired = SubtitleReader.read_srt(str(output), preserve_lines=True)
assert len(original) == len(paired) > 0
for a, b in zip(original, paired):
    assert (a.start_time, a.end_time) == (b.start_time, b.end_time)
    assert b.source_text.startswith(a.source_text + '\n')
assert sum(map(len, requester.sent_batches)) == len(original)
assert before == hashlib.sha256(source.read_bytes()).hexdigest()
ass = task_dir / 'paired.ass'
assert processor._convert_srt_to_ass(str(output), str(ass), logging.getLogger('acceptance'), video_width=1920, video_height=1080, font_family='Noto Sans CJK SC')
assert ass.read_text().count('Dialogue:') == len(original)
for item in original:
    assert tm.TaskProcessor._escape_ass_text(item.source_text) in ass.read_text()
ffmpeg = shutil.which('ffmpeg')
assert ffmpeg, 'FFmpeg is required for the offline burn-in acceptance'
video = task_dir / 'stub-bilingual.mp4'
cmd = [ffmpeg, '-hide_banner', '-y', '-f', 'lavfi', '-i', 'color=c=0x202020:s=1920x1080:r=24:d=7', '-vf', f"subtitles={ass}:fontsdir={isolation.ROOT / 'fonts'}", '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23', str(video)]
result = subprocess.run(cmd, capture_output=True, text=True)
(task_dir / 'ffmpeg.log').write_text(result.stderr)
assert result.returncode == 0, result.stderr
# Decode frames to prove subtitle pixels differ from the plain background.
frame = subprocess.run([ffmpeg, '-v', 'error', '-ss', '1', '-i', str(video), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], capture_output=True, check=True).stdout
assert max(frame) - min(frame) > 100
summary = {'source_sha256': before, 'source_cues': len(original), 'paired_cues': len(paired), 'stub_request_items': sum(map(len, requester.sent_batches)), 'ass_events': ass.read_text().count('Dialogue:'), 'video_bytes': video.stat().st_size, 'decoded_frame_range': [min(frame), max(frame)], 'artifacts': str(task_dir), 'translation': 'STUB ONLY; not real AI', 'platform': 'Linux; not Windows'}
(task_dir / 'verification.json').write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
