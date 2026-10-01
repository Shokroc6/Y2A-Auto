import copy


def test_moderation_checks_composed_source_as_well_as_translation():
    import sys
    from types import SimpleNamespace
    task = {'video_title_original': 'Source', 'video_title_translated': '译文', 'description_original': 'Original paragraph', 'description_translated': '摘要'}
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {}
    moderator = MagicMock()
    moderator.moderate_text.return_value = {'pass': True}
    fake = SimpleNamespace(AlibabaCloudModerator=MagicMock(return_value=moderator))
    with patch.dict(sys.modules, {'modules.content_moderator': fake}), patch.object(tm, 'get_task', return_value=task), patch.object(tm, 'update_task'):
        processor._moderate_content('offline', logging.getLogger('offline'))
    assert moderator.moderate_text.call_args_list[0].args[0] == '译文 | Source'
    assert moderator.moderate_text.call_args_list[1].args[0] == '摘要\n\nOriginal paragraph'


def test_acfun_final_description_preserves_paragraphs_and_repost_budget():
    from modules.acfun_uploader import build_upload_description
    text = '摘要\n\nOriginal\n\n00:12 Chapter\nhttps://example.test/?a=1&b=2'
    result = build_upload_description(text, original_url='https://youtube.com/watch?v=offline')
    assert result == '本视频转载自YouTube\n\n' + text
    result = build_upload_description(text * 100, original_url='source')
    assert len(result) == 1000
    assert result.startswith('本视频转载自YouTube\n\n摘要')
    assert result.endswith('...')
import pytest
import logging
from unittest.mock import patch, MagicMock


@pytest.mark.parametrize('metadata,expected_url', [
    ({}, 'https://youtube.com/watch?v=offline'),
    ({'webpage_url': ''}, 'https://youtube.com/watch?v=offline'),
    ({'webpage_url': None}, 'https://youtube.com/watch?v=offline'),
    ({'webpage_url': '   '}, 'https://youtube.com/watch?v=offline'),
    ({'webpage_url': 'https://youtube.com/watch?v=canonical'}, 'https://youtube.com/watch?v=canonical'),
])
def test_acfun_metadata_url_fallback_reaches_final_repost_parameters(tmp_path, metadata, expected_url):
    import json
    from modules import acfun_uploader as au

    fixture = tmp_path / 'synthetic.bin'
    fixture.write_bytes(b'offline boundary fixture')
    metadata_path = tmp_path / 'metadata.json'
    metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
    task = {
        'id': 'offline', 'upload_target': 'acfun',
        'video_path_local': str(fixture), 'cover_path_local': str(fixture),
        'metadata_json_path_local': str(metadata_path),
        'youtube_url': 'https://youtube.com/watch?v=offline',
        'video_title_original': 'Source', 'description_original': 'Description',
        'selected_partition_id_acfun': '1',
    }
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {}
    # Run the real upload_video wrapper; stop at login/submission I/O boundaries.
    uploader = au.AcfunUploader.__new__(au.AcfunUploader)
    with patch.object(tm, 'get_task', return_value=task), \
         patch.object(tm, 'update_task'), \
         patch.object(tm, 'resolve_cookie_file_path', return_value=str(fixture)), \
         patch.object(tm, 'validate_cookies', return_value=(True, '')), \
         patch.object(processor, '_recover_cover_path', return_value=str(fixture)), \
         patch.object(au, 'AcfunUploader', return_value=uploader), \
         patch.object(au, 'setup_task_logger', return_value=logging.getLogger('offline')), \
         patch.object(uploader, 'log'), \
         patch.object(uploader, 'login', return_value=True), \
         patch.object(uploader, 'create_douga', return_value=(False, 'offline: no upload')) as submit:
        processor._do_upload_to_acfun('offline', logging.getLogger('offline'), subtitle_prepared=True)
    submit.assert_called_once()
    assert submit.call_args.kwargs['creation_type'] == 1
    assert submit.call_args.kwargs['original_url'] == expected_url
    assert submit.call_args.kwargs['desc'].startswith('本视频转载自YouTube\n\n')


@pytest.mark.parametrize('platform', ['acfun', 'bilibili'])
def test_task_upload_boundary_receives_bilingual_metadata(tmp_path, platform):
    import importlib
    module = importlib.import_module('modules.' + platform + '_uploader')
    filename = tmp_path / 'synthetic.bin'
    filename.write_bytes(b'offline boundary fixture')
    task = {'id': 'offline', 'upload_target': platform, 'video_path_local': str(filename), 'cover_path_local': str(filename), 'youtube_url': 'https://youtube.com/watch?v=offline', 'video_title_original': 'Source', 'video_title_translated': '译文', 'description_original': 'First\n\n00:12 Chapter\nhttps://example.test', 'description_translated': '摘要', 'selected_partition_id_acfun': '1', 'selected_partition_id_bilibili': '1'}
    processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
    processor.config = {}
    uploader = MagicMock()
    uploader.upload_video.return_value = (False, 'offline: no upload')
    cls = 'AcfunUploader' if platform == 'acfun' else 'BilibiliUploader'
    with patch.object(tm, 'get_task', return_value=task), patch.object(tm, 'update_task'), patch.object(tm, 'resolve_cookie_file_path', return_value=str(filename)), patch.object(tm, 'validate_cookies', return_value=(True, '')), patch.object(processor, '_recover_cover_path', return_value=str(filename)), patch.object(module, cls, return_value=uploader), patch('modules.bilibili_zones.collect_valid_tids', return_value={'1'}):
        getattr(processor, '_do_upload_to_' + platform)('offline', logging.getLogger('offline'), subtitle_prepared=True)
    kwargs = uploader.upload_video.call_args.kwargs
    assert kwargs['title'] == '译文 | Source'
    assert '摘要\n\nFirst\n\n00:12 Chapter\nhttps://example.test' in kwargs['description']
    assert kwargs.get('original_url', kwargs.get('youtube_url')) == task['youtube_url']

import pytest
from modules import task_manager as tm


@pytest.mark.parametrize('target,limit', [('acfun', 50), ('bilibili', 80), ('both', 50)])
def test_upload_metadata_composes_without_mutating_translations(target, limit):
    task = {'upload_target': target, 'video_title_original': 'Original', 'video_title_translated': '译文', 'description_original': 'Original paragraph\n\n00:12 Chapter\nhttps://example.test/?a=1&b=2', 'description_translated': '摘要'}
    before = copy.deepcopy(task)
    assert hasattr(tm, '_compose_upload_metadata')
    title, desc = tm._compose_upload_metadata(task, {})
    assert title == '译文 | Original'
    assert desc == '摘要\n\n' + task['description_original']
    assert tm._compose_upload_metadata(task, {}) == (title, desc)
    assert task == before
    assert tm._compose_upload_metadata(task, {'BILINGUAL_TITLE': False, 'BILINGUAL_DESCRIPTION': 'false'}) == ('译文', '摘要')
    task['video_title_translated'] = '译' * limit
    assert tm._compose_upload_metadata(task, {})[0] == '译' * limit
    task['video_title_translated'] = '译' * (limit + 10)
    assert len(tm._compose_upload_metadata(task, {})[0]) == limit
    task['video_title_translated'] = ''
    assert tm._compose_upload_metadata(task, {})[0] == 'Original'
