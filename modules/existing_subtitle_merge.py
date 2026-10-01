"""Conservative offline reuse of authored subtitle tracks (no AI)."""
from pathlib import Path
import re
from modules.subtitle_translator import SubtitleItem, SubtitleReader, SubtitleWriter


def merge_existing_tracks(source, target, output):
    def read(path):
        items = (SubtitleReader.read_vtt(path) if str(path).lower().endswith('.vtt')
                 else SubtitleReader.read_srt(path, preserve_lines=True))
        raw = Path(path).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
        blocks = [block for block in re.split(r'\n\s*\n', raw.strip())
                  if block.strip() and not block.lstrip().startswith(('WEBVTT', 'NOTE', 'STYLE', 'REGION'))]
        if len(blocks) != len(items) or any('-->' in item.source_text for item in items):
            raise ValueError('字幕含无法完整解析的条目')
        return items
    sources, targets = read(source), read(target)
    if not sources or not targets:
        raise ValueError('字幕为空')

    def milliseconds(value):
        hours, minutes, seconds = value.replace(',', '.').split(':')
        return round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)

    def timed(items):
        return [(milliseconds(x.start_time), milliseconds(x.end_time), x) for x in items]

    left, right = timed(sources), timed(targets)
    for entries in (left, right):
        previous_end = -1
        for start, end, item in entries:
            if start < previous_end or end <= start or not item.source_text.strip():
                raise ValueError('字幕时间轴倒序、重叠或空白')
            previous_end = end
    # Positive time overlap defines a connected component, not cue ordinal/count.
    # Each authored cue is emitted exactly once in its component, with all lines.
    i = j = 0
    items = []
    while i < len(left) and j < len(right):
        a, b = left[i], right[j]
        if min(a[1], b[1]) <= max(a[0], b[0]):
            raise ValueError('字幕时间轴不重叠')
        source_group, target_group = [a], [b]
        i += 1
        j += 1
        while True:
            if i < len(left) and left[i][0] < target_group[-1][1]:
                source_group.append(left[i])
                i += 1
            elif j < len(right) and right[j][0] < source_group[-1][1]:
                target_group.append(right[j])
                j += 1
            else:
                break
        if (abs(source_group[0][0] - target_group[0][0]) > 200
                or abs(source_group[-1][1] - target_group[-1][1]) > 200
                or max(source_group[-1][1], target_group[-1][1])
                   - min(source_group[0][0], target_group[0][0]) > 12000
                or len(source_group) > 4 or len(target_group) > 4
                or (len(source_group) > 1 and len(target_group) > 1)):
            raise ValueError('字幕时间对齐不可靠：边界偏差或分组过大')
        for group, other in ((source_group, target_group), (target_group, source_group)):
            for start, end, _ in group:
                coverage = sum(max(0, min(end, y[1]) - max(start, y[0])) for y in other)
                if coverage / (end - start) < 0.8:
                    raise ValueError('字幕时间覆盖不足80%')
        first = min(source_group[0], target_group[0], key=lambda x: x[0])[2]
        last = max(source_group[-1], target_group[-1], key=lambda x: x[1])[2]
        items.append(SubtitleItem(len(items) + 1, first.start_time, last.end_time,
                                  '\n'.join(x[2].source_text for x in source_group),
                                  '\n'.join(x[2].source_text for x in target_group)))
    if i != len(left) or j != len(right):
        raise ValueError('存在未匹配字幕')
    # The shared writer logs instead of raising. Write to a fresh sibling, verify,
    # then atomically publish so a failed attempt cannot reuse a stale artifact.
    import os
    import tempfile
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=Path(output).parent, suffix='.srt', delete=False) as handle:
            temporary = handle.name
        SubtitleWriter.write_srt(items, temporary, bilingual=True)
        if not Path(temporary).stat().st_size:
            raise OSError('合并字幕写入失败')
        os.replace(temporary, output)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
