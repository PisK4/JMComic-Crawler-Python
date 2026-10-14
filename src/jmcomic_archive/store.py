#!/usr/bin/env python3
"""存储层：SQLite 索引 + sidecar JSON + 原子落盘。

对应文档：docs/05-archive-design.md §4
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import config as C

# ================================================================ DDL
SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS albums (
  jm_id       TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  name_safe   TEXT,
  author      TEXT,
  cat_id      TEXT, cat_title TEXT, cat_slug TEXT,
  tags        TEXT,
  likes       INTEGER, views INTEGER, photos INTEGER, comments INTEGER,
  scramble_id INTEGER, addtime INTEGER, update_at INTEGER,
  captured_at TEXT, source_host TEXT,
  dir         TEXT,
  status      TEXT DEFAULT 'pending',
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_albums_cat    ON albums(cat_slug);
CREATE INDEX IF NOT EXISTS idx_albums_status ON albums(status);
CREATE INDEX IF NOT EXISTS idx_albums_likes  ON albums(likes DESC);

CREATE TABLE IF NOT EXISTS chapters (
  jm_id     TEXT PRIMARY KEY,
  album_id  TEXT NOT NULL,
  name      TEXT, sort INTEGER,
  pages     INTEGER, dir TEXT,
  status    TEXT DEFAULT 'pending',
  error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_chapters_album ON chapters(album_id, sort);

CREATE TABLE IF NOT EXISTS pages (
  album_id    TEXT NOT NULL,
  chapter_id  TEXT NOT NULL,
  idx         INTEGER NOT NULL,
  file        TEXT, seed INTEGER,
  sha256      TEXT, bytes INTEGER, w INTEGER, h INTEGER,
  source_host TEXT, fetched_at TEXT,
  status      TEXT DEFAULT 'pending',
  PRIMARY KEY (chapter_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_pages_album ON pages(album_id);

CREATE TABLE IF NOT EXISTS comments (
  cid        TEXT PRIMARY KEY,
  aid        TEXT NOT NULL,
  parent_cid TEXT,
  uid        TEXT, username TEXT, nickname TEXT,
  gender     TEXT, level INTEGER, level_name TEXT,
  likes      INTEGER,
  addtime    TEXT, update_at TEXT,
  spoiler    INTEGER,
  content_raw  TEXT,
  content_text TEXT,
  photo      TEXT,
  fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_aid    ON comments(aid);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_cid);

CREATE TABLE IF NOT EXISTS rankings (
  snapshot_date TEXT NOT NULL,
  kind          TEXT NOT NULL,
  category      TEXT DEFAULT '',
  rank          INTEGER,
  jm_id         TEXT,
  PRIMARY KEY (snapshot_date, kind, category, jm_id)
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


# ================================================================ 工具
_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_WS = re.compile(r'\s+')


def safe_name(name: str, limit: int = 80) -> str:
    """净化目录名：去非法字符、折叠空白、截断；保留中文。

    ⚠️ **截断后必须再 strip**：否则会在词中截断而留下尾随空格
    （实测 bug：`... 2024年4月號) [中國翻譯] `）。
    """
    s = _ILLEGAL.sub('_', str(name or '')).strip()
    s = _WS.sub(' ', s)
    s = s.strip('. ')
    return (s[:limit].strip('. ') or 'untitled')


def album_dir_name(jm_id: str, name: str) -> str:
    return f'{jm_id}__{safe_name(name, 60)}'


def chapter_dir_name(jm_id: str, name: str) -> str:
    return f'ch_{jm_id}__{safe_name(name, 50)}'


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """临时文件 + 原子 rename，避免半截文件被误认为完成。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.part')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'))


