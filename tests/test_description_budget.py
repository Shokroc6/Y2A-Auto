import pytest
from modules.repost_description import build_repost_description


@pytest.mark.parametrize('limit', [1000, 2000])
def test_complete_notice_has_priority_over_long_body(limit):
    fields = dict(original_title='Title', original_uploader='Author', original_upload_date='20261008', original_url='https://example.org/video')
    notice = build_repost_description('', **fields)
    result = build_repost_description('English\n\n' + '中文' * 1500, max_len=limit, **fields)
    assert len(result) <= limit
    assert result.startswith(notice + '\n\n')
    assert len(notice.splitlines()) == 4
    assert result.endswith('…')


def test_never_emits_half_url_or_combining_character():
    result = build_repost_description('Intro\nhttps://example.org/' + 'x' * 100, max_len=30, append_repost_notice=False)
    assert result == 'Intro…'
    result = build_repost_description('a\u0301' * 30, max_len=20, append_repost_notice=False)
    assert not result[:-1].endswith('a')


def test_notice_itself_over_budget_is_explicit_error():
    with pytest.raises(ValueError, match='声明.*超过'):
        build_repost_description('Body', original_title='x' * 100, max_len=20)


def test_bilingual_budget_preserves_both_languages():
    source = 'English sentence. ' * 150
    target = '中文翻译。' * 300
    result = build_repost_description(target + '\n\n' + source, original_body=source, original_title='Title', max_len=1000)
    assert len(result) <= 1000
    assert '中文翻译' in result and 'English sentence' in result


def test_exact_budget_and_disabled_notice_preserve_text():
    body = 'https://example.org/a\nhttps://example.org/a'
    assert build_repost_description(body, max_len=len(body), append_repost_notice=False) == body
