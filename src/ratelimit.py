# -*- coding: utf-8 -*-
"""滑动窗口限流器（对应 docs/DEVELOPMENT.md §3.3）。

三条约束同时生效，缺一不可：

1. 窗口：``max_calls / window_seconds``（ITAD 800/5min、Steam store 150/5min）
2. 最小间隔：``min_interval``（ITAD 0.3s、Steam 2s）
3. 并发：串行（每条通道一个实例，调用方不并发）—— 持续高并发本身就触发 429

撞到 429/403 时调用 :meth:`slow_down` 自动降速（§3.3：唯一的被动调参依据）。
时钟与 sleep 可注入，便于单测。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        name: str,
        max_calls: int,
        window_seconds: float,
        min_interval: float = 0.0,
        max_interval: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.name = name
        self.max_calls = max(1, int(max_calls))
        self.window = float(window_seconds)
        self.min_interval = float(min_interval)
        self.max_interval = float(max_interval)
        self._clock = clock
        self._sleep = sleep
        self._times: deque[float] = deque()
        self.calls = 0
        self.waited_seconds = 0.0
        self.slow_downs = 0

    # ---- 内部 ----
    def _trim(self, now: float) -> None:
        while self._times and now - self._times[0] >= self.window:
            self._times.popleft()

    def _sleep_until(self, target: float) -> None:
        now = self._clock()
        if target > now:
            self._sleep(target - now)
            self.waited_seconds += target - now

    # ---- 对外 ----
    def acquire(self) -> None:
        """取得一次调用许可；必要时先 sleep。"""
        while True:
            now = self._clock()
            self._trim(now)
            target = 0.0
            if self._times:
                gap = self.min_interval * (2 ** self.slow_downs)
                gap = min(gap, self.max_interval)
                target = max(target, self._times[-1] + gap)
            if len(self._times) >= self.max_calls:
                target = max(target, self._times[-self.max_calls] + self.window)
            if target <= now:
                break
            self._sleep_until(target)
        self._times.append(self._clock())
        self.calls += 1

    def slow_down(self) -> None:
        """撞到限流后自动降速（间隔翻倍，有上限）。"""
        self.slow_downs += 1

    def wait(self, seconds: float) -> None:
        """显式等待（如尊重 429 的 Retry-After）。"""
        if seconds > 0:
            self._sleep(seconds)
            self.waited_seconds += seconds

    def effective_min_interval(self) -> float:
        return min(self.min_interval * (2 ** self.slow_downs), self.max_interval)

    def stats(self) -> dict:
        return {
            "name": self.name,
            "calls": self.calls,
            "waited_seconds": round(self.waited_seconds, 1),
            "slow_downs": self.slow_downs,
            "limit": f"{self.max_calls}/{int(self.window)}s",
        }