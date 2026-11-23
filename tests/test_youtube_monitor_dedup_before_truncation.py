from datetime import datetime, timedelta, timezone
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


def _install_stubs():
    if "modules.utils" not in sys.modules:
        modules_utils = types.ModuleType("modules.utils")
        modules_utils.get_app_subdir = lambda subdir_name: os.path.join(os.getcwd(), "temp", "unit-tests", subdir_name)
        sys.modules["modules.utils"] = modules_utils

    if "modules.config_manager" not in sys.modules:
        modules_config_manager = types.ModuleType("modules.config_manager")
        modules_config_manager.load_config = lambda: {}
        sys.modules["modules.config_manager"] = modules_config_manager

    if "modules.task_manager" not in sys.modules:
        modules_task_manager = types.ModuleType("modules.task_manager")
        modules_task_manager.add_task = lambda *args, **kwargs: None
        sys.modules["modules.task_manager"] = modules_task_manager

    if "httplib2" not in sys.modules:
        httplib2_module = types.ModuleType("httplib2")

        class _StubHttpLib2Error(Exception):
            pass

        class _StubHttp:
            def __init__(self, timeout=None, proxy_info=None):
                self.timeout = timeout
                self.proxy_info = proxy_info

        httplib2_module.Http = _StubHttp
        httplib2_module.HttpLib2Error = _StubHttpLib2Error
        httplib2_module.proxy_info_from_url = lambda url, method=None: {"url": url, "method": method}
        sys.modules["httplib2"] = httplib2_module

    if "apscheduler.schedulers.background" not in sys.modules:
        apscheduler_module = types.ModuleType("apscheduler")
        apscheduler_schedulers_module = types.ModuleType("apscheduler.schedulers")
        apscheduler_background_module = types.ModuleType("apscheduler.schedulers.background")

        class _StubBackgroundScheduler:
            def __init__(self, *args, **kwargs):
                self.running = False
                self._jobs = {}

            def add_job(self, func=None, trigger=None, minutes=None, id=None, args=None, replace_existing=False):
                if id is not None:
                    self._jobs[id] = {
                        "func": func,
                        "trigger": trigger,
                        "minutes": minutes,
                        "args": args or [],
                    }

            def get_job(self, job_id):
                return self._jobs.get(job_id)

            def remove_job(self, job_id):
                self._jobs.pop(job_id, None)

            def start(self):
                self.running = True

            def shutdown(self, *args, **kwargs):
                self.running = False

        apscheduler_background_module.BackgroundScheduler = _StubBackgroundScheduler
        sys.modules["apscheduler"] = apscheduler_module
        sys.modules["apscheduler.schedulers"] = apscheduler_schedulers_module
        sys.modules["apscheduler.schedulers.background"] = apscheduler_background_module

    if "googleapiclient.discovery" not in sys.modules:
        googleapiclient_module = types.ModuleType("googleapiclient")
        discovery_module = types.ModuleType("googleapiclient.discovery")
        errors_module = types.ModuleType("googleapiclient.errors")
        http_module = types.ModuleType("googleapiclient.http")

        class _StubHttpError(Exception):
            pass

        discovery_module.build = lambda *args, **kwargs: object()
        errors_module.HttpError = _StubHttpError
        http_module.DEFAULT_HTTP_TIMEOUT_SEC = 120

        sys.modules["googleapiclient"] = googleapiclient_module
        sys.modules["googleapiclient.discovery"] = discovery_module
        sys.modules["googleapiclient.errors"] = errors_module
        sys.modules["googleapiclient.http"] = http_module


try:
    from modules.youtube_monitor import YouTubeMonitor
except ModuleNotFoundError:
    _install_stubs()
    sys.modules.pop("modules.youtube_monitor", None)
    from modules.youtube_monitor import YouTubeMonitor


def _make_raw_video(vid):
    """构造一个能通过 _filter_videos 最小校验的原始 YouTube API 视频字典。"""
    return {
        "id": vid,
        "snippet": {
            "title": f"Video {vid}",
            "channelTitle": "TestChannel",
            "channelId": "UCtest",
            "publishedAt": "2024-01-01T00:00:00Z",
            "liveBroadcastContent": "none",
        },
        "contentDetails": {"duration": "PT5M"},
        "statistics": {"viewCount": "100", "likeCount": "10", "commentCount": "1"},
    }


class _FakeResource:
    def __init__(self, request_kind, calls):
        self.request_kind = request_kind
        self.calls = calls

    def list(self, **params):
        self.calls.append((self.request_kind, params))
        return self.request_kind


class _FakeYouTube:
    def __init__(self):
        self.calls = []

    def search(self):
        return _FakeResource('search.list', self.calls)

    def videos(self):
        return _FakeResource('videos.list', self.calls)

    def channels(self):
        return _FakeResource('channels.list', self.calls)

    def playlistItems(self):
        return _FakeResource('playlistItems.list', self.calls)


