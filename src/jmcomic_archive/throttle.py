#!/usr/bin/env python3
"""限速与健康退避 —— collector 的实测资产，现注入 fork 的客户端。

**为什么需要**：fork（上游）**完全没有限速**，默认 `threading.image = 20`，
而我们实测的安全水位是 12。`docs/07` 的立场是「不做对抗，做控制」：
上游不返回 429，所以真被封时**不会有任何显式信号**，只能自己守住节奏。

**注入点**：`jmcomic.jm_client_impl.JmApiClient.request_with_retry`，
覆盖它即可让**所有** API/图片请求走令牌桶（实测已验：6 次请求最小间隔 0.448s）。

保留以下设计决策（含踩坑记录，勿轻易改）：

- 令牌桶带抖动（`uniform(0.8, 1.25)`），避免完全固定的节奏；
- 退避**只信硬信号**（HTTP 错误 / 超时 / 连接失败），**不用延迟反馈** ——
  实测延迟反馈两轮迭代都退化成棘轮：并发下 p90 同时受负载与上游排队影响，
  无法区分「我们打得太快」与「上游本来慢」，速率单调衰减到地板并卡死。
"""

from __future__ import annotations

import random
import threading
import time

from . import config as C

__all__ = ['TokenBucket', 'HealthMonitor']


class TokenBucket:
    """线程安全令牌桶，带抖动以打散固定节奏。"""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                need = (1.0 - self._tokens) / self.rate
            # 抖动：避免完全固定的请求节奏
            time.sleep(need * random.uniform(0.8, 1.25))

    def set_rate(self, rate: float) -> None:
        with self._lock:
            self.rate = max(0.1, rate)

    @property
    def current_rate(self) -> float:
        with self._lock:
            return self.rate


class HealthMonitor:
    """**只在出现硬错误时**退避，成功则缓慢恢复。

    ⚠️ 曾经用「延迟 p90 相对基线劣化」做反馈，实测**不可靠**，两轮迭代都
    退化成棘轮（速率单调衰减到地板并卡死）：
    - 固定阈值版：并发本身让 p90 超过恢复阈值 → 只降不升，CPU 0.2%、产出≈0。
    - 自校准版：floor≈0.35s vs 并发下 p90≈0.7–1.0s，比值长期落在死区内，
      桶速率仍从 11.2 → 7.84 → 5.49 单调下滑。

    根本原因：并发下的延迟**同时受负载与上游排队影响**，无法区分
    "我们打得太快"与"上游本来慢"，因此不适合做控制器输入。

    改为只信**可判定的硬信号**：HTTP 错误 / 超时 / 连接失败。
    上游不返回 429（见 docs/07），所以"无错误"本身就是最强的健康证据。
    """

    def __init__(self, bucket: TokenBucket, base_rate: float) -> None:
        self.bucket = bucket
        self.base_rate = base_rate
        self.current_rate = base_rate
        self.errors = 0                 # 连续错误数
        self.total = 0                  # 总请求数（用于统计错误率）
        self._lock = threading.Lock()

    def record_ok(self) -> None:
        with self._lock:
            self.total += 1
            if self.errors or self.current_rate < self.base_rate:
                self.errors = 0
                # 恢复要慢，避免"降了又升"的抖动
                self.current_rate = min(self.base_rate, self.current_rate * 1.1)
                self.bucket.set_rate(self.current_rate)

    def record_error(self) -> None:
        with self._lock:
            self.total += 1
            self.errors += 1
            if self.errors >= C.ERRORS_BEFORE_BACKOFF:
                self.current_rate = max(C.MIN_RATE, self.current_rate * 0.5)
                self.bucket.set_rate(self.current_rate)
                self.errors = 0         # 重置，避免连续多次减半

    @property
    def error_rate(self) -> float:
        return (self.errors / self.total) if self.total else 0.0