# ================================================================ DB
class Store:
    """线程安全的 SQLite 封装（每线程独立连接）。"""

    def __init__(self, db_path: Path = C.DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys = ON')
            self._local.conn = conn
        return conn

    # ---------------------------------------------------------- 通用
    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn().execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        if rows:
            self._conn().executemany(sql, rows)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def kv_set(self, k: str, v: Any) -> None:
        self.execute('INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)', (k, json.dumps(v)))

    def kv_get(self, k: str, default: Any = None) -> Any:
        row = self.one('SELECT v FROM kv WHERE k = ?', (k,))
        return json.loads(row['v']) if row else default

    # ---------------------------------------------------------- albums
    def upsert_album(self, a: dict) -> None:
        cols = ('jm_id', 'name', 'name_safe', 'author', 'cat_id', 'cat_title', 'cat_slug',
                'tags', 'likes', 'views', 'photos', 'comments', 'scramble_id',
                'addtime', 'update_at', 'captured_at', 'source_host', 'dir')
        vals = tuple(a.get(c) for c in cols)
        self.execute(
            f'INSERT INTO albums ({",".join(cols)}) VALUES ({",".join("?" * len(cols))}) '
            f'ON CONFLICT(jm_id) DO UPDATE SET '
            + ','.join(f'{c}=excluded.{c}' for c in cols if c != 'jm_id'),
            vals,
        )

    def set_album_status(self, jm_id: str, status: str, error: str | None = None) -> None:
        self.execute('UPDATE albums SET status=?, error=? WHERE jm_id=?', (status, error, jm_id))

    # ---------------------------------------------------------- chapters
    def upsert_chapter(self, c: dict) -> None:
        self.execute(
            'INSERT INTO chapters (jm_id, album_id, name, sort, pages, dir) '
            'VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(jm_id) DO UPDATE SET name=excluded.name, sort=excluded.sort, '
            'pages=excluded.pages, dir=excluded.dir',
            (c['jm_id'], c['album_id'], c.get('name'), c.get('sort'),
             c.get('pages'), c.get('dir')),
        )

    def set_chapter_status(self, jm_id: str, status: str, error: str | None = None) -> None:
        self.execute('UPDATE chapters SET status=?, error=? WHERE jm_id=?', (status, error, jm_id))

    # ---------------------------------------------------------- pages
    def upsert_page(self, p: dict) -> None:
        self.execute(
            'INSERT INTO pages (album_id, chapter_id, idx, file, seed, sha256, bytes, w, h, '
            'source_host, fetched_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(chapter_id, idx) DO UPDATE SET file=excluded.file, seed=excluded.seed, '
            'sha256=excluded.sha256, bytes=excluded.bytes, w=excluded.w, h=excluded.h, '
            'source_host=excluded.source_host, fetched_at=excluded.fetched_at, status=excluded.status',
            (p['album_id'], p['chapter_id'], p['idx'], p.get('file'), p.get('seed'),
             p.get('sha256'), p.get('bytes'), p.get('w'), p.get('h'),
             p.get('source_host'), p.get('fetched_at'), p.get('status', 'done')),
        )

    def page_done(self, chapter_id: str) -> set[int]:
        rows = self.query('SELECT idx FROM pages WHERE chapter_id=? AND status=?',
                          (chapter_id, 'done'))
        return {r['idx'] for r in rows}

    # ---------------------------------------------------------- comments
    def upsert_comment(self, c: dict) -> None:
        self.execute(
            'INSERT OR REPLACE INTO comments (cid, aid, parent_cid, uid, username, nickname, '
            'gender, level, level_name, likes, addtime, update_at, spoiler, content_raw, '
            'content_text, photo, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (c['cid'], c['aid'], c.get('parent_cid'), c.get('uid'), c.get('username'),
             c.get('nickname'), c.get('gender'), c.get('level'), c.get('level_name'),
             c.get('likes'), c.get('addtime'), c.get('update_at'), c.get('spoiler'),
             c.get('content_raw'), c.get('content_text'), c.get('photo'), c.get('fetched_at')),
        )

    # ---------------------------------------------------------- rankings
    def upsert_ranking(self, date: str, kind: str, category: str, rank: int, jm_id: str) -> None:
        self.execute(
            'INSERT OR REPLACE INTO rankings (snapshot_date, kind, category, rank, jm_id) '
            'VALUES (?,?,?,?,?)', (date, kind, category, rank, jm_id))


# ================================================================ 评论 HTML 归一化
_HIDDEN_RE = re.compile(
    r'<(script|style|template|noscript|iframe|object|svg|math)\b[^>]*>[\s\S]*?</\1\s*>',
    re.IGNORECASE,
)
_BLOCK_END_RE = re.compile(
    r'</(?:address|article|aside|blockquote|div|dl|fieldset|figcaption|figure|footer|form|'
    r'h[1-6]|header|li|main|nav|ol|p|pre|section|table|tbody|td|tfoot|th|thead|tr|ul)\s*>',
    re.IGNORECASE,
)
_ENTITY_MAP = {'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'apos': "'", 'nbsp': ' '}


def _decode_entities(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        ent = m.group(1)
        if ent in _ENTITY_MAP:
            return _ENTITY_MAP[ent]
        if ent.startswith('#x') or ent.startswith('#X'):
            try:
                return chr(int(ent[2:], 16))
            except ValueError:
                return m.group(0)
        if ent.startswith('#'):
            try:
                return chr(int(ent[1:]))
            except ValueError:
                return m.group(0)
        return m.group(0)
    return re.sub(r'&(#x?[0-9a-fA-F]+|\w+);', repl, value)


def comment_content_text(value: str) -> str:
    """把上游评论 HTML 转成纯文本（对齐前端 commentContentText）。"""
    if not isinstance(value, str):
        return ''
    s = _HIDDEN_RE.sub('', value)          # 1. 去隐藏元素
    s = _BLOCK_END_RE.sub('\n', s)         # 2. 块级标签 → 换行
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', '', s)          # 3. 剥离其余标签
    s = _decode_entities(s)                # 4. 解码实体
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()