class SearchKeywordQueryTests(unittest.TestCase):
    def _new_monitor(self):
        monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        monitor.youtube = _FakeYouTube()
        monitor._last_fetch_had_errors = False
        monitor._last_fetch_quota_skipped = False
        monitor._quota_budget_remaining = lambda: 95
        return monitor

    @staticmethod
    def _config(keywords):
        return {
            'keywords': keywords,
            'max_results': 5,
            'region_code': 'US',
            'order_by': 'date',
            'video_types': 'video,short,live',
            'category_id': '0',
        }

    def test_multiple_keywords_use_one_native_or_search_request(self):
        monitor = self._new_monitor()
        quota_attempts = []
        monitor._try_consume_quota = lambda amount, config_id=None: (
            quota_attempts.append((amount, config_id)) or True
        )

        def execute(request, _operation, quota_consumer=None, **_kwargs):
            if quota_consumer is not None:
                self.assertTrue(quota_consumer())
            if request == 'search.list':
                return {'items': [
                    {'id': {'videoId': 'v1'}},
                    {'id': {'videoId': 'v2'}},
                ]}
            return {'items': [_make_raw_video('v1'), _make_raw_video('v2')]}

        monitor._execute_with_retry = execute

        videos = monitor._fetch_search_videos(
            self._config('alpha beta,gamma'),
            '2024-01-01T00:00:00Z',
        )

        search_calls = [params for kind, params in monitor.youtube.calls if kind == 'search.list']
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(search_calls[0]['q'], 'alpha|beta|gamma')
        self.assertEqual(quota_attempts, [(1, None)])
        self.assertEqual([video['id'] for video in videos], ['v1', 'v2'])
        self.assertFalse(monitor._last_fetch_had_errors)

    def test_failed_search_marks_run_incomplete(self):
        monitor = self._new_monitor()

        def execute(_request, _operation, **_kwargs):
            raise RuntimeError('quota unavailable')

        monitor._execute_with_retry = execute

        videos = monitor._fetch_search_videos(
            self._config('alpha beta'),
            '2024-01-01T00:00:00Z',
        )

        self.assertEqual(videos, [])
        self.assertTrue(monitor._last_fetch_had_errors)
        search_calls = [params for kind, params in monitor.youtube.calls if kind == 'search.list']
        self.assertEqual(len(search_calls), 1)

    def test_invalid_video_details_response_marks_run_incomplete(self):
        monitor = self._new_monitor()

        def execute(request, _operation, **_kwargs):
            if request == 'search.list':
                return {'items': [{'id': {'videoId': 'v1'}}]}
            return {}

        monitor._execute_with_retry = execute

        videos = monitor._fetch_search_videos(
            self._config('alpha'),
            '2024-01-01T00:00:00Z',
        )

        self.assertEqual(videos, [])
        self.assertTrue(monitor._last_fetch_had_errors)


class HistoricalPlaylistDetailsTests(unittest.TestCase):
    def _fetch(self, count, *, missing=(), fail_second_batch=False):
        monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        monitor._last_fetch_had_errors = False
        video_ids = [f'v{i:03d}' for i in range(count)]
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)

        def timestamp(i):
            return (start + timedelta(minutes=i)).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Newest playlist IDs arrive first, so the oldest results are in the last batch.
        playlist = [
            {'snippet': {'publishedAt': timestamp(i), 'resourceId': {'videoId': vid}}}
            for i, vid in reversed(list(enumerate(video_ids)))
        ]
        playlist.insert(0, {'snippet': {
            'publishedAt': timestamp(count), 'resourceId': {'videoId': 'too-new'},
        }})
        playlist.append({'snippet': {
            'publishedAt': timestamp(-1), 'resourceId': {'videoId': 'too-old'},
        }})
        details = {vid: _make_raw_video(vid) for vid in video_ids if vid not in missing}
        for i, vid in enumerate(video_ids):
            if vid in details:
                details[vid]['snippet']['publishedAt'] = timestamp(i)

        youtube = _FakeYouTube()
        monitor.youtube = youtube
        batches = []

        def execute(request, _operation):
            params = youtube.calls[-1][1]
            if request == 'channels.list':
                return {'items': [{'contentDetails': {'relatedPlaylists': {'uploads': 'UUtest'}}}]}
            if request == 'playlistItems.list':
                offset = int(params.get('pageToken', 0))
                end = offset + params['maxResults']
                items = playlist[offset:end]
                if end < len(playlist):
                    return {'items': items, 'nextPageToken': str(end)}
                return {'items': items}
            self.assertEqual(request, 'videos.list')
            ids = params['id'].split(',')
            batches.append(ids)
            if len(ids) > 50:
                raise RuntimeError('invalidFilters: at most 50 IDs')
            if fail_second_batch and len(batches) == 2:
                raise RuntimeError('video details temporarily unavailable')
            self.assertTrue(set(ids).issubset(video_ids), 'date filtering must precede details lookup')
            return {'items': [details[vid] for vid in ids if vid in details]}

        with patch.object(monitor, '_execute_with_retry', side_effect=execute):
            videos = monitor._fetch_channel_videos(
                {'channel_ids': 'UCtest', 'channel_mode': 'historical', 'max_results': 3},
                timestamp(0), timestamp(count),
            )
        return videos, batches, monitor._last_fetch_had_errors

    def test_large_history_is_fetched_and_sorted_globally(self):
        for count, expected_sizes in [(51, [50, 1]), (474, [50] * 9 + [24])]:
            with self.subTest(count=count):
                expected_ids = [f'v{i:03d}' for i in range(count)]
                videos, batches, had_error = self._fetch(count)
                self.assertFalse(had_error)
                self.assertEqual([len(batch) for batch in batches], expected_sizes)
                self.assertEqual([vid for batch in batches for vid in batch], expected_ids[::-1])
                self.assertEqual([video['id'] for video in videos], expected_ids)

    def test_second_batch_error_marks_fetch_incomplete_without_partial_results(self):
        videos, batches, had_error = self._fetch(51, fail_second_batch=True)
        self.assertEqual([len(batch) for batch in batches], [50, 1])
        self.assertTrue(had_error)
        self.assertEqual(videos, [])

    def test_fifty_ids_use_one_request_and_missing_items_are_not_errors(self):
        videos, batches, had_error = self._fetch(50, missing={'v007', 'v048'})
        self.assertEqual([len(batch) for batch in batches], [50])
        self.assertFalse(had_error)
        self.assertEqual([video['id'] for video in videos], [
            f'v{i:03d}' for i in range(50) if i not in {7, 48}
        ])


class DedupBeforeTruncationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.get_app_subdir_patcher = patch(
            "modules.youtube_monitor.get_app_subdir",
            side_effect=self._get_app_subdir,
        )
        self.init_api_patcher = patch.object(
            YouTubeMonitor,
            "_init_youtube_api",
            return_value=(False, "missing"),
        )
        self.get_app_subdir_patcher.start()
        self.init_api_patcher.start()
        self.monitor = YouTubeMonitor()
        # 让 run_monitor 越过「YouTube API 未初始化」早退检查
        self.monitor.youtube = object()

    def tearDown(self):
        try:
            scheduler = getattr(self.monitor, "scheduler", None)
            if scheduler and getattr(scheduler, "running", False):
                scheduler.shutdown(wait=False)
        finally:
            self.get_app_subdir_patcher.stop()
            self.init_api_patcher.stop()
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_app_subdir(self, subdir_name):
        path = os.path.join(self.tmpdir, subdir_name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_max_results_one_still_processes_new_video_when_first_is_processed(self):
        """回归测试（Issue #125）：候选第一条已处理、后续未处理且 max_results=1 时，
        应先去重再按 max_results 截断——即跳过已处理的 v1，处理 1 条新视频（v2），
        而不是在去重之前就把候选截到 1 条导致整轮 0 新视频。"""
        config_id = self.monitor.create_monitor_config({
            "name": "dedup-order",
            "keywords": "test",
            "max_results": 1,
            "auto_add_to_tasks": False,
            "schedule_type": "manual",
        })

        # 预先把候选第一条标记为已处理（使用与 _filter_videos 输出一致的结构）
        v1_info = {
            "id": "v1", "title": "V1", "channel_title": "TestChannel",
            "channel_id": "UCtest", "published_at": "2024-01-01T00:00:00Z",
            "duration": "PT5M", "view_count": 100, "like_count": 10,
            "comment_count": 1, "video_type": "video",
        }
        self.monitor._save_video_history(v1_info, config_id, auto_add_to_tasks=False)
        self.assertTrue(self.monitor._is_video_processed("v1", config_id))

        candidates = [_make_raw_video(f"v{i}") for i in range(1, 11)]  # v1..v10

        saved_ids = []
        real_save = self.monitor._save_video_history

        def spy(video, cid, auto_add_to_tasks=False):
            saved_ids.append(video["id"])
            return real_save(video, cid, auto_add_to_tasks=auto_add_to_tasks)

        with patch.object(YouTubeMonitor, "_fetch_trending_videos", return_value=candidates), \
                patch.object(YouTubeMonitor, "_meets_criteria", return_value=True), \
                patch.object(YouTubeMonitor, "_detect_video_type", return_value="video"), \
                patch.object(YouTubeMonitor, "_save_video_history", side_effect=spy):
            ok, msg = self.monitor.run_monitor(config_id)

        self.assertTrue(ok, f"run_monitor 应成功: {msg}")
        # 去重后截断：仅处理 1 条新视频（v2），v1 跳过，v3..v10 不参与本轮回环
        self.assertEqual(saved_ids, ["v2"], f"应只处理 v2，实际: {saved_ids}")
        self.assertTrue(self.monitor._is_video_processed("v2", config_id))
        self.assertFalse(self.monitor._is_video_processed("v3", config_id))


if __name__ == "__main__":
    unittest.main()
