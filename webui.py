"""Authenticated AstrBot plugin view: settings, diagnostics and two-step tests."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import replace
from pathlib import Path

from astrbot.api.web import request

from . import __version__
from .core import Settings, local_time, plan
from .sources import valid_link
from .sharing import preview_card
from .media import image_endpoint, image_format

PREFIX = "/astrbot_plugin_acg_companion"
SECRET_FIELDS = {"image_api_key", "music_api_key"}
KINDS = {"share", "purchase", "social", "problem", "thursday"}
SAMPLES = {"purchase": "我刚买的游戏手柄到货了，手感还不错",
           "social": "今晚有没有人一起联机打游戏？我晚饭后有空",
           "problem": "游戏更新之后一直连不上服务器，试了重启也不行，大家有办法吗？",
           "thursday": "", "share": ""}


def ok(data=None, message=""):
    return {"status": "ok", "data": data, "message": message}


class WebPanel:
    def __init__(self, plugin):
        self.p = plugin
        self.schema = json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8"))
        self.closed = False
        self.drafts = {}
        self.requests = {}
        self.last_preview = 0
        self.image_jobs = {}
        self.image_task = None
        self.last_caption = 0

    def register(self):
        for suffix, handler, methods in [
            ("status", self.get_status, ["GET"]), ("config", self.save_config, ["POST"]),
            ("preview", self.preview, ["POST"]), ("send", self.send, ["POST"]),
            ("pause", self.pause, ["POST"]),
            ("image-start", self.image_start, ["POST"]), ("image-status", self.image_status, ["POST"]),
            ("image-caption", self.image_caption, ["POST"]),
        ]:
            self.p.context.register_web_api(PREFIX + "/" + suffix, handler, methods, "ACG 群友控制台")

    def unregister(self):
        self.closed = True
        # Do not remove a newer instance's handlers during overlapping reloads.
        apis = getattr(self.p.context, "registered_web_apis", [])
        apis[:] = [row for row in apis if getattr(row[1], "__self__", None) is not self]
        self.drafts.clear()
        self.requests.clear()
        if self.image_task and not self.image_task.done():
            self.image_task.cancel()
        self.image_jobs.clear()

    def revision(self):
        raw = json.dumps(dict(self.p.cfg.raw), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def authorize(self):
        # The extension dispatcher already verifies dashboard authentication.
        if self.closed:
            raise ValueError("插件已重载，请刷新页面")
        if not request.username:
            raise ValueError("请先登录 AstrBot 管理后台")

    async def body(self):
        self.authorize()
        if len(await request.body()) > 32768:
            raise ValueError("请求内容过长")
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    def group(self, value):
        if not isinstance(value, str) or value not in self.p.cfg.sessions:
            raise ValueError("请选择配置中的目标群")
        return value

    def snapshot(self):
        now, p = time.time(), self.p
        groups = []
        for umo in p.cfg.sessions:
            cap, slots = plan(umo, now, p.cfg)
            pending = [dict(row) for row in p.store.db.execute(
                "SELECT id,kind,uid,name,text,due,expires FROM jobs WHERE umo=? AND status='pending' "
                "AND expires>=? ORDER BY due LIMIT 16", (umo, now))]
            attempts = [dict(row) for row in p.store.db.execute(
                "SELECT kind,text,ts,status FROM attempts WHERE umo=? ORDER BY ts DESC LIMIT 8", (umo,))]
            groups.append({"umo": umo, "name": umo.rsplit(":", 1)[-1],
                           "count": p.store.count(umo, now, manual=False), "cap": cap,
                           "manual_count": p.store.count(umo, now, manual=True), "manual_cap": p.cfg.manual_send_limit,
                           "calls": p.store.calls(umo, now), "call_cap": p.cfg.call_cap,
                           "paused": bool(p.store.state(umo).get("paused")),
                           "blockers": p.blockers(umo, now, cap), "pending": pending,
                           "plan": [{"due": due, "expires": expiry} for _, due, expiry in slots],
                           "attempts": attempts})
        config = {key: p.cfg.raw.get(key, item["default"]) for key, item in self.schema.items() if key not in SECRET_FIELDS}
        return {"version": __version__, "now": local_time(now).isoformat(timespec="seconds"),
                "groups": groups, "config": config, "schema": self.schema,
                "secrets": {key: bool(p.cfg.raw.get(key)) for key in SECRET_FIELDS},
                "revision": self.revision(), "total_calls": p.store.total_calls(now),
                "total_call_cap": p.cfg.global_call_cap, "last_error": p.last_error,
                "image_test_budget": p.media.image_test_budget(now),
                "image_test": next(({"token": token, "status": job["status"]} for token, job in reversed(list(self.image_jobs.items()))
                                    if job["expires"] > now), None)}

    async def get_status(self):
        try:
            self.authorize()
            return ok(self.snapshot())
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    def validate_config(self, data):
        updates = data.get("values")
        if not isinstance(updates, dict) or set(updates) - set(self.schema):
            raise ValueError("存在未知配置项")
        values = {key: spec["default"] for key, spec in self.schema.items()}
        values.update(dict(self.p.cfg.raw))
        for key, value in updates.items():
            spec = self.schema[key]
            kind = spec["type"]
            if kind == "bool" and type(value) is not bool:
                raise ValueError(spec["description"] + "必须为开关")
            if kind == "int" and (type(value) is not int or not spec.get("min", 0) <= value <= spec.get("max", 99999)):
                raise ValueError(spec["description"] + "超出允许范围")
            if kind in ("string", "text") and (not isinstance(value, str) or len(value) > 8000):
                raise ValueError(spec["description"] + "格式不正确或过长")
            if kind == "list" and (not isinstance(value, list) or len(value) > 32 or
                                    any(not isinstance(x, str) or len(x) > 160 for x in value)):
                raise ValueError(spec["description"] + "必须是列表，每项填写一行")
            # Empty password inputs preserve a configured key.
            if key not in SECRET_FIELDS or value:
                values[key] = value
        clear = data.get("clear_secrets", [])
        if not isinstance(clear, list) or any(not isinstance(key, str) or key not in SECRET_FIELDS for key in clear):
            raise ValueError("清除密钥参数不正确")
        for key in clear:
            values[key] = ""
        if any(not re.fullmatch(r"[^:\s]+:GroupMessage:\d+", x) for x in values.get("sessions", [])):
            raise ValueError("目标群格式应为 arona:GroupMessage:群号")
        if any(not x.isdigit() for x in values.get("ignored_user_ids", [])):
            raise ValueError("忽略账号必须填写数字 QQ 号")
        url = values.get("music_api_url", "")
        if url and not url.startswith("https://"):
            raise ValueError("歌曲接口必须为 HTTPS 地址")
        image_endpoint(values)
        Settings(values)
        if not values.get("provider_id", "").strip():
            raise ValueError("请填写文本模型提供商 ID")
        return values

    async def save_config(self):
        try:
            data = await self.body()
            if self.p.lock.locked():
                raise ValueError("有任务正在处理，请稍后保存")
            async with self.p.lock:
                if data.get("revision") != self.revision():
                    raise ValueError("配置已在其他页面变更，请刷新后再保存")
                values = self.validate_config(data)
                raw = self.p.cfg.raw
                if not hasattr(raw, "save_config"):
                    raise ValueError("当前配置对象不支持保存")
                before = dict(raw)
                try:
                    raw.update(values)
                    raw.save_config()
                except Exception:
                    raw.clear()
                    raw.update(before)
                    raise ValueError("配置文件保存失败，未应用修改") from None
                self.p.cfg = Settings(raw)
                self.p.media.cfg = self.p.cfg
                self.p.personas.clear()
                self.drafts.clear()
                self.image_jobs.clear()
                if any(before.get(k) != raw.get(k) for k in ("quiet_hours", "daily_limit")):
                    # Rebuild only pending random jobs; completed sends keep their
                    # deduplication records when the daily schedule is changed.
                    self.p.store.db.execute("DELETE FROM jobs WHERE status='pending' AND kind IN ('share','thursday')")
                    self.p.store.db.commit()
                return ok(self.snapshot(), "已保存并立即生效")
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def build_preview(self, data, now):
        p = self.p
        umo = self.group(data.get("umo"))
        kind = data.get("kind", "share")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("不支持的测试类型")
        if p.store.calls(umo, now) >= p.cfg.call_cap or p.store.total_calls(now) >= p.cfg.global_call_cap:
            raise ValueError("今日模型请求额度已用完；预览也计入额度")
        simulation = kind != "share"
        records = None
        origin = None
        topic_id = data.get("topic_id", "")
        if topic_id:
            row = p.store.db.execute("SELECT * FROM jobs WHERE id=? AND umo=? AND status='pending' AND expires>=?",
                                      (str(topic_id), umo, now)).fetchone()
            if not row or row["kind"] not in ("purchase", "social", "problem"):
                raise ValueError("该话题已取消、过期或不可用于测试")
            origin = dict(row)
            kind, simulation = origin["kind"], False
        text = data.get("text", SAMPLES[kind])
        if not isinstance(text, str) or len(text) > 1000:
            raise ValueError("模拟场景最多 1000 字")
        elapsed = {"share": 60, "purchase": 5, "social": 40, "problem": 180, "thursday": 0}[kind]
        job = origin or {"umo": umo, "kind": kind, "uid": "", "name": "模拟群友",
                         "text": text or SAMPLES[kind], "created": now - elapsed * 60}
        if simulation:
            records = [{"ts": job["created"], "uid": "", "name": "模拟群友", "text": job["text"], "bot": 0}]
        items = []
        if kind == "share":
            source = data.get("source", "random")
            if not isinstance(source, str) or source not in ("random", "bilibili", "qq_anime", "qq_game", "netease"):
                raise ValueError("请选择支持的内容来源")
            if source == "random":
                items = await p.sources.candidates(secrets.token_hex(8), lambda key: p.store.seen(umo, key, now))
            else:
                items = [x for x in await p.sources.fetch(source) if not p.store.seen(umo, x.key, now)][:4]
            if not items:
                raise ValueError("此来源暂无可用的新内容，可能接口受限或榜单内容已分享；可以更换来源")
        decision = await p.decide(job, items, time.time(), records_override=records, test_mode=simulation)
        if not decision:
            raise ValueError(p.last_error or "模型未返回有效结果")
        if not decision["send"]:
            return {"decision": "skip", "reason": str(decision.get("reason", "模型判断当前语境不适合发言"))[:160],
                    "sendable": False, "simulation": simulation, "umo": umo}
        item = await p.sources.enrich(items[decision["pick"]] if items else None)
        # Source adapters construct canonical links. Validate again at the UI boundary.
        if item and not valid_link(item.url, {"y.qq.com", "music.163.com", "www.bilibili.com"}):
            raise ValueError("内容链接验证失败")
        output = decision["text"]
        token = secrets.token_urlsafe(24)
        expiry = time.time() + 600
        manual_job = {"id": "manual:" + token, "umo": umo, "kind": kind, "uid": job["uid"],
                      "name": job["name"], "text": job["text"], "created": now, "due": now, "expires": expiry}
        draft = {"job": manual_job, "text": output, "item": item, "expires": expiry,
                 "revision": self.revision(), "origin": origin, "simulation": simulation, "status": "ready"}
        self.drafts[token] = draft
        return {"decision": "send", "text": output, "token": token, "expires": expiry,
                "umo": umo, "sendable": not simulation, "simulation": simulation,
                "kind": kind, "title": item.title if item else "", "source": item.source if item else "",
                "card": preview_card(output, item)}

    async def preview(self):
        request_id = None
        try:
            data = await self.body()
            request_id = data.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{12,80}", request_id):
                raise ValueError("预览请求标识不正确，请刷新页面")
            now = time.time()
            self.requests = {key: value for key, value in self.requests.items() if value[0] > now}
            self.drafts = {key: value for key, value in self.drafts.items() if value["expires"] > now}
            if request_id in self.requests:
                return self.requests[request_id][1]
            if self.p.lock.locked():
                raise ValueError("正在处理其他任务，请稍后再试")
            if now - self.last_preview < 10:
                raise ValueError("请间隔 10 秒再生成，避免重复消耗模型额度")
            async with self.p.lock:
                self.last_preview = now
                self.requests[request_id] = (now + 600, {"status": "error", "message": "本次生成尚未完成或结果不确定，请刷新状态"})
                try:
                    result = ok(await asyncio.wait_for(self.build_preview(data, now), timeout=100))
                except asyncio.TimeoutError:
                    result = {"status": "error", "message": "生成超时，未发群消息；若已调用模型仍会计入额度"}
                except ValueError as exc:
                    result = {"status": "error", "message": str(exc)}
                self.requests[request_id] = (now + 600, result)
                return result
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def image_start(self):
        """Start one durable-reserved request; slow providers run outside HTTP."""
        try:
            data = await self.body()
            umo = self.group(data.get("umo"))
            request_id = data.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{12,80}", request_id):
                raise ValueError("生图请求标识不正确，请刷新页面")
            prompt, caption = data.get("prompt", ""), data.get("caption", "")
            if not isinstance(prompt, str) or len(prompt) > 1200 or not isinstance(caption, str) or len(caption) > 160:
                raise ValueError("生图提示词最多 1200 字，分享配文最多 160 字")
            fresh = data.get("fresh", False)
            if type(fresh) is not bool:
                raise ValueError("重新生成参数不正确")
            now = time.time()
            self.image_jobs = {token: job for token, job in self.image_jobs.items() if job["expires"] > now}
            for token, job in self.image_jobs.items():
                if job["request_id"] == request_id:
                    return ok({"token": token, "status": job["status"]})
            if self.p.lock.locked() or (self.image_task and not self.image_task.done()):
                raise ValueError("正在处理其他任务，请稍后再试；不会提交第二次生图请求")
            image_endpoint(self.p.cfg.raw)
            if not self.p.media.image_configured():
                raise ValueError("请先在插件设置保存生图接口及所需密钥；测试无需开启自动生图")
            token = secrets.token_urlsafe(24)
            job = {"request_id": request_id, "status": "running", "expires": now + 1800,
                   "umo": umo, "result": None, "message": ""}
            self.image_jobs[token] = job
            self.image_task = asyncio.create_task(self.run_image(job, prompt.strip(), caption.strip(), fresh), name="acg-image-test")
            return ok({"token": token, "status": "running"})
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def run_image(self, job, prompt, caption, fresh):
        try:
            async with self.p.lock:
                now = time.time()
                cached = self.p.store.media_cached(now, self.p.cfg.media_days * 86400)
                reused = bool(cached and not fresh)
                item = await asyncio.wait_for(self.p.media.get(now, job["request_id"], force_kind="image", prompt=prompt,
                                             caption=caption, strict=True, manual=True, fresh=fresh), timeout=420)
                if reused and caption:
                    # Explicit wording changes only this preview; the image stays cached.
                    item = replace(item, description=caption, caption_origin="manual")
                caption_note = ""
                if item.caption_origin == "pending":
                    self.p.media.stage = "正在根据图片主题生成配文"
                    wording = await self.p.make_image_caption(job["umo"], item, time.time())
                    if wording:
                        self.p.media.remember_image_caption(item, wording)
                    else:
                        caption_note = self.p.last_error
                        self.p.media.remember_image_caption(item, item.description, "fallback")
                    self.p.media.stage = ""
                elif not item.image_prompt:
                    caption_note = "旧图未保存主题；可在绘图提示词填写这张图的内容，再点“模型重写配文”"
                elif item.caption_origin == "fallback":
                    caption_note = "当前使用简短备用配文；可点“模型重写配文”重试，不会重新生图"
                payload = Path(item.media_file).read_bytes()
                _, mime = image_format(payload)
                token = secrets.token_urlsafe(24)
                expiry = time.time() + 600
                text = item.description
                manual_job = {"id": "manual:" + token, "umo": job["umo"], "kind": "share", "uid": "", "name": "",
                              "text": "手动生图测试", "created": now, "due": now, "expires": expiry}
                self.drafts[token] = {"job": manual_job, "text": text, "item": item, "expires": expiry,
                                      "revision": self.revision(), "origin": None, "simulation": False, "status": "ready"}
                job["result"] = {"token": token, "text": text, "title": item.title, "umo": job["umo"],
                                 "image": "data:" + mime + ";base64," + base64.b64encode(payload).decode("ascii"),
                                 "expires": expiry, "sendable": True, "reused": reused,
                                 "caption_origin": item.caption_origin, "caption_note": caption_note}
                job["status"] = "ready"
        except asyncio.CancelledError:
            job["status"], job["message"] = "error", "插件重载中；已提交任务可能消耗额度，不会自动重试"
            raise
        except TimeoutError:
            job["status"], job["message"] = "error", "生成等待超时，服务端结果可能不确定；不会自动重新提交"
        except ValueError as exc:
            job["status"], job["message"] = "error", str(exc)
        except Exception:
            job["status"], job["message"] = "error", "生图测试失败，请核对接口设置；不会自动重试"

    async def image_status(self):
        try:
            data = await self.body()
            token = data.get("token")
            if not isinstance(token, str) or token not in self.image_jobs or self.image_jobs[token]["expires"] <= time.time():
                raise ValueError("测试记录已过期或插件已重载，请刷新状态；不会自动重新提交")
            job = self.image_jobs[token]
            preview = dict(job["result"]) if job["result"] else None
            if preview:
                draft = self.drafts.get(preview["token"])
                preview["sendable"] = bool(draft and draft["status"] == "ready" and draft["expires"] > time.time()
                                           and draft["revision"] == self.revision())
            return ok({"status": job["status"], "message": job["message"], "preview": preview,
                       "stage": self.p.media.stage if job["status"] == "running" else "",
                       "budget": self.p.media.image_test_budget(time.time())})
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def image_caption(self):
        """Explicit wording-only test, pinned to an unsent preview and image."""
        try:
            data = await self.body()
            request_id = data.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{12,80}", request_id):
                raise ValueError("配文请求标识不正确，请刷新页面")
            key = "caption:" + request_id
            now = time.time()
            self.requests = {k: v for k, v in self.requests.items() if v[0] > now}
            if key in self.requests:
                return self.requests[key][1]
            if self.p.lock.locked():
                raise ValueError("正在处理其他任务，请稍后再重写配文")
            async with self.p.lock:
                token = data.get("token")
                draft = self.drafts.get(token) if isinstance(token, str) else None
                if not draft or draft["status"] != "ready" or draft["expires"] <= now or draft["revision"] != self.revision():
                    raise ValueError("请先生成或复用一张尚未发送的图片预览")
                item = draft["item"]
                if not item or item.media_kind != "image":
                    raise ValueError("只能重写图片预览的配文")
                self.group(draft["job"]["umo"])
                theme = data.get("theme", "")
                if not isinstance(theme, str) or len(theme) > 1200:
                    raise ValueError("图片主题最多 1200 字")
                if now - self.last_caption < 10:
                    raise ValueError("请间隔 10 秒再重写配文")
                self.last_caption = now
                self.requests[key] = (now + 600, {"status": "error", "message": "配文请求已处理或结果不确定，不会自动重试"})
                text = await self.p.make_image_caption(draft["job"]["umo"], item, now, theme=theme.strip())
                if not text:
                    result = {"status": "error", "message": self.p.last_error + "；原预览和配文已保留"}
                else:
                    self.p.media.remember_image_caption(item, text, theme=theme.strip())
                    draft["text"] = text
                    for job in self.image_jobs.values():
                        preview = job.get("result")
                        if preview and preview["token"] == token:
                            preview.update(text=text, caption_origin="model", caption_note="")
                    result = ok({"text": text}, "配文已重写，图片未重新生成；发送将使用新配文")
                self.requests[key] = (now + 600, result)
                return result
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def send(self):
        try:
            data = await self.body()
            if self.p.lock.locked():
                raise ValueError("正在处理其他任务，请稍后再试")
            async with self.p.lock:
                token = data.get("token")
                if not isinstance(token, str) or token not in self.drafts:
                    raise ValueError("请先生成预览；重载前的预览不可发送")
                draft = self.drafts[token]
                job, now = draft["job"], time.time()
                self.group(job["umo"])
                if draft["status"] != "ready":
                    return ok({"result": draft["status"]}, "这条预览已经处理过，不会重复发送")
                if draft["expires"] <= now or draft["revision"] != self.revision():
                    raise ValueError("预览已过期或配置已变更，请重新生成")
                if draft["simulation"]:
                    raise ValueError("模拟场景只在网页预览；试发请选择热榜分享或本群真实话题")
                if not self.p.cfg.enabled or self.p.store.state(job["umo"]).get("paused"):
                    raise ValueError("插件或本群已暂停，请先开启再试发")
                cap = self.p.cfg.manual_send_limit
                used = self.p.store.count(job["umo"], now, manual=True)
                if used >= cap:
                    raise ValueError(f"本群今日手动试发已用 {used}/{cap} 次；可在插件设置提高“每群每天手动试发消息上限”，自动发言额度另计")
                item = draft["item"]
                if item and self.p.store.seen(job["umo"], item.key, now):
                    raise ValueError("这条内容已在本群分享过，请重新生成")
                origin = draft["origin"]
                if origin and (self.p.store.status(origin["id"]) != "pending" or origin["expires"] <= now):
                    raise ValueError("原话题已解决、取消或过期，不再发送")
                # Reserve a separate manual job, then use the same at-most-once
                # delivery accounting as the scheduler. No second LLM request.
                self.p.store.add_job(job["id"], job["umo"], job["kind"], now, draft["expires"], now,
                                     job["uid"], job["name"], job["text"])
                draft["status"] = "uncertain"
                if origin:
                    self.p.store.finish(origin["id"], "manual_test")
                result = await self.p.deliver(job, draft["text"], item, now, cap)
                draft["status"] = result
                message = "已发送到选定群" if result == "sent" else "发送结果不确定，已停止重发，请到群里确认"
                return ok({"result": result, "umo": job["umo"]}, message)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def pause(self):
        try:
            data = await self.body()
            umo = self.group(data.get("umo"))
            if type(data.get("paused")) is not bool:
                raise ValueError("暂停参数不正确")
            self.p.store.update(umo, paused=data["paused"], auto_pause=0, unanswered=0, failures=0, awaiting=0)
            return ok(message="已暂停" if data["paused"] else "已恢复")
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
