# -*- coding: utf-8 -*-
"""滑动窗口限流器单元测试（docs/DEVELOPMENT.md §3.3 / §11）。

用假时钟验证三条约束（窗口 / 最小间隔 / 降速），不发任何真实请求。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ratelimit import RateLimiter  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class WindowTest(unittest.TestCase):
    def test_window_blocks_beyond_limit(self):
        """限额 5/分钟：第 6 次必须先等到窗口滑出。"""
        clock = FakeClock()
        limiter = RateLimiter("test", max_calls=5, window_seconds=60,
                              min_interval=0, clock=clock, sleep=clock.sleep)
        for _ in range(5):
            limiter.acquire()
        self.assertEqual(clock.now, 0)          # 前 5 次不等待
        limiter.acquire()
        self.assertAlmostEqual(clock.now, 60)   # 第 6 次等满一个窗口
        limiter.acquire()
        self.assertAlmostEqual(clock.now, 60)   # 之后每次补一个
        limiter.acquire()
        self.assertAlmostEqual(clock.now, 60)

    def test_min_interval_enforced(self):
        clock = FakeClock()
        limiter = RateLimiter("test", max_calls=100, window_seconds=300,
                              min_interval=2.0, clock=clock, sleep=clock.sleep)
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        self.assertAlmostEqual(clock.now, 4.0)

    def test_slow_down_doubles_interval(self):
        clock = FakeClock()
        limiter = RateLimiter("test", max_calls=100, window_seconds=300,
                              min_interval=1.0, clock=clock, sleep=clock.sleep)
        limiter.acquire()
        limiter.slow_down()
        limiter.acquire()
        self.assertAlmostEqual(clock.now, 2.0)
        limiter.slow_down()
        limiter.acquire()
        self.assertAlmostEqual(clock.now, 6.0)   # 2.0 + 4.0

    def test_slow_down_interval_capped(self):
        limiter = RateLimiter("test", max_calls=10, window_seconds=300,
                              min_interval=2.0, max_interval=30.0)
        for _ in range(10):
            limiter.slow_down()
        self.assertEqual(limiter.effective_min_interval(), 30.0)

    def test_stats(self):
        clock = FakeClock()
        limiter = RateLimiter("itad", max_calls=800, window_seconds=300,
                              min_interval=0.3, clock=clock, sleep=clock.sleep)
        limiter.acquire()
        limiter.wait(5)
        stats = limiter.stats()
        self.assertEqual(stats["name"], "itad")
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["limit"], "800/300s")
        self.assertAlmostEqual(stats["waited_seconds"], 5.0)


if __name__ == "__main__":
    unittest.main()