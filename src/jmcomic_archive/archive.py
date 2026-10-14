#!/usr/bin/env python3
"""采集编排：本子 → 章节 → 页 → 评论，含断点续跑与原子落盘。

对应文档：docs/05-archive-design.md §5/§6
"""
from __future__ import annotations

import concurrent.futures as cf
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

from . import config as C
from . import discover as D
from .decode import SRC_DIRNAME, decode_bytes, encode_webp, stitch_images
from .errors import AlbumMissing, ProtocolError
from .client import ArchiveApi as JmClient
from .seed import calc_seed, needs_descramble
from .store import (
    Store, album_dir_name, atomic_write, chapter_dir_name, safe_name, sha256_bytes,
    write_json,
)

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)

# ---------------------------------------------------------------- 切片拼接
def stitch_chapter_slices(store: Store, cid: str, ch_dir: Path, quality: int,
                          descramble: bool = False) -> int:
    """把上游切开的页拼回完整页（改写顶层成品图 + 更新索引）。

    **判定规则（结构性，非像素）**：上游把每页切成「满高片…满高片 + 余片」，
    所以一个片的高度**恰为 `SLICE_CAP_HEIGHT`** 就表示"本页未完，下一片是它的
    续片"；只有**末片**会短于上限（正好整除时也会等于上限，此时按上限数分块）。
    实测 1482095 为 [(3600,400)]×10，1203066 为 [(3600,400)]×12，完全吻合。

    ⚠️ 不用像素比相邻边界：页面切点常落在装订白槽，两侧都是白，跨页比较也会
    给出接近 0 的差异（实测误拼）。结构规则无此歧义。

    源数据优先取 `_src/`（保真原件，历史归档也留存），因此本函数**幂等**、
    可反复运行；`_src` 缺失时退回已解码成品图。

    返回被并入的片数（0 表示无需拼接）。
    """
    from PIL import Image

    # 1) 复原**原始**分片序列（未解码的尺寸；解扰不影响高度）
    src_dir = ch_dir / SRC_DIRNAME
    from_src = src_dir.is_dir()
    if from_src:
        origs = sorted(src_dir.glob('*.webp'))
    else:                       # 旧归档无 _src：退回已解码的成品图（已是成品，勿再解扰）
        origs = sorted(p for p in ch_dir.glob('*.webp'))
    if len(origs) < 2:
        return 0

    sizes = []
    for p in origs:
        try:
            with Image.open(p) as im:
                sizes.append((im.width, im.height))
        except Exception:                                          # noqa: BLE001
            sizes.append(None)
    if any(s is None for s in sizes):
        return 0

    # 2) 按结构规则分组：连续满高片归入同一页，末片（非满高）收尾
    groups: list[list[int]] = []
    cur: list[int] = []
    for i, (_w, h) in enumerate(sizes):
        cur.append(i)
        if h < C.SLICE_CAP_HEIGHT:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    if not any(len(g) > 1 for g in groups):
        return 0

    # 幂等：若顶层文件数已是分组数，且每个文件高度等于该组总高，说明已拼好
    want = {f'{g[0] + 1:05d}.webp': sum(sizes[k][1] for k in g) for g in groups}
    have = {p.name: p for p in ch_dir.glob('*.webp')}
    if set(have) == set(want):
        done = True
        for name, h in want.items():
            try:
                with Image.open(have[name]) as im:
                    if im.height != h:
                        done = False
                        break
            except Exception:                                      # noqa: BLE001
                done = False
                break
        if done:
            return 0

    # 3) 逐组解码、拼接、落盘（成品图命名沿用该页**首片**的原序号）
    merged = 0
    kept_rows: list[dict] = []
    for g in groups:
        idx = g[0] + 1
        # 只有来自 `_src` 的原件才需要解扰；顶层文件已是成品
        seed = calc_seed(cid, idx) if (descramble and from_src) else 0
        imgs = []
        for k in g:
            raw = origs[k].read_bytes()
            out = decode_bytes(raw, seed, quality) if seed > 1 else raw
            imgs.append(Image.open(io.BytesIO(out)))
        if len(g) == 1:
            data = encode_webp(imgs[0].convert('RGB'), quality)
            kept_rows.append({'idx': idx, 'file': f'{idx:05d}.webp'})
            atomic_write(ch_dir / f'{idx:05d}.webp', data)
        else:
            data = stitch_images(imgs, quality)
            name = f'{idx:05d}.webp'
            atomic_write(ch_dir / name, data)
            kept_rows.append({'idx': idx, 'file': name})
            log(f'  ~ 切片拼接 {cid}: 页 {idx:05d} ← 并入 {len(g)} 片 '
                f'({sum(sizes[k][1] for k in g)}px)')
            merged += len(g) - 1

    # 4) 清理：删除不再使用的顶层文件，重建索引与 chapter.json
    keep = {r['file'] for r in kept_rows}
    for p in ch_dir.glob('*.webp'):
        if p.name not in keep:
            try:
                p.unlink()
            except OSError:
                pass
    store.execute('DELETE FROM pages WHERE chapter_id=?', (cid,))
    album_id = store.one('SELECT album_id FROM chapters WHERE jm_id=?', (cid,))['album_id']
    for r in kept_rows:
        data = (ch_dir / r['file']).read_bytes()
        store.upsert_page({'album_id': album_id, 'chapter_id': cid, 'idx': r['idx'],
                           'file': r['file'], 'sha256': sha256_bytes(data),
                           'bytes': len(data), 'status': 'done'})
    _rewrite_chapter_json(store, cid, ch_dir)
    return merged


