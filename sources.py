"""Public chart metadata, without cookies, music parsing, or playback downloads.

The hotboard response normalization follows the MIT hotboard plugin's API
integration pattern; see THIRD_PARTY_NOTICES.md. No third-party keys are used.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, asdict, replace
from urllib.parse import urlsplit

import aiohttp


@dataclass
class Content:
    key: str
    title: str
    url: str
    source: str
    description: str = ""
    rank: str = ""
    media_file: str = ""
    media_kind: str = ""
    cover: str = ""
    author: str = ""
    image_prompt: str = ""
    caption_origin: str = ""

    def prompt_data(self):
        data = asdict(self)
        data.pop("media_file")
        # Cover URLs are only for display; long CDN paths need no model tokens.
        data.pop("cover")
        data.pop("author")  # Already included in the short description.
        if self.media_kind != "image":
            data.pop("image_prompt")
            data.pop("caption_origin")
        return data


ACG = re.compile(r"动画|动漫|番剧|漫画|二次元|ACG|VOCALOID|初音|洛天依|东方|同人|MAD|MMD|AMV|音MAD|游戏|原神|星铁|崩坏|绝区零|鸣潮|明日方舟|碧蓝|蔚蓝|赛马娘|偶像|虚拟|VTuber|VUP|鬼畜|手书|米哈游|单机|影之刃|宝可梦|任天堂|塞尔达", re.I)


def valid_link(url, domains):
    try:
        p = urlsplit(str(url))
        return p.scheme == "https" and p.hostname in domains and not p.username and not p.password
    except ValueError:
        return False


def cover_url(value):
    """Normalize only public platform CDN images, including older HTTP metadata."""
    value = str(value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("http://"):
        value = "https://" + value[7:]
    try:
        p = urlsplit(value)
        host = (p.hostname or "").lower()
        allowed = host == "y.gtimg.cn" or any(host.endswith("." + d) for d in ("hdslb.com", "music.126.net"))
        if (p.scheme == "https" and allowed and not p.username and not p.password
                and p.port in (None, 443) and len(value) <= 2048):
            return value
    except ValueError:
        pass
    return ""


def qq_items(data, chart):
    out = []
    for i, row in enumerate(data.get("songlist", [])[:30]):
        song = row.get("data", {})
        mid = str(song.get("songmid", ""))
        if not re.fullmatch(r"[a-zA-Z0-9]{5,30}", mid):
            continue
        singer = " / ".join(str(s.get("name", "")) for s in song.get("singer", []))
        album_mid = str(song.get("albummid", ""))
        cover = (f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
                 if re.fullmatch(r"[a-zA-Z0-9]{5,30}", album_mid) else "")
        out.append(Content("qq:" + mid, str(song.get("songname", ""))[:180],
                           f"https://y.qq.com/n/ryqq/songDetail/{mid}", f"QQ音乐{chart}",
                           (singer + "；" + str(song.get("albumdesc", song.get("albumname", ""))))[:500], str(i + 1),
                           cover=cover, author=singer[:180]))
    return out


def netease_items(data):
    playlist = data.get("result") or data.get("playlist") or {}
    out = []
    for i, song in enumerate(playlist.get("tracks", [])[:30]):
        sid = str(song.get("id", ""))
        if not sid.isdigit():
            continue
        artists = " / ".join(str(s.get("name", "")) for s in song.get("artists", song.get("ar", [])))
        album = song.get("album") or song.get("al") or {}
        out.append(Content("netease:" + sid, str(song.get("name", ""))[:180],
                           f"https://music.163.com/#/song?id={sid}", "网易云ACG榜",
                           (artists + "；" + str(album.get("name", "")))[:500], str(i + 1),
                           cover=cover_url(album.get("picUrl")), author=artists[:180]))
    return out


def bili_items(data, aggregate=False):
    rows = data.get("list", []) if aggregate else (data.get("data") or {}).get("list", [])
    out = []
    for i, row in enumerate(rows[:100]):
        extra = row.get("extra", {}) if aggregate else row
        if not isinstance(extra, dict):
            extra = {}
        title = str(row.get("title", ""))[:180]
        desc = str(extra.get("desc", ""))[:500]
        category = str(extra.get("tname", ""))
        if not ACG.search(title + " " + desc + " " + category):
            continue
        bvid = str(extra.get("bvid", row.get("bvid", "")))
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            match = re.search(r"/video/(BV[0-9A-Za-z]{10})(?:[/?#]|$)", str(row.get("url", "")))
            bvid = match.group(1) if match else ""
        if bvid:
            owner = extra.get("owner") or {}
            author = str(owner.get("name", "")) if isinstance(owner, dict) else ""
            author = author or str(extra.get("author", row.get("author", "")))
            cover = next((url for value in (extra.get("pic"), extra.get("cover"), row.get("pic"),
                                            row.get("cover"), row.get("image")) if (url := cover_url(value))), "")
            out.append(Content("bili:" + bvid, title, f"https://www.bilibili.com/video/{bvid}",
                               "B站热门·ACG筛选", category + "；" + desc, str(i + 1),
                               cover=cover, author=author[:180]))
    return out


class Sources:
    def __init__(self):
        self.http = None
        self.cache = {}

    async def client(self):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                              headers={"User-Agent": "Mozilla/5.0"})
        return self.http

    async def get(self, url, referer):
        http = await self.client()
        async with http.get(url, headers={"Referer": referer}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    raise ValueError("chart response too large")
                chunks.append(chunk)
            return json.loads(b"".join(chunks))

    async def fetch(self, source):
        now = time.time()
        if source in self.cache and now < self.cache[source][0]:
            return self.cache[source][1]
        try:
            if source == "qq_anime" or source == "qq_game":
                cid, label = (72, "动漫音乐榜") if source == "qq_anime" else (73, "游戏音乐榜")
                data = await self.get(f"https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg?format=json&topid={cid}&tpl=3&page=detail&type=top", "https://y.qq.com/")
                items = qq_items(data, label)
            elif source == "netease":
                data = await self.get("https://music.163.com/api/playlist/detail?id=71385702", "https://music.163.com/")
                items = netease_items(data)
            else:
                # Bilibili frequently rejects unauthenticated server requests.
                try:
                    data = await self.get("https://api.bilibili.com/x/web-interface/ranking/v2?rid=1&type=all", "https://www.bilibili.com/")
                    items = bili_items(data)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    items = []
                if not items:
                    data = await self.get("https://uapis.cn/api/v1/misc/hotboard?type=bilibili", "https://www.bilibili.com/")
                    items = bili_items(data, aggregate=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
            items = []
        self.cache[source] = (now + 3600, items)
        return items

    async def candidates(self, seed, seen):
        import random
        rng = random.Random(seed)
        order = ["qq_anime", "qq_game", "netease", "bilibili"]
        rng.shuffle(order)
        for source in order:
            items = [item for item in await self.fetch(source) if not seen(item.key)]
            if items:
                # Top 20 only; the model sees a small shortlist, not the whole chart.
                return rng.sample(items[:20], min(4, len(items[:20])))
        return []

    async def enrich(self, item):
        # Aggregated Bilibili charts sometimes omit covers. Fetch metadata for
        # the selected video only, never for every candidate or through an LLM.
        if not item or item.media_file or item.cover or not re.fullmatch(r"bili:BV[0-9A-Za-z]{10}", item.key):
            return item
        try:
            data = await self.get("https://api.bilibili.com/x/web-interface/view?bvid=" + item.key[5:],
                                  "https://www.bilibili.com/")
            video = data.get("data") or {}
            if video.get("bvid") == item.key[5:]:
                owner = video.get("owner") or {}
                return replace(item, cover=cover_url(video.get("pic")),
                               author=str(owner.get("name", item.author))[:180])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            pass
        return item

    async def close(self):
        if self.http:
            await self.http.close()
