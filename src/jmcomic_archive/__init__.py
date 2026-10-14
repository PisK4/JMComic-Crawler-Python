#!/usr/bin/env python3
"""jmcomic_archive —— JMComic 归档层（fork 的上层应用）。

本包把原先独立的 `collector/` 归档代码并入 [JMComic-Crawler-Python] fork，
**上游协议 + 归档编排同一个仓库**。

    本仓库
    ├── src/jmcomic/           上游协议（域名池/签名/AES/下载器）
    └── src/jmcomic_archive/   归档层（本包）

## 为什么合并

原 `collector/` 自研了整套协议层（`protocol.py`，332 行：域名池、签名、AES、
令牌桶），与上游**逐字重复**。合并后只保留上游缺失的部分：

| 能力 | 归属 |
|---|---|
| 协议/签名/AES/域名池 | 上游 `jmcomic` |
| 限速 + 错误驱动退避 | 本包 `throttle.py`（**上游没有**） |
| 原件 `_src/` + 成品双留 | 本包 `client.py`（上游二选一） |
| SQLite 索引 + sidecar | 本包 `store.py` |
| 断点续跑 / 三级状态机 | 本包 `archive.py` |
| 评论落盘（CID 去重） | 本包 `discover.py` |
| 切片拼接 | 上游 `SliceStitchPlugin`（本项目贡献） |
| 榜单全字段 | 本包 `client.rank_rows()` |

## 模块

| 模块 | 作用 |
|---|---|
| `config.py` | 路径、限速水位、上游模板常量 |
| `throttle.py` | 令牌桶 + 健康退避（**实测资产，勿轻易改**） |
| `client.py` | `ArchiveClient`：限速注入、原件留存、榜单全字段 |
| `errors.py` | 上游异常 → `ProtocolError` / `AlbumMissing` 映射 |
| `seed.py` | 解扰 seed（委托上游 `JmImageTool.get_num`） |
| `store.py` | SQLite schema、原子落盘、sidecar |
| `decode.py` | 解扰、切片拼接、webp 编码 |
| `discover.py` | 榜单、详情、页清单、评论（含分页陷阱对策） |
| `archive.py` | 编排：本子 → 章节 → 页 → 评论 |
| `cli.py` | `jmc` 命令行（fetch / snapshot / status / verify / decode / serve） |

## 用法

```bash
# 安装（fork 的 editable 装在 .venv-fork）
uv pip install --python .venv-fork/bin/python -e third_party/JMComic-Crawler-Python

# 采集
.venv-fork/bin/python -m jmcomic_archive.cli fetch --ids 1454718
.venv-fork/bin/python -m jmcomic_archive.cli status
.venv-fork/bin/python -m jmcomic_archive.cli verify --sample 10

# 或经入口点
.venv-fork/bin/jmc fetch --ids 1454718
```

归档根目录默认 `~/archives/jmcomic/`，可用 `JMC_ARCHIVE` 覆盖。
"""

from __future__ import annotations

__version__ = '1.0.0'

__all__ = ['__version__']