def _rewrite_chapter_json(store: Store, cid: str, ch_dir: Path) -> None:
    """拼接后重建 chapter.json，使页清单与磁盘一致（剔除已并入的片）。"""
    path = ch_dir / 'chapter.json'
    try:
        meta = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return
    rows = store.query(
        'SELECT idx, file, sha256, bytes, seed, source_host FROM pages '
        'WHERE chapter_id=? AND status="done" ORDER BY idx', (cid,))
    meta['pages'] = [
        {'index': r['idx'], 'file': r['file'], 'sha256': r['sha256'],
         'bytes': r['bytes'], 'seed': r['seed'], 'source_host': r['source_host']}
        for r in rows]
    write_json(path, meta)


class Collector:
    def __init__(self, store: Store, client: JmClient, *, fetch_images: bool = True,
                 decode: bool = True, quality: int = C.IMAGE_QUALITY,
                 max_pages: int | None = None) -> None:
        self.store = store
        self.client = client
        self.fetch_images = fetch_images
        self.decode = decode                  # 落盘时即复原为可浏览图
        self.image_quality = quality
        self.max_pages = max_pages            # 单本页数上限（None = 不限）
        C.ensure_dirs()

    # ------------------------------------------------------------ 发现
    def snapshot_ranking(self, kind: str = 'mv_t', category: str = '') -> list[str]:
        """抓榜单并落库，返回本子 ID 列表。"""
        date = time.strftime('%Y-%m-%d')
        items = D.fetch_ranking(self.client, kind, category)
        ids: list[str] = []
        for rank, it in enumerate(items, start=1):
            aid = str(it.get('id') or '')
            if not aid:
                continue
            ids.append(aid)
            self.store.upsert_ranking(date, kind, category, rank, aid)
            cat = it.get('category') if isinstance(it.get('category'), dict) else {}
            self.store.execute(
                'INSERT INTO albums (jm_id, name, author, cat_id, cat_title, cat_slug, update_at) '
                'VALUES (?,?,?,?,?,?,?) ON CONFLICT(jm_id) DO NOTHING',
                (aid, it.get('name') or '', it.get('author') or '',
                 str(cat.get('id')) if cat.get('id') is not None else None,
                 cat.get('title'), D.category_slug(cat.get('title'), cat.get('id')),
                 int(it.get('update_at') or 0)),
            )
        # 榜单快照 JSONL
        out = C.RANKING_DIR / f'{date}_{kind}{("_" + category) if category else ""}.jsonl'
        with out.open('w', encoding='utf-8') as fh:
            for rank, it in enumerate(items, start=1):
                fh.write(json.dumps({'rank': rank, **it}, ensure_ascii=False) + '\n')
        self.store.kv_set(f'ranking:{kind}:{category}:last', {'date': date, 'count': len(items)})
        log(f'榜单 {kind}{("/" + category) if category else ""}: {len(items)} 本 → {out.name}')
        return ids

    # ------------------------------------------------------------ 单本
    def fetch_album(self, aid: str) -> dict:
        """抓一本（元数据 + 章节 + 图片 + 评论）。返回统计摘要。"""
        t0 = time.time()
        # /album 不含 category（实测），分类只能来自榜单行。
        # 榜单驱动的抓取总有分类；仅 `--ids` 直抓会缺 → 显式落 uncategorized，
        # 不做启发式猜测（`c={数字}` 实测无效，上游只认 slug，见 docs/03 §2）。
        row = self.store.one(
            'SELECT cat_id, cat_title, cat_slug FROM albums WHERE jm_id=?', (aid,))
        fb = dict(row) if row else None
        album = D.fetch_album(self.client, aid, fallback=fb)
        cat_slug = (fb or {}).get('cat_slug') or D.category_slug(
            album.get('cat_title'), album.get('cat_id'))
        album['cat_slug'] = cat_slug
        dir_name = album_dir_name(aid, album.get('name'))
        album_dir = C.LIBRARY_DIR / cat_slug / dir_name

        # 页数上限：**必须在这里判**（此时才拿到 total_photos；pending 本子
        # 在计划阶段该字段是空的）。超限则落终态 skipped，不下载任何图片。
        if self.max_pages:
            photos = int(album.get('photos') or 0)
            if photos > self.max_pages:
                album.update({'name_safe': safe_name(album['name']), 'dir': str(
                    album_dir.relative_to(C.LIBRARY_DIR))})
                self.store.upsert_album({
                    **album,
                    'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'source_host': '', 'scramble_id': 0,
                })
                self.store.set_album_status(
                    aid, 'skipped', f'pages {photos} > max_pages {self.max_pages}')
                log(f'  – {aid} 跳过（{photos} 页 > 上限 {self.max_pages}）')
                return {'chapters': 0, 'pages': 0, 'bytes': 0, 'comments': 0,
                        'skipped_big': 1}

        # 解扰门槛：**按章节**取，不按本子（实测坑，见 docs/04）
        # 取不到时记 0（=未知，不解扰），绝不能写默认阈值冒充真实值
        try:
            probe_scramble = self.client.scramble_id(aid)
        except ProtocolError as exc:
            log(f'  ! aid={aid} scramble_id 获取失败，标记未知: {str(exc)[:80]}')
            probe_scramble = 0

        chapters = D.normalize_chapters(album)
        album['dir'] = str(album_dir.relative_to(C.LIBRARY_DIR))
        album['scramble_id'] = probe_scramble          # 0 = 未知（不可写默认值）
        self.store.upsert_album({
            **album,
            'name_safe': safe_name(album['name']),
            'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'source_host': self.client.pool.api_hosts[0] if self.client.pool.api_hosts else '',
        })

        # 封面
        self._fetch_cover(aid, album_dir)

        stats = {'chapters': 0, 'pages': 0, 'bytes': 0, 'comments': 0, 'merged': 0}

        # 章节
        chapter_meta: list[dict] = []
        for ch in chapters:
            cid = ch['jm_id']
            ch_dir = album_dir / chapter_dir_name(cid, ch['name'])
            # 关键：解扰判定必须用**章节 id**（不是本子 id）。
            # 同本共用同一 threshold（已实测），仅当本子级探测失败(=0)时逐章重试。
            ch_scramble = probe_scramble
            if not ch_scramble:
                try:
                    ch_scramble = self.client.scramble_id(cid)
                except ProtocolError:
                    ch_scramble = 0
            ch_descramble = needs_descramble(cid, ch_scramble) if ch_scramble else False

            try:
                names, _raw = D.fetch_chapter_pages(self.client, cid)
            except ProtocolError as exc:
                self.store.upsert_chapter({'jm_id': cid, 'album_id': aid, 'name': ch['name'],
                                           'sort': ch['sort'], 'pages': 0,
                                           'dir': str(ch_dir.relative_to(C.LIBRARY_DIR))})
                self.store.set_chapter_status(cid, 'failed', str(exc)[:200])
                continue

            self.store.upsert_chapter({'jm_id': cid, 'album_id': aid, 'name': ch['name'],
                                       'sort': ch['sort'], 'pages': len(names),
                                       'dir': str(ch_dir.relative_to(C.LIBRARY_DIR))})
            self._fetch_chapter_pages(aid, cid, names, ch_dir, ch_descramble, stats)

            # 上游把过高页切成多片 → 拼回完整页（就地，历史归档同样适用）
            if self.fetch_images:
                n = stitch_chapter_slices(self.store, cid, ch_dir, self.image_quality,
                                          ch_descramble)
                if n:
                    # 片数不是页数：以库中实际保留的页为准（已剔除被并入的片）
                    row = self.store.one(
                        'SELECT COUNT(*) n FROM pages WHERE chapter_id=? AND status="done"',
                        (cid,))
                    n_pages = int(row['n'])
                    self.store.execute('UPDATE chapters SET pages=? WHERE jm_id=?',
                                       (n_pages, cid))
                    stats['merged'] += n
                    stats['pages'] -= n
                    names = names[:n_pages]

            chapter_meta.append({'id': cid, 'name': ch['name'], 'sort': ch['sort'],
                                 'pages': len(names), 'scramble_id': ch_scramble,
                                 'needs_descramble': ch_descramble,
                                 # 阅读器链接用**章节 id**
                                 'read_url': C.READ_URL.format(cid=cid),
                                 'web_url': C.WEB_PHOTO_URL.format(cid=cid),
                                 'api_path': f'/chapter?id={cid}&shunt=1'})
            self.store.set_chapter_status(cid, 'done')
            stats['chapters'] += 1

        # 评论（正文落盘，策略见 docs/06 §8）
        stats['comments'] = self._fetch_comments(aid, album_dir)

        # sidecar
        # 源链接：上游 `real_link` 恒为空（实测），故由 ID 确定性推导。
        # 同时记录**抓取当时实际使用的** API/图片主机，便于溯源与重建。
        album['source'] = {
            'jm_id': aid,
            'read_url': (C.READ_URL.format(cid=chapter_meta[0]['id'])
                         if chapter_meta else C.READ_ALBUM_URL.format(aid=aid)),
            'read_album_url': C.READ_ALBUM_URL.format(aid=aid),
            'web_album': C.WEB_ALBUM_URL.format(aid=aid),
            'api_hosts': list(self.client.pool.api_hosts),
            'cdn_hosts': list(self.client.pool.cdn_hosts),
            'api_path': f'/album?id={aid}',
        }
        album['chapters'] = chapter_meta
        album['scramble'] = {
            'scramble_id': probe_scramble,
            'needs_descramble': any(c['needs_descramble'] for c in chapter_meta),
            'per_chapter': {c['id']: c['scramble_id'] for c in chapter_meta},
        }
        album['integrity'] = {
            'chapters_total': len(chapters), 'chapters_done': stats['chapters'],
            'pages_total': stats['pages'], 'bytes': stats['bytes'],
            'comments': stats['comments'],
        }
        album['schema'] = C.SCHEMA_VERSION
        write_json(album_dir / 'album.json', album)
        self.store.set_album_status(aid, 'done')
        album_dir_rel = album['dir']
        log(f'  ✓ {aid} {album["name"][:26]:28} 章{stats["chapters"]:>3} '
            f'页{stats["pages"]:>5} 评{stats["comments"]:>4} '
            f'{stats["bytes"] / 1048576:6.1f}MB {time.time() - t0:5.1f}s → {album_dir_rel}')
        return stats

    # ------------------------------------------------------------ 图片
    def _fetch_cover(self, aid: str, album_dir: Path) -> None:
        path = album_dir / 'cover.jpg'
        if path.exists():
            return
        try:
            data, _host = self.client.image(D.cover_path(aid))
            atomic_write(path, data)
        except ProtocolError as exc:
            log(f'  ! 封面失败 aid={aid}: {exc}')

    def _fetch_chapter_pages(self, aid: str, cid: str, names: list[str], ch_dir: Path,
                             descramble: bool, stats: dict) -> None:
        if not self.fetch_images:
            return
        ch_dir.mkdir(parents=True, exist_ok=True)

        # 断点：已完成的页跳过
        done_rows = {r['idx']: r for r in self.store.query(
            'SELECT idx, file, sha256, bytes FROM pages WHERE chapter_id=? AND status=?',
            (cid, 'done'))}
        page_meta: list[dict] = []
        pending: list[tuple[int, str]] = []

        for idx, fname in enumerate(names, start=1):
            row = done_rows.get(idx)
            if row and row['file'] and (ch_dir / str(row['file'])).exists():
                page_meta.append({'index': idx, 'file': row['file'],
                                  'sha256': row['sha256'], 'bytes': row['bytes'],
                                  'seed': calc_seed(cid, idx) if descramble else 0})
                stats['bytes'] += int(row['bytes'] or 0)
                continue
            pending.append((idx, fname))

        stats['pages'] += len(names) - len(pending)

        # 并发下载只做网络 IO；解扰+编码在主线程做（Pillow 线程安全，避免 IPC 开销）
        def work(item: tuple[int, str]) -> tuple[int, str, bytes, str] | None:
            idx, fname = item
            try:
                data, host = self.client.image(f'/media/photos/{cid}/{fname}')
            except ProtocolError as exc:
                self.store.upsert_page({'album_id': aid, 'chapter_id': cid, 'idx': idx,
                                        'file': fname, 'status': 'failed'})
                log(f'  ! 页失败 {cid}/{fname}: {str(exc)[:80]}')
                return None
            return idx, fname, data, host

        with cf.ThreadPoolExecutor(C.IMG_CONC) as ex:
            for res in ex.map(work, pending):
                if res is None:
                    continue
                idx, _fname, raw, src_host = res
                seed = calc_seed(cid, idx) if descramble else 0
                # 先复原再保存：成品图用规范名，原件退到 _src/
                try:
                    if seed > 1 and self.decode:
                        out = decode_bytes(raw, seed, self.image_quality)
                        atomic_write(ch_dir / SRC_DIRNAME / f'{idx:05d}.webp', raw)
                    else:
                        out = raw          # 未加扰，或 --no-decode：原件即成品
                except Exception as exc:                       # noqa: BLE001
                    log(f'  ! 解码失败 {cid}/{idx:05d}: {str(exc)[:70]}')
                    out = raw
                atomic_write(ch_dir / f'{idx:05d}.webp', out)

                meta = {'album_id': aid, 'chapter_id': cid, 'idx': idx,
                        'file': f'{idx:05d}.webp', 'seed': seed,
                        'sha256': sha256_bytes(out), 'bytes': len(out),
                        'source_host': src_host, 'status': 'done',
                        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
                self.store.upsert_page(meta)
                stats['pages'] += 1
                stats['bytes'] += len(out)
                page_meta.append({'index': idx, 'file': meta['file'],
                                  'sha256': meta['sha256'], 'bytes': meta['bytes'],
                                  'seed': seed, 'source_host': src_host})

        page_meta.sort(key=lambda x: x['index'])
        write_json(ch_dir / 'chapter.json', {
            'schema': C.SCHEMA_VERSION, 'jm_id': cid, 'album_id': aid,
            'needs_descramble': descramble,
            'decoded': True,                     # 顶层文件已是成品图
            'quality': self.image_quality,
            'pages': page_meta,
        })

    # ------------------------------------------------------------ 评论
    def _fetch_comments(self, aid: str, album_dir: Path) -> int:
        rows: list[dict] = []
        try:
            for c in D.fetch_comments(self.client, aid):
                rows.append(c)
        except ProtocolError as exc:
            log(f'  ! 评论失败 aid={aid}: {str(exc)[:100]}')
        if not rows:
            return 0
        self.store.executemany(
            'INSERT OR REPLACE INTO comments (cid, aid, parent_cid, uid, username, nickname, '
            'gender, level, level_name, likes, addtime, update_at, spoiler, content_raw, '
            'content_text, photo, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [(c['cid'], c['aid'], c['parent_cid'], c['uid'], c['username'], c['nickname'],
              c['gender'], c['level'], c['level_name'], c['likes'], c['addtime'],
              c['update_at'], c['spoiler'], c['content_raw'], c['content_text'],
              c['photo'], c['fetched_at']) for c in rows],
        )
        cdir = C.COMMENT_DIR / album_dir_name(aid, album_dir.name.split('__', 1)[-1])
        cdir.mkdir(parents=True, exist_ok=True)
        with (cdir / 'comments.jsonl').open('w', encoding='utf-8') as fh:
            for c in rows:
                fh.write(json.dumps(c, ensure_ascii=False) + '\n')
        return len(rows)

    # ------------------------------------------------------------ 批量
    def run(self, ids: list[str], workers: int = 3, resume: bool = True,
            exclude_categories: set[str] | None = None,
            max_pages: int | None = None) -> dict:
        """并发抓取多个本子（本子级并发；图片在内部再并发）。

        exclude_categories: 按分类 slug 跳过（如 {'hanman'}），
            命中的本子标记为 `skipped` 终态，续跑不会重拾。
        max_pages: 按本子**总页数**上限过滤（`total_photos`）。超过则标记
            `skipped`。用于"先跑通小本全流程"，避开动辄上万页的长篇连载。
        """
        exclude = {c.strip() for c in (exclude_categories or set()) if c and c.strip()}
        todo: list[str] = []
        skipped = too_big = 0
        for aid in ids:
            row = self.store.one(
                'SELECT status, cat_slug, photos FROM albums WHERE jm_id=?', (aid,))
            if exclude and row and row['cat_slug'] in exclude:
                if row['status'] not in ('done', 'skipped'):
                    self.store.set_album_status(aid, 'skipped', f'category {row["cat_slug"]} excluded')
                skipped += 1
                continue
            # 注意：pending 本子的 `photos` 尚未填充（需先抓 /album 详情），
            # 因此**页数上限判定不在计划阶段**，而是放到 fetch_album 里
            # 拿到 total_photos 后立即决断（避免为此多发一次详情请求）。
            if resume and row and row['status'] in ('done', 'skipped', 'missing'):
                continue
            todo.append(aid)

        log(f'待抓 {len(todo)} 本 / 共 {len(ids)} 本'
            f'（已完成或跳过 {len(ids) - len(todo)}'
            + (f'，按分类排除 {skipped}' if skipped else '') + '）')
        summary = {'albums': 0, 'chapters': 0, 'pages': 0, 'bytes': 0, 'comments': 0,
                   'failed': 0, 'skipped': skipped, 'too_big': 0, 'missing': 0}


        def work(aid: str) -> dict | None:
            try:
                return self.fetch_album(aid)
            except AlbumMissing as exc:
                # 终态：本子已不存在，标记后不再重试
                self.store.set_album_status(aid, 'missing', str(exc)[:200])
                log(f'  – {aid} 不存在/已下架')
                return {'missing': 1}
            except Exception as exc:  # noqa: BLE001
                self.store.set_album_status(aid, 'failed', str(exc)[:300])
                log(f'  ✗ {aid} 失败: {str(exc)[:120]}')
                return None

        with cf.ThreadPoolExecutor(max(1, workers)) as ex:
            for res in ex.map(work, todo):
                if res is None:
                    summary['failed'] += 1
                    continue
                if res.get('missing'):
                    summary['missing'] += 1
                    continue
                if res.get('skipped_big'):
                    summary['too_big'] += 1
                    continue
                summary['albums'] += 1
                for k in ('chapters', 'pages', 'bytes', 'comments'):
                    summary[k] += res.get(k, 0)
        return summary
