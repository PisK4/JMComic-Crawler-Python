#!/usr/bin/env python3
"""异常层 —— 把 fork 的异常翻译成本归档层沿用的语义。

`collector` 原先有 `ProtocolError` / `AlbumMissing` 两个异常，`pipeline` 与
`discover` 依此决定「重试 / 终态」。迁入 fork 后无法再自建协议层异常，
改为**映射**上游异常：

| 上游异常 | 归档层语义 | 处理 |
|---|---|---|
| `MissingAlbumPhotoException` | `AlbumMissing` | **终态**，不再重试 |
| `RequestRetryAllFailException` | `ProtocolError` | 可重试 |
| `JmcomicException`（其余） | `ProtocolError` | 可重试 |
| 网络层异常 | `ProtocolError` | 可重试 |

`AlbumMissing` 是**终态**这一点很重要：`missing` 状态入库后不会被重拾
（见 `docs/11` 的状态机）。
"""

from __future__ import annotations

from jmcomic.jm_exception import (
    JmcomicException,
    MissingAlbumPhotoException,
    RequestRetryAllFailException,
)

__all__ = ['AlbumMissing', 'ProtocolError', 'translate']


class ProtocolError(RuntimeError):
    """协议层错误（网络失败 / 重试耗尽 / 响应异常）。可重试。"""


class AlbumMissing(ProtocolError):
    """本子不存在或已下架。**终态**，不应重试。"""


def translate(exc: BaseException) -> ProtocolError:
    """把上游异常翻译成本层异常。"""
    if isinstance(exc, MissingAlbumPhotoException):
        return AlbumMissing(str(exc))
    if isinstance(exc, RequestRetryAllFailException):
        return ProtocolError(f'全部域名重试失败: {exc}')
    if isinstance(exc, JmcomicException):
        return ProtocolError(str(exc))
    return ProtocolError(f'{type(exc).__name__}: {exc}')
