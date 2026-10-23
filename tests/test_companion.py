from __future__ import annotations

import asyncio
import importlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bootstrap import TEST_TEMP

from acg_companion.core import (
    SHANGHAI, Settings, candidate, closes_topic, parse_decision, plan,
)
from acg_companion.store import Store
from acg_companion.sources import bili_items, netease_items, qq_items
from acg_companion.sources import Content, Sources, cover_url
from acg_companion.sharing import components
from acg_companion.media import Media
from acg_companion.main import AcgCompanion
from astrbot.api.message_components import Image, Plain

main = importlib.import_module(AcgCompanion.__module__)
UMO = "test:GroupMessage:123456789"
OTHER = "test:GroupMessage:987654321"
NOW = datetime(2026, 10, 21, 14, tzinfo=SHANGHAI).timestamp()


class RulesTest(unittest.TestCase):
    def test_daily_limit_accepts_fixed_range_and_zero(self):
        for value, bounds in [("2-5", (2, 5)), ("3", (3, 3)), ("0", (0, 0)), (" 0 - 10 ", (0, 10))]:
            cfg = Settings({"daily_limit": value})
            self.assertEqual((cfg.low, cfg.high), bounds)
        self.assertEqual(plan(UMO, NOW, Settings({"daily_limit": "0"})), (0, []))
        for value in ["5-2", "-1", "11", "2-3-4", "two", ""]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings({"daily_limit": value})

    def test_quiet_hours_same_time_disables_quiet_window(self):
        cfg = Settings({"quiet_hours": "00:00-00:00"})
        self.assertTrue(all(not cfg.quiet(NOW + offset * 3600) for offset in range(24)))
        for value in ["99:00-08:00", "02:00", "02:00-08:00-extra"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings({"quiet_hours": value})

    def test_overnight_quiet_boundary(self):
        cfg = Settings({"quiet_hours": "23:30-08:00"})
        for hour, minute, quiet in [(23, 29, False), (23, 30, True), (0, 0, True), (7, 59, True), (8, 0, False)]:
            self.assertEqual(cfg.quiet(datetime(2026, 10, 21, hour, minute, tzinfo=SHANGHAI).timestamp()), quiet)

    def test_restart_stable_distinct_group_plans(self):
        cfg = Settings({"daily_limit": "5"})
        count, slots = plan(UMO, NOW, cfg)
        self.assertEqual(count, 5)
        self.assertEqual(slots, plan(UMO, NOW + 3600, cfg)[1])
        self.assertNotEqual(slots, plan(OTHER, NOW, cfg)[1])
        self.assertTrue(all(not cfg.quiet(due) and due < expiry for _, due, expiry in slots))

    def test_negation_and_questions_are_not_resolution(self):
        for text in ["还没有解决", "没能解决", "问题还没解决", "还是不行", "已经解决了吗？", "你搞定了没", "谁解决了这个？"]:
            with self.subTest(text=text):
                self.assertFalse(closes_topic(text))
        for text in ["问题解决了，谢谢", "我修好了", "已经能用了", "算了不去了", "别催了", "不给"]:
            with self.subTest(text=text):
                self.assertTrue(closes_topic(text))

    def test_local_candidates(self):
        for text, expected in [("新买的手办终于到货了", "purchase"), ("游戏连不上，谁会修", "problem"),
                               ("晚上有人开黑吗", "social"), ("这个角色的立绘真好看", None), ("/联机开黑测试", None)]:
            self.assertEqual(candidate(text), expected)

    def test_strict_llm_output(self):
        self.assertTrue(parse_decision('```json\n{"send":true,"text":"来一局吗"}\n```')["send"])
        for raw in ['{"send":"true"}', '{"send":true,"text":"https://example.com"}',
                    '{"send":true,"text":"[CQ:at,qq=all]"}', '{"send":true,"text":""}', '普通文本']:
            with self.subTest(raw=raw), self.assertRaises((ValueError, json.JSONDecodeError)):
                parse_decision(raw)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-", dir=TEST_TEMP)
        self.path = Path(self.temp.name) / "test.db"
        self.s = Store(self.path)

    def tearDown(self):
        self.s.close()
        self.temp.cleanup()

    def job(self, key="one", umo=UMO):
        self.s.add_job(key, umo, "problem", NOW - 60, NOW + 3600, NOW - 7200, "123", "群友", "游戏连不上怎么办")
        return self.s.due(umo, NOW)[0]

    def test_expired_jobs_never_catch_up(self):
        self.s.add_job("old", UMO, "share", NOW - 10000, NOW - 1, NOW - 20000)
        self.assertEqual(self.s.due(UMO, NOW), [])
        self.assertEqual(self.s.status("old"), "expired")

    def test_reservation_survives_reload(self):
        job = self.job()
        self.assertTrue(self.s.reserve_send(job, NOW, 2, "解决了吗"))
        self.s.close()
        self.s = Store(self.path)
        self.assertEqual(self.s.status("one"), "uncertain")
        self.assertFalse(self.s.reserve_send(job, NOW, 2, "解决了吗"))
        self.assertEqual(self.s.count(UMO, NOW), 1)

    def test_daily_cap_includes_failed_sends(self):
        job = self.job()
        self.s.reserve_send(job, NOW, 1, "hi")
        self.s.sent(job["id"], "uncertain")
        second = self.job("two")
        self.assertFalse(self.s.reserve_send(second, NOW, 1, "hi again"))

    def test_group_cancellation_isolation(self):
        self.job()
        self.job("other", OTHER)
        self.s.cancel_topics(UMO, "123", "解决了")
        self.assertEqual(self.s.status("one"), "cancelled")
        self.assertEqual(self.s.status("other"), "pending")

    def test_manual_and_automatic_caps_are_independent_and_persist(self):
        for key in ('auto-one', 'manual:first'):
            self.s.add_job(key, UMO, 'share', NOW - 1, NOW + 3600, NOW)
            job = dict(self.s.db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            self.assertTrue(self.s.reserve_send(job, NOW, 1, 'offline'))
            self.s.sent(key, 'uncertain')
        self.s.close()
        self.s = Store(self.path)
        self.assertEqual(self.s.count(UMO, NOW), 2)
        self.assertEqual(self.s.count(UMO, NOW, manual=False), 1)
        self.assertEqual(self.s.count(UMO, NOW, manual=True), 1)
        self.assertEqual(self.s.count(UMO, NOW + 86400, manual=True), 0)
        for key in ('auto-two', 'manual:second'):
            self.s.add_job(key, UMO, 'share', NOW - 1, NOW + 3600, NOW)
            job = dict(self.s.db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            self.assertFalse(self.s.reserve_send(job, NOW, 1, 'blocked'))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 2)

    def test_topic_dedup_including_completed_topics(self):
        self.assertTrue(self.s.add_topic(UMO, "123", "群友", "social", "晚上有人开黑吗", NOW, 1200))
        job = self.s.db.execute("SELECT id FROM jobs").fetchone()[0]
        self.s.finish(job, "skipped")
        self.assertFalse(self.s.add_topic(UMO, "123", "群友", "social", "晚上有人开黑吗？", NOW + 100, 1200))

    def test_atomic_model_call_caps(self):
        self.assertTrue(self.s.reserve_call(UMO, NOW, 1, 2))
        self.assertFalse(self.s.reserve_call(UMO, NOW, 1, 2))
        self.assertTrue(self.s.reserve_call(OTHER, NOW, 2, 2))
        self.assertFalse(self.s.reserve_call(OTHER, NOW, 2, 2))
        self.assertEqual(self.s.total_calls(NOW), 2)

    def test_bounded_recent_memory(self):
        for i in range(140):
            self.s.message(UMO, "123", "群友", str(i), NOW + i)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 120)
        self.assertEqual(self.s.recent(UMO, limit=2)[0]["text"], "138")


class SourceTest(unittest.TestCase):
    def test_qq_chart(self):
        rows = qq_items({"songlist": [{"data": {"songmid": "001UsFL62ogT42", "songname": "动画OP",
            "singer": [{"name": "歌手"}], "albumdesc": "新番片头曲", "albummid": "001Album123"}}]}, "动漫音乐榜")
        self.assertEqual(rows[0].source, "QQ音乐动漫音乐榜")
        self.assertTrue(rows[0].url.endswith("001UsFL62ogT42"))
        self.assertEqual(rows[0].author, "歌手")
        self.assertEqual(rows[0].cover, "https://y.gtimg.cn/music/photo_new/T002R300x300M000001Album123.jpg")
        self.assertNotIn(rows[0].cover, json.dumps(rows[0].prompt_data()))

    def test_netease_old_and_new_schema(self):
        old = netease_items({"result": {"tracks": [{"id": 123, "name": "歌", "artists": [{"name": "A"}]}]}})
        new = netease_items({"playlist": {"tracks": [{"id": 123, "name": "歌", "ar": [{"name": "A"}]}]}})
        self.assertEqual(old, new)
        self.assertIn("song?id=123", old[0].url)

    def test_netease_cover_and_artist(self):
        row = netease_items({"playlist": {"tracks": [{"id": 123, "name": "OP", "ar": [{"name": "歌手"}],
            "al": {"name": "专辑", "picUrl": "http://p1.music.126.net/cover.jpg"}}]}})[0]
        self.assertEqual(row.cover, "https://p1.music.126.net/cover.jpg")
        self.assertEqual(row.author, "歌手")

    def test_cover_domains_are_restricted_and_old_urls_normalized(self):
        self.assertEqual(cover_url("//i0.hdslb.com/bfs/archive/test.jpg"), "https://i0.hdslb.com/bfs/archive/test.jpg")
        for url in ["https://evil.example/cover.jpg", "https://i0.hdslb.com.evil.example/x", "https://127.0.0.1/x",
                    "https://user@i0.hdslb.com/x", "https://i0.hdslb.com:8443/x", "file:///tmp/x", "https://[broken"]:
            self.assertEqual(cover_url(url), "")

    def test_native_and_aggregate_bili_cover(self):
        video = {"title": "游戏预告", "bvid": "BV1AHnB6NEhM", "pic": "http://i0.hdslb.com/cover.jpg", "owner": {"name": "UP主"}}
        for data, aggregate in [({"data": {"list": [video]}}, False), ({"list": [{"title": video["title"], "extra": video}]}, True)]:
            row = bili_items(data, aggregate=aggregate)[0]
            self.assertEqual(row.cover, "https://i0.hdslb.com/cover.jpg")
            self.assertEqual(row.author, "UP主")

    def test_bili_only_acg_and_real_bvid(self):
        rows = bili_items({"list": [
            {"title": "游戏最终预告", "extra": {"bvid": "BV1AHnB6NEhM", "tname": "单机游戏"}},
            {"title": "手机评测", "extra": {"bvid": "BV1xrnz6gEVD"}},
            {"title": "动漫", "url": "https://evil.example/"},
        ]}, aggregate=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].url, "https://www.bilibili.com/video/BV1AHnB6NEhM")


class SharingTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_source_link_is_rejected_before_serialization(self):
        item = Content("fake", "游戏预告", "https://example.com/video", "B站",
                       cover="https://i0.hdslb.com/cover.jpg")
        with self.assertRaises(ValueError):
            components("这个预告想看看", item)

    async def test_cover_mode_keeps_thumbnail_title_and_link(self):
        from astrbot.api.message_components import Image, Plain
        item = Content("qq:test12", "新番OP", "https://y.qq.com/n/ryqq/songDetail/test12", "QQ音乐",
                       cover="https://y.gtimg.cn/cover.jpg", author="歌手")
        chain = components("先收藏", item)
        self.assertIsInstance(chain[0], Image)
        self.assertEqual(chain[0].file, item.cover)
        self.assertIsInstance(chain[1], Plain)
        self.assertIn(item.title, chain[1].text)
        self.assertIn(item.url, chain[1].text)
        item.cover = "https://evil.example/image"
        self.assertEqual(len(components("先收藏", item)), 1)

    async def test_generated_media_stays_image_or_audio(self):
        from astrbot.api.message_components import Image, Plain, Record
        for kind, component in [("image", Image), ("audio", Record)]:
            item = Content("media:test", "作品", "", "生成", media_file=str(TEST_TEMP / "media.png"), media_kind=kind)
            chain = components("试着画了张", item)
            self.assertIsInstance(chain[0], Plain)
            self.assertIsInstance(chain[1], component)

    async def test_only_selected_bili_missing_cover_requests_detail(self):
        sources = Sources()
        sources.get = AsyncMock(return_value={"data": {"bvid": "BV1AHnB6NEhM", "pic": "https://i0.hdslb.com/a.jpg", "owner": {"name": "UP主"}}})
        item = Content("bili:BV1AHnB6NEhM", "游戏", "https://www.bilibili.com/video/BV1AHnB6NEhM", "B站")
        enriched = await sources.enrich(item)
        self.assertEqual(enriched.cover, "https://i0.hdslb.com/a.jpg")
        self.assertEqual(enriched.author, "UP主")
        self.assertEqual(item.cover, "")  # Do not mutate cached chart candidates.
        self.assertIs(await sources.enrich(enriched), enriched)
        self.assertIsNone(await sources.enrich(None))
        self.assertEqual(sources.get.await_count, 1)
        sources.get.side_effect = TimeoutError()
        self.assertIs(await sources.enrich(item), item)


