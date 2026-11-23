"""Recover display-window replacement for verified YouTube automatic SRTs."""
import http.client
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import parse_qs, urlsplit
from urllib.request import getproxies

RECEIPT_PREFIX = '__Y2A_CAPTION_WINDOW__ '
RECEIPT_TEMPLATE = 'before_dl:' + RECEIPT_PREFIX + '%(.{id,extractor_key,requested_subtitles})j'
_MAX_BYTES = 4 * 1024 * 1024
_HOST = 'www.youtube.com'
_PATH = '/api/timedtext'
_TIME = r'[0-9]{2,}:[0-5][0-9]:[0-5][0-9],[0-9]{3}'
_TIMING = re.compile(rf'({_TIME}) --> ({_TIME})')
_YT_ID = re.compile(r'[A-Za-z0-9_-]+')


def permits_direct_lookup(command, config):
    """Do not route outside a configured proxy or an unknown CLI config."""
    if config.get('YOUTUBE_PROXY_ENABLED') or any(k != 'no' for k in getproxies()):
        return False
    if any(arg.startswith(('-P', '--paths', '--proxy', '--config-location'))
           or arg in ('-s', '--simulate', '-q', '--quiet') for arg in command[3:]):
        return False
    try:
        from yt_dlp.utils import get_executable_path, get_system_config_dirs, get_user_config_dirs
        if len(command) < 3 or command[1:3] != ['-m', 'yt_dlp']:
            return False  # An external or frozen executable can use other config locations.
        paths = [Path(get_executable_path()) / 'yt-dlp.conf', Path.cwd() / 'yt-dlp.conf']
        for directory in (*get_user_config_dirs('yt-dlp'), *get_system_config_dirs('yt-dlp')):
            loc = Path(directory)
            paths.extend((loc.parent / 'yt-dlp.conf', loc / 'config', loc / 'config.txt'))
            if loc.name == '.yt-dlp':
                paths.append(loc.parent / 'yt-dlp.conf.txt')
        return not any(os.path.lexists(path) for path in paths)
    except (ImportError, OSError):
        return False


def _milliseconds(timestamp):
    h, m, s, ms = re.split('[:,]', timestamp)
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _timestamp(ms):
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def trim_same_window_replacements(srt, payload):
    """Return exactly the original SRT except for proven replacement ends."""
    if not isinstance(payload, dict) or payload.get('wireMagic') != 'pb3':
        return srt
    events = payload.get('events')
    if not isinstance(events, list):
        return srt
    captions = []
    declared = set()
    for event in events:
        if not isinstance(event, dict):
            return srt
        start = event.get('tStartMs')
        if type(start) is not int or start < 0:
            return srt
        if 'segs' not in event:
            # Only initial window declarations are unambiguous non-text events.
            window = event.get('id')
            if (captions or type(window) is not int or window < 0 or window in declared
                    or set(event) - {'tStartMs', 'id', 'wpWinPosId', 'wsWinStyleId'}):
                return srt
            declared.add(window)
            continue
        duration, window = event.get('dDurationMs'), event.get('wWinId')
        append = event.get('aAppend', False)
        segments = event['segs']
        if (set(event) - {'tStartMs', 'dDurationMs', 'wWinId', 'aAppend', 'segs'}
                or type(duration) is not int or duration <= 0 or type(window) is not int or window < 0
                or type(append) not in (bool, int) or append not in (0, 1)
                or not isinstance(segments, list) or not segments
                or (captions and start <= captions[-1][0])):
            return srt
        for segment in segments:
            if (not isinstance(segment, dict) or not isinstance(segment.get('utf8'), str)
                    or type(segment.get('tOffsetMs', 0)) is not int or segment.get('tOffsetMs', 0)):
                return srt
        text = ' '.join(''.join(seg['utf8'] for seg in segments).split())
        if not text:
            return srt
        captions.append((start, start + duration, window, append, text))

    # FFmpeg output uses indexed blocks. Keep the original line endings and
    # text bytes; require a complete ordered timing AND text match before editing.
    lines = srt.splitlines(keepends=True)
    groups = []
    first = 0
    while first < len(lines):
        end = first
        while end < len(lines) and lines[end].strip():
            end += 1
        if end - first < 3 or lines[first].strip() != str(len(groups) + 1):
            return srt
        match = _TIMING.fullmatch(lines[first + 1].rstrip('\r\n'))
        if not match:
            return srt
        body = [line.strip() for line in lines[first + 2:end]]
        groups.append((first + 1, match, body))
        first = end + 1
    if not groups or len(groups) != len(captions):
        return srt
    for (_, match, body), (start, end, _, _, text) in zip(groups, captions):
        if (_milliseconds(match[1]), _milliseconds(match[2]), ' '.join(' '.join(body).split())) != (start, end, text):
            return srt
    # The existing text-based de-roller owns rolling cues. Use its same
    # majority signature, without changing its input, timestamps or behavior.
    rolls = sum(a[2][-1] == b[2][0] for a, b in zip(groups, groups[1:]))
    if rolls * 2 > len(groups) - 1:
        return srt

    previous = {}
    for i, (start, _, window, append, _) in enumerate(captions):
        old = previous.get(window)
        previous[window] = i
        if old is None or append:
            continue
        old_start, old_end, _, old_append, _ = captions[old]
        # Leave append chains alone; do not collapse an interval at ASS
        # centisecond precision or make any non-overlap longer.
        if not old_append and old_start + 10 <= start < old_end:
            line, match, _ = groups[old]
            lines[line] = lines[line][:match.start(2)] + _timestamp(start) + lines[line][match.end(2):]
    return ''.join(lines)


