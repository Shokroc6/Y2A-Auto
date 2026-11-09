"""Overseas UPOS lines (txa / alia) are probed and used like the bundled ones."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.bili_sdk.utils.AsyncEvent import AsyncEvent
from modules.bili_sdk.video_uploader import (
    LINES_INFO,
    Lines,
    VideoUploader,
    VideoUploaderEvents,
    VideoUploaderPage,
    _choose_line,
    _probe,
)


class _Resp:
    def __init__(self, payload):
        self.code = 200
        self._payload = payload

    def json(self):
        return self._payload


class OverseasLineDataTests(unittest.TestCase):
    def test_txa_and_alia_are_bundled_like_existing_lines(self):
        for name in ("txa", "alia"):
            info = LINES_INFO[name]
            self.assertEqual(info["os"], "upos")
            self.assertEqual(info["upcdn"], name)
            self.assertEqual(info["probe_version"], LINES_INFO["bda2"]["probe_version"])
            self.assertEqual(info["probe_url"], f"//upos-cs-upcdn{name}.bilivideo.com/OK")
            self.assertIn(f"upcdn={name}", info["query"])

    def test_explicit_line_selects_bundled_info(self):
        self.assertEqual(asyncio.run(_choose_line(Lines.TXA)), LINES_INFO["txa"])
        self.assertEqual(asyncio.run(_choose_line(Lines.ALIA)), LINES_INFO["alia"])


class ProbeTests(unittest.TestCase):
    def _probe_with(self, costs):
        """costs: upcdn -> seconds, or None for a line that fails (DNS/TLS)."""
        clock = [0.0]

        async def request(method, url, data):
            name = url.split("upcdn", 1)[1].split(".", 1)[0]
            cost = costs.get(name)
            if isinstance(cost, list):
                cost = cost.pop(0) if cost else None
            if cost is None:
                raise OSError("could not resolve host")
            clock[0] += cost

        client = Mock(request=AsyncMock(side_effect=request))
        with patch("modules.bili_sdk.video_uploader.get_client", return_value=client), \
             patch("modules.bili_sdk.video_uploader.time.perf_counter", side_effect=lambda: clock[0]):
            line = asyncio.run(_probe())
        self.calls = [c.kwargs["url"] for c in client.request.await_args_list]
        return line, set(self.calls)

    def test_probe_tries_overseas_lines_and_picks_fastest(self):
        line, probed = self._probe_with({"bda2": 2.0, "txa": 0.4, "alia": 0.3})
        self.assertIn("https://upos-cs-upcdntxa.bilivideo.com/OK", probed)
        self.assertIn("https://upos-cs-upcdnalia.bilivideo.com/OK", probed)
        self.assertEqual(line["upcdn"], "alia")

    def test_domestic_fastest_line_still_wins(self):
        line, _ = self._probe_with(
            {"bda2": 0.1, "qn": 0.2, "ws": 0.3, "bldsa": 0.4, "txa": 1.5, "alia": 1.6})
        self.assertEqual(line["upcdn"], "bda2")


    def test_probe_uses_each_lines_best_of_three_rounds(self):
        # One slow 0.1 MB sample on txa must not hand the upload to bda2.
        line, _ = self._probe_with(
            {"bda2": [1.8, 1.8, 1.8], "txa": [6.18, 0.4, 0.45], "alia": [9.0, 9.0, 9.0]})
        self.assertEqual(line["upcdn"], "txa")
        self.assertEqual(self.calls.count("https://upos-cs-upcdntxa.bilivideo.com/OK"), 3)

    def test_probe_skips_failed_line_in_later_rounds(self):
        line, _ = self._probe_with({"bda2": [0.5, 0.5, 0.5], "alia": [None]})
        self.assertEqual(line["upcdn"], "bda2")
        self.assertEqual(self.calls.count("https://upos-cs-upcdnalia.bilivideo.com/OK"), 1)
        self.assertEqual(self.calls.count("https://upos-cs-upcdnqn.bilivideo.com/OK"), 1)


class PreuploadLineTests(unittest.TestCase):
    def test_preupload_requests_txa_and_keeps_its_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.mp4")
            with open(path, "wb") as f:
                f.write(b"x" * 16)
            page = VideoUploaderPage(path, "t")
            uploader = object.__new__(VideoUploader)
            AsyncEvent.__init__(uploader)
            uploader.line = LINES_INFO["txa"]
            uploader.credential = Mock(get_buvid_cookies=AsyncMock(return_value={}))
            pre = {"OK": 1, "endpoint": "//upos-cs-upcdntxa.bilivideo.com",
                   "upos_uri": "upos://b/v.mp4", "chunk_size": 8, "auth": "a",
                   "biz_id": 1, "threads": 3}
            client = Mock(request=AsyncMock(side_effect=[
                _Resp(pre), _Resp({"OK": 1, "upload_id": "u"})]))
            with patch("modules.bili_sdk.video_uploader.get_client", return_value=client):
                result = asyncio.run(uploader._preupload(page))
        params = client.request.await_args_list[0].kwargs["params"]
        self.assertEqual((params["r"], params["upcdn"]), ("upos", "txa"))
        self.assertEqual(result["endpoint"], "//upos-cs-upcdntxa.bilivideo.com")
        self.assertTrue(client.request.await_args_list[1].kwargs["url"].startswith(
            "https://upos-cs-upcdntxa.bilivideo.com/"))

    def test_pre_page_event_reports_endpoint_without_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.mp4")
            with open(path, "wb") as f:
                f.write(b"x" * 8)
            page = VideoUploaderPage(path, "t")
            uploader = object.__new__(VideoUploader)
            AsyncEvent.__init__(uploader)
            uploader.line = LINES_INFO["alia"]
            events = []
            uploader.add_event_listener(VideoUploaderEvents.PRE_PAGE.value, events.append)
            pre = {"endpoint": "//upos-cs-upcdnalia.bilivideo.com", "chunk_size": 8,
                   "upload_id": "u", "auth": "secret-auth", "threads": 1}
            uploader._preupload = AsyncMock(return_value=pre)
            uploader._upload_chunk = AsyncMock(return_value={"ok": True})
            uploader._complete_page = AsyncMock(return_value={})
            asyncio.run(uploader._upload_page(page))
        self.assertEqual(events[0]["endpoint"], "//upos-cs-upcdnalia.bilivideo.com")
        self.assertNotIn("secret-auth", repr({k: v for k, v in events[0].items() if k != "page"}))


class UploadLineLogTests(unittest.TestCase):
    def test_upload_video_logs_selected_line_and_host(self):
        from modules.bilibili_uploader import BilibiliUploader

        logger = Mock()

        class FakePage:
            def __init__(self, path, title):
                self.path = path

        class FakeUploader(AsyncEvent):
            def __init__(self, pages, meta, credential, cover, **kwargs):
                super().__init__()
                self.pages = pages
                self.line = None
                self.submit_api_used = "web"
                self.submit_attempts = []

            async def start(self):
                self.line = LINES_INFO["txa"]
                self.dispatch(VideoUploaderEvents.PRE_PAGE.value, {
                    "page": self.pages[0], "endpoint": "//upos-cs-upcdntxa.bilivideo.com"})
                return {"bvid": "BV1test", "aid": 1}

        with tempfile.TemporaryDirectory() as tmp:
            video, cover = os.path.join(tmp, "v.mp4"), os.path.join(tmp, "c.jpg")
            for p in (video, cover):
                with open(p, "wb") as f:
                    f.write(b"t")
            with patch("modules.bilibili_uploader.setup_task_logger", return_value=logger), \
                 patch("modules.bilibili_uploader.configure_bilibili_runtime"), \
                 patch("modules.bilibili_uploader.load_credential_from_file", return_value=object()), \
                 patch("modules.bilibili_uploader.validate_credential_remote", return_value=(True, "ok")), \
                 patch("modules.bilibili_uploader.ensure_credential_buvid", return_value=False), \
                 patch("modules.bilibili_uploader.video_uploader.VideoMeta", return_value=object()), \
                 patch("modules.bilibili_uploader.video_uploader.VideoUploaderPage", FakePage), \
                 patch("modules.bilibili_uploader.video_uploader.VideoUploader", FakeUploader):
                ok, _ = BilibiliUploader("cookies.json").upload_video(
                    video, cover, "标题", "简介", ["标签"], 21, task_id="line-task")
        self.assertTrue(ok)
        logs = [c.args[0] for c in logger.info.call_args_list]
        self.assertTrue(any("txa" in m and "upos-cs-upcdntxa.bilivideo.com" in m for m in logs), logs)


if __name__ == "__main__":
    unittest.main()
