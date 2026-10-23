"""Exercise provider contracts and durable costs with offline HTTP responses."""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bootstrap import TEST_TEMP

import aiohttp
from acg_companion.core import Settings
from acg_companion.store import Store
from acg_companion.media import Media, image_endpoint

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")
NOW = 1792735200


class Response:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        self.status = status
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def iter_chunked(self, size):
        for start in range(0, len(self.payload), size):
            yield self.payload[start:start + size]


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class ImageApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.cfg = Settings({"image_api_url": "https://images.example/v1", "image_api_key": "offline-secret",
                             "image_model": "low-cost-model", "image_enabled": True})
        self.client = Client()
        self.sources = SimpleNamespace(client=AsyncMock(side_effect=lambda: self.client))
        self.media = Media(self.cfg, self.store, self.sources, Path(self.temp.name) / "images")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def get(self, ts=NOW, **kwargs):
        return await self.media.get(ts, "offline-test", force_kind="image", strict=True, **kwargs)

    def test_endpoint_types_and_base_urls(self):
        self.assertEqual(image_endpoint({})[0], "modelscope")
        self.assertEqual(image_endpoint({"image_api_url": "https://images.example"}),
                         ("openai", "https://images.example/v1/images/generations"))
        self.assertEqual(image_endpoint({"image_api_url": "https://images.example/draw?p={prompt}"})[0], "url")
        for url in ("http://images.example", "https://user:pass@images.example", "https://images.example/#x", "https://images.example:bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                image_endpoint({"image_api_url": url})

    async def test_openai_base64_single_submission_and_one_image(self):
        self.client.responses = [Response({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})]
        item = await self.get(prompt="动漫风格的雨后街角", caption="画了一张雨后街角")
        self.assertEqual(Path(item.media_file).read_bytes(), PNG)
        self.assertEqual(item.description, "画了一张雨后街角")
        method, url, opts = self.client.calls[0]
        self.assertEqual((method, url), ("POST", "https://images.example/v1/images/generations"))
        self.assertEqual(opts["json"], {"model": "low-cost-model", "prompt": "动漫风格的雨后街角", "n": 1})
        self.assertFalse(opts["allow_redirects"])

    async def test_url_output_download_does_not_forward_api_key(self):
        self.client.responses = [Response({"data": [{"url": "https://cdn.example/result.png"}]}), Response(PNG)]
        item = await self.get()
        self.assertEqual(Path(item.media_file).read_bytes(), PNG)
        self.assertNotIn("headers", self.client.calls[1][2])

    async def test_seedream_ark_base_and_single_image_request(self):
        self.cfg.raw.update(image_api_url="https://ark.cn-beijing.volces.com/api/v3", image_model="account-seedream-model")
        self.client.responses = [Response({"data": [{"url": "https://cdn.example/result.png"}]}), Response(PNG)]
        item = await self.get(prompt="动漫风格的雨后街角")
        self.assertTrue(Path(item.media_file).is_file())
        method, url, opts = self.client.calls[0]
        self.assertEqual((method, url), ("POST", "https://ark.cn-beijing.volces.com/api/v3/images/generations"))
        self.assertEqual(opts["json"], {"model": "account-seedream-model", "prompt": "动漫风格的雨后街角",
                                      "size": "2K", "response_format": "url"})
        self.assertNotIn("n", opts["json"])
        self.assertNotIn("headers", self.client.calls[1][2])

    def test_seedream_relay_requires_explicit_protocol_and_unknown_protocol_rejected(self):
        self.assertEqual(image_endpoint({"image_api_url": "https://relay.example/api/v3", "image_api_type": "seedream"}),
                         ("seedream", "https://relay.example/api/v3/images/generations"))
        self.assertEqual(image_endpoint({"image_api_url": "https://relay.example/v1"})[0], "openai")
        with self.assertRaises(ValueError):
            image_endpoint({"image_api_type": "unknown"})

    async def test_direct_url_encodes_prompt_and_needs_no_key(self):
        self.cfg.raw.update(image_api_url="https://images.example/draw?p={prompt}", image_api_key="")
        self.client.responses = [Response(PNG)]
        self.assertTrue(self.media.enabled())
        item = await self.get(prompt="猫 & dog/?")
        self.assertTrue(Path(item.media_file).exists())
        self.assertEqual(self.client.calls[0][1], "https://images.example/draw?p=%E7%8C%AB%20%26%20dog%2F%3F")
        self.assertEqual(self.client.calls[0][2]["headers"], {})

    async def test_modelscope_async_poll_and_shared_download(self):
        self.cfg.raw.update(image_api_url="", image_model="Tongyi-MAI/Z-Image-Turbo")
        self.client.responses = [Response({"task_id": "task-1"}),
                                 Response({"task_status": "SUCCEED", "output_images": ["https://cdn.example/a.png"]}), Response(PNG)]
        with patch("acg_companion.media.asyncio.sleep", new_callable=AsyncMock):
            item = await self.get()
        self.assertTrue(Path(item.media_file).is_file())
        self.assertEqual(self.client.calls[0][2]["headers"]["X-ModelScope-Async-Mode"], "true")
        self.assertEqual(self.client.calls[1][1], "https://api-inference.modelscope.cn/v1/tasks/task-1")
        self.assertNotIn("headers", self.client.calls[2][2])

    async def test_invalid_html_is_rejected_and_not_resubmitted(self):
        self.cfg.raw.update(image_api_url="https://images.example/draw?p={prompt}", image_api_key="")
        self.client.responses = [Response(b"<html>login required</html>")]
        with self.assertRaisesRegex(ValueError, "未返回"):
            await self.get()
        self.assertFalse(list(self.media.directory.glob("*.png")))
        with self.assertRaisesRegex(ValueError, "生成间隔"):
            await self.get(NOW + 10)
        self.assertEqual(len(self.client.calls), 1)

    async def test_provider_auth_error_is_readable_without_key_or_raw_body(self):
        self.client.responses = [Response(b"offline-secret internal service error", status=401)]
        with self.assertRaisesRegex(ValueError, "密钥无效") as caught:
            await self.get()
        self.assertNotIn("offline-secret", str(caught.exception))

    async def test_cache_reuse_and_explicit_manual_daily_cost_limit(self):
        result = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        self.client.responses = [Response(result), Response(result)]
        first = await self.get(manual=True)
        cached = await self.get(NOW + 1, manual=True, prompt="changed prompt")
        self.assertEqual(first.media_file, cached.media_file)
        self.assertEqual(len(self.client.calls), 1)
        with self.assertRaisesRegex(ValueError, "60 秒"):
            await self.get(NOW + 10, manual=True, fresh=True)
        await self.get(NOW + 61, manual=True, fresh=True)
        with self.assertRaisesRegex(ValueError, "2 次"):
            await self.get(NOW + 122, manual=True, fresh=True)
        self.assertEqual(len(self.client.calls), 2)
        cached_at_limit = await self.get(NOW + 123, manual=True)
        self.assertTrue(Path(cached_at_limit.media_file).is_file())
        self.assertEqual(len(self.client.calls), 2)

    async def test_raising_test_limit_keeps_usage_and_allows_more_than_two(self):
        result = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        self.client.responses = [Response(result) for _ in range(3)]
        await self.get(manual=True)
        await self.get(NOW + 61, manual=True, fresh=True)
        self.assertEqual(self.media.image_test_budget(NOW + 62)["remaining"], 0)
        self.cfg = Settings({**self.cfg.raw, "image_test_daily_limit": 20, "image_test_interval_seconds": 15})
        self.media.cfg = self.cfg
        self.assertEqual(self.media.image_test_budget(NOW + 62)["used"], 2)
        with self.assertRaisesRegex(ValueError, "15 秒"):
            await self.get(NOW + 70, manual=True, fresh=True)
        await self.get(NOW + 76, manual=True, fresh=True)
        self.assertEqual(self.media.image_test_budget(NOW + 76)["remaining"], 17)
        self.assertEqual(len(self.client.calls), 3)

    async def test_manual_budget_resets_next_day_without_deleting_history(self):
        result = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        self.client.responses = [Response(result), Response(result), Response(result)]
        await self.get(manual=True)
        await self.get(NOW + 61, manual=True, fresh=True)
        self.assertEqual(self.media.image_test_budget(NOW + 86400)["used"], 0)
        await self.get(NOW + 86400, manual=True, fresh=True)
        self.assertEqual(self.media.image_test_budget(NOW + 86400)["used"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 3)

    async def test_task_cancelled_keeps_uncertain_reservation(self):
        with patch.object(self.media, "generate_image", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.get()
        self.assertEqual(self.store.media_cached(NOW, 86400)["status"], "uncertain")

    async def test_image_url_json_works_in_direct_mode(self):
        self.cfg.raw.update(image_api_url="https://images.example/draw?p={prompt}", image_api_key="")
        self.client.responses = [Response({"url": "https://cdn.example/a.png"}), Response(PNG)]
        self.assertTrue(Path((await self.get()).media_file).exists())
