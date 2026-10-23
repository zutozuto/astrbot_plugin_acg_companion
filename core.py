"""Pure scheduling and text rules; no framework or network dependencies."""
from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))

SESSION_RE = re.compile(r"[^:\s]+:(?:GroupMessage|FriendMessage):\d+$")


def is_group_session(umo: str) -> bool:
    """Return whether a configured UMO is a group conversation."""
    return str(umo).split(":")[1:2] == ["GroupMessage"]


def session_label(umo: str) -> str:
    """Human-readable label without exposing a private session as a group."""
    parts = str(umo).split(":")
    kind = "群" if len(parts) > 1 and parts[1] == "GroupMessage" else "私聊"
    return f"{kind} {parts[-1]}"


def local_time(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, SHANGHAI)


def day(ts: float) -> str:
    return local_time(ts).strftime("%Y-%m-%d")


def minute(value: str) -> int:
    h, m = map(int, value.split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("time must be HH:MM")
    return h * 60 + m


class Settings:
    def __init__(self, cfg):
        self.raw = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.sessions = tuple(dict.fromkeys(str(x) for x in cfg.get("sessions", [])
                                          if SESSION_RE.fullmatch(str(x))))
        self.provider = str(cfg.get("provider_id", "")).strip()
        limits = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", str(cfg.get("daily_limit", "2-5")))
        if not limits:
            raise ValueError("每天自动发言上限请填写 2-5 这样的范围或单个数字")
        self.low, self.high = int(limits[1]), int(limits[2] or limits[1])
        if not 0 <= self.low <= self.high <= 10:
            raise ValueError("每天自动发言上限应在 0–10 之间，且下界不能大于上界")
        self.idle = self.number("idle_minutes", 5, 1, 60) * 60
        self.gap = self.number("min_interval_minutes", 90, 15, 1440) * 60
        self.bot_gap = self.number("other_bot_gap_minutes", 10, 1, 120) * 60
        self.call_cap = self.number("daily_model_calls", 8, 1, 30)
        self.global_call_cap = self.number("global_daily_model_calls", 16, 1, 100)
        self.manual_send_limit = self.number("manual_send_daily_limit", 10, 1, 100)
        self.media_days = self.number("media_interval_days", 3, 1, 30)
        self.image_test_limit = self.number("image_test_daily_limit", 2, 1, 100)
        self.image_test_gap = self.number("image_test_interval_seconds", 60, 10, 3600)
        try:
            start, end = str(cfg.get("quiet_hours", "02:00-08:00")).split("-")
            self.quiet_start, self.quiet_end = minute(start.strip()), minute(end.strip())
        except (ValueError, TypeError):
            raise ValueError("安静时段请填写 02:00-08:00 这样的有效时间范围") from None
        self.ignored_users = set(map(str, cfg.get("ignored_user_ids", [])))

    def number(self, key, default, low, high):
        return max(low, min(high, int(self.raw.get(key, default))))

    def flag(self, name):
        return bool(self.raw.get(name + "_enabled", True))

    def quiet(self, ts):
        t = local_time(ts)
        m = t.hour * 60 + t.minute
        if self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= m < self.quiet_end
        return m >= self.quiet_start or m < self.quiet_end


def plan(umo: str, ts: float, cfg: Settings) -> tuple[int, list[tuple[str, float, float]]]:
    """Stable daily plan, stratified to reduce clumps; missed slots expire.

    Inspired by proactive_care's UMO/date seed, independently implemented.
    Enumerating civil minutes also handles a quiet window crossing midnight.
    """
    date = day(ts)
    rng = random.Random(f"acg-companion-v1:{umo}:{date}")
    count = rng.randint(cfg.low, cfg.high)
    midnight = local_time(ts).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    active = [i for i in range(1440) if not cfg.quiet(midnight + i * 60)]
    slots = []
    for i in range(count):
        section = active[len(active) * i // count:len(active) * (i + 1) // count]
        if not section:
            continue
        due = midnight + rng.choice(section) * 60
        expiry = min(due + 7200, midnight + 86400)
        slots.append((f"share:{date}:{i}", due, expiry))
    return count, slots


def fingerprint(text: str) -> set[str]:
    text = re.sub(r"[\W_\d]+", "", text.lower())[:200]
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}


def same_topic(a: str, b: str) -> bool:
    x, y = fingerprint(a), fingerprint(b)
    return bool(x and y and len(x & y) / min(len(x), len(y)) >= 0.48)


NEGATIVE = re.compile(r"还没|尚未|没[有能]?解决|未解决|没搞定|没修好|没好|还是不行|仍然|还不行|没成功|没有成功|没能解决")
CLOSED = re.compile(r"解决了|已解决|搞定了|修好了|弄好了|成功了|好了谢谢|好啦|能用了|已经好了|问题解决|不用了|不需要了|算了|取消|不玩了|不去了|别问|别催|别惦记|不送|不给")


def closes_topic(text: str) -> bool:
    # Negative and interrogative reports must never look like a solved report.
    if NEGATIVE.search(text):
        return False
    if re.search(r"[吗么?？]|是不是|有没有|谁.*解决|了没[呀啊呢!！。\s]*$", text):
        return False
    return bool(CLOSED.search(text))


def candidate(text: str) -> str | None:
    if len(text) < 5 or text.startswith(("/", "!", "！")) or closes_topic(text):
        return None
    if re.search(r"新买|刚买|刚下单|新入手|刚入手|到货了|买了.{0,15}(手办|游戏|耳机|键盘|手机|显卡|周边|谷子)", text):
        return "purchase"
    if re.search(r"报错|怎么[弄办修]|怎么办|求助|有[个点].{0,6}问题|解决不了|搞不定|打不开|连不上|不会弄|谁会|救命.{0,20}(电脑|游戏|软件)", text):
        return "problem"
    if re.search(r"联机|开黑|组队|约饭|聚餐|吃什么|去哪吃|一起吃|出去玩|出游|旅游|有没有人玩|有人.{0,5}(玩|打)|谁.{0,5}(玩|打).{0,10}(游戏|瓦|撸)", text):
        return "social"
    return None


def topic_key(umo: str, uid: str, kind: str, text: str, date: str) -> str:
    value = f"{umo}|{uid}|{kind}|{date}|{''.join(sorted(fingerprint(text)))}"
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def parse_decision(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    obj = json.loads(raw)
    if not isinstance(obj, dict) or type(obj.get("send")) is not bool:
        raise ValueError("invalid decision")
    text = obj.get("text", "")
    if obj["send"] and (not isinstance(text, str) or not 1 <= len(text.strip()) <= 180):
        raise ValueError("invalid message length")
    if re.search(r"https?://|\[CQ:|@全体|@所有人", str(text), re.I):
        raise ValueError("model may not create links or mention codes")
    obj["text"] = str(text).strip()
    return obj
