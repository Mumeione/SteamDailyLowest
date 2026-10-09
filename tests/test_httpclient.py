# -*- coding: utf-8 -*-
"""传输底座的**直测**（架构检查卡片 09）。

`src/httpclient.py` 是全仓库最关键的 seam（ITAD / Steam / GetItems 三条线全从
`BaseHttpClient.request` 出去），此前只被间接经过 —— 五类响应策略任何一条走歪，
后果都要等到线上出报表才发现。这里用假 session + 假 limiter 直测：

1. 200 + ``null`` = **软限流**（不是「没数据」）：计入限流、降速、重试耗尽后抛错；
2. 429：尊重 ``Retry-After``；
3. 403：比 429 严重 —— 连续 2 次即 :class:`Blocked`（中止本轮），单次先等 5 分钟；
4. 200 + ``success:false`` = **不是限流**：返回 ``None`` 让调用方跳过；
5. 5xx：服务端故障，重试但**不计入限流统计**；
6. 网络异常：重试耗尽抛错；
7. **出口按宿主类型包装**（架构检查卡片 01）：ItadClient 抛 ItadError/ItadBlocked、
   SteamBrowseClient 抛 SteamBrowseError/SteamBrowseBlocked，底座自身仍抛 HttpError。
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.httpclient import BaseHttpClient, Blocked, HttpError  # noqa: E402
from src.itad import ItadBlocked, ItadClient, ItadError  # noqa: E402
from src.steam_browse import SteamBrowseBlocked, SteamBrowseClient, SteamBrowseError  # noqa: E402


class FakeLimiter:
    def __init__(self):
        self.acquires = 0
        self.waits: list[float] = []
        self.slow_downs = 0

    def acquire(self):
        self.acquires += 1

    def wait(self, seconds):
        self.waits.append(seconds)

    def slow_down(self):
        self.slow_downs += 1

    def stats(self):
        return {}


class Resp:
    def __init__(self, status=200, text="{}", payload=None, headers=None):
        self.status_code = status
        self.text = text
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """按序返回预设响应/异常；用完后重复最后一项。"""

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        item = self.items[min(self.calls - 1, len(self.items) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def make(items, max_attempts=2, cls=BaseHttpClient, **kw):
    limiter = FakeLimiter()
    session = FakeSession(items)
    if cls is ItadClient:
        client = cls(api_key="k", limiter=limiter, session=session,
                     max_attempts=max_attempts, sleep=lambda s: None, **kw)
    else:
        client = cls(limiter=limiter, session=session,
                     max_attempts=max_attempts, sleep=lambda s: None, **kw)
    return client, limiter, session


class SoftNullTest(unittest.TestCase):
    """200 + body=null 是**软限流**（悄悄限你），绝不能当「没数据」。"""

    def test_counts_as_rate_limit_slows_down_and_gives_up(self):
        client, limiter, session = make([Resp(200, text="null")], max_attempts=2)
        with self.assertRaises(HttpError):
            client.request("GET", "/x")
        self.assertEqual(session.calls, 2)
        self.assertEqual(client.soft_null_events, 2)
        self.assertEqual(client.rate_limit_events, 2)   # 计入限流（区别于 success:false）
        self.assertEqual(limiter.slow_downs, 1)
        self.assertTrue(any(e["kind"] == "soft_null" for e in client.events))


class RetryAfterTest(unittest.TestCase):
    def test_429_honours_retry_after_header(self):
        client, limiter, _ = make([Resp(429, headers={"Retry-After": "7"})], max_attempts=2)
        with self.assertRaises(HttpError):
            client.request("GET", "/x")
        self.assertIn(7.0, limiter.waits)
        self.assertEqual(client.rate_limit_events, 2)

    def test_429_without_header_falls_back_to_backoff(self):
        client, limiter, _ = make([Resp(429)], max_attempts=2)
        with self.assertRaises(HttpError):
            client.request("GET", "/x")
        self.assertEqual(limiter.waits, [10.0])         # 起始退避 10s

    def test_garbage_retry_after_ignored(self):
        client, limiter, _ = make([Resp(429, headers={"Retry-After": "soon"})], max_attempts=2)
        with self.assertRaises(HttpError):
            client.request("GET", "/x")
        self.assertEqual(limiter.waits, [10.0])


class BlockedTest(unittest.TestCase):
    """403 比 429 严重：单次先等 5 分钟，连续 2 次直接中止本轮。"""

    def test_second_consecutive_403_aborts(self):
        client, limiter, session = make([Resp(403)], max_attempts=5)
        with self.assertRaises(Blocked):
            client.request("GET", "/x")
        self.assertEqual(session.calls, 2)
        self.assertEqual(limiter.waits, [300])          # 社区做法：等 5 分钟
        self.assertEqual(client._consecutive_403, 2)

    def test_success_resets_consecutive_counter(self):
        client, _, _ = make([Resp(403), Resp(200, payload={"ok": 1})], max_attempts=5)
        self.assertEqual(client.request("GET", "/x"), {"ok": 1})
        self.assertEqual(client._consecutive_403, 0)


class SuccessFalseTest(unittest.TestCase):
    """200 + success:false = 目标不存在 / 该区不售 —— **不是限流**，返回 None。"""

    def test_returns_none_without_retry_and_without_rate_limit(self):
        resp = Resp(200, text='{"success": false}',
                    payload={"success": False, "reason_phrase": "not found"})
        client, limiter, session = make([resp], max_attempts=4)
        self.assertIsNone(client.request("GET", "/x"))
        self.assertEqual(session.calls, 1)
        self.assertEqual(client.rate_limit_events, 0)
        self.assertEqual(limiter.slow_downs, 0)
        self.assertTrue(any(e["kind"] == "success_false" for e in client.events))


class ServerErrorTest(unittest.TestCase):
    def test_5xx_retries_but_not_counted_as_rate_limit(self):
        client, limiter, session = make([Resp(500)], max_attempts=2)
        with self.assertRaises(HttpError):
            client.request("GET", "/x")
        self.assertEqual(session.calls, 2)
        self.assertEqual(client.server_errors, 2)
        self.assertEqual(client.rate_limit_events, 0)
        self.assertEqual(limiter.slow_downs, 0)


class NetworkErrorTest(unittest.TestCase):
    def test_retries_then_raises(self):
        boom = requests.ConnectionError("boom")
        client, _, session = make([boom], max_attempts=2)
        with self.assertRaises(HttpError):
            client.request("GET", "/x")
        self.assertEqual(session.calls, 2)
        self.assertEqual(client.network_errors, 2)


class HostExceptionWrappingTest(unittest.TestCase):
    """出口按宿主类型包装（卡片 01）：失败类型必须能被调用方的 except 接住。"""

    def test_itad_transport_failures_are_itad_errors(self):
        client, _, _ = make([Resp(429)], max_attempts=2, cls=ItadClient)
        with self.assertRaises(ItadError):
            client.request("GET", "/deals/v2")

    def test_itad_consecutive_403_is_itad_blocked(self):
        client, _, _ = make([Resp(403)], max_attempts=5, cls=ItadClient)
        with self.assertRaises(ItadBlocked) as ctx:
            client.request("GET", "/deals/v2")
        # 双身份：既是 ItadError（「失败不阻断」能接住）又是 Blocked（中止本轮）
        self.assertIsInstance(ctx.exception, ItadError)
        self.assertIsInstance(ctx.exception, Blocked)

    def test_steam_browse_transport_failures_are_host_typed(self):
        client, _, _ = make([Resp(500)], max_attempts=2, cls=SteamBrowseClient)
        with self.assertRaises(SteamBrowseError):
            client.request("GET", "/x")
        blocked, _, _ = make([Resp(403)], max_attempts=5, cls=SteamBrowseClient)
        with self.assertRaises(SteamBrowseBlocked):
            blocked.request("GET", "/x")

    def test_base_client_still_raises_base_types(self):
        client, _, _ = make([Resp(500)], max_attempts=2)
        with self.assertRaises(HttpError) as ctx:
            client.request("GET", "/x")
        self.assertIs(type(ctx.exception), HttpError)


class HeadersCompatTest(unittest.TestCase):
    """旧式假 session 不认识 ``headers`` 参数 ⇒ 非空时才传（见 request 的 docstring）。"""

    def test_session_without_headers_kwarg(self):
        seen = []

        class OldSession:
            def request(self, method, url, params=None, json=None, timeout=None):
                seen.append((method, url))
                return Resp(200, payload={"ok": 1})

        client = BaseHttpClient(limiter=FakeLimiter(), session=OldSession())
        self.assertEqual(client.request("GET", "/x"), {"ok": 1})
        self.assertEqual(len(seen), 1)


class StatsTest(unittest.TestCase):
    def test_stats_shape(self):
        client, _, _ = make([Resp(200, payload={})])
        client.request("GET", "/x")
        stats = client.stats()
        self.assertEqual(stats["requests"], 1)
        for key in ("rate_limit_events", "soft_null_events", "server_errors",
                    "network_errors", "limiter"):
            self.assertIn(key, stats)


if __name__ == "__main__":
    unittest.main()
