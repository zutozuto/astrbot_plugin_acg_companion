"""Cover images, source links and generated media. No model requests."""
from __future__ import annotations

from astrbot.api.message_components import Image, Plain, Record

from .sources import cover_url, valid_link


def is_share(item):
    return bool(item and not item.media_file and item.url)


def preview_card(text, item):
    if not is_share(item):
        return None
    if not valid_link(item.url, {"y.qq.com", "music.163.com", "www.bilibili.com"}):
        raise ValueError("内容链接验证失败")
    return {"title": item.title, "author": item.author, "source": item.source,
            "cover": cover_url(item.cover), "url": item.url, "comment": text}


def components(text, item):
    if is_share(item):
        cover = preview_card(text, item)["cover"]
        caption = "\n".join(x for x in (text, item.title, item.author, item.source, item.url) if x)
        return ([Image.fromURL(cover)] if cover else []) + [Plain(caption)]
    chain = [Plain(text)]
    if item and item.media_file:
        chain.append(Image.fromFileSystem(item.media_file) if item.media_kind == "image"
                     else Record.fromFileSystem(item.media_file))
    return chain


def history_text(text, item):
    # Retain what was shared for deduplication, history and later normal replies.
    return "\n".join([text, item.title, item.url]) if is_share(item) else text
