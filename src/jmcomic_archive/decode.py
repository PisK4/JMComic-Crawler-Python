#!/usr/bin/env python3
"""图片解码：把加扰原件还原成可浏览的成品图。

布局（阅读器友好）：解扰后的图用**规范文件名**，原件退到 `_src/` 子目录。
漫画阅读器通常 glob `章节目录/*.webp` 且不递归，因此不会把原件当页读进去。

    ch_XXX__第1话/
    ├── 00001.webp          # ← 解扰后（可直接看）
    ├── chapter.json
    └── _src/
        └── 00001.webp      # ← 上游原件（scrambled，保真底本）
"""

from __future__ import annotations

import io
import os
from pathlib import Path

SRC_DIRNAME = '_src'

# 切图上限 `SLICE_CAP_HEIGHT` 定义在 config（可经 JMC_SLICE_CAP 覆盖）。
# 见 docs/04 §5b：高度恰为上限 = 本页未完，后续片是它的续片。
from .config import SLICE_CAP_HEIGHT          # noqa: E402  (re-export)


def unscramble(img, seed: int):
    """纵向切 seed 片后按相反顺序重排（与 descramble-core.js 等价）。"""
    if seed <= 1:
        return img.copy()
    from PIL import Image
    W, H = img.size
    out = Image.new(img.mode, (W, H))
    rem = H % seed
    for i in range(seed):
        sh = H // seed
        dy = sh * i
        sy = H - sh * (i + 1) - rem
        if i == 0:
            sh += rem
        else:
            dy += rem
        if sh <= 0:
            continue
        out.paste(img.crop((0, sy, W, sy + sh)), (0, dy))
    return out


def encode_webp(img, quality: int = 92, method: int = 4) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format='WEBP', quality=quality, method=method)
    return buf.getvalue()


def decode_bytes(raw: bytes, seed: int, quality: int = 92) -> bytes:
    """原件字节 → 解扰并重新编码为 webp。seed<=0 表示无需解扰（仅规范化编码）。"""
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    if seed > 1:
        img = unscramble(img, seed)
    return encode_webp(img, quality)


def src_path(chapter_dir: Path, name: str) -> Path:
    return chapter_dir / SRC_DIRNAME / name



# ================================================================ 切片拼接
def stitch_images(imgs: list, quality: int = 92) -> bytes:
    """把同一原页被上游切成的多片，纵向拼回完整页并编码为 webp。

    仅做几何拼接；分组规则见 `pipeline.stitch_chapter_slices`（结构性判定，
    不做像素比较 —— 切点常落在装订白槽，跨页比较也会给出接近 0 的差异）。
    """
    from PIL import Image
    width = max(i.width for i in imgs)
    total = sum(i.height for i in imgs)
    canvas = Image.new('RGB', (width, total))
    y = 0
    for im in imgs:
        canvas.paste(im if im.mode == 'RGB' else im.convert('RGB'), (0, y))
        y += im.height
    return encode_webp(canvas, quality)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.part')
    tmp.write_bytes(data)
    os.replace(tmp, path)
