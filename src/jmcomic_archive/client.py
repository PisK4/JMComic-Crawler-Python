#!/usr/bin/env python3
"""归档客户端 —— 把 collector 的实测能力注入 fork 的上游客户端。

上游（fork）客户端与我们的归档需求有三处落差，全部在此补齐：

| 落差 | 对策 |
|---|---|
| **无限速**（默认并发 20，实测安全水位 12） | 覆写 `request_with_retry`，插令牌桶 + 错误驱动退避 |
| **原件/成品二选一** | 覆写 `save_image_resp`，原件另存 `_src/`，成品走上游原逻辑 |
| 榜单实体丢字段（只有 id/name） | `rank_rows()` 走 `req_api().res_data` 拿全字段 |

前两项的注入方式**已实测**：
- 限速：6 次请求最小间隔 0.448s（目标 4 req/s）
- 双份：37 页成品 + 37 页 `_src` 同时产出，原件与归档逐位一致
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from jmcomic.jm_client_impl import JmApiClient

from . import config as C
from .throttle import HealthMonitor, TokenBucket

__all__ = ['ArchiveClient', 'SRC_DIRNAME']

# 原件子目录名（与既有归档一致，勿改）
SRC_DIRNAME = '_src'


class ArchiveClient(JmApiClient):
    """fork 的 API 客户端 + 限速 + 原件留存。

    用法：注册后经 `client.impl` 选用（见 `client.use_archive_client()`）。
    """

    client_key = 'api_archive'

    # 类级共享（多线程共用一套令牌桶与健康状态）
    _api_bucket = TokenBucket(C.API_RATE, C.API_BURST)
    _img_bucket = TokenBucket(C.IMG_RATE, C.IMG_BURST)
    _api_mon = HealthMonitor(_api_bucket, C.API_RATE)
    _img_mon = HealthMonitor(_img_bucket, C.IMG_RATE)
    _save_src = True                      # 是否留存原件
    _lock = threading.Lock()

    # ---------------------------------------------------------------- 限速
    def request_with_retry(self, request, url, *args, **kwargs):
        """所有请求（API 与图片）都经令牌桶，并上报健康状态。

        上游此方法负责重试与域名切换，因此**覆写它等于接管全部出站流量**。
        """
        is_image = bool(kwargs.get('is_image'))
        bucket, mon = (self._img_bucket, self._img_mon) if is_image else \
                      (self._api_bucket, self._api_mon)
        bucket.acquire()
        try:
            resp = super().request_with_retry(request, url, *args, **kwargs)
        except BaseException:
            mon.record_error()
            raise
        mon.record_ok()
        return resp

    # ------------------------------------------------------------ 原件留存
    def save_image_resp(self, decode_image, img_save_path, img_url, resp, scramble_id):
        """先留原件到 `_src/`，再走上游逻辑产出成品。

        上游默认二选一（`decode=True` 丢原件 / `decode=False` 不解码）。
        我们两者都要：原件是**保真底本**（解扰是无损置换，可随时重建），
        成品让阅读器/归档可直接浏览。

        ⚠️ 归档中已有 38,810 个 `_src` 文件，格式不可改。
        """
        if self._save_src:
            try:
                src = Path(img_save_path).parent / SRC_DIRNAME / Path(img_save_path).name
                src.parent.mkdir(parents=True, exist_ok=True)
                tmp = src.with_suffix(src.suffix + '.part')
                tmp.write_bytes(resp.content)
                tmp.replace(src)
            except OSError:
                pass                      # 留存失败不应阻断成品产出
        return super().save_image_resp(decode_image, img_save_path, img_url, resp, scramble_id)

    # ------------------------------------------------------------ 榜单全字段
    def rank_rows(self, order: str = 'mv_t', category: str = '') -> tuple[list[dict], int]:
        """抓榜单并返回**全字段**行。

        上游 `day_ranking()` 只给 `(id, name)` 元组，丢掉 category/author/update_at，
        而归档布局与 `cat_slug` 映射依赖这些字段。改走原始响应。

        返回 `(行列表, total)`；`total` 是上游报的总数（mv/tf 会被截到 10000）。
        """
        res = self.req_api(f'/categories/filter?c={category}&o={order}&page=1')
        data = res.res_data
        if not isinstance(data, dict):
            return [], 0
        return list(data.get('content') or []), int(data.get('total') or 0)

# ================================================================ 兼容层
# 迁入的 archive.py / discover.py 原先调用 collector 自研客户端的接口
# （api / image / scramble_id / pool）。此处把 fork 客户端包一层，接口不变，
# 这样移植只需换 import，不必重写调用点。


class _PoolView:
    """把 fork 的域名列表伪装成 collector 的 `client.pool`。

    仅暴露只读的 `api_hosts` / `cdn_hosts`（归档 sidecar 记录用）。
    """

    def __init__(self) -> None:
        from jmcomic import JmModuleConfig as M
        updated = getattr(M, 'DOMAIN_API_UPDATED_LIST', None) or M.DOMAIN_API_LIST
        self.api_hosts = list(updated)
        self.cdn_hosts = list(M.DOMAIN_IMAGE_LIST)


class ArchiveApi:
    """`client` 的门面：保持 collector 的调用签名，内部走 fork 客户端。"""

    def __init__(self, jm_client) -> None:
        self._c = jm_client
        self.pool = _PoolView()

    # ---- 业务 API：fork 的 req_api(...).res_data 等价于 collector 的 api(path)
    def api(self, path: str) -> Any:
        from .errors import translate
        try:
            res = self._c.req_api(path)
        except BaseException as exc:                     # noqa: BLE001
            raise translate(exc) from exc
        data = res.res_data
        if data is None:
            # 错误/空响应：上游 res_data 为 None，collector 语义是返回原始 payload
            try:
                return res.model().get('data') or {}
            except Exception:                            # noqa: BLE001
                return {}
        return data

    # ---- 解扰门槛（按章节 id）
    def scramble_id(self, aid) -> int:
        from .errors import translate
        try:
            return int(self._c.get_scramble_id(str(aid)))
        except BaseException as exc:                     # noqa: BLE001
            raise translate(exc) from exc

    # ---- 图片：返回 (bytes, host)，与 collector 同签名
    def image(self, path: str) -> tuple[bytes, str]:
        from .errors import translate
        url = f'https://{self.pool.cdn_hosts[0]}{path}'
        try:
            resp = self._c.get_jm_image(url)
        except BaseException as exc:                     # noqa: BLE001
            raise translate(exc) from exc
        return resp.content, self.pool.cdn_hosts[0]


def new_archive_api(option=None) -> ArchiveApi:
    """构造 `ArchiveApi`（注册 ArchiveClient 后选用它）。"""
    from jmcomic import JmOption, JmModuleConfig
    JmModuleConfig.register_client(ArchiveClient)
    opt = option or JmOption.construct({'client': {'impl': ArchiveClient.client_key}})
    return ArchiveApi(opt.new_jm_client())


def use_archive_client(impl_key: str = ArchiveClient.client_key) -> None:
    """把 ArchiveClient 注册进 jmcomic 并设为默认，供 `client.impl` 引用。"""
    from jmcomic import JmModuleConfig
    JmModuleConfig.register_client(ArchiveClient)
    impl_key = impl_key
    return impl_key
