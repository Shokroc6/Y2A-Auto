"""Description budgets include metadata; never truncate the notice itself."""
from datetime import datetime
import re
import unicodedata


def truncate_description_body(text, budget):
    text = str(text or '')
    if len(text) <= budget:
        return text
    if budget <= 0:
        return ''
    cut = budget - 1  # Unicode ellipsis is part of the platform budget.
    for match in re.finditer(r'https?://\S+', text):
        if match.start() < cut < match.end():
            cut = match.start()
            break
    # Prefer a nearby line boundary, without throwing away most of the budget.
    boundary = text.rfind('\n', 0, cut + 1)
    if boundary >= cut * 0.8:
        cut = boundary
    # Do not cut before a combining mark, variation selector or joined emoji.
    while cut > 0 and cut < len(text) and (
        unicodedata.combining(text[cut]) or text[cut] in '\ufe0e\ufe0f\u200d'
        or text[cut - 1] == '\u200d'
        or 0x1F3FB <= ord(text[cut]) <= 0x1F3FF
    ):
        cut -= 1
    return text[:cut].rstrip() + '…'


def budget_description_body(body, budget, original_body=''):
    # Recognize only the exact source suffix produced by metadata translation.
    source = str(original_body or '')
    suffix = '\n\n' + source
    if source and body.endswith(suffix) and len(body) > budget:
        target = body[:-len(suffix)]
        usable = max(0, budget - 2)
        target_budget = min(len(target), max(usable // 2, usable - len(source)))
        return (truncate_description_body(target, target_budget) + '\n\n'
                + truncate_description_body(source, usable - target_budget)).strip()
    return truncate_description_body(body, budget)


def build_repost_description(base_desc, original_url='', original_uploader='',
                             original_upload_date='', original_title='',
                             append_repost_notice=True, max_len=1000,
                             copyright_type='repost', original_body=''):
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
    limit = int(max_len)
    if len(notice) > limit:
        raise ValueError(f'来源声明自身共{len(notice)}字符，超过{limit}字符限制，请编辑元数据后重试')
    if not notice:
        return budget_description_body(body, limit, original_body)
    available = limit - len(notice) - 2
    body = budget_description_body(body, max(0, available), original_body)
    return notice + ('\n\n' + body if body else '')