class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-", dir=TEST_TEMP)
        self.now = NOW
        self.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"send":true,"text":"之前那个问题现在解决了吗？"}'))]))
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.options = lambda **kw: self.client
        self.options_mock = Mock(side_effect=self.options)
        provider = SimpleNamespace(client=SimpleNamespace(with_options=self.options_mock), get_model=lambda: "deepseek-v4-flash")
        self.send = AsyncMock(return_value=True)
        self.context = SimpleNamespace(get_provider_by_id=lambda _: provider, send_message=self.send)
        with patch.object(main.StarTools, "get_data_dir", return_value=Path(self.temp.name)):
            self.p = AcgCompanion(self.context, {"sessions": [UMO, OTHER], "share_enabled": False,
                                                 "provider_id": "test-provider", "daily_limit": "2"})
        self.clock = patch.object(main.time, "time", side_effect=lambda: self.now)
        self.clock.start()
        self.p.store.update(UMO, last_human=NOW - 600)
        self.p.store.message(UMO, "123", "群友", "游戏一直连不上，怎么办", NOW - 3 * 3600)
        self.p.store.update(UMO, last_human=NOW - 600)
        self.p.store.add_job("problem", UMO, "problem", NOW - 60, NOW + 4 * 3600, NOW - 3 * 3600,
                             "123", "群友", "游戏一直连不上，怎么办")

    async def asyncTearDown(self):
        self.clock.stop()
        await self.p.terminate()
        self.temp.cleanup()

    async def test_real_framework_import_and_successful_tick(self):
        await self.p.tick()
        self.send.assert_awaited_once()
        self.assertEqual(self.p.store.status("problem"), "sent")
        self.assertEqual(self.create.call_args.kwargs["max_tokens"], 320)
        self.options_mock.assert_called_once_with(timeout=45, max_retries=0)
        await self.p.tick()
        self.assertEqual(self.send.await_count, 1)

    async def test_unconfigured_provider_never_uses_another_model(self):
        self.p.cfg = Settings({"sessions": [UMO]})
        result = await self.p.decide({"umo": UMO}, [], NOW)
        self.assertIsNone(result)
        self.assertIn("选择文本模型", self.p.last_error)
        item = Content("test", "晚霞", "", "图片", media_kind="image", image_prompt="夕阳与小楼")
        self.assertIsNone(await self.p.make_image_caption(UMO, item, NOW))
        self.create.assert_not_awaited()
        self.assertEqual(self.p.store.calls(UMO, NOW), 0)

    async def test_incoming_message_during_generation_suppresses_send(self):
        result = self.create.return_value
        async def interrupted(**kwargs):
            self.p.store.message(UMO, "456", "路人", "有人说话了", NOW)
            return result
        self.create.side_effect = interrupted
        await self.p.tick()
        self.send.assert_not_awaited()
        self.assertEqual(self.p.store.status("problem"), "interrupted")

    async def test_resolution_during_generation_cancels_send(self):
        result = self.create.return_value
        async def resolved(**kwargs):
            self.p.store.cancel_topics(UMO, "123", "解决了")
            return result
        self.create.side_effect = resolved
        await self.p.tick()
        self.send.assert_not_awaited()
        self.assertEqual(self.p.store.status("problem"), "cancelled")

    async def test_quiet_and_active_group_cost_no_tokens(self):
        self.p.store.update(UMO, last_human=NOW - 10)
        await self.p.tick()
        self.create.assert_not_awaited()
        self.p.store.update(UMO, last_human=NOW - 600)
        self.now = datetime(2026, 10, 22, 3, tzinfo=SHANGHAI).timestamp()
        await self.p.tick()
        self.create.assert_not_awaited()

    async def test_other_plugin_just_spoke(self):
        self.p.store.update(UMO, last_bot=NOW - 10)
        await self.p.tick()
        self.create.assert_not_awaited()

    async def test_onebot_direct_proactive_send_is_noticed(self):
        self.p.bots[UMO] = SimpleNamespace(call_action=AsyncMock(return_value={"messages": [{"time": NOW - 1, "user_id": "999"}]}))
        self.p.self_ids[UMO] = "999"
        await self.p.tick()
        self.create.assert_not_awaited()

    async def test_global_model_budget_blocks_without_request(self):
        for _ in range(16):
            self.p.store.reserve_call(OTHER, NOW, 30, 16)
        await self.p.tick()
        self.create.assert_not_awaited()

    async def test_uncertain_send_not_repeated(self):
        self.send.side_effect = TimeoutError()
        await self.p.tick()
        self.assertEqual(self.p.store.status("problem"), "uncertain")
        self.now += 2 * 3600
        await self.p.tick()
        self.assertEqual(self.send.await_count, 1)

    async def test_invalid_llm_json_fails_closed(self):
        self.create.return_value.choices[0].message.content = "随便来点什么吧"
        await self.p.tick()
        self.send.assert_not_awaited()
        self.assertEqual(self.p.store.calls(UMO, NOW), 1)

    async def test_no_reply_pause_and_human_resume(self):
        self.p.store.update(UMO, awaiting=NOW - 4000, unanswered=1)
        self.p.settle(UMO, NOW)
        self.assertGreater(self.p.store.state(UMO)["auto_pause"], NOW)
        self.p.store.message(UMO, "456", "群友", "来了", NOW)
        self.assertEqual(self.p.store.state(UMO)["auto_pause"], 0)

    async def test_media_without_key_never_requests(self):
        self.assertFalse(self.p.media.enabled())
        with patch.object(self.p.media, "request", new_callable=AsyncMock) as request:
            self.assertIsNone(await self.p.media.get(NOW, "seed"))
            request.assert_not_awaited()

    async def test_failed_media_submission_not_retried_in_other_group(self):
        cfg = Settings({"image_enabled": True, "image_api_key": "test-placeholder"})
        media = Media(cfg, self.p.store, self.p.sources, Path(self.temp.name) / "media")
        with patch.object(media, "request", new_callable=AsyncMock, side_effect=TimeoutError()) as request:
            self.assertIsNone(await media.get(NOW, UMO))
            self.assertIsNone(await media.get(NOW + 10, OTHER))
            self.assertEqual(request.await_count, 1)

    async def test_actual_openai_sdk_payload_without_network(self):
        import httpx
        from openai import AsyncOpenAI
        payloads = []
        def response(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
                "model": "deepseek-v4-flash", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"send":true,"text":"问题解决了吗？"}'}}]})
        client = AsyncOpenAI(api_key="offline-test-only", base_url="https://offline.example/v1",
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)))
        self.context.get_provider_by_id = lambda _: SimpleNamespace(client=client, get_model=lambda: "deepseek-v4-flash")
        try:
            await self.p.tick()
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["max_tokens"], 320)
            self.assertEqual(payloads[0]["thinking"], {"type": "disabled"})
            self.send.assert_awaited_once()
        finally:
            await client.close()

    async def test_observe_records_candidates_and_cancels_on_resolution(self):
        event = SimpleNamespace(unified_msg_origin=OTHER, message_str="游戏报错了，怎么办",
                                get_sender_id=lambda: "456", get_self_id=lambda: "999", get_sender_name=lambda: "群友")
        await self.p.observe(event)
        row = self.p.store.db.execute("SELECT * FROM jobs WHERE umo=?", (OTHER,)).fetchone()
        self.assertEqual(row["kind"], "problem")
        self.assertGreaterEqual(row["due"] - NOW, 120 * 60)
        self.assertLessEqual(row["due"] - NOW, 360 * 60)
        event.message_str = "还没解决这个问题"
        await self.p.observe(event)
        self.assertEqual(self.p.store.status(row["id"]), "pending")
        event.message_str = "已经修好了，谢谢"
        await self.p.observe(event)
        self.assertEqual(self.p.store.status(row["id"]), "cancelled")

    async def web_call(self, method, data=None, username="admin"):
        from astrbot.api.web import PluginRequest, bind_request_context
        from starlette.requests import Request
        body = json.dumps(data).encode() if data is not None else b""
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        req = Request({"type": "http", "method": "POST", "path": "/api/plug/astrbot_plugin_acg_companion/test",
                       "headers": [(b"content-type", b"application/json")], "query_string": b"",
                       "server": ("localhost", 6185), "client": ("localhost", 1), "scheme": "http"}, receive)
        with bind_request_context(PluginRequest(req, username=username)):
            return await method()

    def preview_request(self, kind="share", **extra):
        import uuid
        return {"umo": UMO, "kind": kind, "source": "qq_anime", "request_id": str(uuid.uuid4()), **extra}

    def mock_web_share(self):
        self.create.return_value.choices[0].message.content = '{"send":true,"text":"这首新番OP看着挺有意思","pick":0}'
        self.p.sources.fetch = AsyncMock(return_value=[Content("qq:test123", "新番OP", "https://y.qq.com/n/ryqq/songDetail/test123", "QQ音乐动漫榜",
            cover="https://y.gtimg.cn/cover.jpg", author="歌手")])

    async def image_preview(self, **extra):
        from test_image_api import PNG
        import base64
        self.p.cfg.raw.update(image_api_key="offline-key", image_api_url="https://image.example/v1", image_enabled=False)
        self.p.media.request = AsyncMock(return_value={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        body = self.preview_request(prompt=extra.pop("prompt", "动漫街角"), caption=extra.pop("caption", "试着画了张街角"), **extra)
        start = await self.web_call(self.p.web.image_start, body)
        self.assertEqual(start["status"], "ok", start)
        await self.p.web.image_task
        status = await self.web_call(self.p.web.image_status, {"token": start["data"]["token"]})
        return body, start, status

    async def test_image_web_preview_without_auto_or_text_model_and_no_send(self):
        _, _, status = await self.image_preview()
        self.assertEqual(status["data"]["status"], "ready", status)
        preview = status["data"]["preview"]
        self.assertTrue(preview["image"].startswith("data:image/png;base64,"))
        self.assertEqual(preview["text"], "试着画了张街角")
        self.assertFalse(preview["reused"])
        self.create.assert_not_awaited()
        self.send.assert_not_awaited()
        self.p.media.request.assert_awaited_once()

    async def test_image_web_same_request_and_cached_preview_never_regenerate(self):
        body, start, status = await self.image_preview()
        repeated = await self.web_call(self.p.web.image_start, body)
        self.assertEqual(start["data"]["token"], repeated["data"]["token"])
        cached = await self.web_call(self.p.web.image_start, self.preview_request(prompt="new prompt"))
        await self.p.web.image_task
        result = await self.web_call(self.p.web.image_status, {"token": cached["data"]["token"]})
        self.assertTrue(result["data"]["preview"]["reused"])
        self.p.media.request.assert_awaited_once()

    async def test_image_web_send_uses_same_file_once_and_no_regeneration(self):
        from astrbot.api.message_components import Image, Plain
        _, start, status = await self.image_preview()
        token = status["data"]["preview"]["token"]
        sent = await self.web_call(self.p.web.send, {"token": token})
        self.assertEqual(sent["data"]["result"], "sent", sent)
        await self.web_call(self.p.web.send, {"token": token})
        self.send.assert_awaited_once()
        chain = self.send.call_args.args[1].chain
        self.assertTrue(any(isinstance(x, Image) for x in chain))
        self.assertTrue(any(isinstance(x, Plain) and "试着画了张街角" in x.text for x in chain))
        self.p.media.request.assert_awaited_once()
        checked = await self.web_call(self.p.web.image_status, {"token": start["data"]["token"]})
        self.assertFalse(checked["data"]["preview"]["sendable"])

    async def test_image_theme_caption_is_one_short_text_call_and_cached(self):
        theme = '一碗热腾腾的番茄鸡蛋面，anime illustration'
        wording = '今天吃番茄鸡蛋面，这一碗下去舒服了'
        self.create.return_value.choices[0].message.content = json.dumps({'send': True, 'text': wording}, ensure_ascii=False)
        _, _, status = await self.image_preview(prompt=theme, caption='')
        self.assertEqual(status['data']['preview']['text'], wording)
        self.assertEqual(status['data']['preview']['caption_origin'], 'model')
        self.create.assert_awaited_once()
        args = self.create.call_args.kwargs
        self.assertEqual(args['max_tokens'], 128)
        self.assertEqual(args['model'], 'deepseek-v4-flash')
        self.assertEqual(json.loads(args['messages'][1]['content'])['image_prompt'], theme)
        self.assertNotIn('data:image', json.dumps(args['messages']))
        self.assertNotIn('media_file', json.dumps(args['messages']))
        self.options_mock.assert_called_once_with(timeout=25, max_retries=0)
        cached = await self.web_call(self.p.web.image_start, self.preview_request(prompt='另一个完全不同的主题', caption=''))
        await self.p.web.image_task
        checked = await self.web_call(self.p.web.image_status, {'token': cached['data']['token']})
        self.assertEqual(checked['data']['preview']['text'], wording)
        self.assertTrue(checked['data']['preview']['reused'])
        self.assertEqual(self.p.store.calls(UMO, NOW), 1)
        self.create.assert_awaited_once()
        self.p.media.request.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_wording_only_test_updates_same_image_preview_and_sends_once(self):
        _, start, status = await self.image_preview()
        token = status['data']['preview']['token']
        before = self.p.store.media_cached(NOW, 86400)
        old_image = status['data']['preview']['image']
        wording = '刚才路过这条街，雨后的光还挺好看'
        self.create.return_value.choices[0].message.content = json.dumps({'send': True, 'text': wording}, ensure_ascii=False)
        body = self.preview_request(token=token, theme='雨后的小街，落日暖光')
        first = await self.web_call(self.p.web.image_caption, body)
        repeated = await self.web_call(self.p.web.image_caption, body)
        self.assertEqual(first['status'], 'ok', first)
        self.assertEqual(first, repeated)
        self.create.assert_awaited_once()
        checked = await self.web_call(self.p.web.image_status, {'token': start['data']['token']})
        preview = checked['data']['preview']
        self.assertEqual(preview['token'], token)
        self.assertEqual(preview['text'], wording)
        self.assertEqual(preview['image'], old_image)
        self.assertEqual(self.p.store.media_cached(NOW, 86400)['ts'], before['ts'])
        self.p.media.request.assert_awaited_once()
        for _ in range(2):
            result = await self.web_call(self.p.web.send, {'token': token})
            self.assertEqual(result['data']['result'], 'sent')
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[1].chain[0].text, wording)
        blocked = await self.web_call(self.p.web.image_caption, self.preview_request(token=token))
        self.assertEqual(blocked['status'], 'error')
        self.create.assert_awaited_once()

    async def test_caption_failure_preserves_image_and_does_not_retry_on_cache(self):
        self.create.side_effect = TimeoutError()
        _, start, status = await self.image_preview(caption='')
        preview = status['data']['preview']
        self.assertEqual(status['data']['status'], 'ready', status)
        self.assertEqual(preview['caption_origin'], 'fallback')
        self.assertIn('配文生成未完成', preview['caption_note'])
        self.assertTrue(preview['sendable'])
        self.assertEqual(self.p.store.media_cached(NOW, 86400)['status'], 'ready')
        cached = await self.web_call(self.p.web.image_start, self.preview_request())
        await self.p.web.image_task
        self.create.assert_awaited_once()
        self.p.media.request.assert_awaited_once()
        self.create.side_effect = None
        self.create.return_value.choices[0].message.content = '{"send":true,"text":"https://unwanted.example"}'
        body = self.preview_request(token=preview['token'])
        result = await self.web_call(self.p.web.image_caption, body)
        again = await self.web_call(self.p.web.image_caption, body)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result, again)
        checked = await self.web_call(self.p.web.image_status, {'token': start['data']['token']})
        self.assertEqual(checked['data']['preview']['text'], preview['text'])
        self.assertTrue(checked['data']['preview']['sendable'])
        self.assertEqual(self.create.await_count, 2)

    async def test_caption_model_budget_never_loses_successful_image(self):
        for _ in range(8):
            self.p.store.reserve_call(UMO, NOW, 8, 16)
        _, _, status = await self.image_preview(caption='')
        self.assertEqual(status['data']['status'], 'ready', status)
        self.assertIn('文本模型额度', status['data']['preview']['caption_note'])
        self.create.assert_not_awaited()
        self.p.media.request.assert_awaited_once()

    async def test_cached_manual_caption_overrides_only_current_preview(self):
        await self.image_preview()
        cached = await self.web_call(self.p.web.image_start, self.preview_request(caption='这是手填的新配文'))
        await self.p.web.image_task
        checked = await self.web_call(self.p.web.image_status, {'token': cached['data']['token']})
        self.assertEqual(checked['data']['preview']['text'], '这是手填的新配文')
        self.assertEqual(checked['data']['preview']['caption_origin'], 'manual')
        self.assertEqual(json.loads(self.p.store.media_cached(NOW, 86400)['payload'])['description'], '试着画了张街角')
        self.create.assert_not_awaited()
        self.p.media.request.assert_awaited_once()

    async def test_legacy_image_caption_requires_theme_and_supports_explicit_rewrite(self):
        await self.image_preview()
        row = self.p.store.media_cached(NOW, 86400)
        payload = json.loads(row['payload'])
        payload.pop('image_prompt')
        payload.pop('caption_origin')
        self.p.store.media_finish(row['id'], 'ready', payload)
        cached = await self.web_call(self.p.web.image_start, self.preview_request(caption=''))
        await self.p.web.image_task
        checked = await self.web_call(self.p.web.image_status, {'token': cached['data']['token']})
        token = checked['data']['preview']['token']
        self.assertIn('旧图', checked['data']['preview']['caption_note'])
        result = await self.web_call(self.p.web.image_caption, self.preview_request(token=token))
        self.assertEqual(result['status'], 'error')
        self.create.assert_not_awaited()
        self.now += 11
        self.create.return_value.choices[0].message.content = '{"send":true,"text":"随手摸了只猫，替我把手柄占了"}'
        result = await self.web_call(self.p.web.image_caption, self.preview_request(token=token, theme='doodle，小猫趴在手柄上'))
        self.assertEqual(result['status'], 'ok', result)
        self.create.assert_awaited_once()
        self.p.media.request.assert_awaited_once()

    async def test_automatic_image_uses_existing_decision_for_daily_caption(self):
        from test_image_api import PNG
        import base64
        self.p.cfg.raw.update(image_api_key='offline-key', image_enabled=True, share_enabled=True)
        self.p.media.request = AsyncMock(return_value={'data': [{'b64_json': base64.b64encode(PNG).decode()}]})
        item = await self.p.media.get(NOW, 'offline', force_kind='image', prompt='黄昏河边的风景')
        self.p.store.finish('problem', 'skipped')
        self.p.store.add_job('auto-image', UMO, 'share', NOW - 1, NOW + 3600, NOW - 600)
        self.create.return_value.choices[0].message.content = '{"send":true,"text":"刚看到这个晚霞，想拉你们一起看","pick":0}'
        with patch.object(main, 'plan', return_value=(2, [])), patch.object(main.random, 'Random') as rng, patch.object(self.p.media, 'get', new_callable=AsyncMock, return_value=item):
            rng.return_value.random.return_value = 0
            await self.p.tick()
        self.create.assert_awaited_once()
        self.assertEqual(self.create.call_args.kwargs['max_tokens'], 320)
        candidate = json.loads(self.create.call_args.kwargs['messages'][1]['content'])['candidates'][0]
        self.assertEqual(candidate['image_prompt'], '黄昏河边的风景')
        self.assertEqual(self.p.store.status('auto-image'), 'sent')
        saved = json.loads(self.p.store.media_cached(NOW, 86400)['payload'])
        self.assertEqual(saved['caption_origin'], 'model')
        self.assertEqual(saved['description'], '刚看到这个晚霞，想拉你们一起看')
        self.send.assert_awaited_once()

    async def test_image_web_auth_configuration_and_invalid_group(self):
        for body, username in [(self.preview_request(), None), (self.preview_request(umo="unknown"), "admin"),
                               (self.preview_request(prompt="a" * 1201), "admin"), (self.preview_request(), "admin")]:
            result = await self.web_call(self.p.web.image_start, body, username=username)
            self.assertEqual(result["status"], "error", result)
        self.assertIsNone(self.p.web.image_task)

    async def test_image_manual_send_works_when_automatic_budget_is_exhausted(self):
        for index in range(2):
            key = f'auto-spent:{index}'
            self.p.store.add_job(key, UMO, 'share', NOW - 1, NOW + 3600, NOW)
            job = dict(self.p.store.db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            self.assertTrue(self.p.store.reserve_send(job, NOW, 2, 'offline'))
            self.p.store.sent(key, 'sent')
        _, _, status = await self.image_preview()
        result = await self.web_call(self.p.web.send, {'token': status['data']['preview']['token']})
        self.assertEqual(result['data']['result'], 'sent', result)
        self.send.assert_awaited_once()
        self.p.media.request.assert_awaited_once()
        group = self.p.web.snapshot()['groups'][0]
        self.assertEqual((group['count'], group['manual_count']), (2, 1))
        self.assertIn('今日主动发送额度已用完', group['blockers'])

    async def test_manual_send_limit_blocks_without_consuming_draft_or_regenerating(self):
        self.p.cfg = Settings({**self.p.cfg.raw, 'manual_send_daily_limit': 1})
        self.p.media.cfg = self.p.cfg
        self.p.store.add_job('manual:old', UMO, 'share', NOW - 1, NOW + 3600, NOW)
        old = dict(self.p.store.db.execute("SELECT * FROM jobs WHERE id='manual:old'").fetchone())
        self.assertTrue(self.p.store.reserve_send(old, NOW, 1, 'old'))
        self.p.store.sent(old['id'], 'uncertain')
        _, start, status = await self.image_preview()
        token = status['data']['preview']['token']
        result = await self.web_call(self.p.web.send, {'token': token})
        self.assertEqual(result['status'], 'error', result)
        self.assertIn('1/1', result['message'])
        checked = await self.web_call(self.p.web.image_status, {'token': start['data']['token']})
        self.assertTrue(checked['data']['preview']['sendable'])
        self.send.assert_not_awaited()
        self.assertEqual(self.p.store.count(UMO, NOW, manual=True), 1)
        self.p.media.request.assert_awaited_once()

    async def test_image_web_task_failure_is_readable_and_not_retried(self):
        self.p.cfg.raw.update(image_api_key="offline-key")
        with patch.object(self.p.media, "request", new_callable=AsyncMock, side_effect=TimeoutError()) as call:
            started = await self.web_call(self.p.web.image_start, self.preview_request())
            await self.p.web.image_task
            status = await self.web_call(self.p.web.image_status, {"token": started["data"]["token"]})
            self.assertEqual(status["data"]["status"], "error")
            self.assertIn("超时", status["data"]["message"])
            self.assertIsNone(status["data"]["preview"])
            again = await self.web_call(self.p.web.image_start, self.preview_request())
            await self.p.web.image_task
            self.assertEqual(call.await_count, 1)

    async def test_image_web_unload_cancels_and_persists_uncertain(self):
        entered = asyncio.Event()
        async def hanging(*args):
            entered.set()
            await asyncio.Event().wait()
        self.p.cfg.raw.update(image_api_key="offline-key")
        with patch.object(self.p.media, "generate_image", side_effect=hanging):
            started = await self.web_call(self.p.web.image_start, self.preview_request())
            await entered.wait()
            self.p.web.unregister()
            with self.assertRaises(asyncio.CancelledError):
                await self.p.web.image_task
        self.assertEqual(self.p.store.media_cached(NOW, 86400)["status"], "uncertain")

    async def test_share_sends_cover_and_caption(self):
        from astrbot.api.message_components import Image, Plain
        self.mock_web_share()
        preview = await self.web_call(self.p.web.preview, self.preview_request())
        result = await self.web_call(self.p.web.send, {"token": preview["data"]["token"]})
        self.assertEqual(result["data"]["result"], "sent")
        chain = self.send.call_args.args[1].chain
        self.assertIsInstance(chain[0], Image)
        self.assertIsInstance(chain[1], Plain)
        self.assertIn(preview["data"]["text"], chain[1].text)
        self.assertIn(preview["data"]["card"]["title"], chain[1].text)
        self.assertEqual(len(chain), 2)

    async def test_automatic_share_uses_same_cover_delivery_path(self):
        self.mock_web_share()
        self.p.cfg.raw["share_enabled"] = True
        self.p.store.finish("problem", "skipped")
        self.p.store.add_job("auto-share", UMO, "share", NOW - 60, NOW + 3600, NOW - 600)
        self.p.sources.candidates = AsyncMock(return_value=self.p.sources.fetch.return_value)
        with patch.object(main, "plan", return_value=(2, [])):
            await self.p.tick()
        self.assertEqual(self.p.store.status("auto-share"), "sent")
        self.assertIsInstance(self.send.call_args.args[1].chain[0], Image)
        self.assertIn(self.p.sources.fetch.return_value[0].url, self.send.call_args.args[1].chain[1].text)

    async def test_web_requires_dashboard_login(self):
        result = await self.web_call(self.p.web.get_status, username=None)
        self.assertEqual(result["status"], "error")
        result = await self.web_call(self.p.web.preview, self.preview_request(), username=None)
        self.assertEqual(result["status"], "error")
        self.create.assert_not_awaited()

    async def test_web_status_redacts_keys_and_explains_blockers(self):
        self.p.cfg.raw["image_api_key"] = "secret-do-not-return"
        self.p.store.update(UMO, last_bot=NOW - 1)
        result = await self.web_call(self.p.web.get_status)
        self.assertNotIn("secret-do-not-return", json.dumps(result))
        self.assertTrue(result["data"]["secrets"]["image_api_key"])
        self.assertIn("机器人刚发过消息，避让中", result["data"]["groups"][0]["blockers"])

    async def test_web_preview_never_sends_or_creates_live_job(self):
        self.mock_web_share()
        result = await self.web_call(self.p.web.preview, self.preview_request())
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["data"]["sendable"])
        self.send.assert_not_awaited()
        self.assertEqual(self.p.store.count(UMO, NOW), 0)
        self.assertEqual(self.p.store.calls(UMO, NOW), 1)
        self.assertEqual(self.p.store.db.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE 'manual:%'").fetchone()[0], 0)

    async def test_web_retried_preview_uses_same_request_result(self):
        self.mock_web_share()
        data = self.preview_request()
        first = await self.web_call(self.p.web.preview, data)
        second = await self.web_call(self.p.web.preview, data)
        self.assertEqual(first, second)
        self.create.assert_awaited_once()

    async def test_web_send_reuses_exact_draft_and_is_idempotent(self):
        self.mock_web_share()
        result = await self.web_call(self.p.web.preview, self.preview_request())
        data = {"token": result["data"]["token"], "text": "client cannot replace the preview text"}
        self.p.store.update(UMO, last_human=NOW)  # Explicit test skips idle guards.
        first = await self.web_call(self.p.web.send, data)
        second = await self.web_call(self.p.web.send, data)
        self.assertEqual(first["data"]["result"], "sent")
        self.assertEqual(second["data"]["result"], "sent")
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.create.await_count, 1)
        chain = self.send.call_args.args[1].chain
        self.assertEqual(len(chain), 2)
        self.assertIsInstance(chain[0], Image)
        self.assertIn(result["data"]["text"], chain[1].text)
        self.assertIn(result["data"]["card"]["url"], chain[1].text)
        self.assertNotIn("https://", result["data"]["text"])
        self.assertIn(result["data"]["card"]["title"], self.p.store.own_messages(UMO, NOW)[0]["text"])

    async def test_web_simulated_followup_cannot_be_sent(self):
        result = await self.web_call(self.p.web.preview, self.preview_request("problem"))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["sendable"])
        result = await self.web_call(self.p.web.send, {"token": result["data"]["token"]})
        self.assertEqual(result["status"], "error")
        self.send.assert_not_awaited()

    async def test_web_real_topic_cancelled_after_preview_is_not_sent(self):
        result = await self.web_call(self.p.web.preview, self.preview_request("problem", topic_id="problem"))
        self.assertTrue(result["data"]["sendable"])
        self.p.store.cancel_topics(UMO, "123", "修好了")
        result = await self.web_call(self.p.web.send, {"token": result["data"]["token"]})
        self.assertEqual(result["status"], "error")
        self.send.assert_not_awaited()

    async def test_web_ambiguous_send_never_retried(self):
        self.mock_web_share()
        result = await self.web_call(self.p.web.preview, self.preview_request())
        self.send.side_effect = TimeoutError()
        first = await self.web_call(self.p.web.send, {"token": result["data"]["token"]})
        second = await self.web_call(self.p.web.send, {"token": result["data"]["token"]})
        self.assertEqual(first["data"]["result"], "uncertain")
        self.assertEqual(second["data"]["result"], "uncertain")
        self.send.assert_awaited_once()
        self.assertIsInstance(self.send.call_args.args[1].chain[0], Image)

    async def test_web_limits_unknown_groups_and_expired_drafts(self):
        result = await self.web_call(self.p.web.preview, self.preview_request(umo="arona:GroupMessage:9999"))
        self.assertEqual(result["status"], "error")
        self.create.assert_not_awaited()
        self.now += 11
        self.mock_web_share()
        result = await self.web_call(self.p.web.preview, self.preview_request())
        self.now += 601
        result = await self.web_call(self.p.web.send, {"token": result["data"]["token"]})
        self.assertEqual(result["status"], "error")
        self.send.assert_not_awaited()

    async def test_web_daily_budget_also_applies_to_previews(self):
        for _ in range(8):
            self.p.store.reserve_call(UMO, NOW, 8, 16)
        result = await self.web_call(self.p.web.preview, self.preview_request())
        self.assertEqual(result["status"], "error")
        self.create.assert_not_awaited()

    async def test_web_config_hot_apply_secret_preservation_and_stale_revision(self):
        class FakeConfig(dict):
            def save_config(self):
                self.saved = dict(self)
        raw = FakeConfig(self.p.cfg.raw)
        raw["image_api_key"] = "keep-secret"
        self.p.cfg = Settings(raw)
        old_revision = self.p.web.revision()
        result = await self.web_call(self.p.web.save_config,
            {"revision": old_revision, "values": {"idle_minutes": 12, "image_api_key": ""}})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.p.cfg.idle, 720)
        self.assertEqual(raw.saved["image_api_key"], "keep-secret")
        self.assertNotIn("keep-secret", json.dumps(result))
        result = await self.web_call(self.p.web.save_config, {"revision": old_revision, "values": {"idle_minutes": 2}})
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.p.cfg.idle, 720)
        result = await self.web_call(self.p.web.save_config,
            {"revision": self.p.web.revision(), "values": {}, "clear_secrets": ["image_api_key"]})
        self.assertEqual(raw["image_api_key"], "")

    async def test_web_config_rejects_invalid_input_before_mutation(self):
        original = dict(self.p.cfg.raw)
        for values in [{"idle_minutes": 0}, {"quiet_hours": "99:88-08:00"}, {"sessions": ["bad"]},
                       {"enabled": "true"}, {"daily_limit": "5-2"}, {"unknown": 1}]:
            with self.subTest(values=values):
                result = await self.web_call(self.p.web.save_config, {"revision": self.p.web.revision(), "values": values})
                self.assertEqual(result["status"], "error")
                self.assertEqual(dict(self.p.cfg.raw), original)

    async def test_merged_schedule_config_replans_without_resetting_usage(self):
        class Config(dict):
            def save_config(self):
                pass
        self.p.cfg = Settings(Config(self.p.cfg.raw))
        self.p.store.add_job("old-slot", UMO, "share", NOW + 60, NOW + 7200, NOW)
        self.p.store.reserve_call(UMO, NOW, 8, 16)
        result = await self.web_call(self.p.web.save_config, {"revision": self.p.web.revision(),
                                   "values": {"daily_limit": "0", "quiet_hours": "23:00-08:00"}})
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(self.p.store.status("old-slot"))
        self.assertEqual(self.p.store.status("problem"), "pending")
        self.assertEqual(self.p.store.calls(UMO, NOW), 1)
        self.assertEqual(self.p.cfg.high, 0)
        await self.p.tick()
        self.send.assert_not_awaited()
        self.create.assert_not_awaited()

    async def test_web_routes_cleaned_up_on_unload(self):
        self.context.registered_web_apis = []
        def register(route, handler, methods, desc):
            self.context.registered_web_apis.append((route, handler, methods, desc))
        self.context.register_web_api = register
        self.p.web.register()
        self.assertEqual(len(self.context.registered_web_apis), 8)
        self.p.web.unregister()
        self.assertEqual(self.context.registered_web_apis, [])
        result = await self.web_call(self.p.web.get_status)
        self.assertEqual(result["status"], "error")

    async def test_web_real_http_routers_dispatch_every_control(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from astrbot.core.star.context import Context
        from astrbot.dashboard.api import plugins as routes

        class Config(dict):
            def save_config(self):
                self.saved = dict(self)

        self.p.cfg = Settings(Config(self.p.cfg.raw))
        self.context.registered_web_apis = []
        self.context.register_web_api = Context.register_web_api.__get__(self.context)
        self.p.web.register()
        self.mock_web_share()
        app = FastAPI()
        app.state.core_lifecycle = SimpleNamespace(star_context=self.context)
        # Test-only login, only inside this in-process app. No real credentials,
        # production server or replacement router/matcher is involved.
        app.dependency_overrides[routes.require_dashboard_user] = lambda: "offline-tester"
        app.dependency_overrides[routes.require_plugin_scope] = lambda: SimpleNamespace(username="offline-tester")
        app.include_router(routes.legacy_router)
        app.include_router(routes.router, prefix="/api/v1")
        modern = "/api/v1/plugins/extensions/astrbot_plugin_acg_companion/"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://offline.test") as client:
            for prefix in (modern, "/api/plug/astrbot_plugin_acg_companion/"):
                result = (await client.get(prefix + "status")).json()
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["data"]["groups"][0]["umo"], UMO)
                missing = (await client.post(prefix + "image-start", json=self.preview_request())).json()
                self.assertEqual(missing["status"], "error")
                self.assertIn("密钥", missing["message"])
                missing = (await client.post(prefix + "image-status", json={"token": "unknown"})).json()
                self.assertEqual(missing["status"], "error")
                self.assertIn("过期", missing["message"])
                missing = (await client.post(prefix + "image-caption", json=self.preview_request(token="unknown"))).json()
                self.assertEqual(missing["status"], "error")
                self.assertIn("预览", missing["message"])
            # Reproduce the erroneous double prefix from a full SDK endpoint.
            result = (await client.get(modern + "api/plug/astrbot_plugin_acg_companion/status")).json()
            self.assertEqual(result["message"], "未找到该路由")
            result = (await client.post(modern + "preview", json=self.preview_request())).json()
            self.assertEqual(result["status"], "ok")
            result = (await client.post(modern + "send", json={"token": result["data"]["token"]})).json()
            self.assertEqual(result["data"]["result"], "sent")
            result = (await client.post(modern + "pause", json={"umo": UMO, "paused": True})).json()
            self.assertEqual(result["status"], "ok")
            result = (await client.post(modern + "config", json={"revision": self.p.web.revision(), "values": {"idle_minutes": 9}})).json()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(self.p.cfg.idle, 540)
        self.send.assert_awaited_once()
        self.create.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