def _read_json3(url, video_id):
    # yt-dlp constructs YouTube subtitle variants by changing fmt on the
    # same URL. Keep the original query intact except for its sole fmt=vtt.
    # The URL stays in memory; do not use global proxy/cookie/redirect handlers.
    if (not isinstance(video_id, str) or not _YT_ID.fullmatch(video_id)
            or not isinstance(url, str) or not re.fullmatch(r'[!-~]+', url)):
        return None
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc, parts.path, parts.fragment) != ('https', _HOST, _PATH, ''):
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    lang = query.get('lang', [])
    if (query.get('v') != [video_id] or query.get('kind') != ['asr']
            or len(lang) != 1 or not lang[0] or 'tlang' in query or query.get('fmt') != ['vtt']):
        return None
    raw_query, count = re.subn(r'(^|&)fmt=vtt(?=&|$)', r'\g<1>fmt=json3', parts.query)
    if count != 1:
        return None
    connection = http.client.HTTPSConnection(_HOST, timeout=5)
    try:
        connection.request('GET', _PATH + '?' + raw_query)
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(_MAX_BYTES + 1)
        if len(body) <= _MAX_BYTES:
            return json.loads(body)
    finally:
        connection.close()
    return None


def repair_fresh_auto_srt(task_dir, key, receipts, logger):
    """Best effort; caller gates on a fresh download, never translation/retry reuse."""
    if len(receipts) != 1:
        return
    vtt = Path(task_dir) / f'video.{key}.vtt'
    srt = vtt.with_suffix('.srt')
    try:
        receipt = json.loads(receipts[0])
        if receipt['extractor_key'] != 'Youtube' or set(receipt['requested_subtitles']) != {key}:
            return
        sub = receipt['requested_subtitles'][key]
        if (sub.get('ext') != 'vtt' or sub.get('filepath') != str(vtt)
                or vtt.exists() or not srt.is_file() or srt.is_symlink()):
            return
        original_stat = srt.stat()
        if original_stat.st_size > _MAX_BYTES:
            return
        raw = srt.read_bytes().decode('utf-8')
        if not raw:
            return
        fixed = trim_same_window_replacements(raw, _read_json3(sub.get('url'), receipt['id']))
        if fixed == raw:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=task_dir, delete=False) as output:
                temporary = output.name
                output.write(fixed.encode('utf-8'))
            os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
            os.replace(temporary, srt)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        logger.info('YouTube 自动字幕：已修正同窗替换结束时间')
    except Exception as exc:
        # Exceptions may contain signed URLs. Only log the class.
        logger.debug('YouTube 自动字幕同窗检查跳过: %s', type(exc).__name__)
