#!/usr/bin/env python3
"""本项目（comic-intelligence）对 jmcomic 的定制扩展。

**隔离原则**：本项目所有自研逻辑集中在本文件，尽量不改上游文件，
以便 `git merge upstream/master` 时冲突面最小。上游改动见 FORK.md。

目前提供：

| 组件 | 作用 | 挂载点 |
|---|---|---|
| `SliceStitchPlugin` | 把上游纵向切开的页拼回完整页 | `after_photo` |

---
"""

import os
from typing import Optional

from .jm_config import JmMagicConstants
from .jm_plugin import JmOptionPlugin

__all__ = ['SliceStitchPlugin']


class SliceStitchPlugin(JmOptionPlugin):
    """把上游纵向切开的页拼回完整页（jmcomic 生态盲区）。

    背景（实测，见 comic-intelligence/docs/04 §5b）：上游对**高度超过上限**的页
    会纵向切成多片，各占一个连续序号。逐文件看就像"抓取不完整的图"，
    实为上游切分 —— 本插件把它们拼回完整页。

    判定规则（**结构性，不做像素比较**）：

        「满高片 … 满高片 + 余片」
        高度恰为 SLICE_CAP_HEIGHT 的片 ⇒ 本页未完，下一片是它的续片；
        只有末片会短于上限（若整体正好整除，则末片也等于上限）。

    ⚠️ 不用「相邻边界像素相似度」判续片：切点常落在**装订白槽**，两侧都是白，
    跨页比较同样给出接近 0 的差异，会**跨页误拼**（实测踩坑）。

    用法（option 配置）：:

        plugins:
            after_photo:
                - plugin: slice_stitch
                  kwargs:
                      delete_slices: true

    本插件是**幂等**的：已拼好的目录再跑一次会直接返回。
    """

    plugin_key = 'slice_stitch'

    def invoke(self,
               photo=None,
               album=None,
               downloader=None,
               delete_slices: bool = True,
               quality: int = 92,
               cap_height: Optional[int] = None,
               **kwargs):
        self.require_param(photo or album, 'slice_stitch 需在 after_photo / after_album 阶段使用')

        if photo is None:
            # after_album 阶段：对本子下每个章节各跑一次
            total = 0
            album_dir_photos = list(album) if album is not None else []
            for p in album_dir_photos:
                total += self._stitch_one(p, delete_slices, quality, cap_height)
            if total:
                self.log(f'[slice_stitch] album-{getattr(album, "id", "?")}: 共并入 {total} 片')
            return

        merged = self._stitch_one(photo, delete_slices, quality, cap_height)
        if merged:
            self.log(f'[slice_stitch] photo-{photo.id}: 并入 {merged} 片')

    # ---------------------------------------------------------------- 内部
    def _stitch_one(self, photo, delete_slices: bool, quality: int,
                    cap_height: Optional[int]) -> int:
        from PIL import Image

        cap = int(cap_height or JmMagicConstants.SLICE_CAP_HEIGHT)
        ch_dir = self._photo_dir(photo)
        if not ch_dir or not os.path.isdir(ch_dir):
            self.log(f'[slice_stitch] 目录不存在，跳过: {ch_dir}', 'warning')
            return 0

        names = sorted(f for f in os.listdir(ch_dir)
                       if f.lower().endswith(('.webp', '.jpg', '.jpeg', '.png')))
        if len(names) < 2:
            return 0

        sizes = []
        for f in names:
            try:
                with Image.open(os.path.join(ch_dir, f)) as im:
                    sizes.append((f, im.width, im.height))
            except Exception as e:                                    # noqa: BLE001
                self.log(f'[slice_stitch] 无法读取 {f}: {e}', 'warning')
                return 0

        groups, cur = [], []
        for item in sizes:
            cur.append(item)
            if item[2] < cap:
                groups.append(cur)
                cur = []
        if cur:                       # 末尾仍是满高片：本页被截断，收尾成组
            groups.append(cur)

        multi = [g for g in groups if len(g) > 1]
        if not multi:
            return 0

        # 幂等：顶层文件集合已是分组结果，且每个文件高度等于该组总高 ⇒ 已拼好
        want = {g[0][0]: sum(x[2] for x in g) for g in groups}
        if set(want) == set(names):
            ok = True
            for name, h in want.items():
                try:
                    with Image.open(os.path.join(ch_dir, name)) as im:
                        if im.height != h:
                            ok = False
                            break
                except Exception:                                     # noqa: BLE001
                    ok = False
                    break
            if ok:
                return 0

        merged = 0
        for g in groups:
            if len(g) == 1:
                continue
            head = g[0][0]
            imgs = []
            for f, _, _ in g:
                im = Image.open(os.path.join(ch_dir, f))
                imgs.append(im.convert('RGB') if im.mode != 'RGB' else im)
            w = max(i.width for i in imgs)
            h = sum(i.height for i in imgs)
            canvas = Image.new('RGB', (w, h))
            y = 0
            for im in imgs:
                canvas.paste(im, (0, y))
                y += im.height

            out_path = os.path.join(ch_dir, head)
            ext = os.path.splitext(head)[1].lower()
            if ext in ('.jpg', '.jpeg'):
                canvas.save(out_path, 'JPEG', quality=quality)
            elif ext == '.png':
                canvas.save(out_path, 'PNG')
            else:
                canvas.save(out_path, 'WEBP', quality=quality, method=4)

            if delete_slices:
                for f, _, _ in g[1:]:
                    try:
                        os.remove(os.path.join(ch_dir, f))
                    except OSError:
                        pass
            merged += len(g) - 1
            self.log(f'[slice_stitch] {photo.id}: 页 {head} ← 并入 {len(g)} 片 ({h}px)')

        return merged

    def _photo_dir(self, photo) -> Optional[str]:
        """章节图片目录。优先用 downloader 已落盘的路径推导，退回 option 规则。"""
        save_path = getattr(photo, 'save_path', None)
        if save_path and os.path.isdir(save_path):
            return save_path
        try:
            return self.option.decide_image_save_dir(photo)
        except Exception:                                             # noqa: BLE001
            return None
