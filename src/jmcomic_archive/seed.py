#!/usr/bin/env python3
"""解扰 seed —— 委托给 fork 的官方实现（**不再自研**）。

`collector` 原有一份独立实现 `protocol.calc_seed()`。迁入 fork 后改为委托
`JmImageTool.get_num()`，理由：

1. 公式与上游**本就一致**（逐分支核对过，见 `docs/04` §2.4b）；
2. fork 已修边界语义（`aid <= scramble_id → 0`，采纳站点 `descramble.js`），
   见 `jm_toolkit.py` 的注释；
3. 少一份实现 = 少一处漂移风险。这是本次迁移的目的之一。

保留本模块只为**接口兼容**（`archive.py` 沿用 `calc_seed/needs_descramble`），
并作为边界语义的**单点断言**。
"""

from __future__ import annotations

from jmcomic import JmImageTool

from . import config as C

__all__ = ['calc_seed', 'needs_descramble']


def calc_seed(photo_id, idx: int, scramble_id: int = C.SCRAMBLE_220980) -> int:
    """章节 `photo_id` 第 `idx` 页的分片数（seed）。

    ⚠️ `idx` 必须是 **5 位补零、不带扩展名**（`00003`，不是 `00003.webp`）。
    实测：带扩展名算出的 seed 对不上（见 `docs/04` §2.3）。

    | 条件 | 返回值 |
    |---|---|
    | `pid <= scramble_id` | **0**（不解扰） |
    | `pid < 268850` | **固定 10**（早期素材不参与 md5） |
    | `268850 ≤ pid < 421926` | `(md5 末字符码 % 10) * 2 + 2` |
    | `pid ≥ 421926` | `(md5 末字符码 % 8) * 2 + 2` |

    ⚠️ `[220981, 268849]` 必须返回 10：误走 md5 公式会产出**错位图但不报错**
    （静默错位，见 `docs/04` §2.4c）。
    """
    return JmImageTool.get_num(str(scramble_id), str(photo_id), f'{idx:05d}')


def needs_descramble(photo_id, scramble_id: int) -> bool:
    """`photo_id <= scramble_id` ⇒ 未加扰（站点语义，见 `docs/04` §2.2）。"""
    return int(photo_id) > int(scramble_id)
