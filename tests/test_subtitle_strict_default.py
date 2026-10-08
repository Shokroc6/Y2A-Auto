from test_subtitle_translator_pairing import _make_translator
from modules.subtitle_translator import SubtitleItem


def test_default_does_not_silently_enable_small_partial_fallback():
    translator = _make_translator()
    items = [SubtitleItem(index=i+1, start_time='00:00:00,000', end_time='00:00:01,000', source_text='This sentence requires translation', translated_text='这是合格译文') for i in range(20)]
    items[-1].translated_text = ''
    assert translator.config.allow_partial is False
    assert translator._finalize_residual_untranslated_items(items) is False


def test_explicit_partial_keeps_existing_bounded_tolerance():
    translator = _make_translator(allow_partial=True)
    items = [SubtitleItem(index=i+1, start_time='00:00:00,000', end_time='00:00:01,000', source_text='This sentence requires translation', translated_text='这是合格译文') for i in range(20)]
    items[-1].translated_text = ''
    assert translator._finalize_residual_untranslated_items(items) is True
    assert items[-1].residual_untranslated
