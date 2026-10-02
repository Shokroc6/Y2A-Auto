"""Lossless description formatting shared by the two upload targets."""
from datetime import datetime


def build_repost_description(base_desc, original_url='', original_uploader='',
                             original_upload_date='', original_title='',
                             append_repost_notice=True, max_len=1000,
                             copyright_type='repost'):
    body = str(base_desc or '')
    date = str(original_upload_date or '').strip()
    try:
        date = datetime.strptime(date, '%Y%m%d' if len(date) == 8 else '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        date = ''  # Missing/invalid metadata is not a publish date.
    lines = []
    if append_repost_notice:
        for label, value in (('原视频', original_title), ('原作者', original_uploader),
                             ('发布日期', date), ('视频链接', original_url)):
            if value:
                lines.append(f'{label}：{value}')
    notice = '\n'.join(lines)
    return notice + ('\n\n' + body if body else '') if notice else body
