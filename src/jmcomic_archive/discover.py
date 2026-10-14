#!/usr/bin/env python3
"""发现与元数据抓取：榜单、分类、本子详情、章节页清单、评论。

对应文档：docs/03-api-reference.md、docs/06-comments.md
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Iterator

from . import config as C
from .errors import AlbumMissing, ProtocolError
from .client import ArchiveApi as JmClient

# ================================================================ 分类映射
# ⚠️ 实测坑：上游「列表项」的分类名可能是简化字或繁体字（如 `韩漫` vs `韓漫`），
# 按标题映射会漏。`/categories` 的 `id`→`slug` 才是权威映射，因此以 cat_id 为准。
CATEGORY_SLUGS_BY_ID = {
    '1': 'doujin', '2': 'single', '3': 'short', '4': 'another',
    '5': 'hanman', '6': 'meiman', '7': 'another_cosplay',
    '8': '3D', '9': 'jm_chinese_group',
}
CATEGORY_SLUGS_BY_NAME = {
    '同人': 'doujin', '单本': 'single', '短篇': 'short', '其他类': 'another',
    '韩漫': 'hanman', '韓漫': 'hanman', 'English Manga': 'meiman',
    'Cosplay': 'another_cosplay', '3D': '3D', '禁漫汉化组': 'jm_chinese_group',
    '最新A漫': 'new',
}


def category_slug(cat_title: str | None, cat_id: str | None = None) -> str:
    """按 cat_id 优先映射；无则回退标题；再回退 `cat{id}`。"""
    if cat_id is not None and str(cat_id) in CATEGORY_SLUGS_BY_ID:
        return CATEGORY_SLUGS_BY_ID[str(cat_id)]
    if cat_title and cat_title in CATEGORY_SLUGS_BY_NAME:
        return CATEGORY_SLUGS_BY_NAME[cat_title]
    return f'cat{cat_id}' if cat_id else 'uncategorized'


# ================================================================ 榜单
def fetch_ranking(client: JmClient, order: str = 'mv_t', category: str = '') -> list[dict]:
    """抓全量榜单。返回 [{id, name, author, category, update_at, adddate}, ...]

    注意：mv_t/mv_w/mv_m 的 total 是真实值；mv/tf/默认 被截到 10000（docs/03 §2）。
    """
    items: list[dict] = []
    page = 1
    while True:
        path = f'/categories/filter?c={category}&o={order}&page={page}'
        res = client.api(path)
        content = res.get('content') or []
        if not content:
            break
        items.extend(content)
        total = int(res.get('total') or 0)
        # mv_t 可能 total==len；用 total 判定是否还有下一页
        if total and page * C.PAGE_SIZE_LIST >= total:
            break
        if len(content) < C.PAGE_SIZE_LIST:
            break
        page += 1
        if page > 200:                      # 安全上限（10000/80 = 125）
            break
    return items


# ================================================================ 本子详情
def _as_text(v: Any) -> str:
    """上游 author/tags 类型不固定：实测连载恒为 list，单本可能是 str。"""
    if isinstance(v, list):
        return ' '.join(str(x) for x in v if x)
    return str(v or '')


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def fetch_album(client: JmClient, aid: str, fallback: dict | None = None) -> dict:
    """抓本子详情。

    ⚠️ 实测 `/album` **不含 category 字段**，分类信息只在榜单/列表项里，
    须由 `fallback` 兜底（调用方从 rankings 行传入）。
    """
    a = client.api(f'/album?id={aid}')
    # 上游对不存在/已下架的本子返回 `{"code":200,"data":[]}`（实测）——
    # 这是**终态**，不应算失败，调用方据此标记 `missing` 而非反复重试。
    if isinstance(a, dict) and not isinstance(a.get('data', None), str) and not a.get('id'):
        raise AlbumMissing(f'本子不存在或已下架 aid={aid}')
    if not isinstance(a, dict) or 'id' not in a:
        raise ProtocolError(f'album 响应异常 aid={aid}: {str(a)[:120]}')
    fb = fallback or {}
    return {
        'jm_id': str(a['id']),
        'name': _as_text(a.get('name')),
        'author': _as_text(a.get('author')),
        'tags': _as_text(a.get('tags')),
        'description': _as_text(a.get('description')),
        'cat_id': str(fb['cat_id']) if fb.get('cat_id') is not None else None,
        'cat_title': fb.get('cat_title'),
        'likes': _to_int(a.get('likes')),
        'views': _to_int(a.get('total_views')),
        'photos': _to_int(a.get('total_photos')),
        'comments': _to_int(a.get('comment_total')),
        'addtime': _to_int(a.get('addtime')),
        'series_id': str(a.get('series_id') or '0'),
        'series': a.get('series') or [],
    }


def normalize_chapters(album: dict) -> list[dict]:
    """章节清单。单本（series 为空）视为一章。"""
    series = album.get('series') or []
    if not series:
        return [{'jm_id': album['jm_id'], 'name': album['name'], 'sort': 1}]
    out = []
    for idx, s in enumerate(series, start=1):
        out.append({
            'jm_id': str(s.get('id')),
            'name': s.get('name') or f'第{idx}话',
            'sort': _to_int(s.get('sort'), idx),
        })
    out.sort(key=lambda x: x['sort'])
    return out


def fetch_chapter_pages(client: JmClient, chapter_id: str, shunt: int = 1) -> tuple[list[str], dict]:
    """返回 (页文件名列表, 章节原始数据)。"""
    d = client.api(f'/chapter?id={chapter_id}&shunt={shunt}')
    if not isinstance(d, dict):
        raise ProtocolError(f'chapter 响应异常 id={chapter_id}')
    images = d.get('images') or []
    names = [str(x) for x in images if isinstance(x, str)]
    return names, d


# ================================================================ 评论
def fetch_comments(client: JmClient, aid: str) -> Iterator[dict]:
    """抓单本评论（含楼中楼展开）。

    关键（docs/06 §4）：越界页会返回上一页内容而非空数组，
    因此必须用 total 限页 + CID 去重。
    """
    first = client.api(f'/forum?aid={aid}&page=1')
    if not isinstance(first, dict):
        return
    total = _to_int(first.get('total'))
    items = first.get('list') or []
    if not items:
        return

    page_count = max(1, math.ceil(total / C.PAGE_SIZE_COMMENT)) if total else 1
    page_count = min(page_count, C.COMMENT_MAX_PAGES)

    seen: set[str] = set()

    def emit(lst: list[dict]) -> Iterator[dict]:
        for c in lst:
            cid = str(c.get('CID') or '')
            if not cid or cid in seen:
                continue
            seen.add(cid)
            yield _norm_comment(c, aid, parent=None)
            # 楼中楼只有 2 层（docs/06 §5）
            for reply in (c.get('replys') or []):
                rid = str(reply.get('CID') or '')
                if rid and rid not in seen:
                    seen.add(rid)
                    yield _norm_comment(reply, aid, parent=cid)

    yield from emit(items)

    for p in range(2, page_count + 1):
        try:
            res = client.api(f'/forum?aid={aid}&page={p}')
        except ProtocolError:
            break
        batch = (res.get('list') or []) if isinstance(res, dict) else []
        new = [c for c in batch if str(c.get('CID')) not in seen]
        if not new:
            break                      # 无新增即终止（兜底）
        yield from emit(new)


def _norm_comment(c: dict, aid: str, parent: str | None) -> dict:
    from .store import comment_content_text
    raw = c.get('content') or ''
    exp = c.get('expinfo') if isinstance(c.get('expinfo'), dict) else {}
    return {
        'cid': str(c.get('CID') or ''),
        'aid': aid,
        'parent_cid': parent or str(c.get('parent_CID') or '0'),
        'uid': str(c.get('UID') or ''),
        'username': c.get('username') or '',
        'nickname': c.get('nickname') or '',
        'gender': c.get('gender') or '',
        'level': _to_int(exp.get('level')),
        'level_name': exp.get('level_name') or '',
        'likes': _to_int(c.get('likes')),
        'addtime': c.get('addtime') or '',
        'update_at': c.get('update_at') or '0',
        'spoiler': 1 if str(c.get('spoiler')) == '1' else 0,
        'content_raw': raw,
        'content_text': comment_content_text(raw),
        'photo': c.get('photo') or '',
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


# ================================================================ 封面/头像
_COVER_RE = re.compile(r'^[\w.-]+\.(jpg|jpeg|png|webp|gif)$', re.IGNORECASE)


def cover_path(aid: str) -> str:
    return f'/media/albums/{aid}_3x4.jpg'


def avatar_path(photo: str) -> str | None:
    if photo and _COVER_RE.match(photo):
        return f'/media/users/{photo}'
    return None
