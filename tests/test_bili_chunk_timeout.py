"""Chunk PUT gets a size-scaled timeout; every other SDK request keeps the global one."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.bili_sdk.utils.AsyncEvent import AsyncEvent
from modules.bili_sdk.utils.network import request_settings
from modules.bili_sdk.video_uploader import (
    VideoUploader,
    VideoUploaderPage,
    _chunk_put_timeout,
)

MiB = 1024 * 1024


class _Resp:
    def __init__(self, code=200, text="MULTIPART_PUT_SUCCESS", payload=None):
        self.code = code
        self._text = text
        self._payload = payload or {}

    def utf8_text(self):
        return self._text

    def json(self):
        return self._payload


class ChunkTimeoutFormulaTests(unittest.TestCase):
    def setUp(self):
        self.legacy = request_settings.get_timeout()
        request_settings.set_timeout(30.0)

    def tearDown(self):
        request_settings.set_timeout(self.legacy)

    def test_small_chunk_keeps_global_timeout(self):
        self.assertEqual(_chunk_put_timeout(1 * MiB), 30.0)

    def test_ten_mib_chunk_allows_64_kib_per_second(self):
        self.assertEqual(_chunk_put_timeout(10 * MiB), 160.0)

    def test_huge_chunk_is_capped(self):
        self.assertEqual(_chunk_put_timeout(100 * MiB), 600.0)

    def test_never_shorter_than_global_timeout(self):
        request_settings.set_timeout(900.0)
        self.assertEqual(_chunk_put_timeout(10 * MiB), 900.0)


class ChunkPutTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.legacy = request_settings.get_timeout()
        request_settings.set_timeout(30.0)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "video.mp4")
        with open(self.path, "wb") as f:
            f.write(b"\0" * (10 * MiB + 5))
        self.page = VideoUploaderPage(self.path, "t")
        self.uploader = object.__new__(VideoUploader)
        AsyncEvent.__init__(self.uploader)
        self.uploader.line = None
        self.preupload = {
            "endpoint": "//upload.example.com",
            "upos_uri": "upos://bucket/video.mp4",
            "upload_id": "u",
            "chunk_size": 10 * MiB,
            "auth": "a",
            "biz_id": 1,
        }

    def tearDown(self):
        request_settings.set_timeout(self.legacy)
        self.tmp.cleanup()

    def _run_chunk(self, client, offset=0, number=0):
        with patch("modules.bili_sdk.video_uploader.get_client", return_value=client), \
             patch("modules.bili_sdk.video_uploader.asyncio.sleep", new=AsyncMock()):
            return asyncio.run(self.uploader._upload_chunk(
                self.page, offset, number, 2, dict(self.preupload)))

    def test_full_chunk_put_uses_scaled_timeout(self):
        client = Mock(request=AsyncMock(return_value=_Resp()))
        self.assertTrue(self._run_chunk(client)["ok"])
        self.assertEqual(client.request.await_args.kwargs["timeout"], 160.0)

    def test_short_last_chunk_uses_global_floor(self):
        client = Mock(request=AsyncMock(return_value=_Resp()))
        self._run_chunk(client, offset=10 * MiB, number=1)
        self.assertEqual(client.request.await_args.kwargs["timeout"], 30.0)

    def test_timeout_retries_bounded_and_each_attempt_scaled(self):
        client = Mock(request=AsyncMock(side_effect=TimeoutError("t")))
        with self.assertRaises(TimeoutError):
            self._run_chunk(client)
        self.assertEqual(client.request.await_count, 6)
        self.assertEqual({c.kwargs["timeout"] for c in client.request.await_args_list}, {160.0})

    def test_complete_page_keeps_global_timeout(self):
        client = Mock(request=AsyncMock(return_value=_Resp(payload={"OK": 1, "key": "/v.mp4"})))
        with patch("modules.bili_sdk.video_uploader.get_client", return_value=client):
            asyncio.run(self.uploader._complete_page(self.page, 2, dict(self.preupload), "u"))
        self.assertNotIn("timeout", client.request.await_args.kwargs)

    def test_preupload_keeps_global_timeout(self):
        self.uploader.line = {"os": "upos", "upcdn": "bda2", "probe_version": 1}
        self.uploader.credential = Mock(get_buvid_cookies=AsyncMock(return_value={}))
        pre = {"OK": 1, **self.preupload}
        client = Mock(request=AsyncMock(side_effect=[
            _Resp(payload=pre), _Resp(payload={"OK": 1, "upload_id": "u"})]))
        with patch("modules.bili_sdk.video_uploader.get_client", return_value=client):
            try:
                asyncio.run(self.uploader._preupload(self.page))
            except Exception:
                pass
        self.assertGreaterEqual(client.request.await_count, 2)
        for call in client.request.await_args_list:
            self.assertNotIn("timeout", call.kwargs)


class CurlClientTimeoutTests(unittest.TestCase):
    def _client(self):
        from modules.bili_sdk.clients.CurlCFFIClient import CurlCFFIClient
        resp = Mock(status_code=200, content=b"", url="https://x")
        resp.headers.multi_items.return_value = []
        resp.cookies.jar = []
        session = Mock(request=AsyncMock(return_value=resp), impersonate="")
        return CurlCFFIClient(session=session), session

    def test_explicit_timeout_is_forwarded(self):
        client, session = self._client()
        asyncio.run(client.request(method="PUT", url="https://x", timeout=160.0))
        self.assertEqual(session.request.await_args.kwargs["timeout"], 160.0)

    def test_default_request_leaves_session_timeout_in_charge(self):
        client, session = self._client()
        asyncio.run(client.request(method="GET", url="https://x"))
        self.assertNotIn("timeout", session.request.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
