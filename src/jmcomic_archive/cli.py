#!/usr/bin/env python3
"""JMComic 归档采集器。

用法：
    python -m collector snapshot --kind mv_t        # 抓日榜快照
    python -m collector fetch --kind mv_t           # 采集日榜全部本子
    python -m collector fetch --ids 1114751,286368  # 采集指定本子
    python -m collector status                      # 状态统计
    python -m collector verify --sample 10          # 简单验收（解码抽检）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import config as C
from .decode import SRC_DIRNAME, decode_bytes
from .archive import Collector, log, stitch_chapter_slices
from .client import new_archive_api
from .seed import calc_seed, needs_descramble
from .store import Store, atomic_write, sha256_bytes


def _clients() -> tuple[Store, JmClient]:
    """构造存储与上游客户端（走 fork 的 ArchiveClient：限速 + 原件留存）。"""
    C.ensure_dirs()
    return Store(), new_archive_api()


# ---------------------------------------------------------------- 命令
def cmd_snapshot(args: argparse.Namespace) -> int:
    store, client = _clients()
    col = Collector(store, client, fetch_images=False)
    ids = col.snapshot_ranking(args.kind, args.category)
    return 0 if ids else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    store, client = _clients()
    col = Collector(store, client, fetch_images=not args.no_images,
                    decode=not args.no_decode, quality=args.quality,
                    max_pages=args.max_pages)

    if args.ids:
        ids = [x.strip() for x in args.ids.split(',') if x.strip()]
    else:
        col.snapshot_ranking(args.kind, args.category)
        rows = store.query(
            'SELECT jm_id FROM rankings WHERE kind=? AND category=? ORDER BY rank',
            (args.kind, args.category))
        ids = [r['jm_id'] for r in rows]
        if not ids:
            log('榜单为空，请先 snapshot')
            return 1

    if args.limit:
        ids = ids[:args.limit]

    t0 = time.time()
    exclude = {c.strip() for c in (args.skip_category or '').split(',') if c.strip()}
    summary = col.run(ids, workers=args.workers, resume=not args.no_resume,
                      exclude_categories=exclude, max_pages=args.max_pages)
    dt = time.time() - t0
    log(f'完成：本子 {summary["albums"]}  章节 {summary["chapters"]}  '
        f'页 {summary["pages"]}  评论 {summary["comments"]}  '
        f'失败 {summary["failed"]}  跳过 {summary.get("skipped", 0)}  '
        f'超页数上限 {summary.get("too_big", 0)}  已下架 {summary.get("missing", 0)}  '
        f'{summary["bytes"] / 1048576:.1f} MB  耗时 {dt / 60:.1f} 分钟')
    return 0 if summary['failed'] == 0 else 2



# ---------------------------------------------------------------- 回填源链接
def cmd_backfill_urls(args: argparse.Namespace) -> int:
    """给既有归档回填 `source` 段（源链接 + 溯源信息）。

    纯本地推导，**不需要联网**：链接由 jm_id / 章节 id 确定性拼出，
    主机列表取自 config 的候选池。对已有 `source` 段的文件默认跳过。
    """
    C.ensure_dirs()
    updated = skipped = 0
    for album_json in sorted(C.LIBRARY_DIR.glob('*/*/album.json')):
        if album_json.parent.name == SRC_DIRNAME:
            continue
        try:
            meta = json.loads(album_json.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        aid = str(meta.get('jm_id') or '')
        if not aid:
            continue
        if meta.get('source') and not args.force:
            skipped += 1
            continue
        chapters = meta.get('chapters') or []
        first_cid = str(chapters[0].get('id')) if chapters else ''
        meta['source'] = {
            'jm_id': aid,
            # 阅读器链接：`#/read/` 只认**章节 id**，多章本子取第一章
            'read_url': (C.READ_URL.format(cid=first_cid) if first_cid
                         else C.READ_ALBUM_URL.format(aid=aid)),
            'read_album_url': C.READ_ALBUM_URL.format(aid=aid),
            'web_album': C.WEB_ALBUM_URL.format(aid=aid),
            'api_hosts': list(C.FALLBACK_API_HOSTS),
            'cdn_hosts': list(C.FALLBACK_CDN_HOSTS),
            'api_path': f'/album?id={aid}',
        }
        for ch in chapters:
            cid = str(ch.get('id') or '')
            if not cid:
                continue
            ch['read_url'] = C.READ_URL.format(cid=cid)
            ch['web_url'] = C.WEB_PHOTO_URL.format(cid=cid)
            ch['api_path'] = f'/chapter?id={cid}&shunt=1'
        album_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                              encoding='utf-8')
        updated += 1
    print(f'回填完成：更新 {updated}  跳过（已有 source） {skipped}')
    return 0


# ---------------------------------------------------------------- 修复切片
def cmd_stitch_slices(args: argparse.Namespace) -> int:
    """把上游切开的页拼回完整页（就地修复既有归档，离线）。

    上游对高度 > SLICE_CAP_HEIGHT 的页会切成多片，逐文件看像"不完整的图"。
    本命令扫描归档，把可证实的续片并回完整页，并删除多余的片文件。
    """
    store, _ = _clients()
    rows = store.query(
        'SELECT c.jm_id, c.dir, a.scramble_id FROM chapters c '
        'LEFT JOIN albums a ON a.jm_id=c.album_id '
        'WHERE c.status="done" ORDER BY c.jm_id')
    total = 0
    for r in rows:
        ch_dir = C.LIBRARY_DIR / r['dir']
        if not ch_dir.is_dir():
            continue
        desc = needs_descramble(r['jm_id'], int(r['scramble_id'] or 0))
        n = stitch_chapter_slices(store, r['jm_id'], ch_dir, C.IMAGE_QUALITY, desc)
        total += n
    print(f'切片拼接完成：并入 {total} 片')
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store, _ = _clients()
    print('== albums ==')
    for row in store.query('SELECT status, COUNT(*) n FROM albums GROUP BY status ORDER BY n DESC'):
        print(f'  {row["status"]:10} {row["n"]:>6}')
    print('== chapters ==')
    for row in store.query('SELECT status, COUNT(*) n FROM chapters GROUP BY status ORDER BY n DESC'):
        print(f'  {row["status"]:10} {row["n"]:>6}')
    tot = store.one('SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b, '
                    'COALESCE(SUM(CASE WHEN status="done" THEN 1 ELSE 0 END),0) d FROM pages')
    if tot:
        print(f'== pages ==\n  done {tot["d"]}/{tot["n"]}  '
              f'{tot["b"] / 1048576:.1f} MB')
    c = store.one('SELECT COUNT(*) n FROM comments')
    print(f'== comments ==\n  {c["n"] if c else 0}')
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """简单验收：产物完整性 + 解码正确性 + 路径卫生。

    ⚠️ 这里**不做**"接缝启发式"判图。实测该指标会大面积误报
    （同一页 GT 比对 MAD=0.000，启发式却报 边界/全局=6.39），
    因为分片边界未必落在内容间断处。**正确性靠 GT 比对，见
    `tools/verify_decode.py` / `tools/verify_x10.py`。**
    """
    store, _ = _clients()

    rows = store.query(
        'SELECT chapter_id, idx, file, seed, sha256, bytes FROM pages WHERE status="done" '
        'ORDER BY RANDOM() LIMIT ?', (args.sample,))
    if not rows:
        print('无已完成页可抽检')
        return 1

    print(f'== 产物完整性抽检（{len(rows)} 页）==')
    ok = 0
    for r in rows:
        ch = store.one('SELECT dir, album_id FROM chapters WHERE jm_id=?', (r['chapter_id'],))
        if not ch:
            print(f'  ✗ {r["chapter_id"]}: 索引缺章节记录')
            continue
        path = C.LIBRARY_DIR / ch['dir'] / r['file']
        if not path.exists():
            print(f'  ✗ {r["chapter_id"]}/{r["file"]}: 文件缺失')
            continue
        data = path.read_bytes()
        # 确定性检查：字节数 + sha256 与索引一致
        size_ok = len(data) == r['bytes']
        hash_ok = sha256_bytes(data) == r['sha256'] if r['sha256'] else None
        # seed 必须与确定性公式一致
        alb = store.one('SELECT scramble_id FROM albums WHERE jm_id=?', (ch['album_id'],))
        sid = int((alb['scramble_id'] if alb else 0) or 0)
        expect = calc_seed(r['chapter_id'], r['idx']) if needs_descramble(r['chapter_id'], sid) else 0
        seed_ok = (r['seed'] or 0) == expect
        good = size_ok and (hash_ok is not False) and seed_ok
        ok += good
        print(f'  {"✓" if good else "✗"} {r["chapter_id"]}/{r["file"]} '
              f'{len(data) // 1024}KB size={size_ok} '
              f'sha256={"—" if hash_ok is None else hash_ok} seed={seed_ok}')

    print(f'\n完整性通过 {ok}/{len(rows)}')

    # 产物清点
    print('== 产物清点 ==')
    albums = len(list(C.LIBRARY_DIR.glob('*/*/album.json')))
    chapters = len(list(C.LIBRARY_DIR.glob('*/*/*/chapter.json')))
    imgs = len(list(C.LIBRARY_DIR.glob('*/*/*/*.webp')))
    cms = len(list(C.COMMENT_DIR.glob('*/comments.jsonl')))
    print(f'  album.json {albums}  chapter.json {chapters}  webp {imgs}  comments.jsonl {cms}')

    # 路径卫生：尾随空格/点的目录在 shell 与多数工具里会被静默吞掉，
    # 只能用 os.scandir 原始名检查（实测曾漏过这个 bug）。
    bad = path_hygiene(C.LIBRARY_DIR)
    print(f'== 路径卫生 ==\n  {"✓ 无异常目录名" if not bad else f"✗ {len(bad)} 个异常"}')
    for p in bad[:10]:
        print(f'    {p!r}')

    # 索引 ↔ 磁盘对账（抽检覆盖不到的「缺失」类问题）
    recon = reconcile(store)

    ok_all = ok == len(rows) and not bad and not recon
    return 0 if ok_all else 2


def reconcile(store: Store, verbose: bool = True) -> list[tuple]:
    """对账：**索引记录的页** 是否都真的在磁盘上（verify 的结构性盲区）。

    `verify` 只抽检索引里已 `done` 的页，因此"索引缺行 / 页面从未落盘"
    这类**缺失**永远抽不到。此函数按章节核对：

      · 章节为 `done` 但磁盘页数 != 索引 `done` 页数
      · 顶层图多于索引（页面未登记 —— 中断留下的半截）
      · 缺 `chapter.json`（该章从未写完）
      · 索引登记为 `done` 但文件不存在

    返回异常列表 [(jm_id, 说明), ...]。非零即为真缺口，需修复。
    """
    bad: list[tuple] = []
    rows = store.query(
        'SELECT c.jm_id, c.dir, c.status, a.status AS ast FROM chapters c '
        'LEFT JOIN albums a ON a.jm_id=c.album_id ORDER BY c.jm_id')
    for r in rows:
        d = C.LIBRARY_DIR / r['dir']
        if not d.is_dir():
            continue
        top = list(d.glob('*.webp'))
        n_idx = store.one(
            'SELECT COUNT(*) n FROM pages WHERE chapter_id=? AND status="done"',
            (r['jm_id'],))['n']
        cj = d / 'chapter.json'
        if not cj.exists():
            if top:
                bad.append((r['jm_id'],
                            f'缺 chapter.json，磁盘 {len(top)} 页（本子 {r["ast"]}）'))
            continue
        if n_idx != len(top):
            bad.append((r['jm_id'],
                        f'索引 {n_idx} 页 != 磁盘 {len(top)} 页（本子 {r["ast"]}）'))
            continue
        try:
            n_cj = len(json.loads(cj.read_text(encoding='utf-8')).get('pages') or [])
        except (OSError, json.JSONDecodeError):
            bad.append((r['jm_id'], 'chapter.json 无法解析'))
            continue
        if n_cj != len(top):
            bad.append((r['jm_id'],
                        f'chapter.json {n_cj} 页 != 磁盘 {len(top)} 页（本子 {r["ast"]}）'))
    if verbose:
        print('== 索引 ↔ 磁盘 对账 ==')
        if not bad:
            print('  ✓ 每章索引页数与磁盘一致，且均已写 chapter.json')
        else:
            for jm, why in bad:
                print(f'  ✗ {jm}: {why}')
    return bad


def path_hygiene(root: Path) -> list[str]:
    """递归找出名称带尾随空格/点的目录（这类名字在 Windows 与部分工具下不可移植）。"""
    bad: list[str] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != entry.name.strip() or entry.name.endswith(('.', ' ')):
                            bad.append(str(Path(entry.path)))
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return bad


# ---------------------------------------------------------------- 解码
def _iter_album_chapters(album: str | None) -> list[tuple[str, str, dict]]:
    """返回 [(album_dir_rel, chapter_dirname, chapter_meta), ...]

    ⚠️ 同时扫描**目录树**而不只是 `album.json` —— 中断的本子没有 `album.json`，
    但其 `ch_*/` 里已落盘加扰原件，必须一并纳入（否则会永久残留错乱图）。
    """
    out: list[tuple[str, str, dict]] = []
    seen_chapters: set[str] = set()

    def add(album_dir: Path, cdir: Path) -> None:
        key = str(cdir)
        if key in seen_chapters:
            return
        seen_chapters.add(key)
        cm: dict = {}
        cj = cdir / 'chapter.json'
        if cj.exists():
            try:
                cm = json.loads(cj.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                cm = {}
        rel = str(album_dir.relative_to(C.LIBRARY_DIR))
        out.append((rel, cdir.name, cm))

    # 1) 已完成的（有 album.json，按其章节列表）
    for album_json in sorted(C.LIBRARY_DIR.glob('*/*/album.json')):
        album_dir = album_json.parent
        if album_dir.name == SRC_DIRNAME:
            continue
        try:
            meta = json.loads(album_json.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if album and str(meta.get('jm_id')) != str(album):
            continue
        for ch in meta.get('chapters') or []:
            cands = sorted(album_dir.glob(f'ch_{ch.get("id")}__*'))
            if cands:
                add(album_dir, cands[0])

    # 2) 兜底：任何含 webp 的章节目录（覆盖中断本子、缺 album.json 的情况）
    for cdir in sorted(C.LIBRARY_DIR.glob('*/*/ch_*')):
        if not cdir.is_dir() or SRC_DIRNAME in cdir.parts:
            continue
        album_dir = cdir.parent
        if album and not album_dir.name.startswith(f'{album}__'):
            continue
        if not list(cdir.glob('*.webp')):
            continue
        add(album_dir, cdir)
    return out



def _decode_one(args: tuple[str, str, dict, int]) -> dict:
    """单章解码。返回统计 + 页级记录（供主进程同步 DB）。

    ⚠️ 若只改文件不改 DB，`verify` 会拿旧哈希比对而误报——因此必须回传
    解码后文件的 sha256/bytes，由主进程（唯一 DB 写入点）更新。
    """
    rel, chapter, meta, quality = args
    cdir = C.LIBRARY_DIR / rel / chapter
    need = bool(meta.get('needs_descramble'))
    cid = str(meta.get('jm_id') or chapter.split('__')[0].removeprefix('ch_'))
    album_id = str(meta.get('album_id') or rel.split('/')[-1].split('__')[0])
    seeds = {int(p['index']): int(p.get('seed') or 0)
             for p in (meta.get('pages') or []) if p.get('index')}

    rec: dict = {'album_id': album_id, 'chapter_id': cid,
                 'done': 0, 'skipped': 0, 'failed': 0, 'bytes': 0, 'pages': []}
    for f in sorted(cdir.glob('*.webp')):
        idx = int(f.stem) if f.stem.isdigit() else 0
        seed = seeds.get(idx)
        if seed is None:
            seed = calc_seed(cid, idx) if (need and idx) else 0
        src = cdir / SRC_DIRNAME / f.name
        try:
            if src.exists():
                # 已迁移过：文件是成品图，仅补录 DB（哈希可能已过期）
                rec['pages'].append({'idx': idx, 'file': f.name, 'seed': seed,
                                     'sha256': sha256_bytes(f.read_bytes()),
                                     'bytes': f.stat().st_size})
                rec['skipped'] += 1
                continue
            raw = f.read_bytes()
            out = decode_bytes(raw, seed, quality) if seed > 1 else raw
            atomic_write(src, raw)           # 原件退到 _src/
            atomic_write(f, out)
            rec['pages'].append({'idx': idx, 'file': f.name, 'seed': seed,
                                 'sha256': sha256_bytes(out), 'bytes': len(out)})
            rec['bytes'] += len(out)
            rec['done'] += 1
        except Exception:                    # noqa: BLE001
            rec['failed'] += 1

    # 回写 chapter.json：否则其中记录的是**加扰时期**的 sha256/bytes，
    # 与盘上成品图不符（会让后续校验误判）。
    if meta and rec['pages']:
        meta = dict(meta)
        meta['pages'] = rec['pages']
        meta['decoded'] = True
        meta['quality'] = quality
        try:
            (cdir / 'chapter.json').write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            pass
    return rec


def cmd_decode(args: argparse.Namespace) -> int:
    """把归档的加扰原件解码成可直接浏览的成品图，并同步索引。"""
    from concurrent.futures import ProcessPoolExecutor

    store, _ = _clients()
    chapters = _iter_album_chapters(args.album)
    if not chapters:
        print('没有找到可解码的章节')
        return 1
    total_pages = sum(len(list((C.LIBRARY_DIR / r / c).glob('*.webp')))
                      for r, c, _ in chapters)
    print(f'章节 {len(chapters)} 个，页 {total_pages}，质量 q{args.quality}，'
          f'进程 {args.workers}')

    t0 = time.time()
    done = skipped = failed = synced = 0
    bytes_out = 0
    tasks = [(r, c, m, args.quality) for r, c, m in chapters]
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for i, rec in enumerate(ex.map(_decode_one, tasks), 1):
            done += rec['done']
            skipped += rec['skipped']
            failed += rec['failed']
            bytes_out += rec['bytes']
            # DB 写入集中在主进程（避免多进程写同一 SQLite）
            for p in rec['pages']:
                store.upsert_page({
                    'album_id': rec['album_id'], 'chapter_id': rec['chapter_id'],
                    'idx': p['idx'], 'file': p['file'], 'seed': p['seed'],
                    'sha256': p['sha256'], 'bytes': p['bytes'],
                    'source_host': '', 'status': 'done',
                    'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                })
                synced += 1
            if i % 20 == 0 or i == len(tasks):
                print(f'  [{i}/{len(tasks)}] 已解码 {done}  已同步 {synced}  '
                      f'失败 {failed}  {time.time() - t0:.0f}s')

    dt = time.time() - t0
    rate = f'（{done / dt:.1f} 页/s）' if dt else ''
    print(f'\n完成：解码 {done}  跳过 {skipped}  失败 {failed}  '
          f'索引同步 {synced}  输出 {bytes_out / 1048576:.1f} MB  {dt:.0f}s{rate}')
    print(f'原件保留在 {SRC_DIRNAME}/ 子目录（阅读器不会读到）')
    return 0 if failed == 0 else 2


def cmd_serve(args: argparse.Namespace) -> int:
    """本地阅读器 —— 委托 fork 的 `jm_server` 插件（不再自带 HTTP 服务）。

    历史：collector 自带 `server.py`（279 行，按需解扰）。上游 `jm_server`
    插件覆盖同一需求，迁入后删除自研版本（见 commit message）。
    """
    from jmcomic import JmOption
    opt = JmOption.construct({
        'dir_rule': {'rule': 'Bd_Aid_Pid', 'base_dir': str(C.LIBRARY_DIR)},
        'plugins': {'after_init': [{'plugin': 'jm_server', 'kwargs': {
            'host': args.host, 'port': args.port}}]},
    })
    return 0

# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='collector', description='JMComic 归档采集器')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('snapshot', help='抓榜单快照')
    p.add_argument('--kind', default='mv_t', help='mv_t / mv_w / mv_m / tf / mv')
    p.add_argument('--category', default='', help='分类 slug，空=全站')
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser('fetch', help='采集本子')
    p.add_argument('--kind', default='mv_t')
    p.add_argument('--category', default='')
    p.add_argument('--ids', help='逗号分隔的指定本子 ID')
    p.add_argument('--limit', type=int, help='只抓前 N 本')
    p.add_argument('--workers', type=int, default=3, help='本子级并发')
    p.add_argument('--no-resume', action='store_true', help='不跳过已完成')
    p.add_argument('--no-images', action='store_true', help='只抓元数据')
    p.add_argument('--no-decode', action='store_true',
                   help='只存加扰原件（默认会解码成可直接浏览的成品图）')
    p.add_argument('--quality', type=int, default=C.IMAGE_QUALITY,
                   help=f'解码 webp 质量（默认 {C.IMAGE_QUALITY}）')
    p.add_argument('--skip-category', default='',
                   help='按分类 slug 跳过（逗号分隔），如 --skip-category hanman')
    p.add_argument('--max-pages', type=int,
                   help='只抓总页数不超过 N 的本子（先跑通小本全流程）')
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser('backfill-urls', help='给既有归档回填源链接（离线，纯本地推导）')
    p.add_argument('--force', action='store_true', help='已有 source 段的也重写')
    p.set_defaults(func=cmd_backfill_urls)

    p = sub.add_parser('stitch-slices', help='把上游切开的页拼回完整页（离线修复）')
    p.set_defaults(func=cmd_stitch_slices)

    p = sub.add_parser('status', help='状态统计')
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('verify', help='验收：产物完整性 + 路径卫生')
    p.add_argument('--sample', type=int, default=10)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('decode', help='把归档的加扰原件解码为可浏览的成品图')
    p.add_argument('--quality', type=int, default=92, help='webp 质量（默认 92）')
    p.add_argument('--workers', type=int, default=max(2, (os.cpu_count() or 4) // 2))
    p.add_argument('--keep-src', action='store_true', default=True,
                   help='保留原件到 _src/（默认保留）')
    p.add_argument('--force', action='store_true', help='已解码的也重做')
    p.add_argument('--album', help='只处理指定本子 id')
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser('serve', help='本地阅读器（按需解扰，浏览器直接看）')
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--host', default='127.0.0.1')
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
